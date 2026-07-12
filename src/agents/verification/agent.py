"""线路 1 · 体检 Agent：事实验证 ReAct 循环（PRD §5、issue #4、ADR-0011）。

对论点（main_claim / sub_claim）与论据（evidence）做正向检索校验。内部「推理—行动」
循环仅聚焦「为查清事实而自动调整检索词」：LLM 每步做一个极窄的结构化决策——继续检索
（新/调检索词 + 通道）或就地结论（``credible / doubtful / error`` + 简短理由）。查到明确
比对素材或触发迭代硬上限即退出，就地写回节点状态。

控制流落代码而非 prompt 散文（``docs/langgraph-dev-guide.md`` §0 铁律）：
- 迭代硬上限（``max_iterations``，超时硬上限）为参数、非 prompt 请求——绝不卡死流程。
- 任何异常（LLM 抛、检索抛、结构非法）→ 节点落 ``error``，单向推进到下一节点（PRD §13）。
- ``content`` 永不被改写（节点文本只来自只读表，``parser`` 先例，by construction）。

取证经公共检索层契约（``infra.retrieval``，#3）：按 ``SearchStep`` 构造 ``RetrievalRequest``
发出，合规（白名单/权限/模板）由检索层在接口层强制。返回的 ``Source`` 累积为 observations
回喂 LLM；不整篇 dump、不放入 messages 原文（dev-guide §4 源压缩铁律）。

覆盖范围：``main_claim / sub_claim / evidence``（claim & evidence）。跳过 ``qualification``
（限定条件、非事实断言）与影子节点（``background / evaluation``，只读不参与校验）——
二者保持 ``unverified``、不出现在 partial 更新中（ADR-0011 状态机分工）。

状态语义（ADR-0008 对称、ADR-0011 状态机）：体检产出原文侧 ``credible / doubtful / error``
（↔ 开药假设侧 ``supported / doubtful / refuted``）。绝不产出 ``invalid``（影响传导 #7 的上层
判决）、``adopted`` / ``corrected``（HITL-2 #9 + 回写 #10）。
"""

from __future__ import annotations

from agents.verification.contract import (
    ConcludeStep,
    SearchStep,
    VerifyLlmClient,
    VerifyVerdict,
)
from domain import ArgumentationNode, NodeStatus, NodeType
from infra.history import HistoryStore
from infra.retrieval import RetrievalLayer
from infra.retrieval_tool import RetrievalTool
from infra.tool_protocol import ToolRegistry

__all__ = ["verify"]


_VERIFY_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.MAIN_CLAIM, NodeType.SUB_CLAIM, NodeType.EVIDENCE}
)


def _should_verify(node: ArgumentationNode) -> bool:
    """体检覆盖 claim & evidence；跳过 qualification 与影子节点（保持 ``unverified``）。"""

    return node.node_type in _VERIFY_TYPES


_VERDICT_TO_STATUS: dict[VerifyVerdict, NodeStatus] = {
    VerifyVerdict.CREDIBLE: NodeStatus.CREDIBLE,
    VerifyVerdict.DOUBTFUL: NodeStatus.DOUBTFUL,
    VerifyVerdict.ERROR: NodeStatus.ERROR,
}


def _verify_node(
    node: ArgumentationNode,
    llm: VerifyLlmClient,
    registry: ToolRegistry,
    max_iterations: int,
) -> NodeStatus:
    """单节点 ReAct 循环： bounded、绝不卡死。异常/超时硬上限 → ``error``。

    检索经 ``registry.dispatch("retrieve", step=...)``（ADR-0015）：``SearchStep →
    RetrievalRequest`` 翻译与合规校验收口于 :class:`infra.retrieval_tool.RetrievalTool`。
    观察累积于 :class:`infra.history.HistoryStore`，回喂 LLM 前经压缩（ADR-0016）。
    """

    history = HistoryStore()
    for _ in range(max_iterations):
        try:
            step = llm.next_step(node, history.compressed_view())
        except Exception:
            return NodeStatus.ERROR
        try:
            if isinstance(step, ConcludeStep):
                return _VERDICT_TO_STATUS[step.verdict]
            if isinstance(step, SearchStep):
                result = registry.dispatch("retrieve", step=step)
                history.extend(result.sources)
                continue
            return NodeStatus.ERROR  # 结构非法（非 union 成员）
        except Exception:
            return NodeStatus.ERROR
    return NodeStatus.ERROR  # 迭代硬上限（超时）


def verify(
    tree: list[ArgumentationNode],
    llm: VerifyLlmClient,
    retrieval: RetrievalLayer,
    *,
    max_iterations: int = 8,
) -> dict[str, ArgumentationNode]:
    """对覆盖范围内的节点跑 ReAct 体检，返回 partial 更新（by ``node_id``）。

    - 覆盖 ``main_claim / sub_claim / evidence``；``qualification`` 与影子节点不在 dict 中
      （保持 ``unverified``，下游合并/影响据此识别未体检节点）。
    - 每节点写回**恰好一个**终态（``credible / doubtful / error``）；``content`` 不动。
    - 不修改输入树：返回的节点为 ``model_copy`` 新实例（输入节点状态不变）。
    """

    updates: dict[str, ArgumentationNode] = {}
    registry = ToolRegistry()
    registry.register(RetrievalTool(retrieval))
    for node in tree:
        if not _should_verify(node):
            continue
        status = _verify_node(node, llm, registry, max_iterations)
        updates[node.node_id] = node.model_copy(update={"status": status})
    return updates
