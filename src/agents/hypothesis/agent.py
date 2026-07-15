"""线路 2 · 开药 Agent：投机生成（PRD §5、issue #5、ADR-0002/0007/0008/0011、Slice 3 重构）。

对覆盖范围内的节点投机生成可证伪修订假设，产 ``list[Hypothesis]``（status=pending）。
与体检线路（#4）**乐观并行**：生成不读取体检结论（ADR-0002 解法 A），两线路结果由双轨
合并算子（#6）在并行层之后裁剪。

**Slice 3 重构**：原「投机生成 → 逐条取证」两阶段被拆分——本节点仅 ``propose``、不取证；
取证职责移出，推迟到 Slice 5 的 judgment 节点（吃 citations 判终态）。故假说产出即
``pending``，由 judgment 落终态（``supported / doubtful / refuted``）。

propose 读 ``paragraph_list``（段落聚合根），逐 argument 经 ``argument_tree_ids`` 反查该段
``original_content`` + ``summary`` 调 LLM——T-04：``Argument`` 不存原文字段，原文 / 摘要
均取自段落聚合根（避免一次性 / 逐点喂入时上下文爆炸，PRD §7 输入压缩铁律）。

控制流落代码而非 prompt 散文（``docs/langgraph-dev-guide.md`` §0 铁律）：

- 生成任何异常 → 该节点无假设（空列表，保守、不抛出、不卡死）。
- ``content`` 永不被改写（节点文本只来自只读表，``parser`` / ``verification`` 先例）。

覆盖范围（ADR-0002 成本闸）：``evidence`` + ``sub_claim``。跳过 ``main_claim``（由影响
传导 #7 处理、不直接替换）、``qualification``、影子节点（``background / evaluation``，
只读不参与校验）——被跳过节点不出现在 partial 更新中（``candidate_hypotheses`` 维持空）。

状态语义（ADR-0008 对称）：假设侧 ``supported / doubtful / refuted`` ↔ 原文侧
``credible / doubtful / error``。``confidence`` 仅用于同节点多条 ``supported`` 假设的
排序展示，**绝不**参与取证判决或合并矩阵裁决（ADR-0008 铁律）。

注：体检（#4）写回节点 ``status``、开药（#5）写回 ``candidate_hypotheses``，二者在
``merge_argument_tree`` reducer 处合流到同一节点；双轨合并算子（#6）据此读
``原文.status × 假设.status`` 矩阵裁决。
"""

from __future__ import annotations

import hashlib

from agents.hypothesis.contract import HypothesisLlmClient
from domain import (
    Argument,
    ArgumentType,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    ParagraphRecord,
)

__all__ = ["propose_hypotheses"]


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数 seam，可独立单测
# --------------------------------------------------------------------------- #


_HYPOTHESIS_TYPES: frozenset[ArgumentType] = frozenset(
    {ArgumentType.EVIDENCE, ArgumentType.SUB_CLAIM}
)


def _should_hypothesize(argument: Argument) -> bool:
    """开药覆盖 evidence + sub_claim；跳过 main_claim / qualification / 影子节点。"""

    return argument.argument_type in _HYPOTHESIS_TYPES


def _hypothesis_id(
    argument_id: str, relation: HypothesisRelation, text: str, idx: int
) -> str:
    """确定性 hypothesis_id：节点 id + 关系 + 文本 + 序号派生（非计数器，可重算）。

    供 HITL-2（#9）采纳与回写（#10）幂等链稳定引用同一假设。
    """

    digest = hashlib.blake2b(
        f"{argument_id}|{relation.value}|{text}|{idx}".encode(), digest_size=6
    ).hexdigest()
    return f"h-{digest}"


def propose_hypotheses(
    argument_tree: list[Argument],
    paragraph_list: list[ParagraphRecord],
    llm: HypothesisLlmClient,
) -> dict[str, list[Hypothesis]]:
    """对覆盖范围内的节点投机生成假设，返回 partial 更新（by ``argument_id``）。

    - 覆盖 ``evidence / sub_claim``；``main_claim / qualification / 影子`` 节点不在 dict 中
      （保持空 ``candidate_hypotheses``，下游合并据此识别未开药节点）。
    - 逐 argument 经 ``argument_tree_ids`` 从 ``paragraph_list`` 反查该段 ``original_content`` +
      ``summary``（原文 / 摘要取自段落聚合根，``Argument`` 不存原文字段），调 ``propose``；
      产出的假设一律 ``status=pending``（取证落终态属 Slice 5 的 judgment）。
    - ``status`` 不动（``status`` 由体检 #4 写回，``candidate_hypotheses``
      由本节点写回，二者在 reducer 处合流，供 #6 矩阵裁决）。
    - partial 只存候选假设列表本身，不再塞整节点——合流由 merge 按 ``argument_id`` 取本
      值写回 ``candidate_hypotheses``，不修改输入树。
    """

    paragraph_by_argument_id: dict[str, ParagraphRecord] = {
        aid: record
        for record in paragraph_list
        for aid in record.argument_tree_ids
    }
    updates: dict[str, list[Hypothesis]] = {}
    for argument in argument_tree:
        if not _should_hypothesize(argument):
            continue
        record = paragraph_by_argument_id.get(argument.argument_id)
        paragraph_summary = record.summary if record is not None else ""
        original_content = record.original_content if record is not None else ""
        try:
            proposals = llm.propose(argument, paragraph_summary, original_content)
        except Exception:
            proposals = []
        hypotheses = [
            Hypothesis(
                hypothesis_id=_hypothesis_id(
                    argument.argument_id, proposal.relation, proposal.text, idx
                ),
                text=proposal.text,
                relation=proposal.relation,
                status=HypothesisStatus.PENDING,
                confidence=proposal.confidence,
            )
            for idx, proposal in enumerate(proposals)
        ]
        updates[argument.argument_id] = hypotheses
    return updates
