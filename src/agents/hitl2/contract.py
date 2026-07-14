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
# 闸门 seam + 桩
# --------------------------------------------------------------------------- #


class Hitl2Gate(Protocol):
    """HITL-2 闸门 seam：审阅呈现 → 返回纯数据决策。

    真实实现用 ``interrupt`` 把呈现交给用户、用 ``Command(resume)`` 收回决策（后续切片）；
    本 seam 不绑任何前端 / 中断机制。``confirm`` 保证：闸门看到的是 ``build_review``
    产出的**呈现**，决策的合法性由 ``confirm`` 校验——闸门不可越权（如对无提议重写段
    返回操作、或有提议重写时返回 PASS）。
    """

    def review(self, review: Hitl2Review) -> Hitl2Decision: ...


class FakeHitl2Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。"""

    def __init__(self, decision: Hitl2Decision) -> None:
        self._decision = decision

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        return self._decision.model_copy(deep=True)


class ConservativeHitl2Gate:
    """保守默认闸门：无待决时一键通过，否则 DECIDE 且不确认任何段。

    作为 :func:`agents.assembly.create_real_agents` 未注入闸门时的默认——守住「绝不自动
    采纳」底线：无提议重写 → ``PASS``（闸门内无待办的一键通过，ADR-0010 空过口径）；
    有提议重写 → ``DECIDE`` + 空 ops（人看过、全驳回、原文逐字节保留）。这是一次性同步桩；
    真实人判 ``interrupt`` 属后续切片。本默认使既有端到端集成测试（无人确认）仍逐字节
    等于原文。
    """

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        if not review.has_pending:
            return Hitl2Decision(action=Hitl2Action.PASS)
        return Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])
