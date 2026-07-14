"""``websockets`` PyPI 库互操作 smoke 测试（T-06·ADR-0023）。

验收 §「用 python websockets 客户端连 WS」的字面落地：真实 ``uvicorn`` 服务 + ``websockets.connect``
TCP 客户端，验端点对真客户端互通（建连 → ``graph_static`` → live 事件 → 断开重连 → ``event_seq`` 回放）。
不触图 / 不触 PG（仅 WS-sender over InMemory store pub-sub），快且确定；与 ``tests/test_ws_sender.py``
的进程内 ASGI 客户端测试互补（后者覆盖含 PG run 的全不变量）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import uvicorn
import websockets

from agents.assembly import MANIFEST
from api_layer.app import create_app
from api_layer.graph_view import VisibilityConfig
from api_layer.run import RunService
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import EventType, InMemoryTraceEventStore, TraceEvent
from api_layer.ws import WSSenderConfig, WSSenderService
from runtime.orchestrator import Orchestrator


def _app_and_store() -> tuple[Any, InMemoryTraceEventStore, InMemorySessionCache]:
    store = InMemoryTraceEventStore()
    cache = InMemorySessionCache()
    ws = WSSenderService(
        cache,
        store,
        manifest=MANIFEST,
        visibility=VisibilityConfig(),
        # 短心跳：真实 websockets 客户端断开后，serve 须在下个心跳周期内
        # 经 send_text→WebSocketDisconnect 察觉断开并收尾（生产 30s 心跳为
        # PRD §6.4 设计；此处压到 0.2s 使断开→收尾→uvicorn 关停在 10s 内
        # 确定性完成，不依赖 30s 真等）。
        config=WSSenderConfig(heartbeat_interval_seconds=0.2, queue_maxsize=256),
    )
    app = create_app(RunService(Orchestrator(), InMemorySessionCache()), ws_service=ws)
    return app, store, cache


async def _serve_until(server: uvicorn.Server) -> tuple[asyncio.Task[None], int]:
    """启动 uvicorn（禁信号处理，免污染 pytest）→ 返回 (serve task, bound port)。"""

    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    # uvicorn ``started`` 为 bool；轮询至端口绑定（startup 完成）。
    for _ in range(200):
        if server.started and server.servers and server.servers[0].sockets:
            break
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn 未在 10s 内启动")
    port = server.servers[0].sockets[0].getsockname()[1]
    return task, port


async def _recv_json(ws: Any, timeout: float = 5.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


async def _recv_event(
    ws: Any, *, skip_heartbeat: bool = True, timeout: float = 5.0
) -> dict[str, Any]:
    """收一条事件消息；短心跳下其间可能穿插 ``heartbeat``，按需跳过。"""

    while True:
        msg = await _recv_json(ws, timeout=timeout)
        if skip_heartbeat and msg.get("event_type") == "heartbeat":
            continue
        return msg


async def test_websockets_client_interop_graph_static_live_replay() -> None:
    """真实 ``websockets`` 客户端连真实 uvicorn：graph_static → live → 断开重连回放。"""

    app, store, _cache = _app_and_store()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, loop="asyncio", log_level="warning")
    server = uvicorn.Server(config)
    task, port = await _serve_until(server)
    uri = f"ws://127.0.0.1:{port}/ws/agent/stream?session_id=interop-1"
    try:
        async with websockets.connect(uri, additional_headers={"X-User-Id": "u1"}, open_timeout=10) as ws:
            gs = await _recv_json(ws)
            assert gs["event_type"] == "graph_static"
            assert gs["event_seq"] == -1
            # live：append 一条 → 经 InMemory pub-sub 即时下发。
            await store.append(
                TraceEvent("interop-1", "t1", 0, EventType.TRACE_START, {}, datetime.now(tz=UTC))
            )
            ev = await _recv_event(ws)
            assert (ev["event_seq"], ev["event_type"]) == (0, "trace_start")
        # 断开后重连 → 按 event_seq 回放历史。
        async with websockets.connect(uri, additional_headers={"X-User-Id": "u1"}, open_timeout=10) as ws2:
            await _recv_json(ws2)  # graph_static
            replayed = await _recv_event(ws2)
            assert (replayed["event_seq"], replayed["event_type"]) == (0, "trace_start")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10.0)
