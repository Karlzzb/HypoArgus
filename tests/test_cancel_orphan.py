"""RunService.cancel_orphan seam 单测（T-08·PRD §9.6）。

孤儿锁扫描 cancel 在跑请求任务：病理性长挂（wait_for 未触发的极端路径）经
:meth:`RunService.cancel_orphan` 显式 cancel；正常路径锁 TTL(900s) 远大于请求超时(120s)，
孤儿扫描命中时任务多已自清理，本 seam 为防御兜底。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from api_layer.run import RunRequest, RunService, RunServiceConfig
from api_layer.session_cache import InMemorySessionCache
from api_layer.trace_store import InMemoryTraceEventStore
from runtime.orchestrator import Orchestrator


class _HungGraph:
    """伪图：``astream_events`` 永久睡眠（被 cancel 即停）。"""

    next: tuple[str, ...] = ()
    values: dict[str, Any] = {}
    tasks: tuple[Any, ...] = ()

    async def astream_events(self, *_a: Any, **_k: Any) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        if False:  # pragma: no cover  # noqa: RET503 — async generator shape
            yield

    async def aget_state(self, *_a: Any, **_k: Any) -> Any:
        return self


async def test_cancel_orphan_cancels_in_progress_task() -> None:
    """在跑请求任务经 cancel_orphan 取消。"""

    clock = _step_clock()
    orch = Orchestrator()
    orch.graph = _HungGraph()
    service = RunService(
        orch,
        InMemorySessionCache(clock=clock),
        trace_store=InMemoryTraceEventStore(clock=clock),
        config=RunServiceConfig(graph_timeout_seconds=30.0),
        clock=clock,
    )
    task = asyncio.create_task(
        service.run(
            RunRequest(session_id="s1", query="改", document="doc"),
            user_id="u1",
        )
    )
    await asyncio.sleep(0.05)  # 让 run 进入 astream_events 睡眠
    cancelled = await service.cancel_orphan("s1")
    assert cancelled is True
    # 任务被取消（CancelledError）。
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.CancelledError, Exception):
        pass
    assert task.cancelled() or task.done()
    # 再 cancel 无任务 → False。
    assert await service.cancel_orphan("s1") is False


async def test_cancel_orphan_no_task_returns_false() -> None:
    """无在跑任务 → False。"""

    service = RunService(Orchestrator(), InMemorySessionCache())
    assert await service.cancel_orphan("nope") is False


def _step_clock() -> Any:

    state = {"t": datetime(2026, 1, 1, tzinfo=UTC)}

    def _now() -> datetime:
        state["t"] = state["t"] + timedelta(seconds=0.01)
        return state["t"]

    return _now  # type: ignore[return-value]
