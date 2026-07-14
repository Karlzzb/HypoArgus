"""HITL-2 终稿文本确认硬闸门契约（PRD §13、ADR-0010/0017、Slice 6）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 段落级操作 op 判别联合 + 呈现视图 +
闸门 Protocol + Fake/默认闸门实现 + ``Hitl2GateError``，``agent.py`` 放
``build_review`` / ``resolve_rewrites`` / ``assemble_final_document`` / ``confirm`` 纯函数。

Slice 6（ADR-0017）重定位 hitl2 为**终稿文本确认闸门**：在 rewrite_loop 逐段提议重写
（``proposed_rewrites``）之后、``final_document`` 落地之前触发。界面并列呈现被触达段的
原文 + 提议重写文本；用户逐段**确认 / 编辑 / 驳回**，系统据三态拼装 ``final_document``
（确认→提议文本、编辑→编辑文本、驳回→原文 bytes、未触达→逐字节原文）。

**此节点为不可跳过的硬闸门**，系统绝不在无人拍板时自动采纳提议重写（ADR-0010）。仅当
全篇无任何被触达段（``proposed_rewrites`` 为空）时，本节点呈现「无需修订」一键通过（属
闸门内无待办，非跳过闸门）。用户确认某段时，该段用提议文本（或编辑文本）落终稿；驳回段
回退原文 bytes。整个决策要么全部应用、要么全部丢弃——非法步（引用不在 ``proposed_rewrites``
的段）即抛 :class:`Hitl2GateError`、调用方 ``proposed_rewrites`` 不动。

与 HITL-1 的非对称：HITL-1 是可跳过的结构确认、遇非法编辑即拒（结构变更）；HITL-2 是
不可跳过的终稿文本确认、遇越权操作即拒（段级三态），绝不替人拍板。

本切片为同步注入闸门（``Hitl2Gate`` seam，``FakeHitl2Gate`` 供离线单测）；真实
``interrupt`` + ``Command(resume)`` + checkpointer 属后续切片。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

__all__ = [
    "Hitl2Action",
    "Hitl2GateError",
    "ConfirmRewriteOp",
    "EditRewriteOp",
    "RejectRewriteOp",
    "Hitl2Op",
    "Hitl2Decision",
    "ParagraphRewriteReview",
    "Hitl2Review",
    "Hitl2Question",
    "Hitl2Reply",
    "Hitl2Gate",
    "FakeHitl2Gate",
    "ConservativeHitl2Gate",
]


class Hitl2GateError(Exception):
    """HITL-2 闸门非法决策（硬闸门拦截 / 越权操作）。

    闸门越权或操作非法时抛出——绝不静默修复、绝不替人拍板，整个决策丢弃、
    调用方 ``proposed_rewrites`` 不动（与 HITL-1 对非法编辑的非对称一致）。
    """


class Hitl2Action(StrEnum):
    """HITL-2 会话级决策。

    ``PASS``：闸门内无待办的一键通过（仅当 ``proposed_rewrites`` 为空时合法，
    ADR-0010 空过口径）；``DECIDE``：逐段确认 / 编辑 / 驳回，承载有序操作序列。
    """

    PASS = "pass"
    DECIDE = "decide"


# --------------------------------------------------------------------------- #
# 决策操作（pydantic v2 判别联合，每个 op 只载自身字段；段级三态）
# --------------------------------------------------------------------------- #


class ConfirmRewriteOp(BaseModel):
    """确认某段提议重写：终稿该段用 ``proposed_rewrites[paragraph_id]`` 文本。

    ``paragraph_id`` 必须在 ``proposed_rewrites`` 内——HITL-2 不凭空造段，只能从
    rewrite_loop 已提议的段中确认。确认即「人拍板采纳该提议文本」。
    """

    action: Literal["confirm"] = "confirm"
    paragraph_id: str


class EditRewriteOp(BaseModel):
    """编辑某段提议重写：终稿该段用 ``text``（覆盖提议文本）。

    ``paragraph_id`` 必须在 ``proposed_rewrites`` 内。编辑即「人在提议基础上手改」，
    终稿该段用编辑后的文本。
    """

    action: Literal["edit"] = "edit"
    paragraph_id: str
    text: str


class RejectRewriteOp(BaseModel):
    """驳回某段提议重写：该段回退原文 bytes（不进 ``resolved_rewrites``）。

    ``paragraph_id`` 必须在 ``proposed_rewrites`` 内。驳回即「人看过、决定不修订该段」，
    终稿该段逐字节还原原文。
    """

    action: Literal["reject"] = "reject"
    paragraph_id: str


Hitl2Op = Annotated[
    ConfirmRewriteOp | EditRewriteOp | RejectRewriteOp,
    Field(discriminator="action"),
]


class Hitl2Decision(BaseModel):
    """HITL-2 决策：会话级动作 + （decide 时）有序操作序列。"""

    action: Hitl2Action
    ops: list[Hitl2Op] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 呈现视图（build_review 产出，喂给 gate.seam 与未来前端）
# --------------------------------------------------------------------------- #


class ParagraphRewriteReview(BaseModel):
    """单段提议重写的呈现视图：段落原文 + 提议重写文本。

    ``original_text`` 按 ``paragraph_id`` 从只读原文表取该段原文（ADR-0005 HITL-2
    对比左栏的数据源），**不整篇加载原文**。``proposed_text`` 为 rewrite_loop 的
    LLM 提议文本。
    """

    paragraph_id: str
    original_text: str
    proposed_text: str


class Hitl2Review(BaseModel):
    """HITL-2 闸门看到的呈现：被触达段（有提议重写）列表 + 是否有待决内容。"""

    paragraphs: list[ParagraphRewriteReview]
    has_pending: bool


# --------------------------------------------------------------------------- #
# 拆分 gate seam（T-01·ADR-0022 prefactor）：formulate_question + parse_reply
# --------------------------------------------------------------------------- #


class Hitl2Question(BaseModel):
    """``formulate_question`` 产出 = interrupt payload：终稿确认闸门交给人的问题视图。

    包裹 :class:`Hitl2Review` 呈现（被触达段原文 × 提议重写的逐段待确认表）。ADR-0022：
    interrupt payload = ``formulate_question`` 产出——服务侧经 ``interrupt`` 把此载荷交前端、
    CLI 侧经终端渲染。
    """

    review: Hitl2Review


class Hitl2Reply(BaseModel):
    """``parse_reply`` 输入 = resume value：人对终稿确认问题的回复。

    一期承载 ``action`` + 自由文本；结构化逐段 ``ops``（确认 / 编辑 / 驳回）编辑推后
    （PRD §7.2 注），故 ``parse_reply`` 产 **action-only** :class:`Hitl2Decision`（空 ``ops``，
    DECIDE 即全驳回）。ADR-0022：resume value = ``parse_reply`` 输入。
    """

    action: Hitl2Action
    text: str = ""


# --------------------------------------------------------------------------- #
# 闸门 seam + 桩
# --------------------------------------------------------------------------- #


class Hitl2Gate(Protocol):
    """HITL-2 闸门 seam：拆分为 ``formulate_question`` + ``parse_reply`` 两段（T-01·ADR-0022）。

    两段为图层级共同契约：服务侧 ``InterruptDrivenGate`` 在 ``formulate_question`` 后
    ``interrupt`` 暂停、resume 时把回复喂 ``parse_reply``；CLI 侧 ``TerminalGate`` 同步阻塞
    （``formulate_question`` 后立即 ``input()`` 再 ``parse_reply``）。一期 ``parse_reply`` 产
    **action-only** :class:`Hitl2Decision`（空 ``ops``，DECIDE 即全驳回），结构化逐段 ops 推后
    （PRD §7.2 注）。

    ``review`` 保留为**同步便捷包装**（默认实现抛 ``NotImplementedError``，仅同步 gate 覆写），
    不作为新代码依赖点——业务纯函数 ``confirm`` 现仍调它（全保真含 ops、行为等价）；异步 gate
    走 ``formulate_question`` + ``parse_reply``。``confirm`` 保证：闸门看到的是 ``build_review``
    产出的**呈现**，决策的合法性由 ``confirm`` 校验——闸门不可越权（如对无提议重写段返回操作、
    或有提议重写时返回 PASS）。
    """

    def formulate_question(self, review: Hitl2Review) -> Hitl2Question:
        """据当前呈现构造问题（interrupt payload）。"""
        ...

    def parse_reply(self, reply: Hitl2Reply) -> Hitl2Decision:
        """把人工回复解析成 action-only :class:`Hitl2Decision`（空 ops）。"""
        ...

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        """同步便捷包装（仅同步 gate 覆写；异步 gate 经 ``formulate_question`` + ``parse_reply``）。"""
        raise NotImplementedError(
            "同步 review 未实现：异步 gate 经 formulate_question + parse_reply 驱动（interrupt）"
        )


class FakeHitl2Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。

    ``review`` 返回构造时注入的**完整**决策（含 ops）——同步全保真路径，供 ``confirm`` 现有
    e2e 使用。``formulate_question`` / ``parse_reply`` 实现拆分后契约（action-only），为 T-03
    ``InterruptDrivenGate`` 预留 seam 形状。
    """

    def __init__(self, decision: Hitl2Decision) -> None:
        self._decision = decision

    def formulate_question(self, review: Hitl2Review) -> Hitl2Question:
        return Hitl2Question(review=review)

    def parse_reply(self, reply: Hitl2Reply) -> Hitl2Decision:
        # 一期 action-only：reply 的 text 不影响决策，ops 恒空（逐段 ops 推后）。
        return Hitl2Decision(action=reply.action)

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        return self._decision.model_copy(deep=True)


class ConservativeHitl2Gate:
    """保守默认闸门：无待决时一键通过，否则 DECIDE 且不确认任何段。

    作为 :func:`agents.assembly.create_real_agents` 未注入闸门时的默认——守住「绝不自动
    采纳」底线：无提议重写 → ``PASS``（闸门内无待办的一键通过，ADR-0010 空过口径）；
    有提议重写 → ``DECIDE`` + 空 ops（人看过、全驳回、原文逐字节保留）。这是一次性同步桩；
    真实人判 ``interrupt`` 属后续切片。本默认使既有端到端集成测试（无人确认）仍逐字节
    等于原文。

    ``formulate_question`` / ``parse_reply`` 实现拆分后契约（action-only）——``parse_reply``
    与 ``review`` 在保守闸门内行为一致：均据 ``action`` 落 action-only 决策（``review`` 额外
    据 ``has_pending`` 在 PASS / DECIDE 间择一，属同步便捷包装的派生逻辑）。
    """

    def formulate_question(self, review: Hitl2Review) -> Hitl2Question:
        return Hitl2Question(review=review)

    def parse_reply(self, reply: Hitl2Reply) -> Hitl2Decision:
        # 一期 action-only：与 review 的「无待决→PASS / 有待决→DECIDE+空 ops」决策面一致，
        # 但 parse_reply 只据 reply.action 落 action-only（has_pending 上下文已在 review 侧消化）。
        return Hitl2Decision(action=reply.action)

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        if not review.has_pending:
            return Hitl2Decision(action=Hitl2Action.PASS)
        return Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])
