"""``HistoryStore`` seam 单测（ADR-0016）：源压缩。"""

from __future__ import annotations

from infra.history import (
    DEFAULT_COMPRESSION,
    CompressionConfig,
    CompressionStrategy,
    HistoryStore,
)
from infra.retrieval import RetrievalKind, Source


def _src(i: int) -> Source:
    return Source(
        source_id=f"s{i}",
        kind=RetrievalKind.NETWORK,
        origin="who.int",
        title=f"t{i}",
        snippet=f"snippet {i}",
    )


def test_append_and_all() -> None:
    history = HistoryStore()
    assert len(history) == 0
    history.append(_src(0))
    history.append(_src(1))
    assert len(history) == 2
    assert [s.source_id for s in history.all()] == ["s0", "s1"]


def test_extend_accumulates() -> None:
    history = HistoryStore()
    history.extend([_src(0), _src(1)])
    assert len(history) == 2


def test_default_no_trimming_under_threshold() -> None:
    history = HistoryStore(DEFAULT_COMPRESSION)  # max_items=20
    for i in range(15):
        history.append(_src(i))
    assert len(history.compressed_view()) == 15


def test_max_items_last_keeps_recent() -> None:
    history = HistoryStore(CompressionConfig(max_items=3, strategy=CompressionStrategy.LAST))
    for i in range(5):
        history.append(_src(i))
    assert [s.source_id for s in history.compressed_view()] == ["s2", "s3", "s4"]


def test_max_items_first_keeps_earliest() -> None:
    history = HistoryStore(CompressionConfig(max_items=3, strategy=CompressionStrategy.FIRST))
    for i in range(5):
        history.append(_src(i))
    assert [s.source_id for s in history.compressed_view()] == ["s0", "s1", "s2"]


def test_max_tokens_last_greedy_backfill() -> None:
    # 自定义计数器：每条 4 token；预算 10 → 保留最近 2 条（cost 8 ≤ 10）。
    history = HistoryStore(
        CompressionConfig(
            max_items=None,
            max_tokens=10,
            strategy=CompressionStrategy.LAST,
            token_counter=lambda ms: len(ms) * 4,
        )
    )
    for i in range(5):
        history.append(_src(i))
    assert [s.source_id for s in history.compressed_view()] == ["s3", "s4"]


def test_max_tokens_first_greedy_frontfill() -> None:
    history = HistoryStore(
        CompressionConfig(
            max_items=None,
            max_tokens=10,
            strategy=CompressionStrategy.FIRST,
            token_counter=lambda ms: len(ms) * 4,
        )
    )
    for i in range(5):
        history.append(_src(i))
    assert [s.source_id for s in history.compressed_view()] == ["s0", "s1"]


def test_all_returns_full_uncompressed() -> None:
    history = HistoryStore(CompressionConfig(max_items=2))
    for i in range(5):
        history.append(_src(i))
    assert [s.source_id for s in history.all()] == ["s0", "s1", "s2", "s3", "s4"]


def test_compressed_view_does_not_mutate_store() -> None:
    history = HistoryStore(CompressionConfig(max_items=2))
    for i in range(5):
        history.append(_src(i))
    _ = history.compressed_view()
    assert len(history) == 5


def test_items_then_tokens_both_apply_take_stricter() -> None:
    # max_items=4 先裁到最近 4，再按 token（每条 4，预算 10）裁到最近 2。
    history = HistoryStore(
        CompressionConfig(
            max_items=4,
            max_tokens=10,
            strategy=CompressionStrategy.LAST,
            token_counter=lambda ms: len(ms) * 4,
        )
    )
    for i in range(6):
        history.append(_src(i))
    assert [s.source_id for s in history.compressed_view()] == ["s4", "s5"]
