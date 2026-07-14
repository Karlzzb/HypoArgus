"""WS-sender（trace_events 只读尾随）集成测试（T-06·ADR-0023）。

经一个**进程内 ASGI WebSocket 客户端**（``_WsSession``）驱动真实 FastAPI ``/ws/agent/stream``
端点——与测试事件循环同 loop（使 InMemory pub-sub 与 ``pg_checkpointer`` fixture 共存不串 loop，
亦不引真实 socket / 端口，避免 flaky）。端到端覆盖 ADR-0023 不变量：

- 建连带 ``session_id``；归属校验失败 / 跨用户 / 缺 ``X-User-Id`` → close 4001。
- 建连首推 ``graph_static``（来自 :func:`build_graph_view`）。
- live 尾随：新事件落库即经 pub-sub / LISTEN-NOTIFY 按序下发。
- 重连按 ``event_seq`` 回放历史、接 live；无重复（per-trace 去重）。
- 背压：``_BackpressureBuffer`` 满时合并 ``llm_thinking`` token、丢弃非关键、``ws_event_queue_full_total`` 计数。
- 心跳：``heartbeat_interval`` 无数据帧即发 ``heartbeat``（``event_seq=-1``）。
- 同 ``session_id`` 新连接取代旧连接（停 send、**不发** ``stream_abort``）。
- **不变量**：WS 断开不中止 run（resume 仍达终态；重连按 ``event_seq`` 回放补齐、无关键事件丢失）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from agents.assembly import MANIFEST, create_real_agents
from agents.parser import FakeLlmClient, ParseResult
from api_layer.app import create_app
from api_layer.graph_view import VisibilityConfig
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import EventType, InMemoryTraceEventStore, TraceEvent
from api_layer.ws import (
    WS_CLOSE_FORBIDDEN,
    WsMetrics,
    WSSenderConfig,
    WSSenderService,
    _BackpressureBuffer,
)
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _interrupt_agents() -> Any:
    return create_real_agents(
        llm=FakeLlmClient(result=ParseResult()),
        hitl1_gate=InterruptHitl1Gate(),
        hitl2_gate=InterruptHitl2Gate(),
    )


def _sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _ws_service(
    *,
    heartbeat: float = 30.0,
    queue_maxsize: int = 256,
) -> tuple[WSSenderService, InMemoryTraceEventStore, InMemorySessionCache]:
    store = InMemoryTraceEventStore()
    cache = InMemorySessionCache()
    svc = WSSenderService(
        cache,
        store,
        manifest=MANIFEST,
        visibility=VisibilityConfig(),
        config=WSSenderConfig(heartbeat_interval_seconds=heartbeat, queue_maxsize=queue_maxsize),
    )
    return svc, store, cache


def _ws_only_app(ws: WSSenderService) -> Any:
    """只挂 WS 路由的最小 app（RunService 仅占位，WS 测试不触图）。"""

    return create_app(RunService(Orchestrator(), InMemorySessionCache()), ws_service=ws)


def _ev(sid: str, trace: str, seq: int, et: EventType, payload: dict[str, Any] | None = None) -> TraceEvent:
    return TraceEvent(sid, trace, seq, et, payload or {}, datetime.now(tz=UTC))


# --------------------------------------------------------------------------- #
# 进程内 ASGI WebSocket 客户端（同 loop；不引真实 socket）
# --------------------------------------------------------------------------- #


class _WsSession:
    """进程内 ASGI WS 客户端：以 ``websocket`` scope 直接驱动 ASGI app。

    与 :class:`starlette.testclient.TestClient` 不同——不跨 portal 线程，故 InMemory pub-sub
    的 ``asyncio.Queue`` 与测试 loop 同环、跨协程 ``put_nowait`` 能唤醒等待者；PG fixture 亦同 loop。
    """

    def __init__(self, app: Any, session_id: str, user_id: str, *, send_delay: float = 0.0) -> None:
        headers: list[tuple[bytes, bytes]] = []
        if user_id:
            headers.append((b"x-user-id", user_id.encode()))
        self._app = app
        self._scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": "/ws/agent/stream",
            "root_path": "",
            "query_string": f"session_id={session_id}".encode(),
            "headers": headers,
            "client": ("127.0.0.1", 60000),
            "server": ("test", 80),
        }
        self._received: asyncio.Queue[str] = asyncio.Queue()
        self._recv_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._recv_q.put_nowait({"type": "websocket.connect"})
        self._send_delay = send_delay
        self._accepted = asyncio.Event()
        self._close_code: int | None = None
        self._closed = asyncio.Event()
        self._disconnected = False
        self._task: asyncio.Task[None] | None = None

    async def _send(self, message: dict[str, Any]) -> None:
        if self._send_delay:
            await asyncio.sleep(self._send_delay)
        mtype = message.get("type")
        if mtype == "websocket.accept":
            self._accepted.set()
        elif mtype == "websocket.send":
            if self._disconnected:
                raise ConnectionError("client gone")  # OSError 子类 → Starlette 转 WebSocketDisconnect
            text = message.get("text")
            if text is not None:
                await self._received.put(text)
            b = message.get("bytes")
            if b is not None:
                await self._received.put(b.decode("utf-8", "surrogateescape"))
        elif mtype == "websocket.close":
            self._close_code = int(message.get("code", 1000))
            self._closed.set()

    async def _receive(self) -> dict[str, Any]:
        return await self._recv_q.get()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._app(self._scope, self._receive, self._send)
        except Exception:
            pass

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        text = await asyncio.wait_for(self._received.get(), timeout=timeout)
        loaded: dict[str, Any] = json.loads(text)
        return loaded

    async def wait_close(self, timeout: float = 5.0) -> int | None:
        await asyncio.wait_for(self._closed.wait(), timeout=timeout)
        return self._close_code

    async def aclose(self, timeout: float = 3.0) -> None:
        if self._task is None:
            return
        self._disconnected = True
        self._recv_q.put_nowait({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except (TimeoutError, Exception):
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass


# --------------------------------------------------------------------------- #
# 所有权：4001
# --------------------------------------------------------------------------- #


async def test_ws_missing_user_id_closes_4001() -> None:
    ws, _store, _cache = _ws_service()
    app = _ws_only_app(ws)
    sess = _WsSession(app, _sid("nouser"), "")
    sess.start()
    assert await sess.wait_close() == WS_CLOSE_FORBIDDEN
    await sess.aclose()


async def test_ws_cross_user_closes_4001() -> None:
    ws, _store, cache = _ws_service()
    app = _ws_only_app(ws)
    sid = _sid("xuser")
    await cache.set_session_owner(sid, "u1")  # 首见绑定 u1
    sess = _WsSession(app, sid, "u2")
    sess.start()
    assert await sess.wait_close() == WS_CLOSE_FORBIDDEN
    await sess.aclose()


async def test_ws_first_sight_allows_connect_then_graph_static() -> None:
    """ADR-0023：WS 首见未绑定会话仅允许显示连接、不登记所有权——仍正常下发 graph_static。"""
    ws, _store, _cache = _ws_service()
    app = _ws_only_app(ws)
    sess = _WsSession(app, _sid("fresh"), "u1")
    sess.start()
    msg = await sess.recv()
    assert msg["event_type"] == "graph_static"
    assert msg["event_seq"] == -1
    assert any(n["id"] == "__start__" for n in msg["payload"]["nodes"])
    assert any(e["cond"] == "replay" for e in msg["payload"]["edges"])  # hitl1→parse+partition
    await sess.aclose()


async def test_ws_does_not_bind_or_hijack_session_owner() -> None:
    """ADR-0023 不变量：WS 只读校验所有权——不登记、不绑定、不 touch、不 hijack。

    fresh sid 上 uA 连接 → 允许、owner 仍 None（WS 未绑定）；uB 同 sid 连接 → 也允许
    （existing 仍 None，无主可冲突）；模拟 run 控制面 ``set_session_owner(sid, uReal)``
    绑定后，uOther 连接 → 跨用户 → 4001。所有权绑定由 run 独占，WS 不参与。
    """

    ws, _store, cache = _ws_service()
    app = _ws_only_app(ws)
    sid = _sid("ro")
    # uA 首见连接：允许，不绑定。
    sA = _WsSession(app, sid, "uA")
    sA.start()
    assert (await sA.recv())["event_type"] == "graph_static"
    assert await cache.get_session_owner(sid) is None  # WS 未绑定
    # uB 同 sid 连接：existing 仍 None → 也允许（无主可冲突）。
    sB = _WsSession(app, sid, "uB")
    sB.start()
    assert (await sB.recv())["event_type"] == "graph_static"
    assert await cache.get_session_owner(sid) is None  # 仍未绑定
    # 模拟 run 控制面绑定所有权（RunService._enforce_ownership 的行为）。
    await cache.set_session_owner(sid, "uReal")
    # uOther 连接已绑定会话 → 跨用户 → 4001（WS 只读校验，不覆盖）。
    sOther = _WsSession(app, sid, "uOther")
    sOther.start()
    assert await sOther.wait_close() == WS_CLOSE_FORBIDDEN
    await sA.aclose()
    await sB.aclose()
    await sOther.aclose()


# --------------------------------------------------------------------------- #
# live 尾随 + 回放 + 去重
# --------------------------------------------------------------------------- #


async def test_ws_live_tail_after_connect() -> None:
    ws, store, _cache = _ws_service()
    app = _ws_only_app(ws)
    sid = _sid("live")
    sess = _WsSession(app, sid, "u1")
    sess.start()
    assert (await sess.recv())["event_type"] == "graph_static"
    await store.append(_ev(sid, "t1", 0, EventType.TRACE_START))
    await store.append(_ev(sid, "t1", 1, EventType.NODE_START, {"node_id": "hitl1"}))
    e0 = await sess.recv()
    e1 = await sess.recv()
    assert (e0["event_seq"], e0["event_type"]) == (0, "trace_start")
    assert (e1["event_seq"], e1["event_type"]) == (1, "node_start")
    await sess.aclose()


async def test_ws_replay_history_then_live_no_duplicates() -> None:
    ws, store, _cache = _ws_service()
    app = _ws_only_app(ws)
    sid = _sid("replay")
    await store.append(_ev(sid, "t1", 0, EventType.TRACE_START))
    await store.append(_ev(sid, "t1", 1, EventType.NODE_START, {"node_id": "parse+partition"}))
    sess = _WsSession(app, sid, "u1")
    sess.start()
    assert (await sess.recv())["event_type"] == "graph_static"
    assert (await sess.recv())["event_seq"] == 0  # 回放历史
    assert (await sess.recv())["event_seq"] == 1
    await store.append(_ev(sid, "t1", 2, EventType.NODE_END, {"node_id": "parse+partition"}))
    assert (await sess.recv())["event_seq"] == 2  # 接 live
    await sess.aclose()


async def test_ws_replay_live_boundary_event_not_duplicated() -> None:
    """subscribe→_replay 窗口内落库的事件：既进 live 队列又被 _replay 读到，须仅发一次。

    在 ``_replay`` 读 ``events_for_session`` 前注入一条 append——subscribe 已注册订阅，
    故该事件同时进 live 队列与回放读集；无去重则会重发（ADR-0023 不重发）。
    """

    ws, store, _cache = _ws_service(heartbeat=30.0)
    app = _ws_only_app(ws)
    sid = _sid("boundary")
    await store.append(_ev(sid, "t1", 0, EventType.TRACE_START))  # 纯回放历史
    boundary = _ev(sid, "t1", 1, EventType.NODE_START, {"node_id": "hitl1"})

    original_replay = ws._replay

    async def inject_replay(websocket: Any, session_id: str, seen: dict[str, int]) -> None:
        await store.append(boundary)  # 窗口内落库：live 队列 + _replay 读集都见
        await original_replay(websocket, session_id, seen)

    ws._replay = inject_replay  # type: ignore[method-assign]
    sess = _WsSession(app, sid, "u1")
    sess.start()
    assert (await sess.recv())["event_type"] == "graph_static"
    seqs: list[int] = []
    for _ in range(20):
        try:
            m = await sess.recv(timeout=0.5)
        except TimeoutError:
            break
        if m["event_seq"] != -1:
            seqs.append(m["event_seq"])
    assert seqs.count(0) == 1  # 回放历史
    assert seqs.count(1) == 1  # 窗口内事件仅发一次（不重发）
    await sess.aclose()


# --------------------------------------------------------------------------- #
# 背压缓冲（单元）
# --------------------------------------------------------------------------- #


def _llm(seq: int, token: str, full: str, trace: str = "t1") -> TraceEvent:
    return TraceEvent("s", trace, seq, EventType.LLM_THINKING, {"token": token, "full_thought": full}, datetime.now(tz=UTC))


async def test_backpressure_buffer_merges_llm_tokens_when_full() -> None:
    """cap=3：满后 llm_thinking token 合并到队尾同类（token 拼接、full_thought 取最新）。"""

    metrics = WsMetrics()
    buf = _BackpressureBuffer(3, metrics)
    buf.push(_ev("s", "t1", 0, EventType.TRACE_START))
    buf.push(_llm(1, "a", "a"))
    buf.push(_llm(2, "b", "ab"))  # 队列已满（cap=3）
    buf.push(_llm(3, "c", "abc"))  # 满 → 合并到队尾 llm_thinking（seq=2）
    assert metrics.snapshot()["ws_event_queue_full_total"] == 1
    await buf.pop()  # TRACE_START
    first = await buf.pop()  # llm seq=1（未被合并）
    merged = await buf.pop()  # llm seq=2（合并了 seq=3 的 token）
    assert first.payload["token"] == "a"
    assert merged.event_seq == 2
    assert merged.payload["token"] == "bc"  # b + c 拼接
    assert merged.payload["full_thought"] == "abc"  # 取最新累积
    buf.close()


async def test_backpressure_buffer_drops_non_llm_when_full() -> None:
    """cap=1：满后非 llm 事件 live 丢弃、计数；pop 仍可取已有项。"""

    metrics = WsMetrics()
    buf = _BackpressureBuffer(1, metrics)
    buf.push(_ev("s", "t1", 0, EventType.TRACE_START))  # 满
    buf.push(_ev("s", "t1", 1, EventType.NODE_END, {"node_id": "x"}))  # 满 → 丢弃
    assert metrics.snapshot()["ws_event_queue_full_total"] == 1
    out = await buf.pop()
    assert out.event_seq == 0
    buf.close()


async def test_backpressure_buffer_close_wakes_pop() -> None:
    metrics = WsMetrics()
    buf = _BackpressureBuffer(2, metrics)

    async def popper() -> None:
        try:
            await buf.pop()
            raise AssertionError("应 StopAsyncIteration")
        except StopAsyncIteration:
            pass

    t = asyncio.create_task(popper())
    await asyncio.sleep(0)  # 让 popper 进入 await wait()
    buf.close()
    await t


async def test_ws_backpressure_integration_does_not_lose_key_events() -> None:
    """慢 WS（send_delay）+ 突发 >cap 事件：缓冲满触发合并 / 丢弃，关键事件 durable、重连回放补齐。"""

    ws, store, _cache = _ws_service(heartbeat=30.0, queue_maxsize=3)
    app = _ws_only_app(ws)
    sid = _sid("bp")
    sess = _WsSession(app, sid, "u1", send_delay=0.05)  # 慢 send → 缓冲易满
    sess.start()
    await sess.recv()  # graph_static
    # 突发 8 条 llm_thinking（cap=3）。
    for i in range(8):
        await store.append(_ev(sid, "t1", i, EventType.LLM_THINKING, {"token": f"t{i}", "full_thought": f"t0..t{i}"}))
    await asyncio.sleep(0.01)
    assert ws.metrics.snapshot()["ws_event_queue_full_total"] >= 1
    await sess.aclose()
    # 重连回放：所有 8 条 llm_thinking 在 trace_events durable、经回放下发、无丢失。
    sess2 = _WsSession(app, sid, "u1")
    sess2.start()
    await sess2.recv()  # graph_static
    seen: list[int] = []
    for _ in range(20):
        try:
            m = await sess2.recv(timeout=0.5)
        except TimeoutError:
            break
        if m["event_type"] == "llm_thinking":
            seen.append(m["event_seq"])
    assert seen == list(range(8))  # 重连回放补齐、无丢失
    await sess2.aclose()


# --------------------------------------------------------------------------- #
# 心跳
# --------------------------------------------------------------------------- #


async def test_ws_heartbeat_when_idle() -> None:
    ws, _store, _cache = _ws_service(heartbeat=0.2)
    app = _ws_only_app(ws)
    sess = _WsSession(app, _sid("hb"), "u1")
    sess.start()
    await sess.recv()  # graph_static
    msg = await sess.recv(timeout=3.0)
    assert msg["event_type"] == "heartbeat"
    assert msg["event_seq"] == -1
    await sess.aclose()


# --------------------------------------------------------------------------- #
# 同 session 新连接取代旧连接（不发 stream_abort）
# --------------------------------------------------------------------------- #


async def test_ws_new_connection_supersedes_old_no_abort() -> None:
    ws, store, _cache = _ws_service(heartbeat=30.0)
    app = _ws_only_app(ws)
    sid = _sid("super")
    old = _WsSession(app, sid, "u1")
    old.start()
    await old.recv()  # graph_static
    await store.append(_ev(sid, "t1", 0, EventType.TRACE_START))
    await old.recv()
    # 新连接建连 → 取代旧连接。
    new = _WsSession(app, sid, "u1")
    new.start()
    assert (await new.recv())["event_type"] == "graph_static"
    # 旧连接应停止下发；其 serve 任务在心跳前结束（被取消）。
    await asyncio.wait_for(old.aclose(), timeout=3.0)
    # 旧连接收到的全部消息中无 stream_abort。
    received: list[str] = []
    while True:
        try:
            received.append((await old.recv(timeout=0.2))["event_type"])
        except TimeoutError:
            break
    assert "stream_abort" not in received
    await new.aclose()


async def test_ws_three_connections_supersede_no_orphan() -> None:
    """3 连接依次取代：中间连接不被孤儿——serve 收尾、缓冲注销、``ws_event_queue_size`` 不漂移。

    覆盖 Fix #4：先登记新 handle 再 supersede 旧，跨 ``await old.supersede()`` 期间
    ``_active`` 始终持句柄；被取代连接的 ``_finalize`` 必跑（identity guard 不误清新），
    缓冲注销、不漏 LISTEN 连接 / WS。
    """

    ws, _store, _cache = _ws_service(heartbeat=30.0)
    app = _ws_only_app(ws)
    sid = _sid("super3")
    s1 = _WsSession(app, sid, "u1")
    s1.start()
    await s1.recv()  # graph_static
    s2 = _WsSession(app, sid, "u1")
    s2.start()
    await s2.recv()  # graph_static（取代 s1）
    s3 = _WsSession(app, sid, "u1")
    s3.start()
    await s3.recv()  # graph_static（取代 s2）
    # 被 supersede 的 s1/s2 serve 任务应自行收尾（_finalize ran → 缓冲已注销）。
    await asyncio.wait_for(s1.aclose(), timeout=3.0)
    await asyncio.wait_for(s2.aclose(), timeout=3.0)
    # 无孤儿缓冲：仅 s3 活跃、其队列为空 → ws_event_queue_size == 0（不漂移）。
    assert ws.metrics.snapshot()["ws_event_queue_size"] == 0
    await s3.aclose()


# --------------------------------------------------------------------------- #
# 指标快照形状
# --------------------------------------------------------------------------- #


async def test_ws_metrics_snapshot_shape() -> None:
    ws, _store, _cache = _ws_service()
    snap = ws.metrics.snapshot()
    assert set(snap) == {"ws_event_queue_size", "ws_event_queue_full_total"}
    assert snap["ws_event_queue_size"] == 0
    assert snap["ws_event_queue_full_total"] == 0


# --------------------------------------------------------------------------- #
# 不变量：WS 断开不中止 run + 重连回放补齐、无关键事件丢失（需 PG）
# --------------------------------------------------------------------------- #


def _build_pg_app(
    checkpointer: Any, store: InMemoryTraceEventStore, cache: InMemorySessionCache, ws: WSSenderService
) -> Any:
    orch = Orchestrator(agents=_interrupt_agents(), checkpointer=checkpointer)
    service = RunService(orch, cache, trace_store=store, config=RunServiceConfig())
    return create_app(service, ws_service=ws)


async def _http_run(c: httpx.AsyncClient, sid: str, body: dict[str, Any]) -> httpx.Response:
    return await c.post("/api/agent/run", json=body, headers={"X-User-Id": "u1"})


async def test_ws_disconnect_does_not_abort_run_and_reconnect_replays(
    pg_checkpointer: Any,
) -> None:
    """并发 HTTP fresh run + WS live 尾随；断开 WS → run 不中止（resume skip→hitl2、pass→SUCCESS）；
    重连按 event_seq 回放补齐、human_pause（关键事件）不丢；无 stream_abort。"""

    store = InMemoryTraceEventStore()
    cache = InMemorySessionCache()
    ws = WSSenderService(
        cache,
        store,
        manifest=MANIFEST,
        visibility=VisibilityConfig(),
        config=WSSenderConfig(heartbeat_interval_seconds=0.5, queue_maxsize=256),
    )
    app = _build_pg_app(pg_checkpointer, store, cache, ws)
    sid = _sid("e2e")

    transport = httpx.ASGITransport(app=app)
    sess = _WsSession(app, sid, "u1")
    sess.start()
    assert (await sess.recv())["event_type"] == "graph_static"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        run_task = asyncio.create_task(_http_run(c, sid, {"session_id": sid, "query": "改", "document": _DOC.decode()}))
        saw_pause = False
        for _ in range(60):
            try:
                msg = await sess.recv(timeout=3.0)
            except TimeoutError:
                break
            if msg["event_type"] == "human_pause":
                saw_pause = True
                break
        r1 = await run_task
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["status"] == "NEED_HUMAN_INPUT"
        assert saw_pause, "WS 应 live 收到 human_pause"

        # 断开 WS（模拟抖动）。run 不中止：继续 resume → SUCCESS。
        await sess.aclose()
        r2 = await _http_run(c, sid, {"session_id": sid, "human_response": {"action": "skip"}})
        assert r2.json()["status"] == "NEED_HUMAN_INPUT"
        r3 = await _http_run(c, sid, {"session_id": sid, "human_response": {"action": "pass"}})
        assert r3.json()["status"] == "SUCCESS"
        assert r3.json()["final_document"] == _DOC.decode()

        # 重连 → 按 event_seq 回放补齐；关键事件（human_pause / stream_finish）仍在、无重复、无 stream_abort。
        sess2 = _WsSession(app, sid, "u1")
        sess2.start()
        await sess2.recv()  # graph_static
        replayed: list[dict[str, Any]] = []
        for _ in range(400):
            try:
                m = await sess2.recv(timeout=1.0)
            except TimeoutError:
                break
            replayed.append(m)
            if m["event_type"] == "stream_finish":  # 终态事件已回放完，避免被心跳拖长
                break
        types = [m["event_type"] for m in replayed]
        assert "human_pause" in types  # 关键事件经回放补齐、不丢
        assert "stream_finish" in types
        assert "stream_abort" not in types  # 不变量：WS 断开未产 abort
        seqs = [(m["trace_id"], m["event_seq"]) for m in replayed if m["event_seq"] != -1]
        assert len(seqs) == len(set(seqs))  # 无重复
        await sess2.aclose()
