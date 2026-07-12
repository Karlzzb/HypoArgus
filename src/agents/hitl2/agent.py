"""HITL-2 ``build_review`` 与 ``confirm`` 纯函数（PRD §10 节点 2、issue #9、ADR-0010/0011）。

``build_review`` 构建修订确认呈现（待决节点的段落原文 + 批注 + 候选 + 激活集），
``confirm`` 应用决策（硬闸门校验 + 逐步应用 ops + 状态机拦截）。纯函数子缝
（PRD «Testing Decisions» 呈现缝 / 决策缝），可独立单测。

``build_review`` 只呈现待决节点（:func:`_is_pending`）；``original_text`` 按
``paragraph_id`` 从只读表取该段原文（不整篇加载）。``has_pending`` = 是否存在任何
待决节点，驱动硬闸门的「一键通过」与「禁自动采纳」分支。

``confirm`` 流程：
1. ``build_review(argument_tree, original_paragraphs)``——构建呈现。
2. ``gate.review(review)``——闸门返回决策。
3. 硬闸门校验（ADR-0010）：``PASS`` 仅当无待决内容；有待决时绝不可 ``PASS``
   （绝不无人拍板自动采纳）。
4. ``DECIDE``：在深拷贝上**逐步**应用 ops，每步校验合法性（采纳必须命中已激活
   候选、手改与驳回只作用于待决节点、状态机非法变更一律拦截）；非法步即抛 →
   调用方原树不动。
"""

from __future__ import annotations

from agents.hitl2.contract import (
    AdoptOp,
    ArgumentReview,
    CandidateView,
    EditContentOp,
    Hitl2Action,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Op,
    Hitl2Review,
    RejectOp,
)
from domain import Argument, ArgumentStatus
from original_paragraphs import OriginalParagraphs
from status_machine import (
    IllegalStatusTransitionError,
    validate_transition,
)

__all__ = ["build_review", "confirm"]


_PENDING_STATUSES: frozenset[ArgumentStatus] = frozenset(
    {ArgumentStatus.DOUBTFUL, ArgumentStatus.ERROR, ArgumentStatus.INVALID}
)
"""体检 / 影响传导已下待决终判的状态（矩阵原文侧 + 影响传导上层判决）。"""

_CONFLICT_TAG = "conflict"


def _activated_ids(argument: Argument) -> list[str]:
    if argument.merge_decision is None:
        return []
    return list(argument.merge_decision.activated_hypothesis_ids)


def _is_pending(argument: Argument) -> bool:
    """节点是否需 HITL-2 人判：状态待决、或贴 conflict、或有被激活的候选。

    合并矩阵保证：非 conflict 的 ``credible`` 节点激活集为空、状态非待决 → 不呈现
    （以静制动，原文不动）。``doubtful``/``error``/``invalid`` 节点一律呈现；
    conflict 节点虽 ``credible`` 但需人判对立假设 → 呈现。
    """

    if argument.status in _PENDING_STATUSES:
        return True
    if _CONFLICT_TAG in argument.issue_tags:
        return True
    return bool(_activated_ids(argument))


def build_review(
    argument_tree: list[Argument], original_paragraphs: OriginalParagraphs
) -> Hitl2Review:
    """构建修订确认呈现：待决节点的段落原文 + 批注 + 候选 + 激活集。

    纯函数子缝（PRD «Testing Decisions» 呈现缝）：``标注后的树 + 只读原文表 → 呈现``。
    只呈现待决节点（:func:`_is_pending`）；``original_text`` 按 ``paragraph_id`` 从
    只读表取该段原文（不整篇加载）。``has_pending`` = 是否存在任何待决节点，驱动
    硬闸门的「一键通过」与「禁自动采纳」分支。
    """

    arguments: list[ArgumentReview] = []
    for argument in argument_tree:
        if not _is_pending(argument):
            continue
        original = original_paragraphs.get(argument.paragraph_id).decode("utf-8", errors="surrogateescape")
        arguments.append(
            ArgumentReview(
                argument_id=argument.argument_id,
                paragraph_id=argument.paragraph_id,
                original_text=original,
                argument_type=argument.argument_type,
                status=argument.status,
                issue_tags=list(argument.issue_tags),
                activated_hypothesis_ids=_activated_ids(argument),
                candidates=[
                    CandidateView(
                        hypothesis_id=h.hypothesis_id,
                        text=h.text,
                        relation=h.relation,
                        status=h.status,
                        confidence=h.confidence,
                    )
                    for h in argument.candidate_hypotheses
                ],
            )
        )
    return Hitl2Review(arguments=arguments, has_pending=bool(arguments))


def confirm(
    argument_tree: list[Argument],
    original_paragraphs: OriginalParagraphs,
    gate: Hitl2Gate,
) -> list[Argument]:
    """应用 HITL-2 决策，返回确认后的树。

    流程：
    1. ``build_review(argument_tree, original_paragraphs)``——构建呈现。
    2. ``gate.review(review)``——闸门返回决策。
    3. 硬闸门校验（ADR-0010）：``PASS`` 仅当无待决内容；有待决时绝不可 ``PASS``
       （绝不无人拍板自动采纳）。
    4. ``DECIDE``：在深拷贝上**逐步**应用 ops，每步校验合法性（采纳必须命中已激活
       候选、手改与驳回只作用于待决节点、状态机非法变更一律拦截）；非法步即抛 →
       调用方原树不动。
    """

    review = build_review(argument_tree, original_paragraphs)
    decision = gate.review(review)

    # 硬闸门（ADR-0010）：PASS 仅当闸门内无待办；有待决内容时绝不可一键通过。
    if decision.action is Hitl2Action.PASS:
        if review.has_pending:
            raise Hitl2GateError(
                "硬闸门拦截：有待决内容时不可 PASS（绝不在无人拍板时自动采纳）"
            )
        return [n.model_copy(deep=True) for n in argument_tree]

    # DECIDE：深拷贝上逐步应用 + 每步校验。
    working = [n.model_copy(deep=True) for n in argument_tree]
    for op in decision.ops:
        _apply_op(working, op)
    return working


def _apply_op(arguments: list[Argument], op: Hitl2Op) -> None:
    """应用单个 HITL-2 操作；非法即抛 :class:`Hitl2GateError`。"""

    if isinstance(op, AdoptOp):
        _apply_adopt(arguments, op)
    elif isinstance(op, RejectOp):
        _apply_reject(arguments, op)
    elif isinstance(op, EditContentOp):
        _apply_edit_content(arguments, op)
    else:  # pragma: no cover - 判别联合已穷尽
        raise AssertionError(f"未处理的 Hitl2Op：{op!r}")


def _require_argument(arguments: list[Argument], argument_id: str) -> Argument:
    """按 id 取节点；不存在则抛 :class:`Hitl2GateError`。"""

    for argument in arguments:
        if argument.argument_id == argument_id:
            return argument
    raise Hitl2GateError(f"HITL-2 操作引用不存在的节点：{argument_id}")


def _apply_adopt(arguments: list[Argument], op: AdoptOp) -> None:
    """采纳假设：校验激活集 + 状态机，置 ``adopted`` + 持久化 ``adopted_hypothesis_id``。

    校验（任一失败即抛 :class:`Hitl2GateError`、整个决策丢弃）：
    1. 节点存在；
    2. 节点为待决态（``doubtful``/``error``/``invalid`` 或贴 ``conflict``）——可信非冲突
       节点不呈现、不可采纳（保护原文底线）；
    3. ``hypothesis_id`` 在节点 ``merge_decision.activated_hypothesis_ids`` 内——HITL-2
       不凭空造药，只从系统已激活的候选中勾选；
    4. 状态机迁移合法（``adopted``/``corrected`` 不可重复采纳，ADR-0011）——由集中状态机
       子缝 :func:`validate_transition` 统一拦截，杜绝规则漂移。

    成功则置 ``status = adopted``、``adopted_hypothesis_id = op.hypothesis_id``；
    ``edited_text`` 非空时覆写该假设文本（落回 ``candidate_hypotheses``，供回写 #10 幂等重取）。
    """

    argument = _require_argument(arguments, op.argument_id)
    if not _is_pending(argument):
        raise Hitl2GateError(
            f"采纳非法：节点 {op.argument_id} 非待决态（{argument.status.value}），不可采纳"
        )
    try:
        validate_transition(argument.status, ArgumentStatus.ADOPTED)
    except IllegalStatusTransitionError as exc:
        raise Hitl2GateError(
            f"状态机非法变更：节点 {op.argument_id} 已为 {argument.status.value}，不可重复采纳"
        ) from exc
    activated = _activated_ids(argument)
    if op.hypothesis_id not in activated:
        raise Hitl2GateError(
            f"越权采纳：假设 {op.hypothesis_id} 不在节点 {op.argument_id} 的激活候选集 "
            f"{activated} 内"
        )
    if op.edited_text is not None:
        for h in argument.candidate_hypotheses:
            if h.hypothesis_id == op.hypothesis_id:
                h.text = op.edited_text
                break
    argument.status = ArgumentStatus.ADOPTED
    argument.adopted_hypothesis_id = op.hypothesis_id


def _apply_reject(arguments: list[Argument], op: RejectOp) -> None:
    """驳回假设：从 ``candidate_hypotheses`` 移除该假设（持久化驳回决策）。

    节点 ``status`` 不变（仍待决、原文逐字节保留）；被驳回假设不再参与回写。
    ``hypothesis_id`` 必须在节点当前 ``candidate_hypotheses`` 内，否则抛
    :class:`Hitl2GateError`（越权 / 不存在）。
    """

    argument = _require_argument(arguments, op.argument_id)
    idx = next(
        (i for i, h in enumerate(argument.candidate_hypotheses) if h.hypothesis_id == op.hypothesis_id),
        None,
    )
    if idx is None:
        raise Hitl2GateError(
            f"驳回越权：假设 {op.hypothesis_id} 不在节点 {op.argument_id} 的候选集内"
        )
    argument.candidate_hypotheses.pop(idx)


def _apply_edit_content(arguments: list[Argument], op: EditContentOp) -> None:
    """手动修改节点内容：覆写 ``content``（仅待决节点）。

    仅作用于待决节点（``doubtful``/``error``/``invalid`` 或贴 ``conflict``）；可信非冲突
    节点不呈现、不可手改（守住「保护原文」底线）。不置 ``adopted``——是否进入回写重写
    通道由 #10 据 ``adopted_hypothesis_id`` 与 ``content`` 共同决定。
    """

    argument = _require_argument(arguments, op.argument_id)
    if not _is_pending(argument):
        raise Hitl2GateError(
            f"手改非法：节点 {op.argument_id} 非待决态（{argument.status.value}），不可手改内容"
        )
    argument.content = op.content
