"""段落原子回写（ADR-0001、ADR-0005、PRD §11）。

遍历修订确认后的终版树，以**段落为唯一原子单位**产出修订版文档，坚决杜绝全量重写。

- 未被任何采纳改动命中的段落：按 ``paragraph_id`` 从只读原文表**逐字节流式拷回**，
  100% 保护原文风格与格式。
- 被命中的段落：按被采纳假设的关系分流（对立→替换、递进→改写、扩展→段尾追加）。

本切片（#1 tracer bullet）只实现「逐字节拷回」通道；采纳改动的分流由 #10 接入。
回写遍历只读表的**规范顺序**（分区顺序），而非树的遍历顺序——无论树形如何，
未变更段落始终逐字节还原（这是字节级承诺的工程落点）。

本函数是纯函数子缝（PRD «Testing Decisions»）：``终版树 + 只读段落表 → 终稿文本``，
可独立单测。
"""

from __future__ import annotations

from typing import Protocol

from hypoargus.domain import ArgumentationNode, NodeStatus
from hypoargus.raw_store import RawParagraphStore

__all__ = ["RewriteFn", "writeback"]


class RewriteFn(Protocol):
    """被采纳改动命中段落的重写协议（#10 接入）。

    给定段落 id、该段所有节点、只读原文表，返回该段的终版 bytes。
    本切片不调用它（无采纳改动），留作 #10 的接入点。
    """

    def __call__(
        self,
        paragraph_id: str,
        nodes: list[ArgumentationNode],
        store: RawParagraphStore,
    ) -> bytes: ...


def writeback(
    tree: list[ArgumentationNode],
    store: RawParagraphStore,
    rewrite_fn: RewriteFn | None = None,
) -> bytes:
    """产出终稿 bytes。

    按 :meth:`RawParagraphStore.paragraph_ids` 的规范顺序遍历每段：
    - 若该段无 ``adopted`` 节点 → 从只读表逐字节拷回（字节级无损）。
    - 若该段有 ``adopted`` 节点 → 交给 ``rewrite_fn`` 重写（#10 接入；本切片不会触发）。

    规范顺序遍历保证：只要无采纳改动，输出与原始输入逐字节相等（分区不变式）。
    """

    # 先按 paragraph_id 索引节点一次，避免逐段全量扫描树（O(N) 而非 O(N×P)）。
    nodes_by_paragraph: dict[str, list[ArgumentationNode]] = {}
    for node in tree:
        nodes_by_paragraph.setdefault(node.paragraph_id, []).append(node)

    out = bytearray()
    for paragraph_id in store.paragraph_ids():
        nodes = nodes_by_paragraph.get(paragraph_id, [])
        if any(n.status == NodeStatus.ADOPTED for n in nodes):
            if rewrite_fn is None:
                raise NotImplementedError(
                    f"段落 {paragraph_id} 有 adopted 节点，但未提供 rewrite_fn"
                    "（采纳改动分流回写由 #10 接入）。"
                )
            out += rewrite_fn(paragraph_id, nodes, store)
        else:
            out += store.get(paragraph_id)
    return bytes(out)
