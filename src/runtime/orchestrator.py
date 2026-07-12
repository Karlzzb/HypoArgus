"""全局调度中枢（Orchestrator，PRD §13、ADR 拓扑 §1）。

用 LangGraph ``StateGraph`` 把整条流水线落成一张可执行的状态图——控制流落边而非
prompt 散文（见 ``docs/langgraph-dev-guide.md``）。数据在子环节间经 state channel
路由、无跨模块直接调用；流水线严格单向、绝不打回。

``partition → parse →〖HITL-1〗→ (体检 ∥ 开药) → merge → impact → consistency
→〖HITL-2〗→ writeback → END``

双线路（体检 ∥ 开药）是固定的两条并行分支（非动态 fan-out），用两条并行边表达；
二者在 ``merge`` 处 join。体检 / 开药各自把 partial 更新写入**专用 channel**
（``verification_updates`` / ``hypothesis_updates``），由 ``merge_node`` 经
:func:`agents.merge.merge_with_partials` 字段级合流到同一棵树再整树写入 ``tree``——避免并行
两线路整节点 upsert 互相覆盖、丢 ``status`` 或 ``candidate_hypotheses``
（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）。

本切片 HITL 仍为同步注入闸门（不打断、不接 checkpointer）；真实 ``interrupt`` +
``Command(resume)`` + checkpointer 属后续切片（dev-guide §7、§8）。回写幂等续跑的崩溃
恢复入口见 :meth:`Orchestrator.resume_writeback`（issue #11）。

**stage 降级兜底**（异常就地置错误 + 日志 + 单向向前，PRD §13）与各 stage 的落图闭包
（``build``）落于 :mod:`agents.assembly`（wiring 模块，随 manifest 一处收口）；本模块只
承载状态图 schema（:class:`PipelineState` / reducer）、拓扑 seam 数据载体（:class:`StageSpec`
+ :func:`default_pipeline`）与图装配 / 驱动（:class:`Orchestrator`）。
:class:`agents.hitl2.Hitl2GateError` 为硬闸门正确性硬停（绝不无人拍板自动采纳，ADR-0010），
非单点波动，build 闭包内**原样上抛、不兜底**。

**拓扑 seam（PipelineSpec）**：流水线拓扑由数据驱动的 :class:`StageSpec` 序列描述、
由 :meth:`Orchestrator._build_graph` 据此布线。默认 :func:`default_pipeline` 遍历
:data:`agents.assembly.MANIFEST` 复刻上述固定拓扑；调用方可传入另一种 spec（如省略
``hypothesis`` / ``consistency``）以表达不同拓扑——这是该 seam 的「第二种 adapter」，使其
成为真 seam 而非假 seam（deep-module 原则：two adapters means a real seam）。各 stage 的
*实现* 仍由 :class:`Agents` 逐个注入、可独立插拔（adapter 层 seam）；manifest 把 agent 身份
（stub/real）与 stage 拓扑（deps/build）收口为单一数据源（ADR-0014）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.assembly import MANIFEST, Agents, create_stub_agents
from agents.writeback import WritebackResult
from domain import ArgumentationNode
from raw_store import RawParagraphStore

__all__ = [
    "PipelineState",
    "Orchestrator",
    "RunResult",
    "NodeFn",
    "StageSpec",
    "default_pipeline",
    "merge_tree",
]


def merge_tree(
    left: list[ArgumentationNode] | None,
    right: list[ArgumentationNode] | None,
) -> list[ArgumentationNode]:
    """``tree`` channel 的 reducer：按 ``node_id`` upsert 整树写入，保持首见顺序。

    ``tree`` 只承载**整树**写入（parse / merge / impact / consistency / hitl2 各返回
    一棵完整树）：同 id 覆盖、新 id 追加。体检 / 开药的 partial 更新走各自的专用
    channel（``verification_updates`` / ``hypothesis_updates``），由 ``merge_node`` 经
    :func:`agents.merge.merge_with_partials` 字段级合流后整树写入 ``tree``——避免并行两线路
    整节点 upsert 互相覆盖、丢 ``status`` 或 ``candidate_hypotheses``
    （dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）。
    """

    merged = list(left or [])
    index = {n.node_id: i for i, n in enumerate(merged)}
    for node in right or []:
        pos = index.get(node.node_id)
        if pos is None:
            index[node.node_id] = len(merged)
            merged.append(node)
        else:
            merged[pos] = node
    return merged


def _merge_dict(
    left: dict[str, ArgumentationNode] | None,
    right: dict[str, ArgumentationNode] | None,
) -> dict[str, ArgumentationNode]:
    """partial 更新 channel 的 reducer：按 key 求并集（每通道单写者，无 key 冲突）。"""

    return {**(left or {}), **(right or {})}


class PipelineState(TypedDict, total=False):
    """流水线状态。

    ``raw_text`` 为原始输入；``store`` 为只读原文段落表（旁路贯穿全程，回写拷回的
    真相源，Agent 不整篇加载）；``tree`` 为论证树（带 reducer 合并整树写入）；
    ``verification_updates`` / ``hypothesis_updates`` 为两线路 partial 更新（字段级合流
    于 merge 节点，避免并行整节点覆盖）；``final_doc`` 为终稿 bytes；``errors`` 记录
    异常兜底日志（#11 接入）。
    """

    raw_text: bytes
    store: RawParagraphStore
    tree: Annotated[list[ArgumentationNode], merge_tree]
    verification_updates: Annotated[dict[str, ArgumentationNode], _merge_dict]
    hypothesis_updates: Annotated[dict[str, ArgumentationNode], _merge_dict]
    final_doc: bytes
    errors: Annotated[list[str], _append_errors]


def _append_errors(left: list[str] | None, right: list[str] | None) -> list[str]:
    return (left or []) + (right or [])


@dataclass(frozen=True)
class RunResult:
    """一次流水线运行的结果：终稿 bytes + 异常兜底日志（issue #11）。

    :attr:`final_doc` 为终稿文本；:attr:`errors` 记录沿途各 stage 的异常兜底日志
    （空列表即全程无单点波动）。:meth:`Orchestrator.run` 只返回 ``final_doc``；
    :meth:`Orchestrator.run_with_report` 返回完整 :class:`RunResult` 供审计 / 测试。
    """

    final_doc: bytes
    errors: list[str]


# --------------------------------------------------------------------------- #
# 拓扑 seam：数据驱动的流水线描述（StageSpec + default_pipeline）
# --------------------------------------------------------------------------- #

NodeFn = Callable[[PipelineState], dict[str, object]]
"""单个 stage 的可执行形态：``PipelineState → patch``（与 LangGraph node 函数同构）。"""


@dataclass(frozen=True)
class StageSpec:
    """流水线中一个 stage 的描述（拓扑 seam 的数据载体）。

    :attr:`name` 为图节点名；:attr:`build` 据 :class:`Agents` 产出该 stage 的
    :data:`NodeFn`（注入 seam 在此与拓扑 seam 汇合——实现可换、拓扑亦可换）；
    :attr:`deps` 为上游 stage 名（``()`` 表示接 ``START``）。未被任何 stage 依赖者接
    ``END``。默认 :func:`default_pipeline` 复刻固定拓扑；传入另一种序列即另一种拓扑。
    """

    name: str
    build: Callable[[Agents], NodeFn]
    deps: tuple[str, ...]


def default_pipeline() -> tuple[StageSpec, ...]:
    """默认流水线拓扑：遍历 :data:`agents.assembly.MANIFEST` 复刻 PRD §13 固定顺序的
    10 个 stage 与并行 / join 边。

    partition → parse → hitl1 → (verification ∥ hypothesis) → merge → impact →
    consistency → hitl2 → writeback → END。manifest 是 agent 身份（stub/real）与 stage
    拓扑（deps/build）的单一数据源（ADR-0014）——加 Agent 只需新增子包 + ``Agents`` 字段
    + manifest 条目（触点 3）。
    """

    return tuple(StageSpec(e.name, e.build, e.deps) for e in MANIFEST)


class Orchestrator:
    """全局调度中枢：装配并驱动整条流水线。

    注入一组 :class:`Agents`（默认全套桩）与一条 :data:`StageSpec` 序列（默认
    :func:`default_pipeline`）。后续切片用真实子智能体替换对应桩、其余不变；亦可传入
    另一种 spec 表达不同拓扑（如省略 ``hypothesis`` / ``consistency``）。中枢本身不再
    随切片重写。
    """

    def __init__(
        self,
        agents: Agents | None = None,
        spec: tuple[StageSpec, ...] | None = None,
    ) -> None:
        self.agents = agents or create_stub_agents()
        self.spec: tuple[StageSpec, ...] = spec if spec is not None else default_pipeline()
        self.graph: Any = self._build_graph(self.spec)

    def _build_graph(self, spec: tuple[StageSpec, ...]) -> Any:
        """据 :data:`StageSpec` 序列布线：每个 stage 接其 deps，无下游者接 END。"""

        graph = StateGraph(PipelineState)
        for stage in spec:
            fn = stage.build(self.agents)
            # 包一层字面 lambda 使 mypy 视其为 *函数类型* 以匹配 langgraph
            # ``add_node`` 的 ``_Node[NodeInputT]`` 重载——直接传 ``NodeFn`` 别名
            # （Callable Instance）时，mypy 无法从 ``StateNode`` 联合体（双 TypeVar
            # ``NodeInputT`` / ``ContextT``）反推，报 call-overload。字面 lambda 运行时
            # 仅多一帧直传调用，无行为变化。
            graph.add_node(stage.name, lambda state, _fn=fn: _fn(state))

        depended = {dep for stage in spec for dep in stage.deps}
        for stage in spec:
            if not stage.deps:
                graph.add_edge(START, stage.name)
            else:
                for dep in stage.deps:
                    graph.add_edge(dep, stage.name)
            if stage.name not in depended:
                graph.add_edge(stage.name, END)
        return graph.compile()

    def run(
        self, raw_text: bytes, *, session_config: dict[str, Any] | None = None
    ) -> bytes:
        """驱动整条流水线：原始文本 → 终稿 bytes。

        ``session_config`` 透传 langgraph ``RunnableConfig``（ADR-0016）——``metadata`` /
        ``tags`` / ``callbacks`` 线程贯穿整条 Agent 链路，业务节点零侵入。当前为 #4
        checkpointer 预备（``session_id`` 键），本切片内存态、不消费。
        """

        return self.run_with_report(raw_text, session_config=session_config).final_doc

    def run_with_report(
        self, raw_text: bytes, *, session_config: dict[str, Any] | None = None
    ) -> RunResult:
        """驱动整条流水线，返回终稿 + 异常兜底日志（issue #11）。

        比 :meth:`run` 多返回 ``errors`` 通道——沿途各 stage 的单点波动兜底日志，供审计
        与测试断言「节点置错误状态、流水线仍推进至终稿」。全程无波动时 ``errors`` 为空。
        """

        if not isinstance(raw_text, (bytes, bytearray)):
            raise TypeError(
                f"Orchestrator.run 要求 bytes 输入，收到 {type(raw_text).__name__}"
            )
        result: dict[str, Any] = self.graph.invoke(
            {"raw_text": bytes(raw_text)}, config=session_config
        )
        final: bytes = result["final_doc"]
        errors: list[str] = list(result.get("errors", []))
        return RunResult(final_doc=final, errors=errors)

    def resume_writeback(
        self, tree: list[ArgumentationNode], store: RawParagraphStore
    ) -> WritebackResult:
        """回写幂等续跑入口（issue #11 · 衔接 #10 · ADR-0011）。

        回写中断（崩溃 / 进程退出 / :meth:`run_with_report` 中 writeback stage 兜底回退）
        后，对持久化树续跑：复用纯函数 :func:`agents.writeback.writeback` 的幂等再推导——
        扫「``adopted`` 且未 ``corrected``」的节点、据持久化的 ``adopted_hypothesis_id``
        重新分流缝合，已 ``corrected`` 者从原始 bytes 重新推导、不重复注入，最终状态收敛为
        ``corrected``。不可解析者停留 ``adopted`` + 贴 ``writeback_error``，可再次续跑。

        与 :meth:`run_with_report` 的关系：本方法是崩溃恢复的独立入口——调用方持有落盘的
        终版树 + 只读原文表即可续跑，无需重跑整条流水线。
        """

        return self.agents.writeback(list(tree), store)
