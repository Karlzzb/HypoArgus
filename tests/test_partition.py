"""分区不变式测试（ADR-0009、PRD «Testing Decisions»）。

纯代码段落切分：所有段落按序拼接必须逐字节等于原始输入（含空行、缩进、换行、
末尾空格、代码栅栏）。零模型参与，字节级还原是代码级确定的。
"""

from __future__ import annotations

import pytest

from partition import assert_partition_invariant, partition


def test_partition_concat_equals_input(sample_doc):
    """分区不变式：拼接逐字节等于原始输入。"""

    _name, doc = sample_doc
    paragraphs = partition(doc)
    assert b"".join(p.content for p in paragraphs) == doc


def test_partition_assert_invariant(sample_doc):
    """:func:`assert_partition_invariant` 对所有样例通过。"""

    _name, doc = sample_doc
    paragraphs = partition(doc)
    assert_partition_invariant(doc, paragraphs)  # 不抛即通过


def test_partition_ids_stable_and_ordered():
    """paragraph_id 为零填充序号、稳定反映分区顺序。"""

    doc = b"first.\n\nsecond.\n\nthird.\n"
    paragraphs = partition(doc)
    ids = [p.paragraph_id for p in paragraphs]
    assert ids == ["p0001", "p0002", "p0003"]


def test_partition_code_fence_kept_atomic():
    """代码栅栏内的空行不触发切分：代码块保持一段。"""

    doc = b"intro\n\n```python\na = 1\n\nb = 2\n```\n\noutro\n"
    paragraphs = partition(doc)
    # intro / code block / outro → 三段。
    assert len(paragraphs) == 3
    code_block = paragraphs[1].content
    # 开栅栏与闭栅栏同属一段（原子）。
    assert b"```python" in code_block
    assert code_block.count(b"```") == 2
    # 栅栏内空行保留在代码块内部、未被撕成多段。
    assert b"\n\nb = 2\n" in code_block
    assert b"".join(p.content for p in paragraphs) == doc


def test_partition_preserves_trailing_space_in_content():
    """末尾空格逐字节保留（落在其所属段落内），拼接逐字节相等。"""

    doc = b"line one.   \n\nline two.\n"
    paragraphs = partition(doc)
    assert b"line one.   \n" in paragraphs[0].content
    assert b"".join(p.content for p in paragraphs) == doc


def test_partition_no_trailing_newline():
    """无末尾换行的文档：末段不含尾随换行，拼接仍逐字节相等。"""

    doc = b"para one\n\npara two"
    paragraphs = partition(doc)
    assert paragraphs[-1].content == b"para two"
    assert b"".join(p.content for p in paragraphs) == doc


def test_partition_rejects_non_bytes():
    """非 bytes 输入被拒绝（原文以 bytes 处理，保证字节级）。"""

    with pytest.raises(TypeError):
        partition("not bytes")  # type: ignore[arg-type]


def test_partition_empty():
    """空文档：零段，拼接仍等于空。"""

    paragraphs = partition(b"")
    assert paragraphs == []
    assert b"".join(p.content for p in paragraphs) == b""
