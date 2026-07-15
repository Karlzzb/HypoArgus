"""全局调度中枢（Orchestrator，PRD §13、ADR 拓扑 §1）。

用 LangGraph ``StateGraph`` 把整条流水线落成一张可执行的状态图——控制流落边而非
prompt 散文（见 ``docs/langgraph-dev-guide.md``）。数据在子环节间经 state channel
路由、无跨模块直接调用；流水线严格单向——唯 ``hitl1`` 经条件边有**有界打回**
（``hitl1 → parse+partition``，max retries 默认 3，超限向前 + 贴 ``partition_retry_exhausted``，
ADR-0018），其余 stage 绝不打回。

``parse+partition →〖HITL-1〗→ hypothesis_propose → retrieval → judgment
→ rewrite_loop →〖HITL-2〗→ END``（HITL-1 打回边：``hitl1 --REPLAY--> parse+partition``）

Slice 6（ADR-0017）后，judgment 之后的终稿产出由 ``writeback``（确定性纯函数回写）改为
``rewrite_loop``（逐段 LLM 提议重写）+ ``hitl2``（终稿文本确认闸门）：rewrite_loop 对被触达段
（supported 假说 / 命中 citations）产 ``proposed_rewrites``、未触达段省略；hitl2 逐段确认 /
编辑 / 驳回 ``proposed_rewrites`` 后拼装 ``final_document``（确认→提议文本、驳回 / 未触达→
逐字节原文）。``writeback`` 节点裁撤；未触达段逐字节忠实不变。hypothesis_propose / retrieval
各自把 partial 更新写入**专用 channel**（``hypotheses`` / ``citations``），由 ``judgment_node``
读之判终态后整树写回 ``argument_tree``——避免多线路整节点 upsert 互相覆盖、丢 ``status``
或 ``candidate_hypotheses``（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）。

本切片 HITL 仍为同步注入闸门（不打断、不接 checkpointer）；真实 ``interrupt`` +
``Command(resume)`` + checkpointer 属后续切片（dev-guide §7、§8）。终稿拼装幂等续跑的崩溃
恢复入口见 :meth:`Orchestrator.resume_rewrite`（issue #11）。

**stage 降级兜底**（异常就地置错误 + 日志 + 单向向前，PRD §13）与各 stage 的落图闭包
（``build``）落于 :mod:`agents.assembly`（wiring 模块，随 manifest 一处收口）；本模块只
承载状态图 schema（:class:`PipelineState` / reducer）、拓扑 seam 数据载体（:class:`StageSpec`
+ :func:`default_pipeline`）与图装配 / 驱动（:class:`Orchestrator`）。
:class:`agents.hitl2.Hitl2GateError` 为硬闸门正确性硬停（绝不无人拍板自动采纳，ADR-0010），
非单点波动，build 闭包内**原样上抛、不兜底**。

**拓扑 seam（PipelineSpec）**：流水线拓扑由数据驱动的 :class:`StageSpec` 序列描述、
由 :meth:`Orchestrator._build_graph` 据此布线。默认 :func:`default_pipeline` 遍历
:data:`agents.assembly.MANIFEST` 复刻上述固定拓扑；调用方可传入另一种 spec（如省略
``hypothesis_propose`` / ``judgment``）以表达不同拓扑——这是该 seam 的「第二种 adapter」，使其
成为真 seam 而非假 seam（deep-module 原则：two adapters means a real seam）。各 stage 的
*实现* 仍由 :class:`Agents` 逐个注入、可独立插拔（adapter 层 seam）；manifest 把 agent 身份
（stub/real）与 stage 拓扑（deps/build）收口为单一数据源（ADR-0014）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict, TypeVar

from langgraph.graph import END, START, StateGraph

from agents.assembly import MANIFEST, Agents, create_stub_agents
from agents.hitl2 import assemble_final_document
from domain import (
    DEFAULT_SESSION_CONTEXT,
    Argument,
    Hypothesis,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source
from original_paragraphs import OriginalParagraphs

__all__ = [
    "PipelineState",
    "Orchestrator",
    "RunResult",
    "NodeFn",
    "StageSpec",
    "default_pipeline",
    "merge_argument_tree",
    "merge_paragraph_list",
]


def merge_argument_tree(
    left: list[Argument] | None,
    right: list[Argument] | None,
) -> list[Argument]:
    """``argument_tree`` channel 的 reducer：按 ``argument_id`` upsert 整树写入，保持首见顺序。

    ``argument_tree`` 只承载**整树**写入（parse / judgment / hitl2 各返回一棵完整树）：
    同 id 覆盖、新 id 追加。hypothesis_propose / retrieval 的 partial 更新走各自的专用
    channel（``hypotheses`` / ``citations``），由 ``judgment_node`` 读之判终态、再按序调
    merge/impact/consistency 纯函数后**整树写回** ``argument_tree``（单写者，故裁撤
    ``argument_credibility`` partial channel）——避免多线路整节点 upsert 互相覆盖、丢
    ``status`` 或 ``candidate_hypotheses``（dev-guide §2.2 铁律：共享可变状态换成带 reducer
    的 channel）。
    """

    merged = list(left or [])
    index = {n.argument_id: i for i, n in enumerate(merged)}
    for argument in right or []:
        pos = index.get(argument.argument_id)
        if pos is None:
            index[argument.argument_id] = len(merged)
            merged.append(argument)
        else:
            merged[pos] = argument
    return merged


_T = TypeVar("_T")


def _merge_dict(
    left: dict[str, _T] | None,
    right: dict[str, _T] | None,
) -> dict[str, _T]:
    """partial 更新 channel 的 reducer：按 key 求并集（每通道单写者，无 key 冲突）。"""

    return {**(left or {}), **(right or {})}


def merge_paragraph_list(
    left: list[ParagraphRecord] | None,
    right: list[ParagraphRecord] | None,
) -> list[ParagraphRecord]:
    """``paragraph_list`` channel 的 reducer：按 ``paragraph_id`` upsert，保持首见顺序。

    形如 :func:`merge_argument_tree`（按 ``argument_id`` upsert）：同 ``paragraph_id`` 覆盖、
    新 id 追加。单写者 = parse+partition；即便单写者无冲突亦沿用同形以策安全——hitl1 打回
    重跑 parse 时整列表重写，reducer 保证不重复、不丢序、按段覆盖更新。段落↔节点关系为
    正向、第一类、存储于段落侧（PRD §Solution）；本 reducer 只维护段落集合的合并语义，
    ``argument_tree_ids`` 与 ``argument_tree`` 的一致性由 parse 落地与不变式自检保证。
    """

    merged = list(left or [])
    index = {r.paragraph_id: i for i, r in enumerate(merged)}
    for record in right or []:
        pos = index.get(record.paragraph_id)
        if pos is None:
            index[record.paragraph_id] = len(merged)
            merged.append(record)
        else:
            merged[pos] = record
    return merged


class PipelineState(TypedDict, total=False):
    """流水线状态。

    ``original_doc`` 为原始输入；``original_paragraphs`` 为只读原文段落表（旁路贯穿全程，
    回写拷回的真相源，Agent 不整篇加载）；``argument_tree`` 为论证树（带 reducer 合并
    整树写入，单写者=judgment 整树写回故裁撤 ``argument_credibility`` partial channel）；
    ``hypotheses`` / ``citations`` 为 partial 更新 channel（hypothesis_propose / retrieval
    各为单写者，judgment 读之判终态后整树写回 ``argument_tree``）；``final_document`` 为
    终稿 bytes；``errors`` 记录异常兜底日志（#11 接入）。
    """

    original_doc: bytes
    original_paragraphs: OriginalParagraphs
    session_context: SessionContext
    query_time_range: TimeRange
    paragraph_list: Annotated[list[ParagraphRecord], merge_paragraph_list]
    argument_tree: Annotated[list[Argument], merge_argument_tree]
    hypotheses: Annotated[dict[str, list[Hypothesis]], _merge_dict]
    citations: Annotated[dict[str, list[Source]], _merge_dict]
    proposed_rewrites: Annotated[dict[str, str], _merge_dict]
    partition_retry_count: int
    hitl1_route: str
    final_document: bytes
    errors: Annotated[list[str], _append_errors]


def _append_errors(left: list[str] | None, right: list[str] | None) -> list[str]:
    return (left or []) + (right or [])


@dataclass(frozen=True)
class RunResult:
    """一次流水线运行的结果：终稿 bytes + 异常兜底日志（issue #11）。

    :attr:`final_document` 为终稿文本；:attr:`errors` 记录沿途各 stage 的异常兜底日志
    （空列表即全程无单点波动）。:meth:`Orchestrator.run` 只返回 ``final_document``；
    :meth:`Orchestrator.run_with_report` 返回完整 :class:`RunResult` 供审计 / 测试。
    """

    final_document: bytes
    errors: list[str]


# --------------------------------------------------------------------------- #
# 拓扑 seam：数据驱动的流水线描述（StageSpec + default_pipeline）
# --------------------------------------------------------------------------- #

NodeFn = Callable[[PipelineState], dict[str, object]]
"""单个 stage 的可执行形态：``PipelineState → patch``（与 LangGraph 图节点函数同构）。"""


@dataclass(frozen=True)
class StageSpec:
    """流水线中一个 stage 的描述（拓扑 seam 的数据载体）。

    :attr:`name` 为图节点名；:attr:`build` 据 :class:`Agents` 产出该 stage 的
    :data:`NodeFn`（注入 seam 在此与拓扑 seam 汇合——实现可换、拓扑亦可换）；
    :attr:`deps` 为上游 stage 名（``()`` 表示接 ``START``）。未被任何 stage 依赖者接
    ``END``。默认 :func:`default_pipeline` 复刻固定拓扑；传入另一种序列即另一种拓扑。

    :attr:`route` 为条件路由 seam（ADR-0018 受控打回）：不为 ``None`` 的 stage 由条件边
    据当前 state 路由——返回 ``None`` 走默认下游（依赖该 stage 的节点们）、返回节点名 /
    节点名列表则路由到之（``hitl1`` 打回时返回 ``"parse+partition"``）。有 ``route`` 的
    stage 不再布静态出边（条件边独占其出向），但其入向边仍由依赖者的 ``deps`` 表达。
    :attr:`max_replays` 为该 stage 循环预算（打回上限），用于图 recursion_limit 缩放，
    使有界循环不触发 :class:`langgraph.errors.GraphRecursionError`。
    """

    name: str
    build: Callable[[Agents], NodeFn]
    deps: tuple[str, ...]
    route: Callable[[PipelineState], str | list[str] | None] | None = None
    max_replays: int = 0


def default_pipeline() -> tuple[StageSpec, ...]:
    """默认流水线拓扑：遍历 :data:`agents.assembly.MANIFEST` 复刻 PRD §13 固定顺序的
    7 个 stage。

    parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop
    → hitl2 → END（Slice 6 后，writeback 裁撤、终稿改由 rewrite_loop 提议 + hitl2 确认拼接
    落地）。manifest 是 agent 身份（stub/real）与 stage 拓扑（deps/build）的单一数据源
    （ADR-0014）——加 Agent 只需新增子包 + ``Agents`` 字段 + manifest 条目（触点 3）。
    """

    return tuple(
        StageSpec(e.name, e.build, e.deps, e.route, e.max_replays) for e in MANIFEST
    )


class Orchestrator:
    """全局调度中枢：装配并驱动整条流水线。

    注入一组 :class:`Agents`（默认全套桩）与一条 :data:`StageSpec` 序列（默认
    :func:`default_pipeline`）。后续切片用真实子智能体替换对应桩、其余不变；亦可传入
    另一种 spec 表达不同拓扑（如省略 ``hypothesis_propose`` / ``judgment``）。中枢本身不再
    随切片重写。
    """

    def __init__(
        self,
        agents: Agents | None = None,
        spec: tuple[StageSpec, ...] | None = None,
        *,
        checkpointer: Any | None = None,
    ) -> None:
        self.agents = agents or create_stub_agents()
        self.spec: tuple[StageSpec, ...] = spec if spec is not None else default_pipeline()
        self._recursion_limit: int = self._compute_recursion_limit(self.spec)
        # checkpointer（ADR-0022·T-03）：None = 内存态、同步 invoke（既有路径，保 e2e 字节级
        # 承诺测试零改动）；注入 ``AsyncPostgresSaver`` 后图可 ``interrupt`` 暂停 + 跨进程续跑
        # （thread_id = session_id，见 :meth:`run_with_report` / 驱动者 resume 循环）。
        self.checkpointer: Any | None = checkpointer
        self.graph: Any = self._build_graph(self.spec)

    @staticmethod
    def _compute_recursion_limit(spec: tuple[StageSpec, ...]) -> int:
        """据拓扑推导 recursion_limit（ADR-0018 有界打回不触发 GraphRecursionError）。

        无循环（``max_replays`` 全 0）时返回 langgraph 默认 25——行为与重构前一致。
        有循环时按 ``4 * Σ max_replays`` 留预算：每次打回 = 上游 1 + 本节点 1 ≈ 2 次节点
        执行，乘 2 作余量；打回循环之外的主干节点数已含于 25 基线。调用方仍可经
        ``session_config["recursion_limit"]`` 覆盖。
        """

        total_replays = sum(s.max_replays for s in spec)
        return 25 + 4 * total_replays

    @staticmethod
    def _make_router(
        route: Callable[[PipelineState], str | list[str] | None],
        downstream: tuple[str, ...],
    ) -> Callable[[PipelineState], str | list[str]]:
        """把 StageSpec.route 绑定其下游，产出 langgraph 条件边 router。

        route 返回 ``None`` → 走默认下游（依赖该 stage 的节点们；空则 ``END``）；
        返回节点名 / 节点名列表 → 原样路由到之（``hitl1`` 打回返回 ``"parse+partition"``）。
        """

        def router(state: PipelineState) -> str | list[str]:
            target = route(state)
            if target is None:
                return list(downstream) if downstream else END
            return target

        return router

    def _build_graph(self, spec: tuple[StageSpec, ...]) -> Any:
        """据 :data:`StageSpec` 序列布线：每个 stage 接其 deps，无下游者接 END。

        有 ``route`` 的 stage（如 ``hitl1``）由条件边独占出向——其依赖者不再布静态出边
        到之、改由条件边 router 在 ``None`` 路径返回这些下游（ADR-0018 受控打回边
        ``hitl1 → parse+partition`` 亦由该条件边表达）。
        """

        graph = StateGraph(PipelineState)
        for stage in spec:
            fn = stage.build(self.agents)
            # 包一层字面 lambda 使 mypy 视其为 *函数类型* 以匹配 langgraph
            # ``add_node`` 的 ``_Node[NodeInputT]`` 重载——直接传 ``NodeFn`` 别名
            # （Callable Instance）时，mypy 无法从 ``StateNode`` 联合体（双 TypeVar
            # ``NodeInputT`` / ``ContextT``）反推，报 call-overload。字面 lambda 运行时
            # 仅多一帧直传调用，无行为变化。
            graph.add_node(stage.name, lambda state, _fn=fn: _fn(state))

        # 下游映射：name → 依赖该 stage 的 stage 名们（条件边 None 路径用）。
        downstream: dict[str, tuple[str, ...]] = {s.name: () for s in spec}
        for stage in spec:
            for dep in stage.deps:
                if dep in downstream:
                    downstream[dep] += (stage.name,)

        routed = {s.name for s in spec if s.route is not None}
        depended = {dep for stage in spec for dep in stage.deps}
        for stage in spec:
            if not stage.deps:
                graph.add_edge(START, stage.name)
            else:
                for dep in stage.deps:
                    if dep in routed:
                        # 该上游由条件边独占出向——其下游经 router 的 None 路径路由，不布静态边。
                        continue
                    graph.add_edge(dep, stage.name)
            if stage.route is not None:
                graph.add_conditional_edges(
                    stage.name, self._make_router(stage.route, downstream[stage.name])
                )
            elif stage.name not in depended:
                graph.add_edge(stage.name, END)
        return graph.compile(checkpointer=self.checkpointer)

    def run(
        self,
        original_doc: bytes,
        *,
        session_config: dict[str, Any] | None = None,
        session_context: SessionContext | None = None,
    ) -> bytes:
        """驱动整条流水线：原始文本 → 终稿 bytes。

        ``session_context`` 为贯穿全链的运行上下文（ADR-0021，与 ``original_doc`` 同入 START、
        全链只读）；缺省注入确定性桩 :data:`domain.DEFAULT_SESSION_CONTEXT`（保可测可复现）。
        ``session_config`` 透传 langgraph ``RunnableConfig``（ADR-0016）——``metadata`` /
        ``tags`` / ``callbacks`` 线程贯穿整条 Agent 链路，业务节点零侵入。当前为 #4
        checkpointer 预备（``session_id`` 键），本切片内存态、不消费。
        """

        return self.run_with_report(
            original_doc,
            session_config=session_config,
            session_context=session_context,
        ).final_document

    def run_with_report(
        self,
        original_doc: bytes,
        *,
        session_config: dict[str, Any] | None = None,
        session_context: SessionContext | None = None,
    ) -> RunResult:
        """驱动整条流水线，返回终稿 + 异常兜底日志（issue #11）。

        比 :meth:`run` 多返回 ``errors`` 通道——沿途各 stage 的单点波动兜底日志，供审计
        与测试断言「节点置错误状态、流水线仍推进至终稿」。全程无波动时 ``errors`` 为空。

        ``session_context`` 缺省注入 :data:`domain.DEFAULT_SESSION_CONTEXT`（确定性桩），
        与 ``original_doc`` 同入 START，贯穿全链只读（ADR-0021）。
        """

        if not isinstance(original_doc, (bytes, bytearray)):
            raise TypeError(
                f"Orchestrator.run 要求 bytes 输入，收到 {type(original_doc).__name__}"
            )
        ctx = session_context if session_context is not None else DEFAULT_SESSION_CONTEXT
        config: dict[str, Any] = dict(session_config or {})
        # recursion 预算随拓扑 max_replays 缩放（ADR-0018 有界打回不触发 GraphRecursionError）；
        # 调用方经 session_config["recursion_limit"] 显式覆盖时不改。
        config.setdefault("recursion_limit", self._recursion_limit)
        if self.checkpointer is not None and ctx.session_id:
            # thread_id = session_id（ADR-0022·T-03）：checkpointer 按 thread 持久化 checkpoint
            # 与 interrupt 暂停点；同 session_id 续跑即复用同一 thread 的断点。
            config.setdefault("configurable", {})
            config["configurable"].setdefault("thread_id", ctx.session_id)
        result: dict[str, Any] = self.graph.invoke(
            {"original_doc": bytes(original_doc), "session_context": ctx},
            config=config,
        )
        final: bytes = result["final_document"]
        errors: list[str] = list(result.get("errors", []))
        return RunResult(final_document=final, errors=errors)

    def resume_rewrite(
        self,
        resolved_rewrites: dict[str, str],
        original_paragraphs: OriginalParagraphs,
    ) -> bytes:
        """终稿拼装幂等续跑入口（issue #11 · 衔接 rewrite_loop/hitl2 · ADR-0017）。

        终稿拼装中断（崩溃 / 进程退出 / hitl2 stage 兜底回退原文）后，对持久化的
        ``resolved_rewrites``（HITL-2 已确认 / 编辑的段文本表）续跑：复用纯函数
        :func:`agents.hitl2.assemble_final_document` 的幂等再推导——按 ``original_paragraphs``
        规范顺序缝合（确认 / 编辑段用其文本、驳回 / 未触达段逐字节原文），重跑得同一份 bytes。

        与 :meth:`run_with_report` 的关系：本方法是崩溃恢复的独立入口——调用方持有落盘的
        ``resolved_rewrites`` + 只读原文表即可续跑，无需重跑整条流水线、亦无需再调 LLM /
        闸门（``resolved_rewrites`` 已是 HITL-2 决策应用后的终态）。
        """

        return assemble_final_document(original_paragraphs, resolved_rewrites)
