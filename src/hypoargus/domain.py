"""领域模型：论证节点与状态机（术语与 CONTEXT.md、ADR-0011 逐字一致）。

本切片只承载 tracer bullet 流转所需的最小字段子集；后续切片逐步补全
`argument_weight`、`candidate_hypotheses`、`adopted_hypothesis_id` 等字段。
节点形状沿用 prd_v2.0.md §4 决策的 `ArgumentationNode`（形状为决策、非最终代码）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """论证节点类型。

    核心逻辑节点参与校验与逻辑传导；影子节点只读、不参与校验与传导，但提供上下文
    并参与最终文本拼接。见 CONTEXT.md「核心实体」。
    """

    MAIN_CLAIM = "main_claim"
    SUB_CLAIM = "sub_claim"
    EVIDENCE = "evidence"
    QUALIFICATION = "qualification"
    BACKGROUND = "background"
    EVALUATION = "evaluation"

    @property
    def is_shadow(self) -> bool:
        """影子节点（只读、不参与校验与传导）。"""

        return self in (NodeType.BACKGROUND, NodeType.EVALUATION)


class NodeStatus(StrEnum):
    """节点状态机。

    ``unverified → pending_verification → (credible | doubtful | error)
    → adopted → corrected``；回写失败停留 ``adopted`` 可重试；
    ``invalid`` 由影响传导对上层论点单独判定。见 ADR-0011。
    """

    UNVERIFIED = "unverified"
    PENDING_VERIFICATION = "pending_verification"
    CREDIBLE = "credible"
    DOUBTFUL = "doubtful"
    ERROR = "error"
    ADOPTED = "adopted"
    CORRECTED = "corrected"
    INVALID = "invalid"


class ArgumentationNode(BaseModel):
    """论证树节点。

    节点只携带自身那一段原文（作为推理输入）加 ``paragraph_id`` 指针，
    绝不存整篇原文（ADR-0005）。``paragraph_id`` 为单数——一个节点不可跨段（ADR-0001）。
    """

    node_id: str
    node_type: NodeType
    parent_id: str | None = None
    children_ids: list[str] = Field(default_factory=list)
    paragraph_id: str
    content: str = ""
    status: NodeStatus = NodeStatus.UNVERIFIED
    issue_tags: list[str] = Field(default_factory=list)
