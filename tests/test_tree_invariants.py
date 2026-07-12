"""论证树结构不变式校验的独立单测（PRD «Testing Decisions» 纯函数 seam）。

解析器建树后自检、HITL-1 编辑后复检都走 :func:`validate_tree`——结构正确性由代码
保证，不寄望于 LLM。本测试锁定四条硬约束：``node_id`` 唯一、``parent_id`` 指向存在
节点、父子链无环、``children_ids`` 与 ``parent_id`` 双向一致。

本模块只校验**树自身**结构性质（不依赖只读原文表）；段级字节校验（``paragraph_id``
是否存在、``content`` 是否逐字节来自只读表）在解析器内完成——故此处不测段级约束。
"""

from __future__ import annotations

import pytest

from domain import ArgumentationNode, NodeType
from tree_invariants import TreeInvariantError, validate_tree


def _node(
    node_id: str,
    *,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    paragraph_id: str = "p0001",
) -> ArgumentationNode:
    """构造最小合法节点（避开无关字段噪声）。"""

    return ArgumentationNode(
        node_id=node_id,
        node_type=NodeType.BACKGROUND,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        paragraph_id=paragraph_id,
    )


def _chain(a: str, b: str, c: str) -> list[ArgumentationNode]:
    """构造一致的双向父子链 A → B → C（A 根）。"""

    return [
        _node(a, children_ids=[b]),
        _node(b, parent_id=a, children_ids=[c]),
        _node(c, parent_id=b),
    ]


def test_empty_tree_validates():
    """空树通过校验。"""

    validate_tree([])  # 不抛。


def test_single_root_node_validates():
    """单根节点（无父、无子）通过校验。"""

    validate_tree([_node("n1")])


def test_linear_chain_validates():
    """一致的双向父子链 A → B → C 通过校验。"""

    validate_tree(_chain("a", "b", "c"))


def test_duplicate_node_id_rejected():
    """node_id 重复 → TreeInvariantError。"""

    tree = [_node("n1"), _node("n1", paragraph_id="p0002")]
    with pytest.raises(TreeInvariantError, match="重复"):
        validate_tree(tree)


def test_dangling_parent_id_rejected():
    """parent_id 指向不存在的节点 → TreeInvariantError。"""

    tree = [_node("n1", parent_id="ghost")]
    with pytest.raises(TreeInvariantError, match="不存在的节点"):
        validate_tree(tree)


def test_cycle_rejected():
    """父子链成环（A↔B 互为父）→ TreeInvariantError。"""

    tree = [
        _node("a", parent_id="b", children_ids=["b"]),
        _node("b", parent_id="a", children_ids=["a"]),
    ]
    with pytest.raises(TreeInvariantError, match="环"):
        validate_tree(tree)


def test_children_parent_mismatch_rejected():
    """A 声称 B 是子，但 B 的 parent_id 不是 A（正向不一致）→ TreeInvariantError。"""

    tree = [
        _node("a", children_ids=["b"]),
        _node("b", parent_id="c"),  # 应为 a
    ]
    with pytest.raises(TreeInvariantError):
        validate_tree(tree)


def test_parent_missing_from_children_rejected():
    """B 的 parent_id 是 A，但 A 的 children_ids 不含 B（反向不一致）→ 报错。"""

    tree = [
        _node("a", children_ids=[]),  # 漏了 b
        _node("b", parent_id="a"),
    ]
    with pytest.raises(TreeInvariantError, match="children_ids 不含"):
        validate_tree(tree)
