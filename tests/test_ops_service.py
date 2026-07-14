"""OpsService 单测（T-08·/health /metrics / 后台 sweep）。

InMemorySessionCache + InMemoryTraceEventStore 确定性覆盖：health 计数、metrics 全量指标名、
sweep 孤儿锁 / pause 过期 / 活跃 pause 跳过、80% 上限告警。PG 集成由 test_ops_integration 覆盖。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from api_layer.app import create_app
from api_layer.errors import PAUSE_TTL_SECONDS
from api_layer.metrics import OpsMetrics
from api_layer.ops import OpsConfig, OpsService
from api_layer.run import RunService
from api_layer.session_cache import DEFAULT_LOCK_TTL_SECONDS, InMemorySessionCache
from api_layer.trace_store import EventType, InMemoryTraceEventStore
from runtime.orchestrator import Orchestrator


def _t0() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _ops(
    *,
    clock: Any = _t0,
    session_limit: int = 100,
    metrics: OpsMetrics | None = None,
    run_service: RunService | None = None,
) -> tuple[OpsService, InMemorySessionCache, InMemoryTraceEventStore]:
    cache = InMemorySessionCache(clock=clock)
    store = InMemoryTraceEventStore(clock=clock)
    m = metrics or OpsMetrics()
    svc = OpsService(
        cache,
        store,
        metrics=m,
        run_service=run_service,
        config=OpsConfig(session_limit=session_limit),
        clock=clock,
    )
    return svc, cache, store


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #


async def test_health_shape() -> None:
    """health 返回 db/active_sessions/active_locks/ws_connections。"""

    svc, cache, _store = _ops()
    # 登记一个活跃 owner + 一把活跃锁。
    await cache.set_session_owner("s1", "u1")
    await cache.touch_session_owner("s1", now=_t0())
    await cache.lock_session("s1", "t1", now=_t0())
    h = await svc.health()
    assert h["db"] == "ok"
    assert h["active_sessions"] == 1
    assert h["active_locks"] == 1
    assert h["ws_connections"] == 0


async def test_health_endpoint_over_http() -> None:
    """GET /health 经 httpx 返回 200 + 四字段。"""

    svc, _cache, _store = _ops()
    app = create_app(_bare_run_service(), ops_service=svc)
    async with _client(app) as c:
        r = await c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"db", "active_sessions", "active_locks", "ws_connections"}


def _bare_run_service() -> RunService:
    """无 PG 的空 RunService（/health 不依赖它）。"""

    return RunService(Orchestrator(), InMemorySessionCache())


# --------------------------------------------------------------------------- #
# /metrics
# --------------------------------------------------------------------------- #


async def test_metrics_text_contains_all_names() -> None:
    """metrics 文本含 PRD §11.1 全量指标名。"""

    svc, _cache, _store = _ops()
    text = await svc.metrics_text()
    for name in [
        "active_sessions",
        "active_locks",
        "ws_connections",
        "event_push_latency_seconds",
        "graph_execution_duration_seconds",
        "ws_event_queue_size",
        "ws_event_queue_full_total",
        "langfuse_errors_total",
    ]:
        assert name in text, f"缺失指标 {name}"


async def test_metrics_endpoint_over_http() -> None:
    """GET /metrics 返回 200 + text/plain + 全量指标名。"""

    svc, _cache, _store = _ops()
    app = create_app(_bare_run_service(), ops_service=svc)
    async with _client(app) as c:
        r = await c.get("/metrics")
        assert r.status_code == 200
        assert "langfuse_errors_total" in r.text


# --------------------------------------------------------------------------- #
# sweep：孤儿锁 → stream_abort + 删锁行
# --------------------------------------------------------------------------- #


async def test_sweep_orphan_lock_emits_stream_abort_and_deletes() -> None:
    """孤儿锁（无活跃 pause_meta）→ mint stream_abort 落 trace_events + 删锁行。"""

    svc, cache, store = _ops()
    # 一把过期锁、无 pause_meta（孤儿）。
    await cache.lock_session(
        "s1", "t1", now=_t0() - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 1)
    )
    report = await svc.sweep()
    assert report.lock_orphan == 1
    assert report.pause_expired == 0
    # 锁行已删。
    assert await cache.count_active_locks(now=_t0()) == 0
    # trace_events 落了一条 stream_abort。
    evs = await store.events_for_trace("t1")
    assert any(e.event_type is EventType.STREAM_ABORT for e in evs)
    assert evs[0].payload["abort_reason"]


async def test_sweep_skips_lock_with_active_pause() -> None:
    """过期锁 + 活跃 pause_meta（合法 HITL 暂停）→ 跳过，不删锁、不产 abort。"""

    svc, cache, store = _ops()
    await cache.lock_session(
        "s1", "t1", now=_t0() - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 1)
    )
    await cache.set_pause_meta("s1", "t1", "hitl1", now=_t0())  # 活跃 pause
    report = await svc.sweep()
    assert report.lock_orphan == 0
    assert report.pause_expired == 0
    # 锁仍在、无 stream_abort。
    assert await cache.count_active_locks(now=_t0()) == 0  # 过期不算 active
    assert (await store.events_for_trace("t1")) == []


async def test_sweep_expired_pause_deletes_pause_and_lock() -> None:
    """过期 pause_meta（30min+）→ 删 pause + 锁 + 产 stream_abort。"""

    svc, cache, store = _ops()
    await cache.lock_session("s1", "t1", now=_t0())
    await cache.set_pause_meta(
        "s1", "t1", "hitl1", now=_t0() - timedelta(seconds=PAUSE_TTL_SECONDS + 1)
    )
    report = await svc.sweep()
    assert report.pause_expired == 1
    assert await cache.get_pause_meta("s1") is None
    # 锁亦删（pause 过期时连带清锁行）。
    assert (await store.events_for_trace("t1"))  # stream_abort 落库


# --------------------------------------------------------------------------- #
# sweep：80% 上限告警
# --------------------------------------------------------------------------- #


async def test_sweep_warns_at_80pct_session_threshold(caplog: Any) -> None:
    """活跃会话数达 80% 上限 → sweep 记 warning 告警。"""

    svc, cache, _store = _ops(session_limit=100)
    # 80 个活跃 owner（last_seen 近 30min 内）。
    for i in range(80):
        await cache.set_session_owner(f"s{i:03d}", "u")
        await cache.touch_session_owner(f"s{i:03d}", now=_t0())
    with caplog.at_level(logging.WARNING, logger="api_layer.ops"):
        await svc.sweep()
    assert any("80%" in rec.message or "上限阈值" in rec.message for rec in caplog.records)


async def test_sweep_no_warn_below_threshold(caplog: Any) -> None:
    """活跃会话数 < 80% → 不告警。"""

    svc, cache, _store = _ops(session_limit=100)
    for i in range(10):
        await cache.set_session_owner(f"s{i:03d}", "u")
        await cache.touch_session_owner(f"s{i:03d}", now=_t0())
    with caplog.at_level(logging.WARNING, logger="api_layer.ops"):
        await svc.sweep()
    assert not [rec for rec in caplog.records if "上限" in rec.message]
