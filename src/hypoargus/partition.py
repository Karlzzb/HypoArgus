"""确定性、无损、纯代码段落切分（ADR-0009）。

用纯规则按 Markdown 块级元素 + 空行边界把原文切成有序段落：一个标题/段落/列表项/
代码块各成一段。**零模型参与**——字节级还原是代码级确定的，与解析质量无关。

关键不变式（分区不变式）：所有段落按序拼接必须逐字节等于原始输入（含空行、缩进、
换行、末尾空格）。切分粒度对 Lossless 不做「语义合并」承诺：宁可多段，绝不丢字节。

代码块（fenced code block）内部含空行时不切分，避免把代码块撕碎。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Paragraph", "partition", "assert_partition_invariant"]


@dataclass(frozen=True)
class Paragraph:
    """一个切分出的段落：``paragraph_id`` 与其原始 bytes。

    ``paragraph_id`` 为零填充序号（``p0001``、``p0002`` …），稳定反映分区顺序。
    """

    paragraph_id: str
    content: bytes


def _is_blank(line: bytes) -> bool:
    return line.strip() == b""


def _fence_char(stripped: bytes) -> bytes | None:
    """若是代码栅栏起始行，返回栅栏字符（``b'`'`` 或 ``b'~'``）；否则 None。"""

    if stripped.startswith(b"```"):
        return b"`"
    if stripped.startswith(b"~~~"):
        return b"~"
    return None


def _is_fence_close(stripped: bytes, marker: bytes) -> bool:
    return stripped.startswith(marker * 3)


def _chunk_has_content(buf: list[bytes]) -> bool:
    return any(not _is_blank(line) for line in buf)


def _chunk_ends_blank(buf: list[bytes]) -> bool:
    return bool(buf) and _is_blank(buf[-1])


def partition(text: bytes) -> list[Paragraph]:
    """把原始 bytes 无损切成有序段落。

    切分规则：
    - 块级元素以空行分隔；连续非空行（代码栅栏外）属同一段。
    - 段间空行作为**前一段的尾随字节**归属该段——这是 Lossless 的关键：每个字节
      （含空行、换行、末尾空格）都唯一归属某段，拼接即复原。
    - 文首空行归首段（作为其前导），文末空行归末段（作为其尾随）。
    - 代码栅栏（``` / ~~~）内的空行不触发切分，代码块保持原子。

    返回的段落按序拼接保证等于 ``text``（见 :func:`assert_partition_invariant`）。
    """

    if not isinstance(text, (bytes, bytearray)):
        raise TypeError(f"partition 要求 bytes 输入，收到 {type(text).__name__}")
    text = bytes(text)

    lines = text.splitlines(keepends=True)
    chunks: list[bytes] = []
    buf: list[bytes] = []
    in_fence = False
    fence_marker: bytes | None = None

    for line in lines:
        stripped = line.strip()

        if not in_fence:
            marker = _fence_char(stripped)
            if marker is not None:
                # 栅栏开启：并入手头这段（若前段已有内容且以空行结尾，先收口成块）。
                if _chunk_has_content(buf) and _chunk_ends_blank(buf):
                    chunks.append(b"".join(buf))
                    buf = []
                in_fence = True
                fence_marker = marker
                buf.append(line)
                continue
        else:
            buf.append(line)
            if fence_marker is not None and _is_fence_close(stripped, fence_marker):
                in_fence = False
                fence_marker = None
            continue

        if _is_blank(line):
            # 空行归入当前段（作为尾随）。
            buf.append(line)
            continue

        # 非空内容行：若当前段已有内容且已以空行收尾，说明新块开始——先落盘旧段。
        if _chunk_has_content(buf) and _chunk_ends_blank(buf):
            chunks.append(b"".join(buf))
            buf = []
        buf.append(line)

    if buf:
        chunks.append(b"".join(buf))

    return [
        Paragraph(paragraph_id=f"p{idx:04d}", content=chunk)
        for idx, chunk in enumerate(chunks, start=1)
    ]


def assert_partition_invariant(text: bytes, paragraphs: list[Paragraph]) -> None:
    """分区不变式：所有段落按序拼接必须逐字节等于原始输入。

    任何字节（空行、缩进、换行、末尾空格）丢失都会在此被抓住。用于 :func:`partition`
    自检与测试缝合点断言（PRD «Testing Decisions»）。
    """

    rebuilt = b"".join(p.content for p in paragraphs)
    if rebuilt != text:
        raise AssertionError(
            "分区不变式被破坏：拼接结果与原始输入不一致。\n"
            f"原始长度={len(text)} 重建长度={len(rebuilt)}\n"
            f"原始={text!r}\n重建={rebuilt!r}"
        )
