"""``trace_events`` 持久日志 store seam 单测（T-05·ADR-0023）。

验 :class:`api_layer.trace_store.TraceEventStoreBase` 的两 adapter 同形同义：
``InMemoryTraceEventStore``（离线单测）与 ``PostgresTraceEventStore``（生产 adapter，
PG 不可达即 skip）。store 只管落库 / 查询，``event_seq`` 由翻译层 mint、单调性不在 store 责内。
"""

from __future__ import annotations

import asyncio
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


def _TraceEv(  # noqa: N802 — 与 _ev 对称，可定制 session_id / ts
    trace: str,
    seq: int,
    et: EventType,
    *,
    session_id: str = "sess-1",
    ts: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> TraceEvent:
    return TraceEvent(
        session_id=session_id,
        trace_id=trace,
        event_seq=seq,
        event_type=et,
        payload=payload or {},
        ts=ts or datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC),
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
# events_for_session + subscribe（T-06 live 尾随 seam）
# --------------------------------------------------------------------------- #


async def test_inmemory_events_for_session_orders_across_traces() -> None:
    """单会话跨 trace 全量按 ts 升序、同刻按 (trace_id, event_seq) 稳定排序。"""

    store = InMemoryTraceEventStore()
    # 同 session、两 trace、不同 ts：早 trace 在前。
    await store.append(_TraceEv("tA", 0, EventType.TRACE_START, ts=datetime(2026, 7, 14, 9, 0, tzinfo=UTC)))
    await store.append(_TraceEv("tA", 1, EventType.NODE_END, ts=datetime(2026, 7, 14, 9, 1, tzinfo=UTC)))
    await store.append(_TraceEv("tB", 0, EventType.TRACE_START, ts=datetime(2026, 7, 14, 9, 2, tzinfo=UTC)))
    rows = await store.events_for_session("sess-1")
    assert [r.trace_id for r in rows] == ["tA", "tA", "tB"]
    assert [r.event_seq for r in rows] == [0, 1, 0]


async def test_inmemory_subscribe_yields_new_events_after_subscribe() -> None:
    """订阅在 append 前 → 后续 append 经 pub-sub 按序 yield。"""

    store = InMemoryTraceEventStore()
    sub = await store.subscribe("sess-1")
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    await store.append(_ev("t1", 1, EventType.NODE_START, {"node_id": "hitl1"}))
    first = await asyncio.wait_for(sub.__anext__(), 5)
    second = await asyncio.wait_for(sub.__anext__(), 5)
    assert first.event_type is EventType.TRACE_START
    assert second.payload["node_id"] == "hitl1"
    await sub.close()


async def test_inmemory_subscribe_filters_by_session() -> None:
    """他会话的 append 不入本会话订阅。"""

    store = InMemoryTraceEventStore()
    sub = await store.subscribe("sess-1")
    await store.append(_TraceEv("tOther", 0, EventType.TRACE_START, session_id="sess-2"))
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    ev = await asyncio.wait_for(sub.__anext__(), 5)
    assert ev.session_id == "sess-1"
    assert ev.trace_id == "t1"
    await sub.close()


async def test_inmemory_subscribe_close_deregisters() -> None:
    """close 后 append 不再入队（无残留引用）；再 close 幂等。"""

    store = InMemoryTraceEventStore()
    sub = await store.subscribe("sess-1")
    await sub.close()
    await sub.close()  # 幂等
    await store.append(_ev("t1", 0, EventType.TRACE_START))
    assert "sess-1" not in store._subs  # noqa: SLF001 — 验证登记册已清


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


async def test_postgres_events_for_session_and_subscribe(pg_trace_store: Any) -> None:
    """append 后 events_for_session 返回该会话全量；subscribe 经 LISTEN/NOTIFY 收到后续新事件。"""

    import uuid

    store = pg_trace_store
    sid = f"pgsub-{uuid.uuid4()}"
    trace = f"pgtrace-sub-{uuid.uuid4()}"
    # 历史行（subscribe 前）——经 events_for_session 回放可得，不入 live 订阅。
    await store.append(_TraceEv(trace, 0, EventType.TRACE_START, session_id=sid))
    rows = await store.events_for_session(sid)
    assert len(rows) == 1
    assert rows[0].event_type is EventType.TRACE_START

    # live 订阅：subscribe 后 append 新行 → 经 NOTIFY 唤醒、按序 yield。
    sub = await store.subscribe(sid)
    await store.append(_TraceEv(trace, 1, EventType.NODE_START, session_id=sid, payload={"node_id": "hitl1"}))
    ev = await asyncio.wait_for(sub.__anext__(), 10)
    assert ev.event_seq == 1
    assert ev.payload["node_id"] == "hitl1"
    await sub.close()


async def test_append_uses_separate_connection_contexts(pg_trace_store: Any) -> None:
    """INSERT 与 NOTIFY 必须分处不同连接上下文——NOTIFY 失败不可回滚已提交的 INSERT。"""

    import uuid
    from unittest.mock import patch

    store = pg_trace_store
    trace = f"pgtrace-ctx-{uuid.uuid4()}"

    # 记录每次 `self._pool.connection()` 进入的上下文，统计进入次数。
    real_connection = store._pool.connection  # noqa: SLF001 — 验证 store 内部结构
    enter_count = 0

    class _CountingConnection:
        """包装真实 connection cm，计数进入次数、委托所有操作给真实对象。"""

        def __init__(self, real_cm: Any) -> None:
            self._real = real_cm

        async def __aenter__(self) -> Any:
            nonlocal enter_count
            enter_count += 1
            return await self._real.__aenter__()

        async def __aexit__(self, *exc: object) -> None:
            return await self._real.__aexit__(*exc)

    def counting_connection() -> Any:
        return _CountingConnection(real_connection())

    with patch.object(store._pool, "connection", side_effect=counting_connection):  # noqa: SLF001
        await store.append(_ev(trace, 0, EventType.TRACE_START))

    # 两次进入：一次 INSERT 提交、一次 NOTIFY 提交——分属不同事务/连接上下文。
    assert enter_count == 2
    # INSERT 已落库（即便 NOTIFY 失败也不影响 durable 行）。
    assert await store.max_seq(trace) == 0
    rows = await store.events_for_trace(trace)
    assert len(rows) == 1
    assert rows[0].event_type is EventType.TRACE_START


async def test_append_durable_even_if_notify_fails(pg_trace_store: Any) -> None:
    """NOTIFY 失败时 INSERT 仍已落库——display 副信道失败不丢 durable 事件（ADR-0023）。

    用 >8000 字符的 session_id 触发 ``pg_notify`` 真实报错（payload 超长）：
    INSERT 列为 TEXT 不受限、先独立提交；NOTIFY 单独事务因 payload 超长在提交时抛错。
    """

    import logging
    import uuid
    from unittest.mock import patch

    store = pg_trace_store
    sid = "x" * 9000  # 超过 pg_notify payload 上限 8000 字节 → NOTIFY 真实失败
    trace = f"pgtrace-notifyfail-{uuid.uuid4()}"

    logger = logging.getLogger("api_layer.trace_store")
    with patch.object(logger, "warning") as mock_warn:
        # 不抛——append 须吞 NOTIFY 失败（ADR-0023：display 副信道失败不杀图、不丢 durable 行）。
        await store.append(_TraceEv(trace, 0, EventType.TRACE_START, session_id=sid))
        assert mock_warn.called, "NOTIFY 失败须记 warning（不静默吞错）"

    # durable 行存活：max_seq / events_for_trace 均可见。
    assert await store.max_seq(trace) == 0
    rows = await store.events_for_trace(trace)
    assert len(rows) == 1
    assert rows[0].event_seq == 0
    assert rows[0].trace_id == trace
    assert rows[0].session_id == sid
