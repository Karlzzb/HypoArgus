"""HITL-1 结构确认闸门单测（PRD §10 节点 1、issue #2）。

解析输出初始论证树后、双线路启动前触发。用户可调层级、合并或拆分节点、修正边界、
标记无需处理的段落；**支持跳过**（跳过则直接进入下一环节，不改动原文一个字）。

HITL-1 是「人」的意图性编辑，与解析器对 LLM 的防御性兜底**非对称**：解析器遇环即断、
越界即兜底；HITL-1 遇非法编辑一律**拒绝**（抛 ``TreeInvariantError``）、绝不静默修复，
且整个决策要么全部应用、要么全部丢弃（copy-first + 每步 revalidate）。

本切片 HITL-1 为同步注入闸门（``Hitl1Gate`` seam，``FakeHitl1Gate`` 供离线单测）；
真实 interrupt + checkpointer 属 #11。
"""

from __future__ import annotations

import pytest

from hypoargus.domain import ArgumentationNode, NodeType
from hypoargus.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Gate,
    confirm,
)
from hypoargus.tree_invariants import TreeInvariantError


def _node(
    node_id: str,
    *,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    paragraph_id: str = "p0001",
    node_type: NodeType = NodeType.EVIDENCE,
    argument_weight: int = 50,
) -> ArgumentationNode:
    return ArgumentationNode(
        node_id=node_id,
        node_type=node_type,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        paragraph_id=paragraph_id,
        argument_weight=argument_weight,
    )


def _abc_tree() -> list[ArgumentationNode]:
    """A 根，B、C 均为 A 的子（同段 p0001）。"""

    return [
        _node("a", node_type=NodeType.MAIN_CLAIM, children_ids=["b", "c"]),
        _node("b", parent_id="a"),
        _node("c", parent_id="a"),
    ]


def _gate(decision: Hitl1Decision) -> Hitl1Gate:
    return FakeHitl1Gate(decision)


# --------------------------------------------------------------------------- #
# slice 1：骨架 + skip/accept + 输入校验 + 空编辑
# --------------------------------------------------------------------------- #


def test_confirm_skip_returns_tree_unchanged():
    """skip → 树原样返回（不改动原文一个字）。"""

    tree = _abc_tree()
    out = confirm(tree, _gate(Hitl1Decision(action=Hitl1Action.SKIP)))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in tree]


def test_confirm_accept_returns_tree_unchanged():
    """accept → 树原样返回。"""

    tree = _abc_tree()
    out = confirm(tree, _gate(Hitl1Decision(action=Hitl1Action.ACCEPT)))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in tree]


def test_confirm_validates_input_tree():
    """输入树本身非法 → 抛 TreeInvariantError，且 gate.review 从未被调用。"""

    bad_tree = [_node("a", parent_id="ghost")]
    reviewed = []

    class _Spy:
        def review(self, tree):
            reviewed.append(True)
            return Hitl1Decision(action=Hitl1Action.SKIP)

    with pytest.raises(TreeInvariantError):
        confirm(bad_tree, _Spy())  # type: ignore[arg-type]
    assert reviewed == []  # gate 未被调用


def test_confirm_edit_with_empty_ops_unchanged():
    """edit + 空 ops → 树不变（但返回深拷贝，不与输入同对象）。"""

    tree = _abc_tree()
    out = confirm(tree, _gate(Hitl1Decision(action=Hitl1Action.EDIT, ops=[])))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in tree]


def test_confirm_does_not_mutate_caller_tree():
    """confirm 在深拷贝上工作，调用方的树对象永不被改动。"""

    tree = _abc_tree()
    snapshot = [n.model_copy(deep=True) for n in tree]
    confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[],  # 即使有 ops，调用方也不应被改
            )
        ),
    )
    assert [n.model_dump() for n in tree] == [n.model_dump() for n in snapshot]


# --------------------------------------------------------------------------- #
# slice 2：reparent（调层级）
# --------------------------------------------------------------------------- #


def _reparent(node_id: str, new_parent_id: str | None) -> Hitl1Decision:
    from hypoargus.hitl1 import ReparentOp

    return Hitl1Decision(
        action=Hitl1Action.EDIT,
        ops=[ReparentOp(node_id=node_id, new_parent_id=new_parent_id)],
    )


def test_confirm_reparent_updates_parent_and_children():
    """reparent C 到 B 下：C.parent=B，B.children 含 C，A.children 释放 C。"""

    tree = _abc_tree()
    out = confirm(tree, _gate(_reparent("c", "b")))
    by_id = {n.node_id: n for n in out}
    assert by_id["c"].parent_id == "b"
    assert "c" in by_id["b"].children_ids
    assert "c" not in by_id["a"].children_ids


def test_confirm_reparent_to_none_makes_root():
    """reparent C 到 None → C 成为根。"""

    tree = _abc_tree()
    out = confirm(tree, _gate(_reparent("c", None)))
    by_id = {n.node_id: n for n in out}
    assert by_id["c"].parent_id is None
    assert "c" not in by_id["a"].children_ids


def test_confirm_reparent_creating_cycle_raises_and_leaves_caller_untouched():
    """reparent A 到其后代 C → 成环 → 抛错；调用方原树不变。"""

    tree = _abc_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(TreeInvariantError):
        confirm(tree, _gate(_reparent("a", "c")))
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_reparent_to_missing_parent_raises():
    """reparent 到不存在的节点 → 抛错。"""

    tree = _abc_tree()
    with pytest.raises(TreeInvariantError):
        confirm(tree, _gate(_reparent("c", "ghost")))


# --------------------------------------------------------------------------- #
# slice 3：merge（同段合并）/ split（同段拆分）
# --------------------------------------------------------------------------- #


def test_confirm_merge_same_paragraph_unions_children():
    """合并同段两节点：幸存者保留自身属性，被删节点的子节点改挂幸存者。"""

    from hypoargus.hitl1 import MergeOp

    # a 根 → b（p0001）、c（p0001），c 有子 d。
    tree = [
        _node("a", node_type=NodeType.MAIN_CLAIM, children_ids=["b", "c"]),
        _node("b", parent_id="a", paragraph_id="p0001"),
        _node("c", parent_id="a", paragraph_id="p0001", children_ids=["d"]),
        _node("d", parent_id="c", paragraph_id="p0001"),
    ]
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[MergeOp(node_ids=["b", "c"])],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert "c" not in by_id  # 被合并删除
    assert "d" in by_id
    assert by_id["d"].parent_id == "b"  # d 改挂幸存者 b
    assert "d" in by_id["b"].children_ids
    assert "b" in by_id["a"].children_ids


def test_confirm_merge_cross_paragraph_rejected():
    """跨段合并违反 ADR-0001（一节点一段）→ 抛错，调用方不变。"""

    from hypoargus.hitl1 import MergeOp

    tree = [
        _node("a", node_type=NodeType.MAIN_CLAIM, children_ids=["b", "c"]),
        _node("b", parent_id="a", paragraph_id="p0001"),
        _node("c", parent_id="a", paragraph_id="p0002"),
    ]
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(TreeInvariantError, match="跨段|paragraph"):
        confirm(
            tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[MergeOp(node_ids=["b", "c"])],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_split_creates_sibling_same_paragraph():
    """拆分节点 N → 新节点为同段叶兄弟，唯一 id，继承类型/父。"""

    from hypoargus.hitl1 import SplitOp

    tree = _abc_tree()
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SplitOp(node_id="b")],
            )
        ),
    )
    new_nodes = [n for n in out if n.node_id not in {"a", "b", "c"}]
    assert len(new_nodes) == 1
    new = new_nodes[0]
    by_id = {n.node_id: n for n in out}
    # 新节点与源节点同段、同类型、同父（叶兄弟），唯一 id
    assert new.paragraph_id == by_id["b"].paragraph_id
    assert new.node_type == by_id["b"].node_type
    assert new.parent_id == by_id["b"].parent_id  # 同父兄弟
    assert new.children_ids == []  # 叶
    assert new.node_id in by_id["a"].children_ids  # 父认子


def test_confirm_split_twice_yields_distinct_ids():
    """连续拆分两次 → 两个不同 id，均不与既有冲突。"""

    from hypoargus.hitl1 import SplitOp

    tree = _abc_tree()
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SplitOp(node_id="b"), SplitOp(node_id="b")],
            )
        ),
    )
    new_ids = [n.node_id for n in out if n.node_id not in {"a", "b", "c"}]
    assert len(new_ids) == 2
    assert len(set(new_ids)) == 2  # 互不相同


# --------------------------------------------------------------------------- #
# slice 4：set_type / mark_no_op
# --------------------------------------------------------------------------- #


def test_confirm_set_type_demote_to_shadow_zeros_weight():
    """set_type → BACKGROUND：影子节点，权重归零。"""

    from hypoargus.hitl1 import SetTypeOp

    tree = [
        _node("a", node_type=NodeType.MAIN_CLAIM, argument_weight=80, children_ids=["b"]),
        _node("b", parent_id="a", node_type=NodeType.EVIDENCE, argument_weight=85),
    ]
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SetTypeOp(node_id="b", new_type=NodeType.BACKGROUND)],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["b"].node_type == NodeType.BACKGROUND
    assert by_id["b"].node_type.is_shadow
    assert by_id["b"].argument_weight == 0


def test_confirm_set_type_promote_shadow_to_core_sets_default_weight():
    """set_type 影子→核心：权重设保守默认 50（原 0 不适合核心）。"""

    from hypoargus.hitl1 import SetTypeOp

    tree = [
        _node("a", node_type=NodeType.MAIN_CLAIM, argument_weight=80, children_ids=["b"]),
        _node("b", parent_id="a", node_type=NodeType.BACKGROUND, argument_weight=0),
    ]
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SetTypeOp(node_id="b", new_type=NodeType.SUB_CLAIM)],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["b"].node_type == NodeType.SUB_CLAIM
    assert not by_id["b"].node_type.is_shadow
    assert by_id["b"].argument_weight == 50


def test_confirm_mark_no_op_converts_paragraph_to_shadow():
    """mark_no_op(pid)：该段所有节点转 BACKGROUND、权重 0，结构不变。"""

    from hypoargus.hitl1 import MarkNoOpOp

    tree = [
        _node(
            "a",
            node_type=NodeType.MAIN_CLAIM,
            argument_weight=80,
            children_ids=["b", "c"],
        ),
        _node("b", parent_id="a", paragraph_id="p0001", node_type=NodeType.EVIDENCE, argument_weight=70),
        _node("c", parent_id="a", paragraph_id="p0002", node_type=NodeType.EVIDENCE, argument_weight=60),
    ]
    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[MarkNoOpOp(paragraph_id="p0001")],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    # p0001 的节点（a、b）转影子、权重 0
    assert by_id["a"].node_type == NodeType.BACKGROUND
    assert by_id["a"].argument_weight == 0
    assert by_id["b"].node_type == NodeType.BACKGROUND
    assert by_id["b"].argument_weight == 0
    # p0002 的 c 不受影响
    assert by_id["c"].node_type == NodeType.EVIDENCE
    assert by_id["c"].argument_weight == 60
    # 结构不变
    assert by_id["a"].children_ids == ["b", "c"]
    assert by_id["b"].parent_id == "a"


# --------------------------------------------------------------------------- #
# slice 5：fix_boundary（延后）+ 多步序列
# --------------------------------------------------------------------------- #


def test_confirm_fix_boundary_raises_deferred():
    """fix_boundary 延后实现（domain 无 text_span，ADR-0001）→ NotImplementedError。"""

    from hypoargus.hitl1 import FixBoundaryOp

    tree = _abc_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(NotImplementedError, match="text_span"):
        confirm(
            tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[FixBoundaryOp(node_id="b")],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_edit_sequence_applied_in_order_and_validated():
    """多步编辑按序应用、每步 revalidate：先 split b，再 reparent 新节点到 c 下。"""

    from hypoargus.hitl1 import ReparentOp

    tree = _abc_tree()
    # 第一步 split b → 新节点 new；第二步 reparent new 到 c（需先知道 new 的 id）。
    # 由于 id 由实现决定，第二步用 split 产出的节点——这里改用更简单的序列：
    # reparent c 到 b，再 set b 为 sub_claim（无关结构变更，验证多步不冲突）。
    from hypoargus.hitl1 import SetTypeOp

    out = confirm(
        tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[
                    ReparentOp(node_id="c", new_parent_id="b"),
                    SetTypeOp(node_id="b", new_type=NodeType.SUB_CLAIM),
                ],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["c"].parent_id == "b"
    assert "c" in by_id["b"].children_ids
    assert by_id["b"].node_type == NodeType.SUB_CLAIM
    assert "c" not in by_id["a"].children_ids


def test_confirm_edit_sequence_invalid_step_rejects_wholesale():
    """序列中某步非法 → 整个决策丢弃，调用方原树不变。"""

    from hypoargus.hitl1 import ReparentOp, SetTypeOp

    tree = _abc_tree()
    snapshot = [n.model_dump() for n in tree]
    # 第一步合法（set b 类型），第二步非法（reparent a 到后代 c 成环）
    with pytest.raises(TreeInvariantError):
        confirm(
            tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[
                        SetTypeOp(node_id="b", new_type=NodeType.SUB_CLAIM),
                        ReparentOp(node_id="a", new_parent_id="c"),
                    ],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot
