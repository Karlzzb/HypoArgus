"""翻译层 + ``trace_events`` 持久日志集成测试（T-05·ADR-0023）。

经 ``httpx`` ASGI 驱动真实 FastAPI 应用，端到端验翻译层不变量：

- 一次完整 HITL run 后查 ``trace_events``，事件序列与真实执行流程匹配（回放可信源）；
  ``event_seq`` 单 trace 内从 0 单调、续跑顺延无断层。
- ``node_instance`` 区分回放环 / 多次触发（hitl1 pre-interrupt instance 0、resume instance 1）。
- ``visible=False`` 节点的 ``node_*`` 事件被丢弃、trace 级事件保留。
- ``human_pause`` 的 ``question`` / ``hint`` 与 HTTP ``NEED_HUMAN_INPUT`` 响应同源（同 payload）。
- Langfuse handler 与 ``astream_events`` 消费端共存零冲突；handler 抛错降级不阻塞对话。
- 翻译层非阻塞：慢写 / 写失败不杀图（run 仍达 NEED_HUMAN_INPUT / SUCCESS）。

图注入 ``InterruptHitl*Gate`` + ``AsyncPostgresSaver``；trace_events 落共享 Postgres
（``pg_checkpointer`` + ``pg_session_cache`` + ``pg_trace_store`` 夹具，PG 不可达即 skip）。
业务纯函数零改动：复用 T-04 的 interrupt 图，仅换 ``ainvoke`` → ``astream_events`` + 翻译层。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler

from agents.assembly import create_real_agents
from agents.parser import FakeLlmClient, ParseResult
from api_layer.app import create_app
from api_layer.graph_view import VisibilityConfig
from api_layer.run import RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import (
    EventType,
    InMemoryTraceEventStore,
    TraceEvent,
    TraceEventStoreBase,
)
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


def _build(
    *,
    checkpointer: Any,
    session_cache: Any,
    trace_store: TraceEventStoreBase,
    langfuse_handler: Any | None = None,
    visibility: VisibilityConfig | None = None,
) -> tuple[RunService, Any]:
    orch = Orchestrator(agents=_interrupt_agents(), checkpointer=checkpointer)
    service = RunService(
        orch,
        session_cache,
        trace_store=trace_store,
        langfuse_handler=langfuse_handler,
        visibility=visibility,
        config=RunServiceConfig(),
    )
    return service, create_app(service)


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _fresh(c: httpx.AsyncClient, sid: str) -> httpx.Response:
    return await c.post(
        "/api/agent/run",
        json={"session_id": sid, "query": "改一改", "document": _DOC.decode()},
        headers={"X-User-Id": "u1"},
    )


async def _resume(c: httpx.AsyncClient, sid: str, action: str) -> httpx.Response:
    return await c.post(
        "/api/agent/run",
        json={"session_id": sid, "human_response": {"action": action}},
        headers={"X-User-Id": "u1"},
    )


# --------------------------------------------------------------------------- #
# 回放可信源：完整 HITL run 后 trace_events 序列匹配执行流程
# --------------------------------------------------------------------------- #


async def test_trace_events_replay_matches_full_hitl_flow(
    pg_checkpointer: Any, pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """fresh → hitl1 pause → resume skip → hitl2 pause → resume pass → SUCCESS；
    查 trace_events：``event_seq`` 0..N 无断层、首 trace_start / 末 stream_finish、
    human_pause 与 HTTP 响应同源、node_instance 区分 hitl1 两次触发。"""

    sid = _sid("replay")
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=pg_trace_store,
    )
    async with _client(app) as c:
        # 1) fresh → hitl1
        r1 = await _fresh(c, sid)
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["status"] == "NEED_HUMAN_INPUT"
        assert b1["node_id"] == "hitl1"
        trace_id = b1["trace_id"]
        # 2) resume skip → hitl2
        r2 = await _resume(c, sid, "skip")
        b2 = r2.json()
        assert b2["status"] == "NEED_HUMAN_INPUT"
        assert b2["node_id"] == "hitl2"
        # 3) resume pass → SUCCESS
        r3 = await _resume(c, sid, "pass")
        b3 = r3.json()
        assert b3["status"] == "SUCCESS"

    events = await pg_trace_store.events_for_trace(trace_id)
    # event_seq 0..N-1 无断层：
    assert [e.event_seq for e in events] == list(range(len(events)))
    # 首事件 trace_start、末事件 stream_finish：
    assert events[0].event_type is EventType.TRACE_START
    assert events[-1].event_type is EventType.STREAM_FINISH

    types = [e.event_type for e in events]
    # 两次 human_pause（hitl1 / hitl2）：
    pauses = [e for e in events if e.event_type is EventType.HUMAN_PAUSE]
    assert len(pauses) == 2
    assert pauses[0].payload["node_id"] == "hitl1"
    assert pauses[1].payload["node_id"] == "hitl2"
    # human_pause 与 HTTP 响应同源（同一 payload 的 question/hint）：
    assert pauses[0].payload["question"] == b1["human_question"]
    assert pauses[0].payload["hint"] == b1["hint"]
    assert pauses[1].payload["question"] == b2["human_question"]
    assert pauses[1].payload["hint"] == b2["hint"]
    # detail 同源（HTTP detail == human_pause detail）：
    assert pauses[0].payload["detail"] == b1["detail"]

    # node_instance：hitl1 两次触发（pre-interrupt instance 0、resume instance 1）。
    hitl1_starts = [
        e
        for e in events
        if e.event_type is EventType.NODE_START and e.payload["node_id"] == "hitl1"
    ]
    assert len(hitl1_starts) == 2
    assert hitl1_starts[0].payload["node_instance"] == 0
    assert hitl1_starts[1].payload["node_instance"] == 1

    # 主干节点齐全（parse+partition / hypothesis_propose / judgment / rewrite_loop / hitl2）：
    node_starts = {
        e.payload["node_id"] for e in events if e.event_type is EventType.NODE_START
    }
    for n in (
        "parse+partition",
        "hitl1",
        "hypothesis_propose",
        "retrieval",
        "judgment",
        "rewrite_loop",
        "hitl2",
    ):
        assert n in node_starts, f"缺 node_start: {n}"
    # trace 级事件 + 终态事件存在：
    assert EventType.TRACE_START in types
    assert EventType.STREAM_FINISH in types
    assert EventType.HUMAN_PAUSE in types


async def test_trace_events_resume_continues_seq_gap_free(
    pg_checkpointer: Any, pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """fresh + resume skip + resume pass：全量 ``event_seq`` 0..N 无断层、resume 段不重产 trace_start。"""

    sid = _sid("gap")
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=pg_trace_store,
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        trace_id = r.json()["trace_id"]
        await _resume(c, sid, "skip")
        await _resume(c, sid, "pass")
    events = await pg_trace_store.events_for_trace(trace_id)
    seqs = [e.event_seq for e in events]
    assert seqs == list(range(len(events)))  # 无断层
    # resume 段不重产 trace_start：全量仅一个 trace_start。
    assert sum(1 for e in events if e.event_type is EventType.TRACE_START) == 1


# --------------------------------------------------------------------------- #
# visible=False 节点过滤
# --------------------------------------------------------------------------- #


async def test_hidden_node_events_dropped_trace_level_kept(
    pg_checkpointer: Any, pg_session_cache: Any, pg_trace_store: Any
) -> None:
    """隐藏 parse+partition：其 node_* 事件丢弃；trace_start / hitl1 node / human_pause 保留。"""

    sid = _sid("hidden")
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=pg_trace_store,
        visibility=VisibilityConfig(hidden=frozenset({"parse+partition"})),
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        b = r.json()
        assert b["status"] == "NEED_HUMAN_INPUT"
        trace_id = b["trace_id"]

    events = await pg_trace_store.events_for_trace(trace_id)
    node_starts = {
        e.payload["node_id"] for e in events if e.event_type is EventType.NODE_START
    }
    assert "parse+partition" not in node_starts  # 隐藏 → 丢弃
    assert "hitl1" in node_starts  # interrupt 强制可见 → 保留
    assert any(e.event_type is EventType.TRACE_START for e in events)
    assert any(e.event_type is EventType.HUMAN_PAUSE for e in events)
    # 无 parse+partition 的任何节点级事件：
    for et in (EventType.NODE_START, EventType.NODE_OUTPUT, EventType.NODE_END):
        assert not any(
            e.payload.get("node_id") == "parse+partition"
            for e in events
            if e.event_type is et
        )


# --------------------------------------------------------------------------- #
# Langfuse handler 共存 + 抛错降级
# --------------------------------------------------------------------------- #


class _BenignHandler(BaseCallbackHandler):
    pass


class _RaisingHandler(BaseCallbackHandler):
    def on_chain_start(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("langfuse-boom")


async def test_langfuse_handler_coexists_with_astream_events(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """注入 benign callback handler：与 astream_events 消费端共存零冲突、run 达 NEED_HUMAN_INPUT、
    trace_events 落库。"""

    sid = _sid("langfuse")
    trace_store = InMemoryTraceEventStore()
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=trace_store,
        langfuse_handler=_BenignHandler(),
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        b = r.json()
        assert b["status"] == "NEED_HUMAN_INPUT"
        trace_id = b["trace_id"]
    events = await trace_store.events_for_trace(trace_id)
    assert events  # 翻译层仍落库
    assert events[0].event_type is EventType.TRACE_START


async def test_langfuse_handler_failure_degrades_without_blocking(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """handler 抛错（langchain 内部吞、记错）：run 仍达 NEED_HUMAN_INPUT、不 500、trace_events 落库。"""

    sid = _sid("langfuse-raise")
    trace_store = InMemoryTraceEventStore()
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=trace_store,
        langfuse_handler=_RaisingHandler(),
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        assert r.status_code == 200  # 不 500
        b = r.json()
        assert b["status"] == "NEED_HUMAN_INPUT"
        trace_id = b["trace_id"]
    events = await trace_store.events_for_trace(trace_id)
    assert events  # 翻译层未受 handler 抛错影响


# --------------------------------------------------------------------------- #
# 非阻塞：慢写 / 写失败不杀图
# --------------------------------------------------------------------------- #


class _SlowTraceStore(InMemoryTraceEventStore):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay

    async def append(self, event: TraceEvent) -> None:
        await asyncio.sleep(self._delay)
        await super().append(event)


class _FailingTraceStore(InMemoryTraceEventStore):
    async def append(self, event: TraceEvent) -> None:
        raise RuntimeError("trace-store-boom")


async def test_slow_store_does_not_kill_run(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """慢写（每次 20ms）：run 仍达 NEED_HUMAN_INPUT（图不被慢写杀），trace_events 落库齐全。"""

    sid = _sid("slow")
    trace_store = _SlowTraceStore(delay=0.02)
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=trace_store,
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        assert r.status_code == 200
        b = r.json()
        assert b["status"] == "NEED_HUMAN_INPUT"
        trace_id = b["trace_id"]
    events = await trace_store.events_for_trace(trace_id)
    assert events  # 慢写仍落库（flush 等 drainer 排空）
    assert events[0].event_type is EventType.TRACE_START
    assert any(e.event_type is EventType.HUMAN_PAUSE for e in events)


async def test_failing_store_does_not_kill_run(
    pg_checkpointer: Any, pg_session_cache: Any
) -> None:
    """写失败降级：run 仍达 NEED_HUMAN_INPUT、不 500（trace_events 空但不杀图）。"""

    sid = _sid("fail")
    trace_store = _FailingTraceStore()
    _service, app = _build(
        checkpointer=pg_checkpointer,
        session_cache=pg_session_cache,
        trace_store=trace_store,
    )
    async with _client(app) as c:
        r = await _fresh(c, sid)
        assert r.status_code == 200
        b = r.json()
        assert b["status"] == "NEED_HUMAN_INPUT"
        trace_id = b["trace_id"]
    assert await trace_store.events_for_trace(trace_id) == []  # 全失败 → 空


# --------------------------------------------------------------------------- #
# graph_timeout 产 stream_abort
# --------------------------------------------------------------------------- #


class _SlowGraph:
    """伪图：astream_events 永久睡眠 → GRAPH_TIMEOUT；aget_state 返回空（终态分类前 abort）。"""

    next: tuple[str, ...] = ()
    values: dict[str, Any] = {}
    tasks: tuple[Any, ...] = ()

    async def astream_events(self, *_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(30)
        if False:  # pragma: no cover  # noqa: RET503 — 使本函数为 async generator
            yield

    async def aget_state(self, *_a: Any, **_k: Any) -> Any:
        return self


async def test_graph_timeout_emits_stream_abort() -> None:
    """图超时 → 504 GRAPH_TIMEOUT + trace_events 含 stream_abort。"""

    sid = _sid("abort")
    orch = Orchestrator()
    orch.graph = _SlowGraph()
    trace_store = InMemoryTraceEventStore()
    service = RunService(
        orch,
        InMemorySessionCache(),
        trace_store=trace_store,
        config=RunServiceConfig(graph_timeout_seconds=0.2),
    )
    app = create_app(service)
    async with _client(app) as c:
        r = await _fresh(c, sid)
        assert r.status_code == 504
        assert r.json()["error"] == "GRAPH_TIMEOUT"
    # stream_abort 已落库（504 响应不载 trace_id；直接查 store 全量）：
    rows = list(trace_store._rows)
    assert any(e.event_type is EventType.STREAM_ABORT for e in rows)
    abort = [e for e in rows if e.event_type is EventType.STREAM_ABORT][0]
    assert abort.payload["abort_reason"] == "GRAPH_TIMEOUT"
