"""领域模型：论证节点与状态机（术语与 CONTEXT.md、ADR-0011 逐字一致）。

本切片承载解析所需字段子集（``argument_weight`` 已补全，ADR-0013）；
``candidate_hypotheses`` 由开药 Agent（#5）补全（ADR-0007/0008）；
``adopted_hypothesis_id`` 由 HITL-2（#9）补全（ADR-0011 采纳链）。
节点形状沿用 prd_v2.0.md §4 决策的 ``ArgumentationNode``（形状为决策、非最终代码）。
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


class HypothesisRelation(StrEnum):
    """假设与原文的语义关系（ADR-0007）。

    一条假设只承载一种关系；混合意图必须拆成多条假设。关系在生成时钉定，
    决定回写通道：对立 → 替换、递进 → 改写、扩展 → 段尾追加。
    """

    OPPOSE = "oppose"
    ADVANCE = "advance"
    EXPAND = "expand"


class HypothesisStatus(StrEnum):
    """假设取证三态（ADR-0008），与原文侧 ``credible/doubtful/error`` 对称：

    ``supported``（成立）↔ ``credible``、``doubtful``（存疑）↔ ``doubtful``、
    ``refuted``（被推翻）↔ ``error``。``confidence`` 不参与此判决，仅用于同节点多条
    ``supported`` 假设的排序。
    """

    SUPPORTED = "supported"
    DOUBTFUL = "doubtful"
    REFUTED = "refuted"


class Hypothesis(BaseModel):
    """一条可证伪的修订假设（ADR-0007/0008）。

    ``hypothesis_id`` 由开药 Agent 确定性派生（节点 id + 关系 + 文本 + 序号），
    供 HITL-2（#9）采纳与回写（#10）幂等链引用。``status`` 为取证终判，
    是双轨合并（#6）矩阵 ``原文.status × 假设.status`` 的唯一输入；
    ``confidence`` 0-1，仅排序、不裁决。
    """

    hypothesis_id: str
    text: str
    relation: HypothesisRelation
    status: HypothesisStatus = HypothesisStatus.DOUBTFUL
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ArgumentationNode(BaseModel):
    """论证树节点。

    节点只携带自身那一段原文（作为推理输入）加 ``paragraph_id`` 指针，
    绝不存整篇原文（ADR-0005）。``paragraph_id`` 为单数——一个节点不可跨段（ADR-0001）。

    ``argument_weight`` (0-100) 由解析智能体建树时按明文 rubric 赋值（带数据/引源的
    直接论据高分、泛泛断言低分），供影响传导计算剩余支撑率（ADR-0013）。影子节点
    不参与传导，权重恒 0。
    """

    node_id: str
    node_type: NodeType
    parent_id: str | None = None
    children_ids: list[str] = Field(default_factory=list)
    paragraph_id: str
    content: str = ""
    argument_weight: int = Field(default=0, ge=0, le=100)
    status: NodeStatus = NodeStatus.UNVERIFIED
    issue_tags: list[str] = Field(default_factory=list)
    candidate_hypotheses: list[Hypothesis] = Field(default_factory=list)
