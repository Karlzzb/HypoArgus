"""HITL-1 结构确认闸门（PRD §10 节点 1、issue #2）。

解析输出初始论证树后、双线路启动前触发。用户可调层级、合并或拆分节点、修正边界、
标记无需处理的段落；**支持跳过**（跳过则直接进入下一环节，不改动原文一个字）。

与解析器对 LLM 的防御性兜底**非对称**：解析器遇环即断、越界即兜底（LLM 不可信）；HITL-1
是「人」的意图性编辑，遇非法编辑一律**拒绝**（抛 :class:`TreeInvariantError`）、绝不静默修复。
整个决策要么全部应用、要么全部丢弃——在深拷贝上工作、每步 ``validate_tree``，非法步 #N
即终止、调用方原树不动。

本切片为同步注入闸门（``Hitl1Gate`` seam，``FakeHitl1Gate`` 供离线单测）；真实
``interrupt`` + ``Command(resume)`` + checkpointer 属 #11。``fix_boundary`` 延后——
domain 无 ``text_span``（ADR-0001），待该字段落地后接入。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from domain import ArgumentationNode, NodeType
from tree_invariants import TreeInvariantError, rebuild_children, validate_tree

__all__ = [
    "Hitl1Action",
    "MergeOp",
    "SplitOp",
    "ReparentOp",
    "SetTypeOp",
    "MarkNoOpOp",
    "FixBoundaryOp",
    "Hitl1Op",
    "Hitl1Decision",
    "Hitl1Gate",
    "FakeHitl1Gate",
    "confirm",
]


class Hitl1Action(StrEnum):
    """HITL-1 会话级决策。"""

    SKIP = "skip"  # 跳过结构确认 → 不改动原文一个字
    ACCEPT = "accept"  # 接受解析树原样
    EDIT = "edit"  # 应用结构编辑序列


# --------------------------------------------------------------------------- #
# 编辑操作（pydantic v2 判别联合，每个 op 只载自身字段）
# --------------------------------------------------------------------------- #


class MergeOp(BaseModel):
    """合并同段若干节点为一个（幸存者保留自身属性，被删节点的子节点改挂幸存者）。

    跨段合并违反 ADR-0001（一节点一段），解析器拒绝。
    """

    action: Literal["merge"] = "merge"
    node_ids: list[str]


class SplitOp(BaseModel):
    """拆分节点 → 同段叶兄弟（唯一 id，继承类型/段/父，无子）。"""

    action: Literal["split"] = "split"
    node_id: str


class ReparentOp(BaseModel):
    """调整层级：改 ``parent_id``（``new_parent_id=None`` 即提为根）。"""

    action: Literal["reparent"] = "reparent"
    node_id: str
    new_parent_id: str | None


class SetTypeOp(BaseModel):
    """改节点类型；权重作为副作用调整（影子→0、影子→核心→50、核心→核心保留）。"""

    action: Literal["set_type"] = "set_type"
    node_id: str
    new_type: NodeType


class MarkNoOpOp(BaseModel):
    """标记段落无需处理：该段所有节点转 ``background`` 影子、权重 0，结构不变。"""

    action: Literal["mark_no_op"] = "mark_no_op"
    paragraph_id: str


class FixBoundaryOp(BaseModel):
    """修正段内边界——延后实现（domain 无 ``text_span``，ADR-0001）。"""

    action: Literal["fix_boundary"] = "fix_boundary"
    node_id: str


Hitl1Op = Annotated[
    MergeOp | SplitOp | ReparentOp | SetTypeOp | MarkNoOpOp | FixBoundaryOp,
    Field(discriminator="action"),
]


class Hitl1Decision(BaseModel):
    """HITL-1 决策：会话级动作 + （edit 时）有序编辑序列。"""

    action: Hitl1Action
    ops: list[Hitl1Op] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 闸门 seam + 离线桩
# --------------------------------------------------------------------------- #


class Hitl1Gate(Protocol):
    """HITL-1 闸门 seam：审阅树 → 返回纯数据决策。

    真实实现用 ``interrupt`` 把树交给用户、用 ``Command(resume)`` 收回决策（#11）；
    本 seam 不绑任何前端/中断机制。``confirm`` 保证：闸门看到的是**原始**树，
    而非中间编辑态——多步编辑在闸门一次返回、由 ``confirm`` 顺序应用。
    """

    def review(self, tree: list[ArgumentationNode]) -> Hitl1Decision: ...


class FakeHitl1Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。"""

    def __init__(self, decision: Hitl1Decision) -> None:
        self._decision = decision

    def review(self, tree: list[ArgumentationNode]) -> Hitl1Decision:
        return self._decision.model_copy(deep=True)


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数，可独立单测
# --------------------------------------------------------------------------- #


def _require_node(nodes: list[ArgumentationNode], node_id: str) -> ArgumentationNode:
    """按 id 取节点；不存在则抛 :class:`TreeInvariantError`（结构非法）。"""

    for node in nodes:
        if node.node_id == node_id:
            return node
    raise TreeInvariantError(f"HITL-1 编辑引用不存在的节点：{node_id}")


def _apply_merge(nodes: list[ArgumentationNode], op: MergeOp) -> None:
    by_id = {n.node_id: n for n in nodes}
    merged = [by_id[i] for i in op.node_ids if i in by_id]
    if len(merged) != len(op.node_ids):
        raise TreeInvariantError(
            f"merge 引用不存在的节点：{set(op.node_ids) - set(by_id)}"
        )
    if len({n.paragraph_id for n in merged}) != 1:
        raise TreeInvariantError(
            "跨段合并违反 ADR-0001（一节点一段）；跨段结构变更应走 reparent"
        )
    survivor = merged[0]
    merged_set = {n.node_id for n in merged}
    # 被删节点的子节点（非合并集自身）改挂幸存者。
    for n in merged:
        for child_id in n.children_ids:
            if child_id not in merged_set and child_id in by_id:
                by_id[child_id].parent_id = survivor.node_id
    # 移除非幸存者。
    nodes[:] = [n for n in nodes if n.node_id not in merged_set or n.node_id == survivor.node_id]
    rebuild_children(nodes)


def _mint_split_id(existing: set[str], source_id: str) -> str:
    """为拆分产出唯一 id：``{source}-s{n}``，与既有 id 不撞。"""

    base = f"{source_id}-s"
    i = 1
    new_id = f"{base}{i}"
    while new_id in existing:
        i += 1
        new_id = f"{base}{i}"
    return new_id


def _apply_split(nodes: list[ArgumentationNode], op: SplitOp) -> None:
    source = _require_node(nodes, op.node_id)
    new_id = _mint_split_id({n.node_id for n in nodes}, op.node_id)
    new_node = source.model_copy(deep=True)
    new_node.node_id = new_id
    new_node.children_ids = []  # 叶兄弟
    nodes.append(new_node)
    rebuild_children(nodes)


def _apply_reparent(nodes: list[ArgumentationNode], op: ReparentOp) -> None:
    node = _require_node(nodes, op.node_id)
    node.parent_id = op.new_parent_id
    rebuild_children(nodes)


def _apply_set_type(nodes: list[ArgumentationNode], op: SetTypeOp) -> None:
    node = _require_node(nodes, op.node_id)
    old_type = node.node_type
    node.node_type = op.new_type
    if op.new_type.is_shadow:
        # 影子节点不参与传导，权重恒 0。
        node.argument_weight = 0
    elif old_type.is_shadow and not op.new_type.is_shadow:
        # 影子→核心：原 0 不适合核心，设保守默认 50。
        node.argument_weight = 50
    # 核心→核心：保留原权重。


def _apply_mark_no_op(nodes: list[ArgumentationNode], op: MarkNoOpOp) -> None:
    for node in nodes:
        if node.paragraph_id == op.paragraph_id:
            node.node_type = NodeType.BACKGROUND
            node.argument_weight = 0


def _apply_op(nodes: list[ArgumentationNode], op: Hitl1Op) -> None:
    if isinstance(op, MergeOp):
        _apply_merge(nodes, op)
    elif isinstance(op, SplitOp):
        _apply_split(nodes, op)
    elif isinstance(op, ReparentOp):
        _apply_reparent(nodes, op)
    elif isinstance(op, SetTypeOp):
        _apply_set_type(nodes, op)
    elif isinstance(op, MarkNoOpOp):
        _apply_mark_no_op(nodes, op)
    elif isinstance(op, FixBoundaryOp):
        raise NotImplementedError(
            "fix_boundary 延后实现：domain 无 text_span（ADR-0001）；"
            "待 text_span 字段落地后接入。"
        )
    else:  # pragma: no cover - 判别联合已穷尽
        raise AssertionError(f"未处理的 Hitl1Op：{op!r}")


def confirm(
    tree: list[ArgumentationNode], gate: Hitl1Gate
) -> list[ArgumentationNode]:
    """应用 HITL-1 决策，返回确认后的树。

    流程：
    1. ``validate_tree(tree)``——输入自检（不信任调用方，解析器已保证但 HITL-1 复检）。
    2. ``gate.review(tree)``——闸门看**原始**树，返回决策。
    3. ``skip``/``accept``：返回树的深拷贝（不改原文一个字）。
    4. ``edit``：在深拷贝上**逐步**应用 ops，每步 ``validate_tree``——非法步即终止、
       整个决策丢弃（调用方原树永不被改）。
    """

    validate_tree(tree)  # 1. 输入自检。
    decision = gate.review(tree)  # 2. 闸门看原始树。

    # 3. skip / accept：不改原文。
    if decision.action in (Hitl1Action.SKIP, Hitl1Action.ACCEPT):
        return [n.model_copy(deep=True) for n in tree]

    # 4. edit：深拷贝上逐步应用 + 每步 revalidate。
    working = [n.model_copy(deep=True) for n in tree]
    for op in decision.ops:
        _apply_op(working, op)
        validate_tree(working)  # 非法步即抛 → 调用方原树不动（working 丢弃）。
    return working
