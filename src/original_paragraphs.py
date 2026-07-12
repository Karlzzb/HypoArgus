"""只读原文段落表（Raw Paragraph Store，ADR-0005）。

``{ paragraph_id → 原始 bytes }`` 的不可变副本。它是字节级还原的唯一真相源，
也是回写拷贝的真相源。**任何 Agent 的 prompt 都不整篇加载它**——节点只携带自身
那一段原文加 ``paragraph_id`` 指针；回写按 ``paragraph_id`` 逐段拷回。

本表提供按 ``paragraph_id`` 的有序访问（:meth:`paragraph_ids` 返回分区顺序），
使回写可按规范顺序遍历而**不依赖树的遍历顺序**——无论树形如何，输出始终逐字节确定。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import MappingProxyType

from partition import Paragraph, partition

__all__ = ["OriginalParagraphs"]


class OriginalParagraphs:
    """不可变原文段落表。

    构造后冻结：仅暴露按 ``paragraph_id`` 的只读访问，无整篇 dump，故原文 bytes
    永不整篇进入任何 Agent 上下文。
    """

    __slots__ = ("_entries", "_order")

    def __init__(self, paragraphs: list[Paragraph]) -> None:
        # 冻结：顺序列表 + 不可变 id→bytes 映射。
        self._order: tuple[str, ...] = tuple(p.paragraph_id for p in paragraphs)
        self._entries: MappingProxyType[str, bytes] = MappingProxyType(
            {p.paragraph_id: p.content for p in paragraphs}
        )

    @classmethod
    def from_text(cls, text: bytes) -> OriginalParagraphs:
        """从原始文本构造：先切分（纯代码、无损），再固化。"""

        return cls(partition(text))

    def has(self, paragraph_id: str) -> bool:
        return paragraph_id in self._entries

    def get(self, paragraph_id: str) -> bytes:
        try:
            return self._entries[paragraph_id]
        except KeyError:
            raise KeyError(f"未知 paragraph_id: {paragraph_id}") from None

    def paragraph_ids(self) -> tuple[str, ...]:
        """分区顺序的段落 id 序列（回写的规范遍历顺序）。"""

        return self._order

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, paragraph_id: object) -> bool:
        return paragraph_id in self._entries

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"OriginalParagraphs(paragraphs={len(self._entries)})"
