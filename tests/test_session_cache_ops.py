"""SessionCache 主动扫描 / 计数单测（T-08·为 sweep + /health 扩展）。

T-04 落了惰性 ``clean_idle``；T-08 后台 sweep 与 /health 需主动枚举过期锁 / 过期 pause_meta +
活跃锁计数 + ping。InMemory 实现确定性测；Postgres 由集成测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api_layer.errors import PAUSE_TTL_SECONDS
from api_layer.session_cache import (
    DEFAULT_LOCK_TTL_SECONDS,
    InMemorySessionCache,
)


async def test_list_expired_locks_and_count_active() -> None:
    """过期锁枚举 + 活跃锁计数：跨 TTL 边界。"""

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    cache = InMemorySessionCache(clock=lambda: t0)
    # s1 锁刚获（活跃）；s2 锁心跳已超 TTL（孤儿）。
    await cache.lock_session("s1", "t1", now=t0)
    await cache.lock_session("s2", "t2", now=t0 - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 1))

    expired = await cache.list_expired_locks(now=t0)
    expired_sids = {li.session_id for li in expired}
    assert expired_sids == {"s2"}
    assert {li.trace_id for li in expired} == {"t2"}

    assert await cache.count_active_locks(now=t0) == 1


async def test_list_expired_locks_skips_active_pause_partner() -> None:
    """枚举只判锁 TTL；是否跳过由 sweep 据 pause_meta 决定（cache 不耦合 pause 判定）。"""

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    cache = InMemorySessionCache(clock=lambda: t0)
    await cache.lock_session("s1", "t1", now=t0 - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 1))
    await cache.set_pause_meta("s1", "t1", "hitl1", now=t0)  # 活跃 pause_meta
    expired = await cache.list_expired_locks(now=t0)
    assert {li.session_id for li in expired} == {"s1"}  # 仍枚举（sweep 判跳过）


async def test_list_expired_pauses() -> None:
    """过期 pause_meta（pause_time + 30min < now）枚举。"""

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    cache = InMemorySessionCache(clock=lambda: t0)
    await cache.set_pause_meta("s1", "t1", "hitl1", now=t0 - timedelta(seconds=PAUSE_TTL_SECONDS + 1))
    await cache.set_pause_meta("s2", "t2", "hitl2", now=t0)  # 活跃
    expired = await cache.list_expired_pauses(now=t0)
    assert {p.session_id for p in expired} == {"s1"}
    assert expired[0].trace_id == "t1"


async def test_ping_returns_true_for_inmemory() -> None:
    assert await InMemorySessionCache().ping() is True
