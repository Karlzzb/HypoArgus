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


def test_partition_splits_on_atx_headings_without_blank_lines() -> None:
    """无空行分隔时，ATX 标题起始新段（ADR-0009「标题各成一段」）；拼接逐字节相等。

    治单换行无空行分隔的真实论文（如 paper_01 整篇无空行）——否则塌成一段、
    下游 LLM 单调用喂入巨块超时。标题边界 = CommonMark 块级边界。
    """

    doc = "# 一级标题\n正文一。\n## 二级标题\n正文二。\n## 二级标题二\n正文三。\n".encode()
    paragraphs = partition(doc)
    # 每个标题起始新段：3 段。
    assert len(paragraphs) == 3
    assert paragraphs[0].content == "# 一级标题\n正文一。\n".encode()
    assert paragraphs[1].content == "## 二级标题\n正文二。\n".encode()
    assert paragraphs[2].content == "## 二级标题二\n正文三。\n".encode()
    # 分区不变式：拼接逐字节等于原文。
    assert b"".join(p.content for p in paragraphs) == doc


def test_partition_atx_heading_does_not_split_tables_or_inline_hash() -> None:
    """表格分隔行 ``| --- |``、行中 ``#``（如「编号 #1」）非 ATX 标题、不触发切分。"""

    doc = "正文编号 #1 的段。\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n".encode()
    paragraphs = partition(doc)
    # 空行分隔为两段：正文 + 表格；「#1」、``| --- |`` 不误判为标题起始新段。
    assert len(paragraphs) == 2
    assert paragraphs[0].content == "正文编号 #1 的段。\n\n".encode()
    assert b"".join(p.content for p in paragraphs) == doc


# --------------------------------------------------------------------------- #
# 真实论文分区测试：markdown/ 下整篇中文论文作为 bytes，喂给确定性分区层。
# 零模型参与——这些测试只验证纯代码切分在真实论文上的字节级无损性。
# --------------------------------------------------------------------------- #


def test_partition_invariant_real_paper(real_paper: tuple[str, bytes]) -> None:
    """真实论文：分区不变式逐字节成立（拼接等于原文 + assert_partition_invariant 通过）。"""

    _name, doc = real_paper
    paragraphs = partition(doc)
    assert b"".join(p.content for p in paragraphs) == doc
    assert_partition_invariant(doc, paragraphs)  # 不抛即通过


def test_partition_real_paper_yields_paragraphs(real_paper: tuple[str, bytes]) -> None:
    """真实论文切分至少产出一段（非空文档必有段落）。"""

    _name, doc = real_paper
    paragraphs = partition(doc)
    assert len(paragraphs) >= 1


def test_partition_real_paper_ids_zero_padded_ordered(real_paper: tuple[str, bytes]) -> None:
    """真实论文段落 id 为 p0001、p0002 … 零填充且严格递增反映分区顺序。"""

    _name, doc = real_paper
    paragraphs = partition(doc)
    expected = [f"p{i:04d}" for i in range(1, len(paragraphs) + 1)]
    assert [p.paragraph_id for p in paragraphs] == expected
