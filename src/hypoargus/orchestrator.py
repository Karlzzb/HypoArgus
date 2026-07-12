"""全局调度中枢（Orchestrator，PRD §13、ADR 拓扑 §1）。

用 LangGraph ``StateGraph`` 把整条流水线落成一张可执行的状态图——控制流落边而非
prompt 散文（见 ``docs/langgraph-dev-guide.md``）。数据在子环节间经 state channel
路由、无跨模块直接调用；流水线严格单向、绝不打回。

``partition → parse →〖HITL-1〗→ (体检 ∥ 开药) → merge → impact → consistency
→〖HITL-2〗→ writeback → END``

双线路（体检 ∥ 开药）是固定的两条并行分支（非动态 fan-out），用两条并行边表达；
二者在 ``merge`` 处 join。体检 / 开药各自把 partial 更新写入**专用 channel**
（``verification_updates`` / ``hypothesis_updates``），由 ``merge_node`` 经
:func:`apply_partial_updates` 字段级合流到同一棵树再整树写入 ``tree``——避免并行
两线路整节点 upsert 互相覆盖、丢 ``status`` 或 ``candidate_hypotheses``
（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）。

本切片 HITL 仍为同步注入闸门（不打断、不接 checkpointer）；真实 ``interrupt`` +
``Command(resume)`` + checkpointer 属后续切片（dev-guide §7、§8）。回写幂等续跑的崩溃
恢复入口见 :meth:`Orchestrator.resume_writeback`（issue #11）。

异常兜底与单向流控（issue #11 · PRD §13）：每个下游 stage 包 ``try/except``，任一智能体
异常 / 超时即「就地置目标节点错误状态 + 附日志 + 单向向前推进」，绝不因单点波动卡死整篇。
硬约束：无复杂分布式重试降级与跨模块挂起——异常即记日志、就地降级、继续向前。
:class:`Hitl2GateError` 为硬闸门正确性硬停（绝不无人拍板自动采纳，ADR-0010），非单点
波动，**不兜底、原样上抛**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from hypoargus.agents import Agents, create_stub_agents
from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.hitl2 import Hitl2GateError
from hypoargus.merge import apply_partial_updates
from hypoargus.raw_store import RawParagraphStore
from hypoargus.status_machine import mark_node_error
from hypoargus.writeback import WritebackResult

__all__ = [
    "PipelineState",
    "Orchestrator",
    "RunResult",
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
    :func:`apply_partial_updates` 字段级合流后整树写入 ``tree``——避免并行两线路
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


# 体检覆盖范围（PRD §5）：claim & evidence。整体异常时把范围内未判决节点就地置 error。
_VERIFY_SCOPE: frozenset[NodeType] = frozenset(
    {NodeType.MAIN_CLAIM, NodeType.SUB_CLAIM, NodeType.EVIDENCE}
)


def _log_error_patch(stage: str, exc: BaseException) -> dict[str, list[str]]:
    """构造异常兜底日志 patch：``{"errors": ["[stage] ExcType: msg"]}``。"""

    return {"errors": [f"[{stage}] {type(exc).__name__}: {exc}"]}


def _mark_verify_scope_error(
    tree: list[ArgumentationNode], reason: str
) -> list[ArgumentationNode]:
    """体检整体异常时把覆盖范围内、仍处未判决态的节点就地置 error（PRD §13）。

    ``claim`` / ``evidence`` 节点若仍 ``unverified`` / ``pending_verification``（体检本应
    判决却整体失败），就地置 ``error`` + 贴 ``orchestrator_error`` 标签——既兑现「目标节点
    置错误状态」，又使这些节点以 ``error``（待决）态流入 HITL-2 被驳回 → 原文逐字节还原。
    已判决（``credible`` / ``doubtful`` / ``error``）或非覆盖节点不动。
    """

    out: list[ArgumentationNode] = []
    for node in tree:
        if node.node_type in _VERIFY_SCOPE and node.status in (
            NodeStatus.UNVERIFIED,
            NodeStatus.PENDING_VERIFICATION,
        ):
            out.append(mark_node_error(node, reason=reason))
        else:
            out.append(node.model_copy())
    return out


class Orchestrator:
    """全局调度中枢：装配并驱动整条流水线。

    注入一组 :class:`Agents`（默认全套桩）；后续切片用真实子智能体替换对应桩、
    其余不变。中枢本身不再随切片重写。
    """

    def __init__(self, agents: Agents | None = None) -> None:
        self.agents = agents or create_stub_agents()
        self.graph: Any = self._build_graph()

    def _build_graph(self) -> Any:
        agents = self.agents

        def partition_node(state: PipelineState) -> dict[str, object]:
            """确定性段落切分 + 固化只读原文表（纯代码·零 LLM）。

            纯代码、无智能体波动，不包兜底——分区不变式自检失败即正确性 bug、应硬停。
            """

            raw_text: bytes = state["raw_text"]
            store = RawParagraphStore.from_text(raw_text)
            # store 自检：分区不变式（字节级还原是代码级确定的，不依赖任何模型）。
            rebuilt = b"".join(store.get(pid) for pid in store.paragraph_ids())
            assert rebuilt == raw_text, "分区不变式自检失败：拼接 ≠ 原始输入"
            return {"store": store, "tree": []}

        def parse_node(state: PipelineState) -> dict[str, object]:
            """论证结构解析（#2 接入真实 LLM）。异常 → 记日志 + 保留空树向前（PRD §13）。"""

            try:
                return {"tree": agents.parse(state["store"])}
            except Hitl2GateError:
                raise
            except Exception as exc:
                # 解析整体失败：无树可挂 → 保留 partition 的空树向前，下游空树 → 终稿逐字节还原。
                return {**_log_error_patch("parse", exc), "tree": []}

        def hitl1_node(state: PipelineState) -> dict[str, object]:
            """HITL-1 结构确认（#2 接入，可跳过）。异常 → 记日志 + 保留 stale 树向前。"""

            tree = state["tree"]
            try:
                return {"tree": agents.hitl1(tree)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("hitl1", exc), "tree": tree}

        def verification_node(state: PipelineState) -> dict[str, object]:
            """线路 1 · 体检（#4 接入 ReAct）。异常 → 覆盖范围内未判决节点置 error + 日志。

            整体异常（非单节点 LLM 抛错——单节点异常由体检内部已置 ``error``）时，把
            ``claim`` / ``evidence`` 范围内仍 ``unverified`` / ``pending`` 的节点就地置
            ``error``（PRD §13「目标节点置错误状态」），整树写入 ``tree``；不写
            ``verification_updates``（无 partial 可信）。下游合并据此见 ``error`` 状态。
            """

            tree = state["tree"]
            try:
                return {"verification_updates": agents.verification(tree)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                marked = _mark_verify_scope_error(tree, reason="verify")
                return {**_log_error_patch("verification", exc), "tree": marked}

        def hypothesis_node(state: PipelineState) -> dict[str, object]:
            """线路 2 · 开药（#5 接入）。异常 → 记日志 + 无假设向前（不置节点 error）。

            开药不持有节点 ``status``（只产 ``candidate_hypotheses``），整体异常即「本轮
            无假设」——不置节点 ``error``（避免覆盖体检判决），记日志、空 partial 向前。
            """

            tree = state["tree"]
            try:
                return {"hypothesis_updates": agents.hypothesis(tree)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("hypothesis", exc)}

        def merge_node(state: PipelineState) -> dict[str, object]:
            """双轨合并算子（#6 接入 12 格矩阵）。异常 → 记日志 + 保留已合流的 combined 向前。

            先字段级合流两线路 partial（``status`` ← 体检、``candidate_hypotheses`` ← 开药），
            再跑矩阵裁决。合并算子本身异常时，partial 已合流（``combined``）可信——保留之向前
            （无 ``merge_decision``，下游以体检/开药结果继续），记日志。
            """

            combined = apply_partial_updates(
                state["tree"],
                state.get("verification_updates", {}),
                state.get("hypothesis_updates", {}),
            )
            try:
                return {"tree": agents.merge(combined)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("merge", exc), "tree": combined}

        def impact_node(state: PipelineState) -> dict[str, object]:
            """影响传导（#7 接入，串行·不产文本）。异常 → 记日志 + 保留 stale 树向前。"""

            tree = state["tree"]
            try:
                return {"tree": agents.impact(tree)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("impact", exc), "tree": tree}

        def consistency_node(state: PipelineState) -> dict[str, object]:
            """一致性校验（#8 接入，批注门禁·不打回）。异常 → 记日志 + 保留 stale 树向前。"""

            tree = state["tree"]
            try:
                return {"tree": agents.consistency(tree)}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("consistency", exc), "tree": tree}

        def hitl2_node(state: PipelineState) -> dict[str, object]:
            """HITL-2 修订确认（#9 接入，不可跳过硬闸门）。异常 → 记日志 + 保留 stale 树向前。

            接收标注完成的树 + 只读原文表（HITL-2 对比左栏数据源，ADR-0005），返回采纳后的树。
            :class:`Hitl2GateError`（含硬闸门拒绝越权 PASS）为正确性硬停，**原样上抛、不兜底**
            （绝不无人拍板自动采纳，ADR-0010）；其余异常兜底：记日志 + 保留 stale 树（无人采纳）
            → 回写逐字节还原。
            """

            tree = state["tree"]
            try:
                return {"tree": agents.hitl2(tree, state["store"])}
            except Hitl2GateError:
                raise
            except Exception as exc:
                return {**_log_error_patch("hitl2", exc), "tree": tree}

        def writeback_node(state: PipelineState) -> dict[str, object]:
            """修订回写（#10 接入真实分流·幂等）。异常 → 记日志 + 回退原文 bytes 向前（PRD §13）。

            按段落原子缝合终稿 bytes、翻正采纳节点状态。回写整体异常时保护原文底线：回退为
            只读原文表逐字节拼接（== 原始输入，分区不变式），``adopted`` 节点保留 ``adopted`` 不
            翻正、待 :meth:`resume_writeback` 续跑（issue #11 衔接 #10）；记日志。幂等：续跑
            据持久化的 ``adopted_hypothesis_id`` 重新推导、不重复注入。
            """

            tree = state["tree"]
            store = state["store"]
            try:
                result = agents.writeback(tree, store)
                return {"final_doc": result.final_doc, "tree": result.tree}
            except Hitl2GateError:
                raise
            except Exception as exc:
                # 回退原文 bytes（保护原文底线）；adopted 节点不动、待续跑。
                fallback_doc = b"".join(store.get(pid) for pid in store.paragraph_ids())
                return {
                    **_log_error_patch("writeback", exc),
                    "final_doc": fallback_doc,
                    "tree": tree,
                }

        graph = StateGraph(PipelineState)
        graph.add_node("partition", partition_node)
        graph.add_node("parse", parse_node)
        graph.add_node("hitl1", hitl1_node)
        graph.add_node("verification", verification_node)
        graph.add_node("hypothesis", hypothesis_node)
        graph.add_node("merge", merge_node)
        graph.add_node("impact", impact_node)
        graph.add_node("consistency", consistency_node)
        graph.add_node("hitl2", hitl2_node)
        graph.add_node("writeback", writeback_node)

        graph.add_edge(START, "partition")
        graph.add_edge("partition", "parse")
        graph.add_edge("parse", "hitl1")
        # 双线路 · 乐观并行：两条并行边，在 merge 处 join barrier。
        graph.add_edge("hitl1", "verification")
        graph.add_edge("hitl1", "hypothesis")
        graph.add_edge("verification", "merge")
        graph.add_edge("hypothesis", "merge")
        graph.add_edge("merge", "impact")
        graph.add_edge("impact", "consistency")
        graph.add_edge("consistency", "hitl2")
        graph.add_edge("hitl2", "writeback")
        graph.add_edge("writeback", END)

        return graph.compile()

    def run(self, raw_text: bytes) -> bytes:
        """驱动整条流水线：原始文本 → 终稿 bytes。"""

        return self.run_with_report(raw_text).final_doc

    def run_with_report(self, raw_text: bytes) -> RunResult:
        """驱动整条流水线，返回终稿 + 异常兜底日志（issue #11）。

        比 :meth:`run` 多返回 ``errors`` 通道——沿途各 stage 的单点波动兜底日志，供审计
        与测试断言「节点置错误状态、流水线仍推进至终稿」。全程无波动时 ``errors`` 为空。
        """

        if not isinstance(raw_text, (bytes, bytearray)):
            raise TypeError(
                f"Orchestrator.run 要求 bytes 输入，收到 {type(raw_text).__name__}"
            )
        result: dict[str, Any] = self.graph.invoke({"raw_text": bytes(raw_text)})
        final: bytes = result["final_doc"]
        errors: list[str] = list(result.get("errors", []))
        return RunResult(final_doc=final, errors=errors)

    def resume_writeback(
        self, tree: list[ArgumentationNode], store: RawParagraphStore
    ) -> WritebackResult:
        """回写幂等续跑入口（issue #11 · 衔接 #10 · ADR-0011）。

        回写中断（崩溃 / 进程退出 / :meth:`run_with_report` 中 writeback stage 兜底回退）
        后，对持久化树续跑：复用纯函数 :func:`hypoargus.writeback.writeback` 的幂等再推导——
        扫「``adopted`` 且未 ``corrected``」的节点、据持久化的 ``adopted_hypothesis_id``
        重新分流缝合，已 ``corrected`` 者从原始 bytes 重新推导、不重复注入，最终状态收敛为
        ``corrected``。不可解析者停留 ``adopted`` + 贴 ``writeback_error``，可再次续跑。

        与 :meth:`run_with_report` 的关系：本方法是崩溃恢复的独立入口——调用方持有落盘的
        终版树 + 只读原文表即可续跑，无需重跑整条流水线。
        """

        return self.agents.writeback(list(tree), store)
