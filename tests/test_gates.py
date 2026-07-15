"""中断驱动 HITL 闸门单测（T-03·ADR-0022）。

仅验 ``formulate_question`` / ``parse_reply`` 两段纯数据 seam（不触图上下文、不调
``interrupt``）。``review()`` 的「``formulate_question → interrupt → parse_reply``」组合由
``test_orchestrator_interrupt.py`` 的图驱动集成测试覆盖（``interrupt`` 需图执行上下文）。
"""

from __future__ import annotations

from agents.hitl1 import (
    Hitl1Action,
    Hitl1Decision,
    Hitl1Question,
    Hitl1Reply,
)
from agents.hitl2 import (
    Hitl2Action,
    Hitl2Decision,
    Hitl2Question,
    Hitl2Reply,
    Hitl2Review,
    ParagraphRewriteReview,
)
from domain import Argument, ArgumentType, ParagraphRecord
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate


def _tree() -> list[Argument]:
    a1 = Argument(
        argument_id="n0001",
        argument_type=ArgumentType.MAIN_CLAIM,
    )
    object.__setattr__(a1, "_test_paragraph_id", "p0001")
    object.__setattr__(a1, "_test_content", "主论点。")
    a2 = Argument(
        argument_id="n0002",
        argument_type=ArgumentType.EVIDENCE,
        parent_id="n0001",
    )
    object.__setattr__(a2, "_test_paragraph_id", "p0002")
    object.__setattr__(a2, "_test_content", "论据。")
    return [a1, a2]


def _paragraph_list_for(tree: list[Argument]) -> list[ParagraphRecord]:
    """从树派生 paragraph_list（按 ``_test_paragraph_id`` 分组），供 gate seam 测试。"""

    by_para: dict[str, list[str]] = {}
    for a in tree:
        by_para.setdefault(getattr(a, "_test_paragraph_id", "p0001"), []).append(a.argument_id)
    return [
        ParagraphRecord(paragraph_id=pid, argument_tree_ids=ids)
        for pid, ids in by_para.items()
    ]


def _review(has_pending: bool) -> Hitl2Review:
    paragraphs = (
        [
            ParagraphRewriteReview(
                paragraph_id="p0002",
                original_text="论据。",
                proposed_text="论据[已修订]",
            )
        ]
        if has_pending
        else []
    )
    return Hitl2Review(paragraphs=paragraphs, has_pending=has_pending)


# --------------------------------------------------------------------------- #
# hitl1
# --------------------------------------------------------------------------- #


def test_hitl1_formulate_question_returns_tree_snapshot_decoupled() -> None:
    gate = InterruptHitl1Gate()
    tree = _tree()
    paragraph_list = _paragraph_list_for(tree)
    question = gate.formulate_question(tree, paragraph_list=paragraph_list)
    assert isinstance(question, Hitl1Question)
    assert [n.model_dump() for n in question.argument_tree] == [
        n.model_dump() for n in tree
    ]
    # paragraph_list 随载荷快照、与原表解耦（T-03：渲染反查所据）。
    assert [r.model_dump() for r in question.paragraph_list] == [
        r.model_dump() for r in paragraph_list
    ]
    assert question.paragraph_list is not paragraph_list
    # 快照与原树解耦（不别名）。
    assert question.argument_tree is not tree
    tree.append(
        Argument(argument_id="z", argument_type=ArgumentType.BACKGROUND)
    )
    assert "z" not in {n.argument_id for n in question.argument_tree}


def test_hitl1_parse_reply_is_action_only_empty_ops() -> None:
    """一期 parse_reply 产 action-only 决策（空 ops）；reply.text 不影响。"""

    gate = InterruptHitl1Gate()
    decision = gate.parse_reply(Hitl1Reply(action=Hitl1Action.EDIT, text="自由文本"))
    assert isinstance(decision, Hitl1Decision)
    assert decision.action is Hitl1Action.EDIT
    assert decision.ops == []  # action-only：ops 恒空（结构化 ops 推后）


def test_hitl1_parse_reply_round_trips_all_actions() -> None:
    gate = InterruptHitl1Gate()
    for action in (
        Hitl1Action.SKIP,
        Hitl1Action.ACCEPT,
        Hitl1Action.EDIT,
        Hitl1Action.REPLAY,
    ):
        assert gate.parse_reply(Hitl1Reply(action=action)).action is action


# --------------------------------------------------------------------------- #
# hitl2
# --------------------------------------------------------------------------- #


def test_hitl2_formulate_question_wraps_review() -> None:
    gate = InterruptHitl2Gate()
    review = _review(has_pending=True)
    question = gate.formulate_question(review)
    assert isinstance(question, Hitl2Question)
    assert question.review is review


def test_hitl2_parse_reply_is_action_only_empty_ops() -> None:
    gate = InterruptHitl2Gate()
    decision = gate.parse_reply(Hitl2Reply(action=Hitl2Action.DECIDE, text="逐段意见"))
    assert isinstance(decision, Hitl2Decision)
    assert decision.action is Hitl2Action.DECIDE
    assert decision.ops == []  # DECIDE 空 ops = 全驳回（一期 action-only）


def test_hitl2_parse_reply_round_trips_pass_and_decide() -> None:
    gate = InterruptHitl2Gate()
    for action in (Hitl2Action.PASS, Hitl2Action.DECIDE):
        assert gate.parse_reply(Hitl2Reply(action=action)).action is action
