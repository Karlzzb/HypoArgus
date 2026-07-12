"""论证树结构不变式校验（纯函数 seam，PRD §4、ADR-0001）。

解析器自检与 HITL-1 编辑后复检共用此 seam：所有「LLM 不可信、必须代码兜底」的
结构硬约束在此以可执行断言表达。本 seam 只校验**树自身**的结构性质（不依赖只读
原文表）；段级字节校验（``paragraph_id`` 是否存在、``content`` 是否逐字节来自只读表）
在解析器内完成，因为只有解析器持有只读表。

校验的不变式：

1. ``node_id`` 唯一。
2. 每个非空 ``parent_id`` 指向树内存在的节点。
3. 父子链无环（一个节点不是自己的祖先）。
4. ``children_ids`` 与 ``parent_id`` 双向一致：A 的 children 含 B ⟺ B 的 parent 是 A。
"""

from __future__ import annotations

from domain import ArgumentationNode

__all__ = ["TreeInvariantError", "validate_tree", "rebuild_children"]


class TreeInvariantError(Exception):
    """论证树违反结构不变式。"""


def rebuild_children(nodes: list[ArgumentationNode]) -> None:
    """据 ``parent_id`` 重建 ``children_ids``（双向一致），覆盖任何既有值。

    解析器建树与 HITL-1 编辑后复检共用的结构维护 seam——与 :func:`validate_tree`
    同处本模块（两者皆是「LLM 不可信、人编辑亦可能错、必须代码兜底」的树结构兜底）。
    """

    for node in nodes:
        node.children_ids = []
    by_id = {n.node_id: n for n in nodes}
    for node in nodes:
        if node.parent_id is not None and node.parent_id in by_id:
            by_id[node.parent_id].children_ids.append(node.node_id)


def validate_tree(tree: list[ArgumentationNode]) -> None:
    """校验论证树结构不变式；违反任一则抛 :class:`TreeInvariantError`。

    纯函数、可独立单测（PRD «Testing Decisions» seam）。解析器建树后自检、
    HITL-1 编辑后复检都走此函数——结构正确性由代码保证，不寄望于 LLM。
    """

    nodes = list(tree)
    by_id: dict[str, ArgumentationNode] = {}

    # 1. node_id 唯一 + 索引。
    for node in nodes:
        if node.node_id in by_id:
            raise TreeInvariantError(f"node_id 重复：{node.node_id}")
        by_id[node.node_id] = node

    # 2. parent_id 指向存在节点或 None。
    for node in nodes:
        if node.parent_id is not None and node.parent_id not in by_id:
            raise TreeInvariantError(
                f"节点 {node.node_id} 的 parent_id "
                f"{node.parent_id!r} 指向不存在的节点"
            )

    # 3. 父子链无环（一个节点不可是自己的祖先）。
    for node in nodes:
        seen: set[str] = set()
        cur = node.parent_id
        while cur is not None:
            if cur == node.node_id:
                raise TreeInvariantError(
                    f"父子链成环：节点 {node.node_id} 是自己的祖先"
                )
            if cur in seen:
                # 别处环已报或将被报；此处避免无限循环。
                raise TreeInvariantError(
                    f"父子链成环（经 {cur} 回到环上）"
                )
            seen.add(cur)
            cur = by_id[cur].parent_id

    # 4. children_ids 与 parent_id 双向一致。
    for node in nodes:
        for child_id in node.children_ids:
            child = by_id.get(child_id)
            if child is None:
                raise TreeInvariantError(
                    f"节点 {node.node_id} 的 children_ids 含不存在的子节点 "
                    f"{child_id}"
                )
            if child.parent_id != node.node_id:
                raise TreeInvariantError(
                    f"节点 {node.node_id} 声称 {child_id} 是子节点，"
                    f"但后者的 parent_id 是 {child.parent_id!r}"
                )
        # 反向：若 B 的 parent 是 A，则 A 的 children 必含 B。
    for node in nodes:
        if node.parent_id is not None:
            parent = by_id[node.parent_id]
            if node.node_id not in parent.children_ids:
                raise TreeInvariantError(
                    f"节点 {node.node_id} 的 parent_id 是 "
                    f"{node.parent_id!r}，但父节点 children_ids 不含它"
                )
