"""HITL-1 ``confirm`` / ``confirm_partition`` 纯函数（PRD §10 节点 1、ADR-0018）。

- ``confirm``：应用 SKIP/ACCEPT/EDIT 决策，返回确认后的树（既有结构确认语义）。
- ``confirm_partition``：partition 确认闸门 + 有界打回。读 ``retry_count`` + 闸门决策,
  产 :class:`Hitl1Outcome`（路由 + 计数 + 耗尽标签 + 同步后的 paragraph_list）。REPLAY
  在预算内 → route=REPLAY、计数 +1；超限 → route=CONTINUE + exhausted=True（受控分支、
  非异常降级）；SKIP/ACCEPT/EDIT → 复用 ``_apply_decision`` 应用结构编辑、route=CONTINUE。

流程（``confirm`` / ``confirm_partition`` 共用）：

1. ``validate_tree(argument_tree)`` + ``validate_paragraph_consistency(argument_tree,
   paragraph_list)``——输入自检（不信任调用方，解析器已保证但 HITL-1 复检）。
2. ``gate.review(argument_tree)``——闸门看**原始**树，返回决策。
3. ``skip``/``accept``：返回树 + paragraph_list 的深拷贝（不改原文一个字）。
4. ``edit``：在深拷贝上**逐步**应用 ops（每步 ``validate_tree`` +
   ``validate_paragraph_consistency``）——非法步即终止、整个决策丢弃（调用方原树不动）。

T-02/T-04：HITL-1 三 op（merge / split / mark_no_op）在改节点集合时**同步维护**
``paragraph_list.argument_tree_ids``——merge 从所属段 ids 移除被合并掉的非幸存者、split 把
新 id（``{source}-s{n}``）加进源段 ids、mark_no_op 经 ``argument_tree_ids`` 定位该段节点；
「同段才能合并」断言由 ``argument_tree_ids`` 归属判定（``Argument`` 不存 ``paragraph_id``，
T-04 已移除该字段）。
``reparent`` / ``set_type`` 不动段落成员关系（节点段落归属恒定，ADR-0001）。

``fix_boundary`` 延后——domain 无 ``text_span``（ADR-0001），待该字段落地后接入。
"""

from __future__ import annotations

from agents.hitl1.contract import (
    DEFAULT_MAX_PARTITION_RETRIES,
    FixBoundaryOp,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Gate,
    Hitl1Op,
    Hitl1Outcome,
    Hitl1Route,
    MarkNoOpOp,
    MergeOp,
    ReparentOp,
    SetTypeOp,
    SplitOp,
)
from domain import Argument, ArgumentType, ParagraphRecord
from tree_invariants import (
    TreeInvariantError,
    rebuild_children,
    validate_paragraph_consistency,
    validate_tree,
)

__all__ = ["confirm", "confirm_partition"]


def _require_argument(arguments: list[Argument], argument_id: str) -> Argument:
    """按 id 取节点；不存在则抛 :class:`TreeInvariantError`（结构非法）。"""

    for argument in arguments:
        if argument.argument_id == argument_id:
            return argument
    raise TreeInvariantError(f"HITL-1 编辑引用不存在的节点：{argument_id}")


def _owner_paragraph_id(
    paragraph_list: list[ParagraphRecord], argument_id: str
) -> str | None:
    """经 ``argument_tree_ids`` 反查节点所属段（T-02：取代 ``Argument.paragraph_id``）。"""

    for record in paragraph_list:
        if argument_id in record.argument_tree_ids:
            return record.paragraph_id
    return None


def _apply_merge(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: MergeOp,
) -> None:
    by_id = {n.argument_id: n for n in arguments}
    merged = [by_id[i] for i in op.argument_ids if i in by_id]
    if len(merged) != len(op.argument_ids):
        raise TreeInvariantError(
            f"merge 引用不存在的节点：{set(op.argument_ids) - set(by_id)}"
        )
    # 同段才能合并：经 argument_tree_ids 归属判定（T-02：取代 Argument.paragraph_id 比较）。
    owners = [_owner_paragraph_id(paragraph_list, aid) for aid in op.argument_ids]
    if any(o is None for o in owners) or len(set(owners)) != 1:
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
    arguments[:] = [
        n
        for n in arguments
        if n.argument_id not in merged_set or n.argument_id == survivor.argument_id
    ]
    # 同步 argument_tree_ids：从所属段 ids 移除被合并掉的非幸存者（T-02）。
    owning_pid = owners[0]
    removed = merged_set - {survivor.argument_id}
    for record in paragraph_list:
        if record.paragraph_id == owning_pid:
            record.argument_tree_ids[:] = [
                aid for aid in record.argument_tree_ids if aid not in removed
            ]
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


def _apply_split(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: SplitOp,
) -> None:
    source = _require_argument(arguments, op.argument_id)
    new_id = _mint_split_id({n.argument_id for n in arguments}, op.argument_id)
    new_argument = source.model_copy(deep=True)
    new_argument.argument_id = new_id
    new_argument.children_ids = []  # 叶兄弟
    arguments.append(new_argument)
    # 同步 argument_tree_ids：新拆分节点归属源段，加入该段 ids（T-02）。
    owning_pid = _owner_paragraph_id(paragraph_list, op.argument_id)
    if owning_pid is not None:
        for record in paragraph_list:
            if record.paragraph_id == owning_pid:
                record.argument_tree_ids.append(new_id)
    rebuild_children(arguments)


def _apply_reparent(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: ReparentOp,
) -> None:
    # reparent 不动段落成员关系（节点段落归属恒定，ADR-0001）；paragraph_list 不变。
    argument = _require_argument(arguments, op.argument_id)
    argument.parent_id = op.new_parent_id
    rebuild_children(arguments)


def _apply_set_type(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: SetTypeOp,
) -> None:
    # set_type 不动段落成员关系；paragraph_list 不变。
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


def _apply_mark_no_op(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: MarkNoOpOp,
) -> None:
    # 经 argument_tree_ids[op.paragraph_id] 定位该段节点（T-02：取代扫描 Argument.paragraph_id）。
    target_ids: list[str] = []
    for record in paragraph_list:
        if record.paragraph_id == op.paragraph_id:
            target_ids = list(record.argument_tree_ids)
            break
    by_id = {n.argument_id: n for n in arguments}
    for aid in target_ids:
        node = by_id.get(aid)
        if node is not None:
            node.argument_type = ArgumentType.BACKGROUND
            node.argument_weight = 0


def _apply_op(
    arguments: list[Argument],
    paragraph_list: list[ParagraphRecord],
    op: Hitl1Op,
) -> None:
    if isinstance(op, MergeOp):
        _apply_merge(arguments, paragraph_list, op)
    elif isinstance(op, SplitOp):
        _apply_split(arguments, paragraph_list, op)
    elif isinstance(op, ReparentOp):
        _apply_reparent(arguments, paragraph_list, op)
    elif isinstance(op, SetTypeOp):
        _apply_set_type(arguments, paragraph_list, op)
    elif isinstance(op, MarkNoOpOp):
        _apply_mark_no_op(arguments, paragraph_list, op)
    elif isinstance(op, FixBoundaryOp):
        raise NotImplementedError(
            "fix_boundary 延后实现：domain 无 text_span（ADR-0001）；"
            "待 text_span 字段落地后接入。"
        )
    else:  # pragma: no cover - 判别联合已穷尽
        raise AssertionError(f"未处理的 Hitl1Op：{op!r}")


def _apply_decision(
    argument_tree: list[Argument],
    paragraph_list: list[ParagraphRecord],
    decision: Hitl1Decision,
) -> tuple[list[Argument], list[ParagraphRecord]]:
    """应用 SKIP/ACCEPT/EDIT 决策，返回确认后的 (树, paragraph_list)（深拷贝；EDIT 逐步 revalidate）。

    skip/accept → 树与 paragraph_list 原样深拷贝（不改原文一个字）；edit → 深拷贝上逐步应用 ops、
    每步 ``validate_tree`` + ``validate_paragraph_consistency``，非法步即抛 → 调用方原树不动
    （working 丢弃）。
    """

    if decision.action in (Hitl1Action.SKIP, Hitl1Action.ACCEPT):
        return (
            [n.model_copy(deep=True) for n in argument_tree],
            [r.model_copy(deep=True) for r in paragraph_list],
        )

    # edit：深拷贝上逐步应用 + 每步 revalidate（树结构 + 段落↔节点一致性）。
    working = [n.model_copy(deep=True) for n in argument_tree]
    working_pl = [r.model_copy(deep=True) for r in paragraph_list]
    for op in decision.ops:
        _apply_op(working, working_pl, op)
        validate_tree(working)  # 非法步即抛 → 调用方原树不动（working 丢弃）。
        validate_paragraph_consistency(working, working_pl)
    return working, working_pl


def confirm(
    argument_tree: list[Argument],
    gate: Hitl1Gate,
    *,
    paragraph_list: list[ParagraphRecord],
) -> list[Argument]:
    """应用 HITL-1 结构确认决策（SKIP/ACCEPT/EDIT），返回确认后的树。

    ``paragraph_list`` 必填（T-04：``Argument`` 不存 ``paragraph_id``，段落↔节点归属只能
    由段落聚合根提供；production 由 ``confirm_partition`` 显式传入 parse 产出的真
    paragraph_list）。三 op 在内部同步 ``argument_tree_ids``，但本函数只返回树
    （paragraph_list 同步结果经 ``confirm_partition`` 的 :class:`Hitl1Outcome` 返回）。

    流程：
    1. ``validate_tree`` + ``validate_paragraph_consistency``——输入自检（不信任调用方）。
    2. ``gate.review(argument_tree)``——闸门看**原始**树，返回决策。
    3. 委托 :func:`_apply_decision` 应用决策（skip/accept 原样深拷贝；edit 逐步应用 + revalidate）。

    partition 打回（REPLAY）不在此函数职责内——见 :func:`confirm_partition`。
    """

    validate_tree(argument_tree)  # 1. 输入自检。
    validate_paragraph_consistency(argument_tree, paragraph_list)
    decision = gate.review(argument_tree, paragraph_list=paragraph_list)  # 2. 闸门看原始树。
    tree, _pl = _apply_decision(argument_tree, paragraph_list, decision)  # 3. 应用决策。
    return tree


def confirm_partition(
    argument_tree: list[Argument],
    paragraph_list: list[ParagraphRecord],
    retry_count: int = 0,
    *,
    gate: Hitl1Gate,
    max_retries: int = DEFAULT_MAX_PARTITION_RETRIES,
) -> Hitl1Outcome:
    """partition 确认闸门 + 有界打回（ADR-0018）。

    人确认段落切分是否合理；闸门决策分两类语义：

    - **确认继续**（SKIP/ACCEPT/EDIT）：复用 :func:`_apply_decision` 应用结构编辑（含
      ``argument_tree_ids`` 同步），返回 ``route=CONTINUE``、计数不变（打回计数只随 REPLAY
      递增）；``Hitl1Outcome.paragraph_list`` 携同步后的段落聚合根。
    - **打回重跑**（REPLAY）：重跑 ``parse+partition``（按 user prompt，当前伪代码桩，
      ADR-0020）。
      - 预算内（``retry_count < max_retries``）：``route=REPLAY``、``retry_count+1``。
      - 超限（``retry_count >= max_retries``）：受控分支——``route=CONTINUE`` +
        ``exhausted=True``（向前推进 + 贴 ``partition_retry_exhausted``，**不**经异常降级）。
        树与 paragraph_list 原样深拷贝（partition 重切为桩、不在本节点改树）。

    输入自检同 :func:`confirm`：``validate_tree`` + ``validate_paragraph_consistency`` 在闸门
    review 前复检。``paragraph_list`` 必填（T-04：``Argument`` 不存段落归属，须显式传入 parse
    产出的真 paragraph_list）。
    """

    validate_tree(argument_tree)  # 输入自检（不信任调用方）。
    validate_paragraph_consistency(argument_tree, paragraph_list)
    decision = gate.review(argument_tree, paragraph_list=paragraph_list)  # 闸门看原始树。

    if decision.action is Hitl1Action.REPLAY:
        if retry_count >= max_retries:
            # 超限：受控分支、非异常降级——向前推进 + 贴标签（由 build 闭包落 errors）。
            return Hitl1Outcome(
                argument_tree=[n.model_copy(deep=True) for n in argument_tree],
                paragraph_list=[r.model_copy(deep=True) for r in paragraph_list],
                route=Hitl1Route.CONTINUE,
                retry_count=retry_count,
                exhausted=True,
            )
        # 预算内：打回重跑 parse+partition（重切为桩、不在本节点改树）。
        return Hitl1Outcome(
            argument_tree=[n.model_copy(deep=True) for n in argument_tree],
            paragraph_list=[r.model_copy(deep=True) for r in paragraph_list],
            route=Hitl1Route.REPLAY,
            retry_count=retry_count + 1,
            exhausted=False,
        )

    # 确认继续（SKIP/ACCEPT/EDIT）：应用结构编辑、route=CONTINUE、计数不变。
    tree_out, pl_out = _apply_decision(argument_tree, paragraph_list, decision)
    return Hitl1Outcome(
        argument_tree=tree_out,
        paragraph_list=pl_out,
        route=Hitl1Route.CONTINUE,
        retry_count=retry_count,
        exhausted=False,
    )
