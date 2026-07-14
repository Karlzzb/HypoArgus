"""HTTP 控制面集成测试（T-04·ADR-0022 / ADR-0024）。

经 ``httpx`` ASGI 传输驱动真实 FastAPI 应用，端到端覆盖 ``/api/agent/run`` 的完整 HITL 流程
（发起 → ``NEED_HUMAN_INPUT`` → 回填 → ``SUCCESS``）与并发 / 跨用户 / 超时 / 重复提交 / 断点过期
场景，以及 ``GET /api/agent/graph`` 形状。图注入 ``InterruptHitl*Gate`` + ``AsyncPostgresSaver``，
side metadata 落共享 Postgres（``pg_checkpointer`` + ``pg_session_cache`` 夹具，PG 不可达即 skip）。

业务纯函数零改动：本测试复用 T-03 的 ``interrupt + PostgresSaver`` 图，仅把 CLI resume 驱动
换成 HTTP resume 驱动。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agents.assembly import create_real_agents
from agents.parser import FakeLlmClient, ParseResult
from api_layer.app import create_app
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _interrupt_agents() -> Any:
    """真实解析 + InterruptHitl*Gate，下游为桩（无触达 → 终稿逐字节原文）。"""

    return create_real_agents(
        llm=FakeLlmClient(result=ParseResult()),  # 空 proposals → 全段 background 影子
        hitl1_gate=InterruptHitl1Gate(),
        hitl2_gate=InterruptHitl2Gate(),
    )


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _build_app(
    *,
    checkpointer: Any,
    session_cache: Any,
    trace_store: Any | None = None,
    agents: Any | None = None,
    config: RunServiceConfig | None = None,
    langfuse_handler: Any | None = None,
    visibility: Any | None = None,
) -> tuple[RunService, Any]:
    orch = Orchestrator(agents=agents or _interrupt_agents(), checkpointer=checkpointer)
    service = RunService(
        orch,
        session_cache,
        trace_store=trace_store,
        langfuse_handler=langfuse_handler,
        visibility=visibility,
        config=config or RunServiceConfig(),
    )
    return service, create_app(service)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- #
# 完整 HITL 流程 over HTTP
# --------------------------------------------------------------------------- #


async def test_full_hitl_flow_over_http(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """fresh → hitl1 NEED_HUMAN_INPUT → resume skip → hitl2 NEED_HUMAN_INPUT →
    resume pass → SUCCESS（终稿逐字节等于原文）。"""

    sid = _sid("full")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        # 1) fresh：发起 → hitl1 暂停。
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改一改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "NEED_HUMAN_INPUT"
        assert body["node_id"] == "hitl1"
        assert body["human_question"]
        assert body["hint"]
        assert body["detail"]  # interrupt payload 序列化
        trace_id = body["trace_id"]
        assert trace_id  # fresh mint

        # 2) resume skip → hitl2 暂停（同 trace_id 复用）。
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "skip"}},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "NEED_HUMAN_INPUT"
        assert body["node_id"] == "hitl2"
        assert body["trace_id"] == trace_id  # resume 复用 trace_id

        # 3) resume pass → SUCCESS（无触达段 → 一键通过 → 终稿逐字节原文）。
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "pass"}},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["final_document"] == _DOC.decode()
        assert body["errors"] == []


# --------------------------------------------------------------------------- #
# 重复提交 → LOCK_EXIST
# --------------------------------------------------------------------------- #


async def test_concurrent_duplicate_submit_is_lock_exist(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """两并发 fresh 请求同 session：一个 NEED_HUMAN_INPUT、一个 409 LOCK_EXIST。"""

    sid = _sid("dup")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        payload = {"session_id": sid, "query": "改", "document": _DOC.decode()}
        headers = {"X-User-Id": "u1"}
        r1, r2 = await asyncio.gather(
            c.post("/api/agent/run", json=payload, headers=headers),
            c.post("/api/agent/run", json=payload, headers=headers),
        )
        statuses = {r1.status_code, r2.status_code}
        assert statuses == {200, 409}, (r1.json(), r2.json())
        loser = r1 if r1.status_code == 409 else r2
        assert loser.json()["error"] == "LOCK_EXIST"


async def test_fresh_while_active_pause_is_lock_exist(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """已有未处理断点（hitl1 暂停）时再发 fresh query → 409 LOCK_EXIST（未处理断点）。"""

    sid = _sid("active")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "再改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 409
        assert r.json()["error"] == "LOCK_EXIST"


# --------------------------------------------------------------------------- #
# 跨用户 → FORBIDDEN
# --------------------------------------------------------------------------- #


async def test_cross_user_access_is_forbidden(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """u1 发起并登记会话；u2 同 session resume → 403 FORBIDDEN。"""

    sid = _sid("xuser")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "skip"}},
            headers={"X-User-Id": "u2"},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "FORBIDDEN"


async def test_missing_user_id_is_forbidden(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """缺 X-User-Id 头 → 403 FORBIDDEN（一期信任 Nginx 注入）。"""

    sid = _sid("nouser")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "FORBIDDEN"


# --------------------------------------------------------------------------- #
# 断点过期 → PAUSE_EXPIRED
# --------------------------------------------------------------------------- #


async def test_pause_expired_is_410(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """fresh → hitl1 暂停；把 pause_meta.pause_time 回拨 31min；resume → 410 PAUSE_EXPIRED。"""

    sid = _sid("expired")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        # 直接把 pause_meta 回拨到 31min 前（模拟断点 30min 超时）。
        old = datetime.now(tz=UTC) - timedelta(minutes=31)
        async with pg_session_cache._pool.connection() as conn:
            await conn.execute(
                "UPDATE pause_meta SET pause_time = %s WHERE session_id = %s", (old, sid)
            )
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "skip"}},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 410
        assert r.json()["error"] == "PAUSE_EXPIRED"


# --------------------------------------------------------------------------- #
# 参数互斥 → PARAM_ERROR
# --------------------------------------------------------------------------- #


async def test_param_error_both_query_and_human_response(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """query 与 human_response 同时给 → 400 PARAM_ERROR（驱动图之前，不触 PG）。"""

    sid = _sid("param")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        r = await c.post(
            "/api/agent/run",
            json={
                "session_id": sid,
                "query": "改",
                "human_response": {"action": "skip"},
                "document": _DOC.decode(),
            },
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "PARAM_ERROR"


async def test_param_error_neither_query_nor_human_response(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """query 与 human_response 都不给 → 400 PARAM_ERROR。"""

    sid = _sid("noparam")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "PARAM_ERROR"


# --------------------------------------------------------------------------- #
# 图执行超时 → GRAPH_TIMEOUT
# --------------------------------------------------------------------------- #


class _SlowGraph:
    """伪图：``ainvoke`` / ``astream_events`` 永久睡眠，触 ``asyncio.wait_for`` 超时。不触 PG / 真图。"""

    next: tuple[str, ...] = ()
    values: dict[str, Any] = {}
    tasks: tuple[Any, ...] = ()

    async def ainvoke(self, *_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(30)

    async def astream_events(self, *_a: Any, **_k: Any) -> Any:
        # async generator：永不 yield（wait_for 在睡眠期取消 → TimeoutError）。
        await asyncio.sleep(30)
        if False:  # pragma: no cover  # noqa: RET503 — 使本函数为 async generator
            yield

    async def aget_state(self, *_a: Any, **_k: Any) -> Any:
        return self


async def test_graph_timeout_is_504() -> None:
    """图执行超过请求超时 → 504 GRAPH_TIMEOUT（锁行清理、不残留）。"""

    sid = _sid("timeout")
    orch = Orchestrator()  # 仅借 _recursion_limit；graph 被替换为伪图。
    orch.graph = _SlowGraph()
    service = RunService(
        orch,
        InMemorySessionCache(),
        config=RunServiceConfig(graph_timeout_seconds=0.2),
    )
    app = create_app(service)
    async with _client(app) as c:
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        assert r.status_code == 504
        assert r.json()["error"] == "GRAPH_TIMEOUT"


# --------------------------------------------------------------------------- #
# /api/agent/graph 形状
# --------------------------------------------------------------------------- #


async def test_graph_endpoint_contains_replay_edge() -> None:
    """GET /api/agent/graph 返回 §5.4 形状，含 hitl1→parse+partition 条件回放边。"""

    orch = Orchestrator()
    service = RunService(orch, InMemorySessionCache())
    app = create_app(service)
    async with _client(app) as c:
        r = await c.get("/api/agent/graph")
        assert r.status_code == 200
        data = r.json()
        ids = [n["id"] for n in data["nodes"]]
        assert "__start__" in ids and "__end__" in ids
        assert "parse+partition" in ids  # 全可见默认（不隐藏）
        assert "hitl1" in ids and "hitl2" in ids
        replay = [
            e for e in data["edges"] if e.get("cond") == "replay"
        ]
        assert replay, "应含 hitl1→parse+partition 条件回放边"
        assert (replay[0]["source"], replay[0]["target"]) == ("hitl1", "parse+partition")
        assert replay[0]["max"] == 3  # DEFAULT_MAX_PARTITION_RETRIES


# --------------------------------------------------------------------------- #
# 业务纯函数零改动回归：终稿逐字节等于原文（中断路径下）
# --------------------------------------------------------------------------- #


async def test_business_pure_functions_unchanged_via_http(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """经 HTTP resume 路径，``confirm`` / ``confirm_partition`` / ``resolve_rewrites`` /
    ``assemble_final_document`` 不改：无人确认 → 终稿逐字节等于原文。"""

    sid = _sid("pure")
    _service, app = _build_app(checkpointer=pg_checkpointer, session_cache=pg_session_cache)
    async with _client(app) as c:
        await c.post(
            "/api/agent/run",
            json={"session_id": sid, "query": "改", "document": _DOC.decode()},
            headers={"X-User-Id": "u1"},
        )
        await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "skip"}},
            headers={"X-User-Id": "u1"},
        )
        r = await c.post(
            "/api/agent/run",
            json={"session_id": sid, "human_response": {"action": "decide"}},
            headers={"X-User-Id": "u1"},
        )
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["final_document"] == _DOC.decode()
