"""延迟指标埋点单测（T-08·PRD §11.1）。

:event_push_latency_seconds（翻译层 drainer 落库延迟）与 :graph_execution_duration_seconds
（RunService 单请求图执行墙钟时长）经可注入时钟确定性测得非零。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agents.assembly import MANIFEST
from api_layer.metrics import OpsMetrics
from api_layer.run import RunRequest, RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import InMemoryTraceEventStore
from api_layer.translator import EventTranslator
from runtime.orchestrator import Orchestrator


def _step_clock() -> Any:
    """每次调用前进 0.01s 的时钟，供延迟确定性断言。"""


    state = {"t": datetime(2026, 1, 1, tzinfo=UTC)}

    def _now() -> datetime:
        state["t"] = state["t"] + timedelta(seconds=0.01)
        return state["t"]

    return _now  # type: ignore[return-value]


def _manifest_index() -> dict[str, Any]:
    return {e.name: e for e in MANIFEST}


class _FastGraph:
    """伪图：``astream_events`` 空、``aget_state`` 返回终态（final_document 在、无 errors）。"""

    next: tuple[str, ...] = ()
    values: dict[str, Any] = {"final_document": b"done", "errors": []}
    tasks: tuple[Any, ...] = ()

    async def astream_events(self, *_a: Any, **_k: Any) -> Any:
        if False:  # pragma: no cover  # noqa: RET503 — async generator shape
            yield

    async def aget_state(self, *_a: Any, **_k: Any) -> Any:
        return self


async def test_translator_records_event_push_latency() -> None:
    """drainer 落库后记 ``event_push_latency_seconds`` = 落库时刻 − event.ts（>0）。"""

    clock = _step_clock()
    store = InMemoryTraceEventStore(clock=clock)
    metrics = OpsMetrics()
    tx = EventTranslator(
        store,
        session_id="s1",
        trace_id="t1",
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=frozenset(),
        clock=clock,
        metrics=metrics,
    )
    await tx.feed({"event": "on_chain_start", "name": "LangGraph", "tags": [], "data": {}})
    await tx.flush()
    assert metrics.event_push_latency_seconds.value > 0


async def test_runservice_records_graph_execution_duration() -> None:
    """fresh run 完成后记 ``graph_execution_duration_seconds``（>0）。"""

    clock = _step_clock()
    orch = Orchestrator()
    orch.graph = _FastGraph()
    metrics = OpsMetrics()
    service = RunService(
        orch,
        InMemorySessionCache(clock=clock),
        trace_store=InMemoryTraceEventStore(clock=clock),
        metrics=metrics,
        config=RunServiceConfig(),
        clock=clock,
    )
    resp = await service.run(
        RunRequest(session_id="s1", query="改", document="doc"),
        user_id="u1",
    )
    assert resp.status.value == "SUCCESS"
    assert metrics.graph_execution_duration_seconds.value > 0
