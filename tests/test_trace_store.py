"""``trace_events`` 持久日志 store seam 单测（T-05·ADR-0023）。

验 :class:`api_layer.trace_store.TraceEventStoreBase` 的两 adapter 同形同义：
``InMemoryTraceEventStore``（离线单测）与 ``PostgresTraceEventStore``（生产 adapter，
PG 不可达即 skip）。store 只管落库 / 查询，``event_seq`` 由翻译层 mint、单调性不在 store 责内。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from api_layer.trace_store import (
    EventType,
    InMemoryTraceEventStore,
    TraceEvent,
)


def _ev(
    trace: str, seq: int, et: EventType, payload: dict[str, Any] | None = None
) -> TraceEvent:
    return TraceEvent(
        session_id="sess-1",
        trace_id=trace,
        event_seq=seq,
        event_type=et,
        payload=payload or {},
        ts=datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- #
# InMemoryTraceEventStore
# --------------------------------------------------------------------------- #


async def test_inmemory_append_and_query_ordered_by_seq() -> None:
    store = InMemoryTraceEventStore()
    await store.setup()
    # 乱序 append（调用方负责单调 mint，store 不重排）→ 查询按 event_seq 排序返回。
    await store.append(_ev("t1", 2, EventType.NODE_END))
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    await store.append(_ev("t1", 1, EventType.NODE_START))

    rows = await store.events_for_trace("t1")
    assert [r.event_seq for r in rows] == [0, 1, 2]
    assert [r.event_type for r in rows] == [
        EventType.TRACE_START,
        EventType.NODE_START,
        EventType.NODE_END,
    ]


async def test_inmemory_max_seq_unknown_trace_is_minus_one() -> None:
    store = InMemoryTraceEventStore()
    assert await store.max_seq("nope") == -1


async def test_inmemory_max_seq_returns_max() -> None:
    store = InMemoryTraceEventStore()
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    await store.append(_ev("t1", 5, EventType.NODE_END))
    await store.append(_ev("t1", 2, EventType.NODE_START))
    assert await store.max_seq("t1") == 5


async def test_inmemory_isolates_traces() -> None:
    """两 trace 各自独立；max_seq / 查询不串。"""

    store = InMemoryTraceEventStore()
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    await store.append(_ev("t2", 0, EventType.TRACE_START))
    await store.append(_ev("t2", 1, EventType.NODE_START))
    assert await store.max_seq("t1") == 0
    assert await store.max_seq("t2") == 1
    assert len(await store.events_for_trace("t1")) == 1
    assert len(await store.events_for_trace("t2")) == 2


async def test_inmemory_payload_roundtrip() -> None:
    store = InMemoryTraceEventStore()
    await store.append(
        _ev("t1", 0, EventType.NODE_START, {"node_id": "hitl1", "label": "HITL-1"})
    )
    rows = await store.events_for_trace("t1")
    assert rows[0].payload == {"node_id": "hitl1", "label": "HITL-1"}


# --------------------------------------------------------------------------- #
# PostgresTraceEventStore（PG 集成；不可达 skip）
# --------------------------------------------------------------------------- #


async def test_postgres_roundtrip(pg_trace_store: Any) -> None:
    """append 三事件（乱序）→ 查询按 event_seq 排序、payload JSONB 还原、max_seq 正确。"""

    import uuid

    store = pg_trace_store
    trace = f"pgtrace-rt-{uuid.uuid4()}"
    await store.append(_ev(trace, 2, EventType.NODE_END, {"node_id": "hitl2"}))
    await store.append(_ev(trace, 0, EventType.TRACE_START))
    await store.append(
        _ev(trace, 1, EventType.NODE_START, {"node_id": "parse+partition", "n": 1})
    )

    assert await store.max_seq(trace) == 2
    rows = await store.events_for_trace(trace)
    assert [r.event_seq for r in rows] == [0, 1, 2]
    assert rows[0].event_type is EventType.TRACE_START
    assert rows[1].payload["node_id"] == "parse+partition"
    assert rows[1].payload["n"] == 1
    assert rows[2].payload["node_id"] == "hitl2"


async def test_postgres_max_seq_unknown_is_minus_one(pg_trace_store: Any) -> None:
    assert await pg_trace_store.max_seq("nonexistent-trace") == -1


async def test_postgres_setup_is_idempotent(pg_trace_store: Any) -> None:
    """重复 setup 不抛（CREATE TABLE IF NOT EXISTS）。"""

    await pg_trace_store.setup()
    await pg_trace_store.setup()
