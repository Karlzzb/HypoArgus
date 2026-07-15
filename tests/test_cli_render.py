"""T-03：HITL-1 终端渲染经 ``paragraph_list`` 反查、不读 ``Argument.paragraph_id``。

覆盖两处渲染入口：

- :func:`runtime.run_real._render_hitl1_question`：异步 resume 循环渲染 interrupt 载荷。
- :class:`runtime.cli_gates.CliHitl1Gate._print_tree`：同步 CLI 闸门渲染解析树。

两处均改经 ``paragraph_list.argument_tree_ids`` 反查节点所属段（取代读
``Argument.paragraph_id``），为 T-04 移除该字段扫清渲染侧依赖。回归锁：构造
``Argument.paragraph_id`` 与 ``paragraph_list`` 归属**矛盾**的样本，断言渲染取
``paragraph_list`` 的归属。
"""

from __future__ import annotations

from agents.hitl1 import Hitl1Question
from agents.hitl2 import Hitl2Question, Hitl2Review, ParagraphRewriteReview
from domain import Argument, ArgumentType, ParagraphRecord
from runtime.cli_gates import CliHitl1Gate
from runtime.run_real import _render_hitl1_question, _render_hitl2_question


def _contradictory_tree() -> list[Argument]:
    """节点 ``n0`` 的 ``Argument.paragraph_id=p0001``，但段落聚合根声称它属 ``p0099``。

    用来证伪「渲染读 ``Argument.paragraph_id``」：若读字段则渲染 ``p0001``，
    若经 ``paragraph_list`` 反查则渲染 ``p0099``。
    """

    return [
        Argument(
            argument_id="n0",
            argument_type=ArgumentType.MAIN_CLAIM,
            paragraph_id="p0001",
            content="主论点",
        ),
    ]


def _contradictory_paragraph_list() -> list[ParagraphRecord]:
    return [
        ParagraphRecord(
            paragraph_id="p0099",
            summary="主论点段",
            original_content="主论点",
            argument_tree_ids=["n0"],
        ),
    ]


def test_render_hitl1_question_shows_para_from_paragraph_list_not_argument_field() -> None:
    """``_render_hitl1_question`` 经 ``paragraph_list`` 反查所属段，不读 ``Argument.paragraph_id``。"""

    question = Hitl1Question(
        argument_tree=_contradictory_tree(),
        paragraph_list=_contradictory_paragraph_list(),
    )
    lines: list[str] = []
    _render_hitl1_question(question, lines.append)
    rendered = "\n".join(lines)
    assert "para=p0099" in rendered  # 取 paragraph_list 归属
    assert "p0001" not in rendered  # 不读 Argument.paragraph_id


def test_print_tree_shows_para_from_paragraph_list_not_argument_field() -> None:
    """``CliHitl1Gate`` 同步渲染经 ``paragraph_list`` 反查所属段，不读 ``Argument.paragraph_id``。"""

    lines: list[str] = []
    gate = CliHitl1Gate(interactive=False, out_fn=lambda *a, **k: lines.append(a[0]))
    gate.review(
        _contradictory_tree(),
        paragraph_list=_contradictory_paragraph_list(),
    )
    rendered = "\n".join(lines)
    assert "para=p0099" in rendered
    assert "p0001" not in rendered


def test_render_hitl2_question_is_paragraph_level_unchanged() -> None:
    """T-03：HITL-2 终端渲染已段落级（读 ``ParagraphRewriteReview``、不读 ``Argument``）。

    核实 ``_render_hitl2_question`` 不动、仍正确：逐被触达段呈现原文 × 提议重写，
    无 ``Argument.paragraph_id`` / ``.content`` 依赖。
    """

    review = Hitl2Review(
        paragraphs=[
            ParagraphRewriteReview(
                paragraph_id="p0002",
                original_text="论据。",
                proposed_text="论据[已修订]",
            )
        ],
        has_pending=True,
    )
    lines: list[str] = []
    _render_hitl2_question(Hitl2Question(review=review), lines.append)
    rendered = "\n".join(lines)
    assert "--- p0002 ---" in rendered
    assert "原文：论据。" in rendered
    assert "提议：论据[已修订]" in rendered
