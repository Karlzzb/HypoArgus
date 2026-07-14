"""领域模型：论证节点与状态机（术语与 CONTEXT.md、ADR-0011 逐字一致）。

本切片承载解析所需字段子集（``argument_weight`` 已补全，ADR-0013）；
``candidate_hypotheses`` 由开药 Agent（#5）补全（ADR-0007/0008）；
``adopted_hypothesis_id`` 由 HITL-2（#9）补全（ADR-0011 采纳链）。
节点形状 ``Argument``（形状为决策、非最终代码，术语见 ``CONTEXT.md``「核心实体」）。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ArgumentType(StrEnum):
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

        return self in (ArgumentType.BACKGROUND, ArgumentType.EVALUATION)


class ArgumentStatus(StrEnum):
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
    """假设状态（ADR-0008 + PRD §1 重构）。

    ``pending`` 为 propose 期状态（hypothesis_propose 节点产出、尚未取证）；judgment
    节点（Slice 5）据 ``citations`` 取证后落终态 ``supported / doubtful / refuted``，与
    原文侧 ``credible/doubtful/error`` 对称：``supported`` ↔ ``credible``、``doubtful`` ↔
    ``doubtful``、``refuted`` ↔ ``error``。``confidence`` 不参与此判决，仅用于同节点多条
    ``supported`` 假设的排序。
    """

    PENDING = "pending"
    SUPPORTED = "supported"
    DOUBTFUL = "doubtful"
    REFUTED = "refuted"


class MergeAction(StrEnum):
    """双轨合并算子对节点的裁决动作（ADR-0006 12 格矩阵）。

    ``KEEP`` 保留原文（credible 各非冲突格、doubtful/error 无有效候选格）；
    ``REPLACE`` / ``REWRITE`` / ``SUPPLEMENT`` 为「成立(supported)」列按假设与原文的
    语义关系分流（对立 / 递进 / 扩展）；``CONFLICT`` 为 credible × 对立成立 → 贴
    ``conflict`` 交人判、系统不自动裁决；``FREEZE`` 为 credible × 递进/扩展成立 →
    严格冻结、原文不动（以静制动）。
    """

    KEEP = "keep"
    REPLACE = "replace"
    REWRITE = "rewrite"
    SUPPLEMENT = "supplement"
    CONFLICT = "conflict"
    FREEZE = "freeze"


class MergeDecision(BaseModel):
    """合并算子对单节点的裁决（ADR-0006）。

    ``action`` 为节点级裁决动作。``activated_hypothesis_ids`` 为被激活推入 HITL-2 的
    supported 假设 id——``CONFLICT`` 时为对立 supported 假设、
    ``REPLACE``/``REWRITE``/``SUPPLEMENT`` 时为被激活的 supported 假设、
    ``KEEP``/``FREEZE`` 时为空。弱呈现（doubtful）的假设仍留在节点的
    ``candidate_hypotheses`` 供 HITL-2 参考，但不计入 activated（未证实 ≠ 激活）。
    """

    action: MergeAction
    activated_hypothesis_ids: list[str] = Field(default_factory=list)


HYPOTHESIS_RELATION_TO_MERGE_ACTION: dict[HypothesisRelation, MergeAction] = {
    HypothesisRelation.OPPOSE: MergeAction.REPLACE,
    HypothesisRelation.ADVANCE: MergeAction.REWRITE,
    HypothesisRelation.EXPAND: MergeAction.SUPPLEMENT,
}
"""「成立(supported)」列动作由假设与原文的语义关系决定（ADR-0006/0007）：

对立 → 替换、递进 → 改写、扩展 → 补充。双轨合并算子（#6）的分流与影响传导（#7）
复用既有成立假设去激活共用此映射——单一定义点、避免漂移。
"""


class Hypothesis(BaseModel):
    """一条可证伪的修订假设（ADR-0007/0008）。

    ``hypothesis_id`` 由开药 Agent 确定性派生（节点 id + 关系 + 文本 + 序号），
    供 HITL-2（#9）采纳与回写（#10）幂等链引用。``status`` 初值为 ``pending``
    （hypothesis_propose 产出、尚未取证），judgment（Slice 5）取证后落终态
    ``supported / doubtful / refuted``，是双轨合并（#6）矩阵
    ``原文.status × 假设.status`` 的唯一输入；``confidence`` 0-1，仅排序、不裁决。
    """

    hypothesis_id: str
    text: str
    relation: HypothesisRelation
    status: HypothesisStatus = HypothesisStatus.PENDING
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Argument(BaseModel):
    """论证树节点。

    节点只携带自身那一段原文（作为推理输入）加 ``paragraph_id`` 指针，
    绝不存整篇原文（ADR-0005）。``paragraph_id`` 为单数——一个节点不可跨段（ADR-0001）。

    ``argument_weight`` (0-100) 由解析智能体建树时按明文 rubric 赋值（带数据/引源的
    直接论据高分、泛泛断言低分），供影响传导计算剩余支撑率（ADR-0013）。影子节点
    不参与传导，权重恒 0。

    ``merge_decision`` 由双轨合并算子（#6）写回——据体检 ``status`` × 开药
    ``candidate_hypotheses`` 矩阵裁决；合并前为 ``None``。合并只标注、绝不替人拍板
    （不置 ``adopted``、不写 ``adopted_hypothesis_id``，那由 HITL-2 #9 负责）。

    ``adopted_hypothesis_id`` 由 HITL-2（#9）在用户采纳某条假设时**立即持久化**
    （ADR-0011 采纳链）：节点 ``status`` 置 ``adopted``、本字段指向被采纳假设 id。
    回写（#10）失败重试时据本字段扫「``adopted`` 且未 ``corrected``」的节点续跑、
    不重复注入，故用户决策不丢失。采纳前为 ``None``。
    """

    argument_id: str
    argument_type: ArgumentType
    parent_id: str | None = None
    children_ids: list[str] = Field(default_factory=list)
    paragraph_id: str
    content: str = ""
    argument_weight: int = Field(default=0, ge=0, le=100)
    status: ArgumentStatus = ArgumentStatus.UNVERIFIED
    issue_tags: list[str] = Field(default_factory=list)
    candidate_hypotheses: list[Hypothesis] = Field(default_factory=list)
    merge_decision: MergeDecision | None = None
    adopted_hypothesis_id: str | None = None


# --------------------------------------------------------------------------- #
# 贯穿 state 域类型（ADR-0021 / PRD §17·Slice 1）
#
# session_context 为贯穿全链的运行上下文（单写者=入口注入、全链只读，进 LLM 检索与生成
# seam 的背景）；query_time_range 为本文所需数据查询时间范围（单写者=parse+partition，
# 当前伪代码桩，真实 LLM 时间识别待后续切片）。二者以单一嵌套对象流转，不污染顶层 channel。
# --------------------------------------------------------------------------- #


class TimeRange(BaseModel):
    """数据查询时间范围（ADR-0021）。

    ``start`` / ``end`` 为日期（可空，表示无界）；``rationale`` 为时间窗的说明。
    当前由 ``parse+partition`` 以桩值注入（不真实调 LLM 识别），供 retrieval / rewrite /
    judgment 限定时间窗与提供时间上下文。
    """

    start: date | None = None
    end: date | None = None
    rationale: str = ""


class SessionContext(BaseModel):
    """贯穿全链的运行上下文（ADR-0021）。

    单写者=入口注入（``runtime/run_real.py``，与 ``original_doc`` 同入 START），全链只读。
    供 LLM 检索与生成 seam 携带一致的运行背景（同一会话多轮调用可对齐）。
    ``current_time`` 由入口注入（非节点内 ``datetime.now()``），保证可测、可复现。
    """

    session_id: str = ""
    user_id: str = ""
    current_time: datetime
    user_prompt: str = ""


DEFAULT_QUERY_TIME_RANGE: TimeRange = TimeRange(
    start=date(2025, 1, 1),
    end=date(2026, 12, 31),
    rationale="默认值·真实识别待后续",
)
"""``query_time_range`` 的伪代码桩（PRD §22 / ADR-0021）。

当前不真实调 LLM 识别时间范围——由 ``parse+partition`` 直接注入此默认值（2025–2026）。
真实 LLM 时间识别属后续切片（PRD Out of Scope），届时替换为 LLM 产出的 ``TimeRange``。
"""

DEFAULT_SESSION_CONTEXT: SessionContext = SessionContext(
    session_id="",
    user_id="",
    current_time=datetime(2025, 1, 1, 0, 0, 0),
    user_prompt="",
)
"""``session_context`` 的确定性桩（ADR-0021）。

入口未显式注入时（如测试 ``orch.run(doc)``）用此桩，保 ``current_time`` 固定、可测可复现。
真实运行时刻由 ``runtime/run_real.py`` 注入（``datetime.now()``），不在此处取实时时间。
"""
