"""回写纯函数子缝测试（ADR-0001、ADR-0005、PRD §11、«Testing Decisions»）。

回写按段落原子缝合：未变更段逐字节拷回、变更段按关系正确替换或改写或段尾追加。
本切片（#1）只覆盖未变更场景（逐字节拷回）；采纳改动分流由 #10 覆盖。
"""

from __future__ import annotations

import pytest

from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.raw_store import RawParagraphStore
from hypoargus.writeback import writeback


def _shadow_tree(store: RawParagraphStore):
    """每段一个只读影子节点（与解析桩一致）。"""

    return [
        ArgumentationNode(
            node_id=f"n-{pid}",
            node_type=NodeType.BACKGROUND,
            paragraph_id=pid,
            content=store.get(pid).decode("utf-8", errors="surrogateescape"),
        )
        for pid in store.paragraph_ids()
    ]


def test_writeback_no_adoptions_byte_identical(sample_doc):
    """无采纳改动时，终稿逐字节等于原始输入（含空行/缩进/末尾空格）。"""

    _name, doc = sample_doc
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    assert writeback(tree, store) == doc


def test_writeback_uses_store_canonical_order_not_tree_order():
    """回写按只读表规范顺序遍历，而非树遍历顺序——保证字节级确定。"""

    doc = b"aaa\n\nbbb\n\nccc\n"
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    # 故意打乱树顺序，回写仍按 store 规范顺序输出。
    tree.reverse()
    assert writeback(tree, store) == doc


def test_writeback_preserves_code_fence_block_bytes():
    """代码块段逐字节无损（含栅栏内空行）。"""

    doc = b"intro\n\n```python\na = 1\n\nb = 2\n```\n\noutro\n"
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    assert writeback(tree, store) == doc


def test_writeback_adopted_node_without_rewrite_fn_raises():
    """有 adopted 节点但未提供 rewrite_fn：本切片明确未实现（#10 接入）。"""

    doc = b"para\n"
    store = RawParagraphStore.from_text(doc)
    node = ArgumentationNode(
        node_id="n-p0001",
        node_type=NodeType.EVIDENCE,
        paragraph_id="p0001",
        content="para",
        status=NodeStatus.ADOPTED,
    )

    with pytest.raises(NotImplementedError):
        writeback([node], store)


def test_writeback_adopted_node_delegates_to_rewrite_fn():
    """adopted 段落交给 rewrite_fn（#10 分流的接入点）。"""

    doc = b"original\n"
    store = RawParagraphStore.from_text(doc)
    node = ArgumentationNode(
        node_id="n-p0001",
        node_type=NodeType.EVIDENCE,
        paragraph_id="p0001",
        content="original",
        status=NodeStatus.ADOPTED,
    )

    def rewrite_fn(paragraph_id, nodes, store):
        return b"REWRITTEN\n"

    assert writeback([node], store, rewrite_fn=rewrite_fn) == b"REWRITTEN\n"


def test_writeback_mixed_adopted_and_untouched():
    """多段文档：仅一段 adopted 被重写，未变更段逐字节无损还原。

    按 ADR-0001，被采纳改动命中的段落整段进入重写通道，段内/段间不再作硬字节
    承诺；本测试只断言：未变更段逐字节还原、被重写段由 ``rewrite_fn`` 接管。
    采纳改动的分流语义（oppose/advance/expand）由 #10 覆盖。
    """

    doc = b"keep1\n\nchange\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    tree = [
        ArgumentationNode(
            node_id="n-p0001",
            node_type=NodeType.BACKGROUND,
            paragraph_id="p0001",
            content="keep1",
        ),
        ArgumentationNode(
            node_id="n-p0002",
            node_type=NodeType.EVIDENCE,
            paragraph_id="p0002",
            content="change",
            status=NodeStatus.ADOPTED,
        ),
        ArgumentationNode(
            node_id="n-p0003",
            node_type=NodeType.BACKGROUND,
            paragraph_id="p0003",
            content="keep2",
        ),
    ]

    def rewrite_fn(paragraph_id, nodes, store):
        return b"CHANGED\n"

    out = writeback(tree, store, rewrite_fn=rewrite_fn)
    # 未变更段逐字节还原。
    assert store.get("p0001") in out
    assert store.get("p0003") in out
    # 被重写段由 rewrite_fn 接管。
    assert b"CHANGED\n" in out
    # 原始 change 文本不再出现（已被替换）。
    assert b"change\n" not in out.replace(b"CHANGED\n", b"")
