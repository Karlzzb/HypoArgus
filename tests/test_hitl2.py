"""HITL-2 终稿文本确认硬闸门单测（PRD §13、ADR-0010/0017、Slice 6）。

Slice 6 重定位 hitl2 为**终稿文本确认闸门**：在 rewrite_loop 逐段提议重写
（``proposed_rewrites``）之后、``final_document`` 落地之前触发。界面并列呈现被触达段的
原文 + 提议重写文本；用户逐段**确认 / 编辑 / 驳回**，系统据三态拼装 ``final_document``
（确认→提议文本、编辑→编辑文本、驳回→逐字节原文、未触达→逐字节原文）。

**此节点为不可跳过的硬闸门**，系统绝不在无人拍板时自动采纳提议重写（ADR-0010）。
仅当全篇无任何被触达段（``proposed_rewrites`` 为空）时，本节点呈现「无需修订」一键通过
（属闸门内无待办，非跳过闸门）。用户确认某段时，该段用提议文本（或编辑文本）落终稿；
驳回段回退原文 bytes。整个决策要么全部应用、要么全部丢弃——非法步（引用不在
``proposed_rewrites`` 的段）即抛、调用方 ``proposed_rewrites`` 不动。

本切片 HITL-2 为同步注入闸门（``Hitl2Gate`` seam，``FakeHitl2Gate`` 供离线单测）；
真实 ``interrupt`` + ``Command(resume)`` + checkpointer 属后续切片。
"""

from __future__ import annotations

import pytest

from agents.hitl2 import (
    ConfirmRewriteOp,
    ConservativeHitl2Gate,
    EditRewriteOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Confirmation,
    Hitl2Decision,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Question,
    Hitl2Reply,
    Hitl2Review,
    ParagraphRewriteReview,
    RejectRewriteOp,
    build_review,
    confirm,
)
from original_paragraphs import OriginalParagraphs

# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #


def _store(*paragraphs: tuple[str, str]) -> OriginalParagraphs:
    """从 (paragraph_id, text) 序列构造只读原文表（文本以 utf-8 编码固化）。"""

    from partition import Paragraph

    return OriginalParagraphs(
        [Paragraph(pid, text.encode("utf-8")) for pid, text in paragraphs]
    )


def _doc_and_proposed() -> tuple[OriginalParagraphs, dict[str, str]]:
    """两段原文 + p0002 有一段提议重写（has_pending=True）。"""

    original_paragraphs = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    proposed = {"p0002": "重写后的分论点。"}
    return original_paragraphs, proposed


def _gate(decision: Hitl2Decision) -> Hitl2Gate:
    return FakeHitl2Gate(decision)


# --------------------------------------------------------------------------- #
# build_review —— 并列呈现「原文 × 提议重写」
# --------------------------------------------------------------------------- #


def test_build_review_surfaces_proposed_rewrites_with_original_text():
    """build_review 呈现每段提议重写：原文 + 提议文本；has_pending=True。"""

    original_paragraphs, proposed = _doc_and_proposed()
    review = build_review(original_paragraphs, proposed)

    assert isinstance(review, Hitl2Review)
    assert review.has_pending is True
    assert len(review.paragraphs) == 1
    para = review.paragraphs[0]
    assert isinstance(para, ParagraphRewriteReview)
    assert para.paragraph_id == "p0002"
    assert para.original_text == "分论点。"
    assert para.proposed_text == "重写后的分论点。"


def test_build_review_no_proposed_yields_empty_review():
    """无提议重写 → 空呈现 + has_pending=False（一键通过口径）。"""

    original_paragraphs = _store(("p0001", "主论点。"))
    review = build_review(original_paragraphs, {})
    assert review.paragraphs == []
    assert review.has_pending is False


# --------------------------------------------------------------------------- #
# confirm · PASS（一键通过）+ 硬闸门禁自动采纳
# --------------------------------------------------------------------------- #


def test_confirm_pass_no_pending_returns_original_bytes():
    """无提议重写 → PASS 一键通过：终稿逐字节等于原文。"""

    original_paragraphs = _store(("p0001", "主论点。"))
    confirmation = confirm(
        original_paragraphs,
        {},
        _gate(Hitl2Decision(action=Hitl2Action.PASS)),
    )
    assert isinstance(confirmation, Hitl2Confirmation)
    assert confirmation.final_document == "主论点。".encode()
    assert confirmation.resolved_rewrites == {}


def test_confirm_pass_with_pending_raises_hard_gate():
    """有提议重写时 gate 返回 PASS → 硬闸门拦截（绝不无人拍板自动采纳）。"""

    original_paragraphs, proposed = _doc_and_proposed()
    snapshot = dict(proposed)
    with pytest.raises(Hitl2GateError, match="硬闸门|PASS|待决|自动采纳"):
        confirm(
            original_paragraphs,
            proposed,
            _gate(Hitl2Decision(action=Hitl2Action.PASS)),
        )
    # 调用方 proposed_rewrites 不动。
    assert proposed == snapshot


# --------------------------------------------------------------------------- #
# confirm · DECIDE 确认 / 编辑 / 驳回
# --------------------------------------------------------------------------- #


def test_confirm_decide_confirm_uses_proposed_text():
    """确认段 → 终稿用提议文本、未触达段逐字节原文。"""

    original_paragraphs, proposed = _doc_and_proposed()
    confirmation = confirm(
        original_paragraphs,
        proposed,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[ConfirmRewriteOp(paragraph_id="p0002")],
            )
        ),
    )
    assert "重写后的分论点。".encode() in confirmation.final_document
    assert b"\xe5\x88\x86\xe8\xae\xba\xe7\x82\xb9\xe3\x80\x82" not in confirmation.final_document.replace(
        "重写后的分论点。".encode(), b""
    )  # 原句（分论点。）在确认段被替换后消失
    # 未触达段逐字节还原。
    assert original_paragraphs.get("p0001") in confirmation.final_document
    assert confirmation.resolved_rewrites == {"p0002": "重写后的分论点。"}


def test_confirm_decide_edit_uses_edited_text():
    """编辑段 → 终稿用编辑文本（覆盖提议文本）。"""

    original_paragraphs, proposed = _doc_and_proposed()
    confirmation = confirm(
        original_paragraphs,
        proposed,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[EditRewriteOp(paragraph_id="p0002", text="手改后的文本。")],
            )
        ),
    )
    assert "手改后的文本。".encode() in confirmation.final_document
    assert "重写后的分论点。".encode() not in confirmation.final_document
    assert confirmation.resolved_rewrites == {"p0002": "手改后的文本。"}


def test_confirm_decide_reject_falls_back_to_original_bytes():
    """驳回段 → 回退原文 bytes、终稿逐字节等于原文。"""

    original_paragraphs, proposed = _doc_and_proposed()
    confirmation = confirm(
        original_paragraphs,
        proposed,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[RejectRewriteOp(paragraph_id="p0002")],
            )
        ),
    )
    assert confirmation.final_document == original_paragraphs.get("p0001") + original_paragraphs.get("p0002")
    assert confirmation.resolved_rewrites == {}


def test_confirm_decide_empty_ops_rejects_all_and_keeps_original():
    """DECIDE + 空 ops：人看过、全驳回 → 终稿逐字节等于原文（非自动跳过）。"""

    original_paragraphs, proposed = _doc_and_proposed()
    confirmation = confirm(
        original_paragraphs,
        proposed,
        _gate(Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])),
    )
    assert confirmation.final_document == original_paragraphs.get("p0001") + original_paragraphs.get("p0002")
    assert confirmation.resolved_rewrites == {}


# --------------------------------------------------------------------------- #
# confirm · 越权 / 原子性（非法步丢弃整个决策）
# --------------------------------------------------------------------------- #


def test_confirm_op_referencing_unknown_paragraph_raises():
    """op 引用不在 proposed_rewrites 的段 → 越权 → 抛错，调用方 proposed 不动。"""

    original_paragraphs, proposed = _doc_and_proposed()
    snapshot = dict(proposed)
    with pytest.raises(Hitl2GateError, match="越权|不在|proposed"):
        confirm(
            original_paragraphs,
            proposed,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[ConfirmRewriteOp(paragraph_id="p9999")],
                )
            ),
        )
    assert proposed == snapshot


def test_confirm_multi_op_invalid_step_rejects_wholesale():
    """序列中某步非法（引用未知段）→ 整个决策丢弃，调用方 proposed 不变。"""

    original_paragraphs, proposed = _doc_and_proposed()
    snapshot = dict(proposed)
    with pytest.raises(Hitl2GateError):
        confirm(
            original_paragraphs,
            proposed,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[
                        ConfirmRewriteOp(paragraph_id="p0002"),
                        ConfirmRewriteOp(paragraph_id="p9999"),
                    ],
                )
            ),
        )
    assert proposed == snapshot


def test_confirm_does_not_mutate_caller_proposed_rewrites():
    """DECIDE 在新 dict 上工作，调用方 proposed_rewrites 对象永不被改。"""

    original_paragraphs, proposed = _doc_and_proposed()
    snapshot = dict(proposed)
    confirm(
        original_paragraphs,
        proposed,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[ConfirmRewriteOp(paragraph_id="p0002")],
            )
        ),
    )
    assert proposed == snapshot


# --------------------------------------------------------------------------- #
# ConservativeHitl2Gate —— 默认闸门（绝不自动采纳）
# --------------------------------------------------------------------------- #


def test_conservative_gate_no_pending_passes():
    """无提议重写 → 保守闸门 PASS → 终稿逐字节等于原文。"""

    original_paragraphs = _store(("p0001", "主论点。"))
    confirmation = confirm(original_paragraphs, {}, ConservativeHitl2Gate())
    assert confirmation.final_document == "主论点。".encode()
    assert confirmation.resolved_rewrites == {}


def test_conservative_gate_pending_decides_empty_ops_rejects_all():
    """有提议重写 → 保守闸门 DECIDE + 空 ops（全驳回）→ 终稿逐字节等于原文。"""

    original_paragraphs, proposed = _doc_and_proposed()
    confirmation = confirm(original_paragraphs, proposed, ConservativeHitl2Gate())
    assert confirmation.final_document == original_paragraphs.get("p0001") + original_paragraphs.get("p0002")
    assert confirmation.resolved_rewrites == {}


# --------------------------------------------------------------------------- #
# 拆分 gate seam（T-01·ADR-0022 prefactor）— formulate_question + parse_reply
# --------------------------------------------------------------------------- #


def test_formulate_question_wraps_review_as_interrupt_payload():
    """formulate_question 产 Hitl2Question：包裹 build_review 呈现（interrupt payload）。

    ADR-0022：interrupt payload = formulate_question 产出。hitl2 的「问题」即被触达段
    原文 × 提议重写的逐段待确认表（Hitl2Review）。
    """

    original_paragraphs, proposed = _doc_and_proposed()
    review = build_review(original_paragraphs, proposed)
    gate = FakeHitl2Gate(Hitl2Decision(action=Hitl2Action.PASS))
    question = gate.formulate_question(review)
    assert isinstance(question, Hitl2Question)
    assert question.review is review  # 包裹同一呈现视图


def test_parse_reply_produces_action_only_decision_with_empty_ops():
    """一期 parse_reply 产 action-only Hitl2Decision（空 ops）；结构化逐段 ops 推后（PRD §7.2）。

    DECIDE + 空 ops = 全驳回（保守口径）；PASS 直通。reply 的 text 不影响决策。
    """

    gate = FakeHitl2Gate(Hitl2Decision(action=Hitl2Action.PASS))
    decided = gate.parse_reply(Hitl2Reply(action=Hitl2Action.DECIDE, text="自由文本"))
    assert isinstance(decided, Hitl2Decision)
    assert decided.action is Hitl2Action.DECIDE
    assert decided.ops == []  # action-only：逐段 ops 推后


@pytest.mark.parametrize("action", [Hitl2Action.PASS, Hitl2Action.DECIDE])
def test_parse_reply_action_round_trips(action: Hitl2Action) -> None:
    """parse_reply 对 PASS / DECIDE 均原样落到 Hitl2Decision.action（action-only）。"""

    gate = FakeHitl2Gate(Hitl2Decision(action=Hitl2Action.PASS))
    assert gate.parse_reply(Hitl2Reply(action=action)).action is action


def test_conservative_gate_implements_split_seam_action_only():
    """ConservativeHitl2Gate 实现拆分后 Protocol：formulate_question 包裹 review、parse_reply action-only。"""

    original_paragraphs, proposed = _doc_and_proposed()
    review = build_review(original_paragraphs, proposed)
    gate = ConservativeHitl2Gate()
    question = gate.formulate_question(review)
    assert isinstance(question, Hitl2Question)
    assert question.review.has_pending is True
    decided = gate.parse_reply(Hitl2Reply(action=Hitl2Action.DECIDE))
    assert decided.action is Hitl2Action.DECIDE
    assert decided.ops == []


def test_seam_does_not_alter_conservative_sync_review_full_fidelity():
    """拆分后 ConservativeHitl2Gate.review 仍据 has_pending 全保真决策（同步便捷包装）。"""

    gate = ConservativeHitl2Gate()
    # 有待决 → DECIDE + 空 ops（全驳回）；无待决 → PASS。
    pending_review = Hitl2Review(
        paragraphs=[ParagraphRewriteReview(paragraph_id="p0002", original_text="x", proposed_text="y")],
        has_pending=True,
    )
    empty_review = Hitl2Review(paragraphs=[], has_pending=False)
    assert gate.review(pending_review) == Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])
    assert gate.review(empty_review) == Hitl2Decision(action=Hitl2Action.PASS)
