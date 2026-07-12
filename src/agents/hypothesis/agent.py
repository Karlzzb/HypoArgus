"""线路 2 · 开药 Agent：投机生成 + 逐条取证（PRD §5、issue #5、ADR-0002/0007/0008/0011）。

对节点投机生成可证伪修订假设、再对每条假设经公共检索层取证，产出
``supported / doubtful / refuted`` 三态。与体检线路（#4）**乐观并行**：投机生成不读取
体检结论（ADR-0002 解法 A），两线路结果由双轨合并算子（#6）在并行层之后裁剪。

两阶段（每节点）：

1. **投机生成**——LLM 一步产出 0..N 条 ``HypothesisProposal``（``text`` + 单一
   ``relation`` + ``confidence``）。一条假设只承载一种关系（ADR-0007），混合意图须由
   LLM 拆成多条（结构上：每条 proposal 恰一个 ``relation`` 字段）。「无假设」= 空列表
   （ADR-0008，非第四种枚举）。生成不依赖体检结论，亦不读检索。
2. **逐条取证**——对每条假设跑「推理—行动」循环（镜像体检 #4 的 ReAct），LLM 每步
   做一个极窄结构化决策：继续检索（新/调检索词 + 通道）或就地结论
   （``supported / doubtful / refuted`` + 简短理由）。查到明确比对素材或触发迭代硬上限
   即退出。

控制流落代码而非 prompt 散文（``docs/langgraph-dev-guide.md`` §0 铁律）：

- 迭代硬上限（``max_iterations``）为参数、非 prompt 请求——绝不卡死流程。
- 取证任何异常（LLM 抛、检索抛/合规违规、结构非法、迭代耗尽）→ 该假设落 ``doubtful``
  （取证失败 ≠ 被推翻；不因检索波动丢弃假设，弱呈现供人判，ADR-0008）。
- 生成任何异常 → 该节点无假设（空列表，保守、不抛出、不卡死）。
- ``content`` 永不被改写（节点文本只来自只读表，``parser`` / ``verification`` 先例）。

覆盖范围（ADR-0002 成本闸）：``evidence`` + ``sub_claim``。跳过 ``main_claim``（由影响
传导 #7 处理、不直接替换）、``qualification``、影子节点（``background / evaluation``，
只读不参与校验）——被跳过节点不出现在 partial 更新中（``candidate_hypotheses`` 维持空）。

状态语义（ADR-0008 对称）：假设侧 ``supported / doubtful / refuted`` ↔ 原文侧
``credible / doubtful / error``。``confidence`` 仅用于同节点多条 ``supported`` 假设的
排序展示，**绝不**参与取证判决或合并矩阵裁决（ADR-0008 铁律）。

注：体检（#4）写回节点 ``status``、开药（#5）写回 ``candidate_hypotheses``，二者在
``merge_tree`` reducer 处合流到同一节点；双轨合并算子（#6）据此读
``原文.status × 假设.status`` 矩阵裁决。
"""

from __future__ import annotations

import hashlib

from agents.hypothesis.contract import (
    HypothesisConcludeStep,
    HypothesisLlmClient,
    HypothesisSearchStep,
    HypothesisVerdict,
)
from domain import (
    ArgumentationNode,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    NodeType,
)
from infra.history import HistoryStore
from infra.retrieval import RetrievalLayer
from infra.retrieval_tool import RetrievalTool
from infra.tool_protocol import ToolRegistry

__all__ = ["hypothesize"]


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数 seam，可独立单测
# --------------------------------------------------------------------------- #


_HYPOTHESIS_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.EVIDENCE, NodeType.SUB_CLAIM}
)


def _should_hypothesize(node: ArgumentationNode) -> bool:
    """开药覆盖 evidence + sub_claim；跳过 main_claim / qualification / 影子节点。"""

    return node.node_type in _HYPOTHESIS_TYPES


_VERDICT_TO_STATUS: dict[HypothesisVerdict, HypothesisStatus] = {
    HypothesisVerdict.SUPPORTED: HypothesisStatus.SUPPORTED,
    HypothesisVerdict.DOUBTFUL: HypothesisStatus.DOUBTFUL,
    HypothesisVerdict.REFUTED: HypothesisStatus.REFUTED,
}


def _hypothesis_id(
    node_id: str, relation: HypothesisRelation, text: str, idx: int
) -> str:
    """确定性 hypothesis_id：节点 id + 关系 + 文本 + 序号派生（非计数器，可重算）。

    供 HITL-2（#9）采纳与回写（#10）幂等链稳定引用同一假设。
    """

    digest = hashlib.blake2b(
        f"{node_id}|{relation.value}|{text}|{idx}".encode(), digest_size=6
    ).hexdigest()
    return f"h-{digest}"


def _verify_hypothesis(
    hypothesis_text: str,
    llm: HypothesisLlmClient,
    registry: ToolRegistry,
    max_iterations: int,
) -> HypothesisStatus:
    """单条假设的取证 ReAct 循环：bounded、绝不卡死。

    任何异常 / 迭代硬上限 / 结构非法 → ``doubtful``（取证失败 ≠ 被推翻，ADR-0008）。
    检索经 ``registry.dispatch("retrieve", step=...)``（ADR-0015）；观察累积于
    :class:`infra.history.HistoryStore`，回喂 LLM 前经压缩（ADR-0016）。
    """

    history = HistoryStore()
    for _ in range(max_iterations):
        try:
            step = llm.next_verify_step(hypothesis_text, history.compressed_view())
        except Exception:
            return HypothesisStatus.DOUBTFUL
        try:
            if isinstance(step, HypothesisConcludeStep):
                return _VERDICT_TO_STATUS[step.verdict]
            if isinstance(step, HypothesisSearchStep):
                result = registry.dispatch("retrieve", step=step)
                history.extend(result.sources)
                continue
            return HypothesisStatus.DOUBTFUL  # 结构非法（非 union 成员）
        except Exception:
            return HypothesisStatus.DOUBTFUL
    return HypothesisStatus.DOUBTFUL  # 迭代硬上限（超时）


def hypothesize(
    tree: list[ArgumentationNode],
    llm: HypothesisLlmClient,
    retrieval: RetrievalLayer,
    *,
    max_iterations: int = 8,
) -> dict[str, ArgumentationNode]:
    """对覆盖范围内的节点跑「投机生成 → 逐条取证」，返回 partial 更新（by ``node_id``）。

    - 覆盖 ``evidence / sub_claim``；``main_claim / qualification / 影子`` 节点不在 dict 中
      （保持空 ``candidate_hypotheses``，下游合并据此识别未开药节点）。
    - 每节点写回 ``candidate_hypotheses``（0..N 条，各带取证终态）；``content`` 与
      ``status`` 不动（``status`` 由体检 #4 写回，``candidate_hypotheses`` 由本切片写回，
      二者在 reducer 处合流，供 #6 矩阵裁决）。
    - 不修改输入树：返回的节点为 ``model_copy`` 新实例（输入节点不变）。
    """

    updates: dict[str, ArgumentationNode] = {}
    registry = ToolRegistry()
    registry.register(RetrievalTool(retrieval))
    for node in tree:
        if not _should_hypothesize(node):
            continue
        try:
            proposals = llm.propose(node)
        except Exception:
            proposals = []
        hypotheses: list[Hypothesis] = []
        for idx, proposal in enumerate(proposals):
            status = _verify_hypothesis(
                proposal.text, llm, registry, max_iterations
            )
            hypotheses.append(
                Hypothesis(
                    hypothesis_id=_hypothesis_id(
                        node.node_id, proposal.relation, proposal.text, idx
                    ),
                    text=proposal.text,
                    relation=proposal.relation,
                    status=status,
                    confidence=proposal.confidence,
                )
            )
        updates[node.node_id] = node.model_copy(
            update={"candidate_hypotheses": hypotheses}
        )
    return updates
