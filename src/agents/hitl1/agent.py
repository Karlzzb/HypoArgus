"""HITL-1 ``confirm`` 纯函数（PRD §10 节点 1、issue #2）。

应用 HITL-1 决策，返回确认后的树。流程：

1. ``validate_tree(argument_tree)``——输入自检（不信任调用方，解析器已保证但 HITL-1 复检）。
2. ``gate.review(argument_tree)``——闸门看**原始**树，返回决策。
3. ``skip``/``accept``：返回树的深拷贝（不改原文一个字）。
4. ``edit``：在深拷贝上**逐步**应用 ops，每步 ``validate_tree``——非法步即终止、
   整个决策丢弃（调用方原树永不被改）。

``fix_boundary`` 延后——domain 无 ``text_span``（ADR-0001），待该字段落地后接入。
"""

from __future__ import annotations

from agents.hitl1.contract import (
    FixBoundaryOp,
    Hitl1Action,
    Hitl1Gate,
    Hitl1Op,
    MarkNoOpOp,
    MergeOp,
    ReparentOp,
    SetTypeOp,
    SplitOp,
)
from domain import Argument, ArgumentType
from tree_invariants import TreeInvariantError, rebuild_children, validate_tree

__all__ = ["confirm"]


def _require_argument(arguments: list[Argument], argument_id: str) -> Argument:
    """按 id 取节点；不存在则抛 :class:`TreeInvariantError`（结构非法）。"""

    for argument in arguments:
        if argument.argument_id == argument_id:
            return argument
    raise TreeInvariantError(f"HITL-1 编辑引用不存在的节点：{argument_id}")


def _apply_merge(arguments: list[Argument], op: MergeOp) -> None:
    by_id = {n.argument_id: n for n in arguments}
    merged = [by_id[i] for i in op.argument_ids if i in by_id]
    if len(merged) != len(op.argument_ids):
        raise TreeInvariantError(
            f"merge 引用不存在的节点：{set(op.argument_ids) - set(by_id)}"
        )
    if len({n.paragraph_id for n in merged}) != 1:
        raise TreeInvariantError(
            "跨段合并违反 ADR-0001（一节点一段）；跨段结构变更应走 reparent"
        )
    survivor = merged[0]
    merged_set = {n.argument_id for n in merged}
    # 被删节点的子节点（非合并集自身）改挂幸存者。
    for n in merged:
        for child_id in n.children_ids:
            if child_id not in merged_set and child_id in by_id:
                by_id[child_id].parent_id = survivor.argument_id
    # 移除非幸存者。
    arguments[:] = [n for n in arguments if n.argument_id not in merged_set or n.argument_id == survivor.argument_id]
    rebuild_children(arguments)


def _mint_split_id(existing: set[str], source_id: str) -> str:
    """为拆分产出唯一 id：``{source}-s{n}``，与既有 id 不撞。"""

    base = f"{source_id}-s"
    i = 1
    new_id = f"{base}{i}"
    while new_id in existing:
        i += 1
        new_id = f"{base}{i}"
    return new_id


def _apply_split(arguments: list[Argument], op: SplitOp) -> None:
    source = _require_argument(arguments, op.argument_id)
    new_id = _mint_split_id({n.argument_id for n in arguments}, op.argument_id)
    new_argument = source.model_copy(deep=True)
    new_argument.argument_id = new_id
    new_argument.children_ids = []  # 叶兄弟
    arguments.append(new_argument)
    rebuild_children(arguments)


def _apply_reparent(arguments: list[Argument], op: ReparentOp) -> None:
    argument = _require_argument(arguments, op.argument_id)
    argument.parent_id = op.new_parent_id
    rebuild_children(arguments)


def _apply_set_type(arguments: list[Argument], op: SetTypeOp) -> None:
    argument = _require_argument(arguments, op.argument_id)
    old_type = argument.argument_type
    argument.argument_type = op.new_type
    if op.new_type.is_shadow:
        # 影子节点不参与传导，权重恒 0。
        argument.argument_weight = 0
    elif old_type.is_shadow and not op.new_type.is_shadow:
        # 影子→核心：原 0 不适合核心，设保守默认 50。
        argument.argument_weight = 50
    # 核心→核心：保留原权重。


def _apply_mark_no_op(arguments: list[Argument], op: MarkNoOpOp) -> None:
    for argument in arguments:
        if argument.paragraph_id == op.paragraph_id:
            argument.argument_type = ArgumentType.BACKGROUND
            argument.argument_weight = 0


def _apply_op(arguments: list[Argument], op: Hitl1Op) -> None:
    if isinstance(op, MergeOp):
        _apply_merge(arguments, op)
    elif isinstance(op, SplitOp):
        _apply_split(arguments, op)
    elif isinstance(op, ReparentOp):
        _apply_reparent(arguments, op)
    elif isinstance(op, SetTypeOp):
        _apply_set_type(arguments, op)
    elif isinstance(op, MarkNoOpOp):
        _apply_mark_no_op(arguments, op)
    elif isinstance(op, FixBoundaryOp):
        raise NotImplementedError(
            "fix_boundary 延后实现：domain 无 text_span（ADR-0001）；"
            "待 text_span 字段落地后接入。"
        )
    else:  # pragma: no cover - 判别联合已穷尽
        raise AssertionError(f"未处理的 Hitl1Op：{op!r}")


def confirm(
    argument_tree: list[Argument], gate: Hitl1Gate
) -> list[Argument]:
    """应用 HITL-1 决策，返回确认后的树。

    流程：
    1. ``validate_tree(argument_tree)``——输入自检（不信任调用方，解析器已保证但 HITL-1 复检）。
    2. ``gate.review(argument_tree)``——闸门看**原始**树，返回决策。
    3. ``skip``/``accept``：返回树的深拷贝（不改原文一个字）。
    4. ``edit``：在深拷贝上**逐步**应用 ops，每步 ``validate_tree``——非法步即终止、
       整个决策丢弃（调用方原树永不被改）。
    """

    validate_tree(argument_tree)  # 1. 输入自检。
    decision = gate.review(argument_tree)  # 2. 闸门看原始树。

    # 3. skip / accept：不改原文。
    if decision.action in (Hitl1Action.SKIP, Hitl1Action.ACCEPT):
        return [n.model_copy(deep=True) for n in argument_tree]

    # 4. edit：深拷贝上逐步应用 + 每步 revalidate。
    working = [n.model_copy(deep=True) for n in argument_tree]
    for op in decision.ops:
        _apply_op(working, op)
        validate_tree(working)  # 非法步即抛 → 调用方原树不动（working 丢弃）。
    return working
