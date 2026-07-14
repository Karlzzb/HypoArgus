"""``trace_events`` 持久日志 store seam（T-05·ADR-0023·PRD §4.2.2）。

翻译层（:mod:`api_layer.translator`）把 ``astream_events`` 词汇映射为 PRD §6.4 事件、
mint ``event_seq``、**非阻塞**写本 seam。本表是显示层的 **durable 回放源**——WS-sender
（T-06）只读尾随；WS 断开不中止 run（ADR-0023 不变量）。

两个 adapter 使本 seam 成真 seam（deep-module 原则，与 :mod:`api_layer.session_cache` 同形）：

- :class:`InMemoryTraceEventStore`：进程内 list，供翻译层逻辑单测（映射 / 非阻塞 / 过滤）
  **无需 Postgres** 即可跑。
- :class:`PostgresTraceEventStore`：``psycopg`` async 连接池，落同一 Postgres（ADR-0022
  「一期无需 Redis」）。``setup()`` 幂等执行 :data:`SCHEMA_SQL`（CREATE TABLE IF NOT EXISTS）。

store 责任仅：落库（:meth:`append`）+ 查询（:meth:`max_seq` / :meth:`events_for_trace`）。
``event_seq`` 的单调 mint 与续跑顺延（``max_seq + 1``）由翻译层负责——store 不重排、
不补号，按调用方给的 ``event_seq`` 原样落库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from typing import Any

__all__ = [
    "EventType",
    "TraceEvent",
    "TraceEventStoreBase",
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

    async def setup(self) -> None:
        return None

    async def append(self, event: TraceEvent) -> None:
        self._rows.append(event)

    async def max_seq(self, trace_id: str) -> int:
        seqs = [r.event_seq for r in self._rows if r.trace_id == trace_id]
        return max(seqs) if seqs else -1

    async def events_for_trace(self, trace_id: str) -> list[TraceEvent]:
        return sorted(
            (r for r in self._rows if r.trace_id == trace_id),
            key=lambda r: r.event_seq,
        )

    async def node_instance_counts(self, trace_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._rows:
            if r.trace_id == trace_id and r.event_type is EventType.NODE_START:
                nid = str(r.payload.get("node_id", ""))
                counts[nid] = counts.get(nid, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# Postgres 实现（生产 adapter）
# --------------------------------------------------------------------------- #


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
