"""``api_layer.session_cache`` 内存实现单测（T-04·ADR-0024）。

验 ``InMemorySessionCache`` 的 side-meta 语义——与 :class:`PostgresSessionCache`
同形契约（OCND 获锁 / TTL 接管 / 活跃窗口 / pause_meta upsert），但无需 Postgres、
可注入时钟测过期分支。这些是 :class:`api_layer.run.RunService` 逻辑判定的地基。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api_layer.errors import PAUSE_TTL_SECONDS
from api_layer.session_cache import (
    DEFAULT_LOCK_TTL_SECONDS,
    OWNER_ACTIVE_WINDOW_SECONDS,
    InMemorySessionCache,
    PauseMeta,
)


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def cache() -> InMemorySessionCache:
    return InMemorySessionCache(clock=_fixed_clock)


# --------------------------------------------------------------------------- #
# pause_meta
# --------------------------------------------------------------------------- #


async def test_pause_meta_upsert_roundtrip(cache: InMemorySessionCache) -> None:
    meta = await cache.set_pause_meta("s1", "t1", "hitl1")
    assert meta == PauseMeta("s1", "t1", "hitl1", _fixed_clock())
    assert await cache.get_pause_meta("s1") == meta

    # upsert：复用 session_id 覆盖 trace / node / pause_time。
    meta2 = await cache.set_pause_meta("s1", "t1", "hitl2")
    assert meta2.node_id == "hitl2"
    assert (await cache.get_pause_meta("s1")).trace_id == "t1"
    await cache.delete_pause_meta("s1")
    assert await cache.get_pause_meta("s1") is None


# --------------------------------------------------------------------------- #
# session_owner
# --------------------------------------------------------------------------- #


async def test_owner_register_once_then_touch(cache: InMemorySessionCache) -> None:
    assert await cache.get_session_owner("s1") is None  # 首见未登记
    await cache.set_session_owner("s1", "u1")  # 登记 + 绑定
    assert await cache.get_session_owner("s1") == "u1"
    # ON CONFLICT DO NOTHING：再次登记不覆盖。
    await cache.set_session_owner("s1", "u2")
    assert await cache.get_session_owner("s1") == "u1"


async def test_active_count_windowed(cache: InMemorySessionCache) -> None:
    await cache.set_session_owner("s1", "u1")
    await cache.set_session_owner("s2", "u2")
    assert await cache.get_active_count() == 2
    # s2 的 last_seen 超出 30min 窗口 → 不计活跃。
    later = _fixed_clock() + timedelta(seconds=OWNER_ACTIVE_WINDOW_SECONDS + 1)
    assert await cache.get_active_count(now=later) == 0


# --------------------------------------------------------------------------- #
# session_locks
# --------------------------------------------------------------------------- #


async def test_lock_acquire_then_conflict_is_false(cache: InMemorySessionCache) -> None:
    assert await cache.lock_session("s1", "t1") is True  # 获锁
    # 同 session 再次获锁（未过期）→ False（LOCK_EXIST 语义）。
    assert await cache.lock_session("s1", "t2") is False


async def test_lock_expired_takeover(cache: InMemorySessionCache) -> None:
    assert await cache.lock_session("s1", "t1") is True
    # TTL 过期后另一 trace 接管 → True。
    expired = _fixed_clock() + timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 1)
    assert await cache.lock_session("s1", "t2", now=expired) is True


async def test_heartbeat_keeps_lock_alive_across_resume(cache: InMemorySessionCache) -> None:
    """续跑路径 touch heartbeat，行留存、不重 INSERT 故不误触 LOCK_EXIST。"""

    assert await cache.lock_session("s1", "t1") is True
    # 暂停期不释放、不 INSERT；只 heartbeat。
    await cache.heartbeat_lock("s1", "t1")
    # 再发 fresh-style 获锁仍 False（行未过期、留存）。
    assert await cache.lock_session("s1", "t1") is False


async def test_unlock_releases_row(cache: InMemorySessionCache) -> None:
    assert await cache.lock_session("s1", "t1") is True
    await cache.unlock_session("s1")
    assert await cache.lock_session("s1", "t2") is True  # 行已删、可重新获锁


async def test_heartbeat_rebuilds_missing_lock_row(cache: InMemorySessionCache) -> None:
    """异常路径：锁行丢失时 heartbeat best-effort 重建（不误触 LOCK_EXIST 的兜底）。"""

    await cache.heartbeat_lock("s1", "t1")  # 无行亦不抛
    # 重建后行存在、未过期 → fresh 获锁 False。
    assert await cache.lock_session("s1", "t1") is False


# --------------------------------------------------------------------------- #
# clean_idle（惰性清理）
# --------------------------------------------------------------------------- #


async def test_clean_idle_purges_expired_locks_and_pauses(cache: InMemorySessionCache) -> None:
    await cache.lock_session("s1", "t1")
    await cache.set_pause_meta("s2", "t2", "hitl1")
    later = _fixed_clock() + timedelta(
        seconds=max(DEFAULT_LOCK_TTL_SECONDS, PAUSE_TTL_SECONDS) + 1
    )
    n = await cache.clean_idle(now=later)
    assert n == 2
    assert await cache.get_pause_meta("s2") is None
