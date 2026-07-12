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

本切片 HITL 仍为桩（不打断），故暂不接 checkpointer；#2/#9 接入真实 interrupt 时
再 ``compile(checkpointer=...)``（dev-guide §7、§8）。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from hypoargus.agents import Agents, create_stub_agents
from hypoargus.domain import ArgumentationNode
from hypoargus.merge import apply_partial_updates
from hypoargus.raw_store import RawParagraphStore

__all__ = ["PipelineState", "Orchestrator", "merge_tree"]


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
            """确定性段落切分 + 固化只读原文表（纯代码·零 LLM）。"""

            raw_text: bytes = state["raw_text"]
            store = RawParagraphStore.from_text(raw_text)
            # store 自检：分区不变式（字节级还原是代码级确定的，不依赖任何模型）。
            rebuilt = b"".join(store.get(pid) for pid in store.paragraph_ids())
            assert rebuilt == raw_text, "分区不变式自检失败：拼接 ≠ 原始输入"
            return {"store": store, "tree": []}

        def parse_node(state: PipelineState) -> dict[str, object]:
            """论证结构解析（#2 接入真实 LLM）。"""

            return {"tree": agents.parse(state["store"])}

        def hitl1_node(state: PipelineState) -> dict[str, object]:
            """HITL-1 结构确认（#2 接入，可跳过）。"""

            return {"tree": agents.hitl1(state["tree"])}

        def verification_node(state: PipelineState) -> dict[str, object]:
            """线路 1 · 体检（#4 接入 ReAct）。返回 partial 更新至专用 channel。"""

            return {"verification_updates": agents.verification(state["tree"])}

        def hypothesis_node(state: PipelineState) -> dict[str, object]:
            """线路 2 · 开药（#5 接入）。返回 partial 更新至专用 channel。"""

            return {"hypothesis_updates": agents.hypothesis(state["tree"])}

        def merge_node(state: PipelineState) -> dict[str, object]:
            """双轨合并算子（#6 接入 12 格矩阵）。

            先字段级合流两线路 partial（``status`` ← 体检、``candidate_hypotheses`` ← 开药），
            再跑矩阵裁决、整树写入 ``tree``。
            """

            combined = apply_partial_updates(
                state["tree"],
                state.get("verification_updates", {}),
                state.get("hypothesis_updates", {}),
            )
            return {"tree": agents.merge(combined)}

        def impact_node(state: PipelineState) -> dict[str, object]:
            """影响传导（#7 接入，串行·不产文本）。"""

            return {"tree": agents.impact(state["tree"])}

        def consistency_node(state: PipelineState) -> dict[str, object]:
            """一致性校验（#8 接入，批注门禁·不打回）。"""

            return {"tree": agents.consistency(state["tree"])}

        def hitl2_node(state: PipelineState) -> dict[str, object]:
            """HITL-2 修订确认（#9 接入，不可跳过硬闸门）。

            接收标注完成的树 + 只读原文表（HITL-2 对比左栏数据源，ADR-0005），返回采纳后的树。
            """

            return {"tree": agents.hitl2(state["tree"], state["store"])}

        def writeback_node(state: PipelineState) -> dict[str, object]:
            """修订回写（#10 接入真实分流·幂等）。"""

            return {"final_doc": agents.writeback(state["tree"], state["store"])}

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

        if not isinstance(raw_text, (bytes, bytearray)):
            raise TypeError(
                f"Orchestrator.run 要求 bytes 输入，收到 {type(raw_text).__name__}"
            )
        result = self.graph.invoke({"raw_text": bytes(raw_text)})
        final: bytes = result["final_doc"]
        return final
