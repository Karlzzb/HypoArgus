"""只读原文段落表测试（ADR-0005）。"""

from __future__ import annotations

import pytest

from partition import partition
from raw_store import RawParagraphStore


def test_store_from_text_roundtrip(sample_doc):
    """store 按序拼接等于原始输入。"""

    _name, doc = sample_doc
    store = RawParagraphStore.from_text(doc)
    rebuilt = b"".join(store.get(pid) for pid in store.paragraph_ids())
    assert rebuilt == doc


def test_store_paragraph_ids_partition_order():
    """paragraph_ids 返回分区顺序。"""

    doc = b"a\n\nb\n\nc\n"
    store = RawParagraphStore.from_text(doc)
    assert store.paragraph_ids() == ("p0001", "p0002", "p0003")


def test_store_has_and_get():
    doc = b"first.\n\nsecond.\n"
    store = RawParagraphStore.from_text(doc)
    assert store.has("p0001")
    assert not store.has("p9999")
    # 段落 bytes 含其尾随分隔（trailing attachment），但都以原文内容起头。
    assert store.get("p0001").startswith(b"first.\n")
    assert store.get("p0002") == b"second.\n"


def test_store_get_unknown_raises():
    store = RawParagraphStore.from_text(b"x\n")
    with pytest.raises(KeyError):
        store.get("p9999")


def test_store_is_immutable():
    """store 不可变：构造后无法篡改原文 bytes。"""

    doc = b"original\n"
    store = RawParagraphStore.from_text(doc)
    # bytes 不可变；映射冻结：无法新增/改写条目。
    with pytest.raises(TypeError):
        store._entries["p0001"] = b"tampered"  # type: ignore[index]
    assert store.get("p0001") == b"original\n"  # 原文未变


def test_store_from_paragraphs():
    """可直接由 Paragraph 列表构造。"""

    paragraphs = partition(b"a\n\nb\n")
    store = RawParagraphStore(paragraphs)
    assert len(store) == 2
    assert list(store) == ["p0001", "p0002"]
    assert "p0001" in store
