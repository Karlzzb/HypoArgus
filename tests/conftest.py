"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

# 一组覆盖各类边界形态的样例文档（bytes），用于分区不变式与字节级回写断言。
SAMPLE_DOCS: dict[str, bytes] = {
    "simple": b"First paragraph.\n\nSecond paragraph.\n",
    "blank_lines": b"\n\nLeading blanks.\n\n\nBetween.\n\nTrailing.\n\n\n",
    "indent": b"    indented para\n\n      deeper indent\n\nnormal\n",
    "list": b"- item one\n- item two\n\n- item three\n\nafter list\n",
    "code_fence": b"intro\n\n```python\nx = 1\n\ny = 2\n```\n\nafter code\n",
    "tilde_fence": b"intro\n\n~~~\nblank line inside\n\n~~~\n\ndone\n",
    "no_trailing_newline": b"para one\n\npara two",
    "trailing_spaces": b"line one.   \n\nline two.\n",
    "mixed": b"# Title\n\nintro paragraph.\n\n- bullet\n\n```python\ncode\n```\n\nFinal.\n",
    "only_blanks": b"\n\n\n",
    "single_line": b"only one paragraph no newline",
}


@pytest.fixture(params=list(SAMPLE_DOCS.items()), ids=list(SAMPLE_DOCS.keys()))
def sample_doc(request):
    name, doc = request.param
    return name, doc
