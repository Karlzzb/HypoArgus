"""``trace_events`` 持久日志 store seam（T-05·ADR-0023·PRD §4.2.2）。

翻译层（:mod:`api_layer.translator`）把 ``astream_events`` 词汇映射为 PRD §6.4 事件、
mint ``event_seq``、**非阻塞**写本 seam。本表是显示层的 **durable 回放源**——WS-sender
（T-06）只读尾随；WS 断开不中止 run（ADR-0023 不变量）。

两个 adapter 使本 seam 成真 seam（deep-module 原则，与 :mod:`api_layer.session_cache` 同形）：

- :class:`InMemoryTraceEventStore`：进程内 list，供翻译层逻辑单测（映射 / 非阻塞 / 过滤）
  **无需 Postgres** 即可跑。
- :class:`PostgresTraceEventStore`：``psycopg`` async 连接池，落同一 Postgres（ADR-0022
  「一期无需 Redis」）。``setup()`` 幂等执行 :data:`SCHEMA_SQL`（CREATE TABLE IF NOT EXISTS）。

store 责任：落库（:meth:`append`）+ 查询（:meth:`max_seq` / :meth:`events_for_trace` /
:meth:`events_for_session`）+ **实时尾随订阅**（:meth:`subscribe`，供 T-06 WS-sender 只读尾随）。
``event_seq`` 的单调 mint 与续跑顺延（``max_seq + 1``）由翻译层负责——store 不重排、
不补号，按调用方给的 ``event_seq`` 原样落库。

``append`` 落库后即时通知该 session 的活跃订阅者（InMemory 进程内 pub-sub；Postgres
``NOTIFY trace_events``），使 WS-sender ``subscribe`` 近实时收到新事件（ADR-0023 不变量：
``NOTIFY`` 只唤醒尾随者拉取已落库行，不反压图、不阻塞 ``append`` 调用方）。Postgres 实现中
INSERT 与 NOTIFY **分两个连接上下文**：INSERT 先独立提交（durable 行不可被随后的 display
副信道失败回滚），NOTIFY 在自身事务提交时送达——此时行已可见，订阅者唤醒查询必得该行。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from typing import Any

_logger = logging.getLogger(__name__)

__all__ = [
    "EventType",
    "TraceEvent",
    "TraceEventStoreBase",
    "TraceSubscription",
    "InMemoryTraceEventStore",
    "PostgresTraceEventStore",
]


class EventType(StrEnum):
    """PRD §6.4 全事件类型词汇（翻译层 / WS-sender / 前端共享契约）。

    翻译层（T-05）产 ``trace_start`` 起的运行时事件；``graph_static``（建连首推，T-06 WS-sender
    据 :func:`api_layer.graph_view.build_graph_view` 产）与 ``heartbeat``（``event_seq=-1``，
    T-06 sender 产）**不由翻译层产**——列于此仅为共享词汇，便于 T-06 复用。
    """

    GRAPH_STATIC = "graph_static"
    TRACE_START = "trace_start"
    NODE_START = "node_start"
    NODE_OUTPUT = "node_output"
    NODE_END = "node_end"
    LLM_THINKING = "llm_thinking"
    TOOL_CALL = "tool_call"
    HUMAN_PAUSE = "human_pause"
    STREAM_FINISH = "stream_finish"
    STREAM_ABORT = "stream_abort"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class TraceEvent:
    """``trace_events`` 一行：单 trace 内一个生命周期事件（PRD §4.2.2 / §6.4）。

    :attr:`event_seq` 单 trace 内从 0 自增（翻译层 mint）；:attr:`payload` 为该事件类型
    特有的 JSONB 载荷（如 ``node_start`` 的 ``node_id`` / ``label`` / ``input``、
    ``human_pause`` 的 ``question`` / ``hint``）。:attr:`ts` 为落库时刻（tz-aware）。
    """

    session_id: str
    trace_id: str
    event_seq: int
    event_type: EventType
    payload: dict[str, Any]
    ts: datetime


class TraceEventStoreBase(ABC):
    """``trace_events`` 持久日志 seam（PRD §4.2.2）。

    所有方法 async（PG 实现为 async I/O；内存实现亦 async 保接口同形）。store 自身
    **不抛**控制面异常——落库失败由翻译层降级记错、不杀图（ADR-0023 不变量）。
    """

    @abstractmethod
    async def setup(self) -> None:
        """幂等初始化（建表）。PG 实现执行 :data:`SCHEMA_SQL`。"""
        ...

    @abstractmethod
    async def append(self, event: TraceEvent) -> None:
        """写一行（翻译层每事件一行、非阻塞调用方）。PK ``(trace_id, event_seq)``。"""
        ...

    @abstractmethod
    async def max_seq(self, trace_id: str) -> int:
        """单 trace 内已落库的最大 ``event_seq``；无行 → ``-1``（续跑派生 ``+1`` 即 ``0``）。"""
        ...

    @abstractmethod
    async def events_for_trace(self, trace_id: str) -> list[TraceEvent]:
        """单 trace 全量事件，按 ``event_seq`` 升序（回放源视图）。"""
        ...

    @abstractmethod
    async def node_instance_counts(self, trace_id: str) -> dict[str, int]:
        """单 trace 内各节点已落库的 ``node_start`` 次数（翻译层续跑时 seed ``node_instance``，
        使跨段计数连续——区分回放环 / 多次触发）。无 ``node_start`` 事件 → ``{}``。"""
        ...

    @abstractmethod
    async def events_for_session(self, session_id: str) -> list[TraceEvent]:
        """单会话全量事件（跨 trace），按 ``ts`` 升序、同刻按 ``(trace_id, event_seq)`` 稳定。

        WS-sender（T-06）建连 / 重连首推 ``graph_static`` 后据本方法按序回放该会话已落库
        事件到最新，再接 :meth:`subscribe` live 尾随（ADR-0023：重连按 ``event_seq`` 回放）。
        一个会话同时刻只有一个活跃 trace（锁 + pause_meta 共保），故跨 trace 序仅作历史
        回放；前端按 per-trace ``event_seq`` 过滤乱序 / 滞后（§6.5）。
        """
        ...

    @abstractmethod
    async def subscribe(self, session_id: str) -> TraceSubscription:
        """注册该会话的 live 尾随订阅：新事件落库即经 :meth:`append` 通知，本订阅按序 yield。

        返回 :class:`TraceSubscription`（async iterator）。**必须先 ``LISTEN`` / 注册再返回**——
        否则订阅建连前已 ``NOTIFY`` 的事件会丢失（PG 须在 ``subscribe`` 内开专用连接并 ``LISTEN``，
        再交回迭代器）。WS-sender 回放完毕后 ``async for`` 本订阅接 live，断开 / 被新连接取代时调
        ``close()`` 释放（Postgres 释放 LISTEN 专用连接）。
        订阅者**不反压**落库——``append`` 只 ``put_nowait`` / ``NOTIFY``，落库已完成、不阻塞调用方。
        """
        ...


class TraceSubscription(AsyncIterator[TraceEvent], ABC):
    """单会话 ``trace_events`` live 尾随订阅（async iterator）。

    WS-sender（T-06）据 ``event_seq`` 回放完历史后 ``async for`` 本订阅接 live 新事件；
    连接断开或被同会话新连接取代时调 :meth:`close` 释放底层资源（Postgres 的 LISTEN 专用连接）。

    订阅 yield 已落库的 :class:`TraceEvent`（与回放源同源同词汇，ADR-0023）；WS-sender 侧以
    per-trace ``event_seq`` 去重（回放 / live 交界竞态），保证不重发。
    """

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[TraceEvent]:
        ...

    @abstractmethod
    async def __anext__(self) -> TraceEvent:
        ...

    @abstractmethod
    async def close(self) -> None:
        """释放订阅底层资源（Postgres LISTEN 连接）。幂等。"""
        ...


# --------------------------------------------------------------------------- #
# 内存实现（单测 adapter）
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryTraceEventStore(TraceEventStoreBase):
    """进程内 list 实现，供翻译层逻辑单测无需 Postgres。

    语义与 :class:`PostgresTraceEventStore` 一致（PK 唯一、查询按序），仅持久化换内存。
    """

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._rows: list[TraceEvent] = []
        # session_id → 活跃订阅者队列（进程内 pub-sub）。
        self._subs: dict[str, list[asyncio.Queue[TraceEvent]]] = {}

    async def setup(self) -> None:
        return None

    async def append(self, event: TraceEvent) -> None:
        self._rows.append(event)
        # 进程内 pub-sub：非阻塞唤醒该会话所有 live 订阅者（ADR-0023：不反压 append 调用方）。
        for q in self._subs.get(event.session_id, ()):
            q.put_nowait(event)

    async def max_seq(self, trace_id: str) -> int:
        seqs = [r.event_seq for r in self._rows if r.trace_id == trace_id]
        return max(seqs) if seqs else -1

    async def events_for_trace(self, trace_id: str) -> list[TraceEvent]:
        return sorted(
            (r for r in self._rows if r.trace_id == trace_id),
            key=lambda r: r.event_seq,
        )

    async def events_for_session(self, session_id: str) -> list[TraceEvent]:
        return sorted(
            (r for r in self._rows if r.session_id == session_id),
            key=lambda r: (r.ts, r.trace_id, r.event_seq),
        )

    async def node_instance_counts(self, trace_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._rows:
            if r.trace_id == trace_id and r.event_type is EventType.NODE_START:
                nid = str(r.payload.get("node_id", ""))
                counts[nid] = counts.get(nid, 0) + 1
        return counts

    async def subscribe(self, session_id: str) -> TraceSubscription:
        return _InMemorySubscription(self, session_id)


# --------------------------------------------------------------------------- #
# Postgres 实现（生产 adapter）
# --------------------------------------------------------------------------- #


class _InMemorySubscription(TraceSubscription):
    """InMemory live 尾随：一个 ``asyncio.Queue``，``append`` 即 ``put_nowait`` 入队。

    ``close`` 从 store 订阅者登记册移除（断开后不再入队、队列可被 GC）；幂等。
    """

    def __init__(self, store: InMemoryTraceEventStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._queue: asyncio.Queue[TraceEvent] = asyncio.Queue()
        self._closed = False
        store._subs.setdefault(session_id, []).append(self._queue)

    def __aiter__(self) -> AsyncIterator[TraceEvent]:
        return self

    async def __anext__(self) -> TraceEvent:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def close(self) -> None:
        self._closed = True
        subs = self._store._subs.get(self._session_id)
        if subs is not None and self._queue in subs:
            subs.remove(self._queue)
            if not subs:
                self._store._subs.pop(self._session_id, None)


def _load_schema_sql() -> str:
    """从包内 :file:`trace_events.sql` 读建表 SQL（与实现同包、不漂移）。"""

    ref = resources.files("api_layer").joinpath("trace_events.sql")
    return ref.read_text(encoding="utf-8")


SCHEMA_SQL: str = _load_schema_sql()
"""``trace_events`` 建表 SQL（幂等 ``CREATE TABLE IF NOT EXISTS``）。"""


class PostgresTraceEventStore(TraceEventStoreBase):
    """``psycopg`` async 连接池实现的 ``trace_events`` 持久日志（ADR-0022：同一 Postgres）。

    自持 :class:`psycopg_pool.AsyncConnectionPool`（与 :class:`AsyncPostgresSaver` /
    :class:`api_layer.session_cache.PostgresSessionCache` 各自管连接、共用同一 DSN）。
    ``setup()`` 幂等执行 :data:`SCHEMA_SQL`。连接生命周期由 ``async with`` 承载，与
    :class:`api_layer.session_cache.PostgresSessionCache` 同形——调用方在作用域内持有 store
    期间服务请求（PRD §10.3 单例）。
    """

    def __init__(
        self,
        conn_string: str,
        *,
        pool: Any | None = None,
    ) -> None:
        from psycopg.types.json import Json
        from psycopg_pool import AsyncConnectionPool

        self._conn_string = conn_string
        self._Json = Json
        self._pool: Any = pool or AsyncConnectionPool(
            conninfo=conn_string, min_size=1, max_size=8, open=False
        )

    async def __aenter__(self) -> PostgresTraceEventStore:
        await self._pool.open()
        await self.setup()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._pool.close()

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)

    async def append(self, event: TraceEvent) -> None:
        # INSERT 先独立提交——durable 行不受后续 NOTIFY 失败影响（ADR-0023：trace_events
        # 是 durable 回放源，display 副信道失败不可丢已落库行）。
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO trace_events (session_id, trace_id, event_seq, event_type, payload, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    event.session_id,
                    event.trace_id,
                    event.event_seq,
                    event.event_type.value,
                    self._Json(event.payload),
                    event.ts,
                ),
            )
        # INSERT 已提交；NOTIFY 单独发，失败仅记错、不回滚 durable 行。
        # 订阅者下次 LISTEN 唤醒或重连回放会补拉此行（ADR-0023：重连按 event_seq 回放）。
        # NOTIFY 在自身事务提交时送达——此时 INSERT 早已可见，订阅者唤醒查询必得该行。
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "SELECT pg_notify(%s, %s)",
                    (self.NOTIFY_CHANNEL, event.session_id),
                )
        except Exception:
            _logger.warning(
                "trace_events NOTIFY 失败（行已落库，订阅者重连回放补齐）",
                exc_info=True,
            )

    async def max_seq(self, trace_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT coalesce(max(event_seq), -1) FROM trace_events WHERE trace_id = %s",
                (trace_id,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row is not None else -1

    async def events_for_trace(self, trace_id: str) -> list[TraceEvent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, trace_id, event_seq, event_type, payload, ts "
                "FROM trace_events WHERE trace_id = %s ORDER BY event_seq",
                (trace_id,),
            )
            rows = await cur.fetchall()
        return [
            TraceEvent(
                session_id=str(r[0]),
                trace_id=str(r[1]),
                event_seq=int(r[2]),
                event_type=EventType(str(r[3])),
                payload=dict(r[4]) if isinstance(r[4], dict) else {},
                ts=r[5],
            )
            for r in rows
        ]

    async def node_instance_counts(self, trace_id: str) -> dict[str, int]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT payload->>'node_id' AS nid, count(*) "
                "FROM trace_events "
                "WHERE trace_id = %s AND event_type = 'node_start' "
                "GROUP BY nid",
                (trace_id,),
            )
            rows = await cur.fetchall()
        return {str(r[0]): int(r[1]) for r in rows if r[0] is not None}

    async def events_for_session(self, session_id: str) -> list[TraceEvent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, trace_id, event_seq, event_type, payload, ts "
                "FROM trace_events WHERE session_id = %s "
                "ORDER BY ts ASC, trace_id ASC, event_seq ASC",
                (session_id,),
            )
            rows = await cur.fetchall()
        return [
            TraceEvent(
                session_id=str(r[0]),
                trace_id=str(r[1]),
                event_seq=int(r[2]),
                event_type=EventType(str(r[3])),
                payload=dict(r[4]) if isinstance(r[4], dict) else {},
                ts=r[5],
            )
            for r in rows
        ]

    #: ``LISTEN/NOTIFY`` 通道名（一期单实例：所有会话事件同通道，订阅者按 payload=session_id 过滤）。
    NOTIFY_CHANNEL: str = "trace_events"

    async def subscribe(self, session_id: str) -> TraceSubscription:
        """开专用连接 ``LISTEN trace_events`` 后返回迭代器（先 LISTEN 再返回，不漏 NOTIFY）。"""

        sub = _PostgresSubscription(self, self._conn_string, session_id)
        await sub._open()  # noqa: SLF001 — 建连 + LISTEN 必须在返回前完成
        return sub


class _PostgresSubscription(TraceSubscription):
    """Postgres live 尾随：专用连接 ``LISTEN trace_events``，收到 ``NOTIFY`` 即拉取该会话新行。

    一期单实例：所有会话事件同通道，订阅者按 ``Notify.payload == session_id`` 过滤（PRD §4.4
    跨实例扇出属二期）。专用连接 ``autocommit=True``（``LISTEN`` 须事务外），``notifies()``
    async generator 阻塞等待；每条 notify 触发一次 :meth:`PostgresTraceEventStore.events_for_session`
    拉取，按 per-trace ``event_seq`` 游标筛新、按序入缓冲队列 yield。

    ``close`` 关闭专用 LISTEN 连接（断开即停收 notify）；幂等。落库（``append`` + ``NOTIFY``）
    不等待订阅者——订阅者拉取的是已 durable 的行（ADR-0023 不变量）。
    """

    def __init__(
        self, store: PostgresTraceEventStore, conn_string: str, session_id: str
    ) -> None:
        self._store = store
        self._conn_string = conn_string
        self._session_id = session_id
        self._conn: Any | None = None
        self._notifies: Any | None = None
        self._buf: deque[TraceEvent] = deque()
        self._cursor: dict[str, int] = {}
        self._closed = False

    def __aiter__(self) -> AsyncIterator[TraceEvent]:
        return self

    async def _open(self) -> None:
        """建专用连接（autocommit）并 ``LISTEN``——必须在 ``subscribe`` 返回前完成，不漏 NOTIFY。

        随即 seed per-trace 游标为当前该会话已落库的 ``max(event_seq)``：subscribe 时刻已存在
        的事件属**回放**（WS-sender 经 :meth:`events_for_session` 直发），本 live 订阅只 yield
        subscribe 之后新落库的行。回放 / live 交界竞态由 WS-sender 侧 per-trace 去重兜底。
        """

        import psycopg

        self._conn = await psycopg.AsyncConnection.connect(
            self._conn_string, autocommit=True
        )
        await self._conn.execute(f"LISTEN {self._store.NOTIFY_CHANNEL}")
        self._notifies = self._conn.notifies()
        rows = await self._store.events_for_session(self._session_id)
        for r in rows:
            cur = self._cursor.get(r.trace_id, -1)
            if r.event_seq > cur:
                self._cursor[r.trace_id] = r.event_seq

    async def __anext__(self) -> TraceEvent:
        if self._closed or self._notifies is None:
            raise StopAsyncIteration
        while not self._buf:
            notify = await self._notifies.__anext__()
            if getattr(notify, "payload", "") != self._session_id:
                continue
            await self._refill()
        return self._buf.popleft()

    async def _refill(self) -> None:
        """拉取该会话全量行，按 per-trace 游标筛新入缓冲、推进游标。

        一期单会话历史小（一锁一 trace），全量拉取 + 内存过滤可接受；大历史窗口化拉取属二期优化。
        """

        rows = await self._store.events_for_session(self._session_id)
        fresh = [
            r
            for r in rows
            if r.event_seq > self._cursor.get(r.trace_id, -1)
        ]
        fresh.sort(key=lambda r: (r.trace_id, r.event_seq))
        self._buf.extend(fresh)
        for r in rows:
            cur = self._cursor.get(r.trace_id, -1)
            if r.event_seq > cur:
                self._cursor[r.trace_id] = r.event_seq

    async def close(self) -> None:
        self._closed = True
        conn = self._conn
        self._conn = None
        self._notifies = None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
