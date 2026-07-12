"""只读原文段落表测试（ADR-0005）。"""

from __future__ import annotations

import pytest

from original_paragraphs import OriginalParagraphs
from partition import partition


def test_store_from_text_roundtrip(sample_doc):
    """original_paragraphs 按序拼接等于原始输入。"""

    _name, doc = sample_doc
    original_paragraphs = OriginalParagraphs.from_text(doc)
    rebuilt = b"".join(original_paragraphs.get(pid) for pid in original_paragraphs.paragraph_ids())
    assert rebuilt == doc


def test_store_paragraph_ids_partition_order():
    """paragraph_ids 返回分区顺序。"""

    doc = b"a\n\nb\n\nc\n"
    original_paragraphs = OriginalParagraphs.from_text(doc)
    assert original_paragraphs.paragraph_ids() == ("p0001", "p0002", "p0003")


def test_store_has_and_get():
    doc = b"first.\n\nsecond.\n"
    original_paragraphs = OriginalParagraphs.from_text(doc)
    assert original_paragraphs.has("p0001")
    assert not original_paragraphs.has("p9999")
    # 段落 bytes 含其尾随分隔（trailing attachment），但都以原文内容起头。
    assert original_paragraphs.get("p0001").startswith(b"first.\n")
    assert original_paragraphs.get("p0002") == b"second.\n"


def test_store_get_unknown_raises():
    original_paragraphs = OriginalParagraphs.from_text(b"x\n")
    with pytest.raises(KeyError):
        original_paragraphs.get("p9999")


def test_store_is_immutable():
    """original_paragraphs 不可变：构造后无法篡改原文 bytes。"""

    doc = b"original\n"
    original_paragraphs = OriginalParagraphs.from_text(doc)
    # bytes 不可变；映射冻结：无法新增/改写条目。
    with pytest.raises(TypeError):
        original_paragraphs._entries["p0001"] = b"tampered"  # type: ignore[index]
    assert original_paragraphs.get("p0001") == b"original\n"  # 原文未变


def test_store_from_paragraphs():
    """可直接由 Paragraph 列表构造。"""

    paragraphs = partition(b"a\n\nb\n")
    original_paragraphs = OriginalParagraphs(paragraphs)
    assert len(original_paragraphs) == 2
    assert list(original_paragraphs) == ["p0001", "p0002"]
    assert "p0001" in original_paragraphs
