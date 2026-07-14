"""PG 集成：运维 sweep + /health + /metrics（T-08·PRD §9 / §11 验收）。

PG 不可达即 skip（与 T-04 / T-05 集成测试同形）。各 test 用唯一 session_id / trace_id 避免共享
PG 跨 test 碰撞（session_owner / session_locks / pause_meta / trace_events 跨 test 持久、不清理）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from api_layer.app import create_app
from api_layer.metrics import OpsMetrics
from api_layer.ops import OpsConfig, OpsService
from api_layer.run import RunService
from api_layer.session_cache import DEFAULT_LOCK_TTL_SECONDS
from api_layer.trace_store import EventType
from runtime.orchestrator import Orchestrator


def _sid(p: str) -> str:
    return f"{p}-{uuid.uuid4()}"


async def test_sweep_orphan_lock_pg(
    pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """PG：孤儿锁（过期 heartbeat、无 pause）→ sweep 推 stream_abort + 删锁行。"""

    sid = _sid("orph")
    trace = f"t-{uuid.uuid4()}"
    now = datetime.now(tz=UTC)
    # 插一把过期锁行（heartbeat 回拨超 TTL），无 pause_meta（孤儿）。
    await pg_session_cache.lock_session(
        sid, trace, now=now - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 5)
    )
    ops = OpsService(pg_session_cache, pg_trace_store, config=OpsConfig())
    report = await ops.sweep()
    assert report.lock_orphan >= 1
    # 本 trace 落了 stream_abort。
    rows = await pg_trace_store.events_for_trace(trace)
    assert any(e.event_type is EventType.STREAM_ABORT for e in rows)
    # 本锁行已删（PG 跨 test 共享，只断言本 sid）。
    async with pg_session_cache._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM session_locks WHERE session_id = %s", (sid,)
        )
        assert await cur.fetchone() is None


async def test_sweep_skips_active_pause_pg(
    pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """PG：过期锁 + 活跃 pause_meta → 跳过（不产 abort、不删锁）。"""

    sid = _sid("skip")
    trace = f"t-{uuid.uuid4()}"
    now = datetime.now(tz=UTC)
    await pg_session_cache.lock_session(
        sid, trace, now=now - timedelta(seconds=DEFAULT_LOCK_TTL_SECONDS + 5)
    )
    await pg_session_cache.set_pause_meta(sid, trace, "hitl1", now=now)  # 活跃 pause
    ops = OpsService(pg_session_cache, pg_trace_store, config=OpsConfig())
    before = len(await pg_trace_store.events_for_trace(trace))
    await ops.sweep()
    after = len(await pg_trace_store.events_for_trace(trace))
    assert before == after  # 活跃 pause → 不产 stream_abort
    # 锁行仍在（未误删）。
    async with pg_session_cache._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM session_locks WHERE session_id = %s", (sid,)
        )
        assert await cur.fetchone() is not None
    # 清理本 test 残留（pause + lock），不依赖后续 pause TTL sweep。
    await pg_session_cache.delete_pause_meta(sid)
    await pg_session_cache.unlock_session(sid)


async def test_health_and_metrics_over_pg(
    pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """PG：GET /health 返回 db=ok + 四字段；GET /metrics 含全量指标名。"""

    ops = OpsService(
        pg_session_cache, pg_trace_store, metrics=OpsMetrics(),
    )
    run_service = RunService(Orchestrator(), pg_session_cache)
    app = create_app(run_service, ops_service=ops)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        h = await c.get("/health")
        assert h.status_code == 200
        body = h.json()
        assert body["db"] == "ok"
        assert set(body) >= {"db", "active_sessions", "active_locks", "ws_connections"}

        m = await c.get("/metrics")
        assert m.status_code == 200
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
            assert name in m.text, f"缺失指标 {name}"
