"""会话级 side-metadata 缓存抽象 + 内存 / Postgres 双实现（T-04·ADR-0022 / ADR-0024）。

PRD §4.1 ``SessionCacheBase`` 落地：承载 ``pause_meta`` / ``session_owner`` /
``session_locks`` 三类 side metadata。**不含** ``get_state`` / ``save_state``——state 是
:class:`AsyncPostgresSaver` checkpointer 的契约（ADR-0022），side 表只管控制面元数据。

两个 adapter 使本 seam 成真 seam（deep-module 原则）：

- :class:`InMemorySessionCache`：进程内 dict + 可注入时钟，供 RunService 逻辑单测
  （参数互斥 / fresh-resume 判定 / 错误码映射）**无需 Postgres** 即可跑。
- :class:`PostgresSessionCache`：``psycopg`` async 连接池，side 表落同一 Postgres（ADR-0022
  「一期无需 Redis」）。``setup()`` 幂等执行 :data:`SCHEMA_SQL`（CREATE TABLE IF NOT EXISTS）。

惰性清理（T-04 范围）：:meth:`SessionCacheBase.clean_idle` 删过期锁 / pause_meta；
请求路径上命中过期由 :class:`api_layer.run.RunService` 判定返回 ``PAUSE_EXPIRED`` /
``LOCK_EXIST``。后台 sweep 扫孤儿属 T-08 运维加固。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from api_layer.errors import PAUSE_TTL_SECONDS

__all__ = [
    "PauseMeta",
    "LockInfo",
    "SessionCacheBase",
    "InMemorySessionCache",
    "PostgresSessionCache",
    "DEFAULT_LOCK_TTL_SECONDS",
    "OWNER_ACTIVE_WINDOW_SECONDS",
]

#: ``session_locks`` 默认 TTL（秒）。孤儿 run 由 ``last_heartbeat + ttl`` 兜底（T-08 sweep）。
DEFAULT_LOCK_TTL_SECONDS: int = 900

#: ``session_owner`` 活跃窗口：``last_seen`` 近 30min 计活跃会话数（PRD §9.7 / §3.2）。
OWNER_ACTIVE_WINDOW_SECONDS: int = 30 * 60


@dataclass(frozen=True)
class PauseMeta:
    """HITL 暂停点 side metadata（``pause_meta`` 表行）。

    :attr:`trace_id` 为该次修订执行链路 ID（fresh 时 mint、resume 复用，ADR-0022）；
    :attr:`node_id` 为暂停的图节点名（``hitl1`` / ``hitl2``）；:attr:`pause_time` 为暂停时刻
    （tz-aware），超 :data:`PAUSE_TTL_SECONDS` 即 ``PAUSE_EXPIRED``。
    """

    session_id: str
    trace_id: str
    node_id: str
    pause_time: datetime


@dataclass(frozen=True)
class LockInfo:
    """``session_locks`` 行的扫描视图（T-08 sweep）：``session_id`` + ``trace_id``。"""

    session_id: str
    trace_id: str


class SessionCacheBase(ABC):
    """会话级 side-metadata 缓存 seam（PRD §4.1）。

    所有方法为 async（Postgres 实现为 async I/O；内存实现亦 async 保接口同形）。
    cache 自身**不抛** :class:`api_layer.errors.ApiError`——只返回数据 / 布尔，错误码判定
    由 :class:`api_layer.run.RunService` 据返回值做（保持 cache 纯数据、可单测）。
    """

    @abstractmethod
    async def setup(self) -> None:
        """幂等初始化（建表 / 清空无关）。PG 实现执行 :data:`SCHEMA_SQL`。"""
        ...

    # ------------------------------------------------------------------ #
    # pause_meta
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def get_pause_meta(self, session_id: str) -> PauseMeta | None:
        """取活跃 pause_meta；无则 ``None``（过期与否由调用方据 :data:`PAUSE_TTL_SECONDS` 判）。"""
        ...

    @abstractmethod
    async def set_pause_meta(
        self, session_id: str, trace_id: str, node_id: str, *, now: datetime | None = None
    ) -> PauseMeta:
        """upsert pause_meta（fresh 到达 interrupt / resume 再暂停时写；复用 trace_id）。"""
        ...

    @abstractmethod
    async def delete_pause_meta(self, session_id: str) -> None:
        """删 pause_meta（终态 / 过期清理）。"""
        ...

    # ------------------------------------------------------------------ #
    # session_owner
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def get_session_owner(self, session_id: str) -> str | None:
        """取已登记 user_id；未登记 → ``None``（首见登记由 :meth:`set_session_owner`）。"""
        ...

    @abstractmethod
    async def set_session_owner(self, session_id: str, user_id: str) -> None:
        """登记会话所有权（``INSERT ... ON CONFLICT DO NOTHING``；首见绑定，不覆盖）。"""
        ...

    @abstractmethod
    async def touch_session_owner(self, session_id: str, *, now: datetime | None = None) -> None:
        """更新 ``last_seen``（每次请求 touch，驱动活跃计数与淘汰）。"""
        ...

    @abstractmethod
    async def get_active_count(self, *, now: datetime | None = None) -> int:
        """活跃会话数 = ``session_owner.last_seen`` 近 :data:`OWNER_ACTIVE_WINDOW_SECONDS` 计数。"""
        ...

    # ------------------------------------------------------------------ #
    # session_locks
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def lock_session(
        self,
        session_id: str,
        trace_id: str,
        *,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """尝试获取执行锁（fresh query 路径）。

        ``INSERT ... ON CONFLICT DO NOTHING``：插入成功 → ``True``（获锁）；冲突 → 查过期，
        过期则接管（UPDATE trace_id / heartbeat）→ ``True``；未过期 → ``False``（``LOCK_EXIST``）。
        """
        ...

    @abstractmethod
    async def heartbeat_lock(
        self, session_id: str, trace_id: str, *, now: datetime | None = None
    ) -> None:
        """续跑路径更新 ``last_heartbeat``（HITL 暂停期行留存、不重 INSERT，故 touch 保 TTL）。"""
        ...

    @abstractmethod
    async def unlock_session(self, session_id: str) -> None:
        """删锁行（终态 ``stream_finish`` / abort）。无行亦不抛。"""
        ...

    @abstractmethod
    async def clean_idle(self, *, now: datetime | None = None) -> int:
        """惰性清理：删过期锁（``last_heartbeat + ttl < now``）+ 过期 pause_meta。返回清理条数。"""
        ...

    # ------------------------------------------------------------------ #
    # 主动扫描 / 计数（T-08·sweep + /health）
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def list_expired_locks(self, *, now: datetime | None = None) -> list[LockInfo]:
        """枚举过期锁行（``last_heartbeat + ttl < now``），返回 ``(session_id, trace_id)``。

        供 T-08 sweep：对每行查 pause_meta 判孤儿 vs 合法 HITL 暂停（cache 不耦合该判定）。
        """
        ...

    @abstractmethod
    async def list_expired_pauses(self, *, now: datetime | None = None) -> list[PauseMeta]:
        """枚举过期 pause_meta（``pause_time + PAUSE_TTL_SECONDS < now``）。"""
        ...

    @abstractmethod
    async def count_active_locks(self, *, now: datetime | None = None) -> int:
        """活跃锁数（``last_heartbeat + ttl >= now``），供 /health。"""
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """底层可达性探活（PG 实现发 ``SELECT 1``）；不可达 → ``False``。"""
        ...


# --------------------------------------------------------------------------- #
# 内存实现（单测 adapter）
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class InMemorySessionCache(SessionCacheBase):
    """进程内 dict 实现 + 可注入时钟，供 RunService 逻辑单测无需 Postgres。

    语义与 :class:`PostgresSessionCache` 一致（OCND / TTL 接管 / 活跃窗口），仅持久化换内存。
    时钟经 ``clock`` 注入以测过期分支（``PAUSE_EXPIRED`` / ``LOCK_EXIST``）。
    """

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._pauses: dict[str, PauseMeta] = {}
        self._owners: dict[str, tuple[str, datetime]] = {}  # sid -> (user_id, last_seen)
        self._locks: dict[str, tuple[str, datetime, datetime, int]] = {}  # sid -> (trace, acquired, hb, ttl)

    async def setup(self) -> None:
        return None

    async def get_pause_meta(self, session_id: str) -> PauseMeta | None:
        return self._pauses.get(session_id)

    async def set_pause_meta(
        self, session_id: str, trace_id: str, node_id: str, *, now: datetime | None = None
    ) -> PauseMeta:
        meta = PauseMeta(
            session_id=session_id,
            trace_id=trace_id,
            node_id=node_id,
            pause_time=now or self._clock(),
        )
        self._pauses[session_id] = meta
        return meta

    async def delete_pause_meta(self, session_id: str) -> None:
        self._pauses.pop(session_id, None)

    async def get_session_owner(self, session_id: str) -> str | None:
        entry = self._owners.get(session_id)
        return entry[0] if entry is not None else None

    async def set_session_owner(self, session_id: str, user_id: str) -> None:
        if session_id not in self._owners:
            self._owners[session_id] = (user_id, self._clock())

    async def touch_session_owner(self, session_id: str, *, now: datetime | None = None) -> None:
        entry = self._owners.get(session_id)
        if entry is not None:
            self._owners[session_id] = (entry[0], now or self._clock())

    async def get_active_count(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        cutoff = moment - timedelta(seconds=OWNER_ACTIVE_WINDOW_SECONDS)
        return sum(1 for _, (_, last_seen) in self._owners.items() if last_seen >= cutoff)

    async def lock_session(
        self,
        session_id: str,
        trace_id: str,
        *,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        moment = now or self._clock()
        existing = self._locks.get(session_id)
        if existing is None:
            self._locks[session_id] = (trace_id, moment, moment, ttl_seconds)
            return True
        _trace, _acq, hb, ttl = existing
        if hb + timedelta(seconds=ttl) < moment:
            # 过期接管：孤儿锁，T-08 会 sweep；本路径惰性回收。
            self._locks[session_id] = (trace_id, moment, moment, ttl_seconds)
            return True
        return False

    async def heartbeat_lock(
        self, session_id: str, trace_id: str, *, now: datetime | None = None
    ) -> None:
        existing = self._locks.get(session_id)
        if existing is None:
            # 锁行丢失（异常路径，如前次 abort 删行）：best-effort 重建。
            moment = now or self._clock()
            self._locks[session_id] = (trace_id, moment, moment, DEFAULT_LOCK_TTL_SECONDS)
            return
        _trace, acq, _hb, ttl = existing
        self._locks[session_id] = (trace_id, acq, now or self._clock(), ttl)

    async def unlock_session(self, session_id: str) -> None:
        self._locks.pop(session_id, None)

    async def clean_idle(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        n = 0
        for sid in [s for s, (_t, _a, hb, ttl) in self._locks.items() if hb + timedelta(seconds=ttl) < moment]:
            self._locks.pop(sid, None)
            n += 1
        for sid in [s for s, m in self._pauses.items() if m.pause_time + timedelta(seconds=PAUSE_TTL_SECONDS) < moment]:
            self._pauses.pop(sid, None)
            n += 1
        return n

    async def list_expired_locks(self, *, now: datetime | None = None) -> list[LockInfo]:
        moment = now or self._clock()
        return [
            LockInfo(session_id=sid, trace_id=trace)
            for sid, (trace, _acq, hb, ttl) in self._locks.items()
            if hb + timedelta(seconds=ttl) < moment
        ]

    async def list_expired_pauses(self, *, now: datetime | None = None) -> list[PauseMeta]:
        moment = now or self._clock()
        return [
            m
            for m in self._pauses.values()
            if m.pause_time + timedelta(seconds=PAUSE_TTL_SECONDS) < moment
        ]

    async def count_active_locks(self, *, now: datetime | None = None) -> int:
        moment = now or self._clock()
        return sum(
            1
            for _trace, _acq, hb, ttl in self._locks.values()
            if hb + timedelta(seconds=ttl) >= moment
        )

    async def ping(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Postgres 实现（生产 adapter）
# --------------------------------------------------------------------------- #


def _load_schema_sql() -> str:
    """从包内 :file:`schema.sql` 读建表 SQL（与实现同包、不漂移）。"""

    ref = resources.files("api_layer").joinpath("schema.sql")
    return ref.read_text(encoding="utf-8")


SCHEMA_SQL: str = _load_schema_sql()
"""side-meta 三表建表 SQL（幂等 ``CREATE TABLE IF NOT EXISTS``）。"""


class PostgresSessionCache(SessionCacheBase):
    """``psycopg`` async 连接池实现的 side-metadata 缓存（ADR-0022：同一 Postgres）。

    自持 :class:`psycopg_pool.AsyncConnectionPool`（与 :class:`AsyncPostgresSaver` 各自管连接、
    共用同一 DSN）。``setup()`` 幂等执行 :data:`SCHEMA_SQL`。连接生命周期由 ``async with``
    承载（``__aenter__`` / ``__aexit__`` 开池 / 关池），与 :func:`runtime.checkpoint.build_async_checkpointer`
    同形——调用方在作用域内持有 cache 期间服务请求（PRD §10.3 单例）。
    """

    def __init__(
        self,
        conn_string: str,
        *,
        pool: Any | None = None,
    ) -> None:
        # 延迟 import：psycopg_pool 为运行时依赖，模块导入不强制（供 mypy / 离线 import 安全）。
        from psycopg_pool import AsyncConnectionPool

        self._conn_string = conn_string
        self._pool: Any = pool or AsyncConnectionPool(
            conninfo=conn_string, min_size=1, max_size=8, open=False
        )

    async def __aenter__(self) -> PostgresSessionCache:
        await self._pool.open()
        await self.setup()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._pool.close()

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)

    async def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def get_pause_meta(self, session_id: str) -> PauseMeta | None:
        row = await self._fetchone(
            "SELECT session_id, trace_id, node_id, pause_time FROM pause_meta WHERE session_id = %s",
            (session_id,),
        )
        if row is None:
            return None
        sid, trace, node, pause_time = row
        return PauseMeta(
            session_id=sid, trace_id=trace, node_id=node, pause_time=pause_time
        )

    async def set_pause_meta(
        self, session_id: str, trace_id: str, node_id: str, *, now: datetime | None = None
    ) -> PauseMeta:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pause_meta (session_id, trace_id, node_id, pause_time) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (session_id) DO UPDATE SET trace_id = EXCLUDED.trace_id, "
                "node_id = EXCLUDED.node_id, pause_time = EXCLUDED.pause_time",
                (session_id, trace_id, node_id, moment),
            )
        return PauseMeta(
            session_id=session_id, trace_id=trace_id, node_id=node_id, pause_time=moment
        )

    async def delete_pause_meta(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM pause_meta WHERE session_id = %s", (session_id,)
            )

    async def get_session_owner(self, session_id: str) -> str | None:
        row = await self._fetchone(
            "SELECT user_id FROM session_owner WHERE session_id = %s", (session_id,)
        )
        if row is None:
            return None
        return str(row[0])

    async def set_session_owner(self, session_id: str, user_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO session_owner (session_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id, user_id),
            )

    async def touch_session_owner(self, session_id: str, *, now: datetime | None = None) -> None:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE session_owner SET last_seen = %s WHERE session_id = %s",
                (moment, session_id),
            )

    async def get_active_count(self, *, now: datetime | None = None) -> int:
        moment = now or _utcnow()
        cutoff = moment - timedelta(seconds=OWNER_ACTIVE_WINDOW_SECONDS)
        row = await self._fetchone(
            "SELECT count(*) FROM session_owner WHERE last_seen >= %s", (cutoff,)
        )
        if row is None:
            return 0
        return int(row[0])

    async def lock_session(
        self,
        session_id: str,
        trace_id: str,
        *,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        moment = now or _utcnow()
        # INSERT ... ON CONFLICT DO NOTHING：插入成功即获锁。
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO session_locks (session_id, trace_id, acquired_at, last_heartbeat, ttl_seconds) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id, trace_id, moment, moment, ttl_seconds),
            )
            if cur.rowcount == 1:
                return True
            # 冲突：查现有行是否过期；过期则接管，否则 LOCK_EXIST。
            cur = await conn.execute(
                "SELECT last_heartbeat, ttl_seconds FROM session_locks WHERE session_id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            # 极端：刚 INSERT 冲突却查不到——并发已被删；best-effort 重插。
            return await self.lock_session(session_id, trace_id, ttl_seconds=ttl_seconds, now=now)
        hb, ttl = row
        if hb + timedelta(seconds=int(ttl)) < moment:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE session_locks SET trace_id = %s, acquired_at = %s, last_heartbeat = %s, "
                    "ttl_seconds = %s WHERE session_id = %s",
                    (trace_id, moment, moment, ttl_seconds, session_id),
                )
            return True
        return False

    async def heartbeat_lock(
        self, session_id: str, trace_id: str, *, now: datetime | None = None
    ) -> None:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE session_locks SET last_heartbeat = %s, trace_id = %s WHERE session_id = %s",
                (moment, trace_id, session_id),
            )
            if cur.rowcount == 0:
                # 锁行丢失（异常路径）：best-effort 重建。
                await conn.execute(
                    "INSERT INTO session_locks (session_id, trace_id, acquired_at, last_heartbeat) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
                    (session_id, trace_id, moment, moment),
                )

    async def unlock_session(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM session_locks WHERE session_id = %s", (session_id,)
            )

    async def clean_idle(self, *, now: datetime | None = None) -> int:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM session_locks "
                "WHERE last_heartbeat + make_interval(secs => ttl_seconds) < %s",
                (moment,),
            )
            n = cur.rowcount or 0
            cur = await conn.execute(
                "DELETE FROM pause_meta WHERE pause_time + interval '30 minutes' < %s",
                (moment,),
            )
            n += cur.rowcount or 0
        return int(n)

    async def list_expired_locks(self, *, now: datetime | None = None) -> list[LockInfo]:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, trace_id FROM session_locks "
                "WHERE last_heartbeat + make_interval(secs => ttl_seconds) < %s",
                (moment,),
            )
            rows = await cur.fetchall()
        return [LockInfo(session_id=str(r[0]), trace_id=str(r[1])) for r in rows]

    async def list_expired_pauses(self, *, now: datetime | None = None) -> list[PauseMeta]:
        moment = now or _utcnow()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, trace_id, node_id, pause_time FROM pause_meta "
                "WHERE pause_time + interval '30 minutes' < %s",
                (moment,),
            )
            rows = await cur.fetchall()
        return [
            PauseMeta(
                session_id=str(r[0]),
                trace_id=str(r[1]),
                node_id=str(r[2]),
                pause_time=r[3],
            )
            for r in rows
        ]

    async def count_active_locks(self, *, now: datetime | None = None) -> int:
        moment = now or _utcnow()
        row = await self._fetchone(
            "SELECT count(*) FROM session_locks "
            "WHERE last_heartbeat + make_interval(secs => ttl_seconds) >= %s",
            (moment,),
        )
        return int(row[0]) if row is not None else 0

    async def ping(self) -> bool:
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            return False
        return True
