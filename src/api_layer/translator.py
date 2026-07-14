"""``astream_events`` → PRD §6.4 事件翻译层（T-05·ADR-0023）。

消费 langgraph 编译图的 ``astream_events(version="v2")``，把 langchain 事件词汇映射为
§6.4 生命周期事件类型、mint 单 trace 内单调的 ``event_seq``、**非阻塞**写
:class:`api_layer.trace_store.TraceEventStoreBase`。ADR-0023 不变量：显示侧落库不得反压图、
不得因写慢 / 写失败杀图——故落库经独立 drainer 协程串行 await，``feed`` 仅入队（``put_nowait``）。

事件映射（``astream_events`` v2 → §6.4）：

- ``on_chain_start`` ``name='LangGraph'`` → ``trace_start``（仅首段，``start_seq==0``；
  resume 段同 trace 已有首推，不重产）。
- ``on_chain_start`` / ``on_chain_stream`` / ``on_chain_end`` 带 ``graph:step:N`` tag、
  ``name != 'LangGraph'`` → ``node_start`` / ``node_output`` / ``node_end``（携带
  ``node_id`` / ``node_instance`` / ``label`` / ``type`` / ``color`` / ``input`` / ``output``）。
- ``on_llm_stream`` → ``llm_thinking``（``token`` + 累积 ``full_thought``；按 ``parent_ids``
  归属所在节点 run）。
- ``on_tool_start`` → ``tool_call``（``node_id`` / ``name`` / ``args``）。
- ``human_pause`` / ``stream_finish`` / ``stream_abort`` 由驱动者（:class:`api_layer.run.RunService`）
  在 ``astream_events`` 结束后据 ``aget_state`` 终态显式 ``emit_*``——``human_pause`` 的
  ``question`` / ``hint`` 取自 checkpoint interrupt payload（与 HTTP ``NEED_HUMAN_INPUT`` 同源）。
- ``graph_static`` / ``heartbeat`` **不由翻译层产**（T-06 WS-sender 产，见 :class:`api_layer.trace_store.EventType`）。

``visible=False`` 节点（:func:`api_layer.graph_view.compute_hidden_names`）：翻译层丢弃其
``node_*`` / ``llm_thinking`` / ``tool_call``，保留 trace 级事件（``trace_start`` /
``human_pause`` / ``stream_finish`` 等）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from api_layer.trace_store import EventType, TraceEvent, TraceEventStoreBase

__all__ = ["EventTranslator"]

_logger = logging.getLogger(__name__)

#: drainer 收到此哨兵即停止（flush 用）。
_SENTINEL: Any = object()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _json_safe(obj: Any) -> Any:
    """把任意 langgraph state 值（bytes / pydantic / dataclass / Enum / 原生）转 JSON 安全形。

    ``astream_events`` 的 ``input`` / ``output`` / ``chunk`` 可能含 ``bytes``（``original_doc``）、
    pydantic ``Argument`` / ``Hitl*Question``、dataclass 等 JSONB 直存不了的类型——本函数递归
    归一为 dict / list / 原生，使 ``trace_events.payload`` JSONB 落库不抛。
    """

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="surrogateescape")
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _json_safe(getattr(obj, f.name)) for f in dataclasses.fields(obj)
        }
    return str(obj)


def _extract_token(chunk: Any) -> str | None:
    """从 ``on_llm_stream`` 的 ``data['chunk']``（``BaseMessageChunk``）取文本 token。

    空内容（``content == ""``，如纯 tool-call 元数据 chunk）→ ``None``（不产 llm_thinking）。
    非字符串 content（少数模型返回 content blocks 列表）→ 取 str 形。
    """

    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content or None
    if content is None:
        return None
    text = str(content)
    return text or None


class EventTranslator:
    """``astream_events`` → §6.4 事件翻译层（非阻塞落库）。

    一次 ``/api/agent/run`` 驱动（fresh 或 resume 段）构造一个 translator：``feed`` 消费
    astream_events 原始事件、映射 + mint ``event_seq`` + 入队；驱动者在 astream_events 结束后
    调 ``emit_human_pause`` / ``emit_stream_finish`` / ``emit_stream_abort`` 产终态事件；
    ``flush`` 等 drainer 排空（保证返回 client 前事件 durable，但不反压图推进）。

    :attr:`start_seq` 为本段起始 ``event_seq``（fresh=0；resume= ``store.max_seq(trace_id)+1``，
    续跑顺延无断层）。``node_instance`` 单 trace 内同节点第几次触发（从 0 起，区分回放环）。
    """

    def __init__(
        self,
        store: TraceEventStoreBase,
        *,
        session_id: str,
        trace_id: str,
        start_seq: int,
        manifest_index: dict[str, Any],
        hidden: frozenset[str],
        clock: Callable[[], datetime] = _utcnow,
        prior_node_instances: dict[str, int] | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._trace_id = trace_id
        self._next_seq = start_seq
        self._manifest_index = manifest_index
        self._hidden = hidden
        self._clock = clock
        self._queue: asyncio.Queue[TraceEvent | Any] = asyncio.Queue()
        self._drainer: asyncio.Task[None] | None = None
        # node_id → 已触发次数（node_instance = 触发前计数，从 0 起）。续跑段 seed 自
        # store 中既有 node_start 计数，使跨段连续（区分回放环 / 多次触发）。
        self._node_instances: dict[str, int] = dict(prior_node_instances or {})
        # 节点 run_id → node_id（LLM / tool 经 parent_ids 归属节点）。
        self._node_runs: dict[str, str] = {}
        # llm run_id → 累积 token 列表（产 full_thought）。
        self._llm_full: dict[str, list[str]] = {}
        # 首段产 trace_start；resume 段（start_seq > 0）不重产。
        self._emit_trace_start = start_seq == 0

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    async def feed(self, raw: dict[str, Any]) -> None:
        """消费一个 astream_events 原始事件：映射 + mint + 入队（非阻塞）。"""

        self._ensure_drainer()
        for event in self._map(raw):
            self._queue.put_nowait(event)

    async def emit_human_pause(
        self,
        node_id: str,
        question: str,
        hint: str,
        detail: dict[str, Any],
    ) -> None:
        """产 ``human_pause``（``question`` / ``hint`` / ``detail`` 取自 interrupt payload）。"""

        self._ensure_drainer()
        self._queue.put_nowait(
            self._mint(
                EventType.HUMAN_PAUSE,
                {
                    "node_id": node_id,
                    "question": question,
                    "hint": hint,
                    "detail": _json_safe(detail),
                },
            )
        )

    async def emit_stream_finish(self) -> None:
        self._ensure_drainer()
        self._queue.put_nowait(self._mint(EventType.STREAM_FINISH, {}))

    async def emit_stream_abort(self, reason: str) -> None:
        self._ensure_drainer()
        self._queue.put_nowait(
            self._mint(EventType.STREAM_ABORT, {"abort_reason": reason})
        )

    async def flush(self) -> None:
        """等 drainer 排空：保证返回 client 前本段事件 durable。"""

        self._ensure_drainer()
        assert self._drainer is not None
        await self._queue.put(_SENTINEL)
        await self._drainer

    # ------------------------------------------------------------------ #
    # 映射
    # ------------------------------------------------------------------ #

    def _mint(self, event_type: EventType, payload: dict[str, Any]) -> TraceEvent:
        seq = self._next_seq
        self._next_seq += 1
        return TraceEvent(
            session_id=self._session_id,
            trace_id=self._trace_id,
            event_seq=seq,
            event_type=event_type,
            payload=payload,
            ts=self._clock(),
        )

    def _map(self, raw: dict[str, Any]) -> list[TraceEvent]:
        event = raw.get("event")
        name = raw.get("name")
        tags = raw.get("tags") or []
        is_graph_step = any(str(t).startswith("graph:step") for t in tags)

        # graph 级 chain 事件：仅 on_chain_start LangGraph → trace_start（首段）。
        if name == "LangGraph":
            if event == "on_chain_start" and self._emit_trace_start:
                self._emit_trace_start = False
                return [self._mint(EventType.TRACE_START, {})]
            return []

        if not is_graph_step:
            # LLM / tool 事件：经 parent_ids 归属节点。
            return self._map_non_node(raw)

        assert name is not None
        return self._map_node(raw, event, name)

    def _map_node(
        self, raw: dict[str, Any], event: Any, node_id: str
    ) -> list[TraceEvent]:
        run_id = raw.get("run_id")
        data = raw.get("data") or {}
        hidden = node_id in self._hidden

        if event == "on_chain_start":
            if run_id is not None:
                self._node_runs[run_id] = node_id
            inst = self._node_instances.get(node_id, 0)
            self._node_instances[node_id] = inst + 1
            if hidden:
                return []
            entry = self._manifest_index.get(node_id)
            return [
                self._mint(
                    EventType.NODE_START,
                    {
                        "node_id": node_id,
                        "node_instance": inst,
                        "label": (entry.label if entry is not None else node_id) or node_id,
                        "type": (entry.node_type if entry is not None else "") or "",
                        "color": entry.color if entry is not None else None,
                        "input": _json_safe(data.get("input")),
                    },
                )
            ]
        if event == "on_chain_stream":
            if hidden:
                return []
            return [
                self._mint(
                    EventType.NODE_OUTPUT,
                    {
                        "node_id": node_id,
                        "node_instance": self._running_instance(node_id),
                        "output": _json_safe(data.get("chunk")),
                    },
                )
            ]
        if event == "on_chain_end":
            if run_id is not None:
                self._node_runs.pop(run_id, None)
            if hidden:
                return []
            return [
                self._mint(
                    EventType.NODE_END,
                    {
                        "node_id": node_id,
                        "node_instance": self._running_instance(node_id),
                        "output": _json_safe(data.get("output")),
                    },
                )
            ]
        return []

    def _map_non_node(self, raw: dict[str, Any]) -> list[TraceEvent]:
        event = raw.get("event")
        run_id = raw.get("run_id")
        parent_ids = raw.get("parent_ids") or []
        node_id = self._node_for_run(parent_ids)
        hidden = node_id in self._hidden if node_id is not None else False
        data = raw.get("data") or {}

        if event == "on_llm_start":
            if run_id is not None:
                self._llm_full[run_id] = []
            return []
        if event == "on_llm_stream":
            token = _extract_token(data.get("chunk"))
            if token is None:
                return []
            if run_id is not None:
                self._llm_full.setdefault(run_id, []).append(token)
                full_text = "".join(self._llm_full[run_id])
            else:
                full_text = token
            if hidden:
                return []
            assert node_id is not None
            return [
                self._mint(
                    EventType.LLM_THINKING,
                    {
                        "node_id": node_id,
                        "token": token,
                        "full_thought": full_text,
                    },
                )
            ]
        if event == "on_llm_end":
            if run_id is not None:
                self._llm_full.pop(run_id, None)
            return []
        if event == "on_tool_start":
            if hidden:
                return []
            assert node_id is not None
            return [
                self._mint(
                    EventType.TOOL_CALL,
                    {
                        "node_id": node_id,
                        "name": raw.get("name"),
                        "args": _json_safe(data.get("input")),
                    },
                )
            ]
        return []

    # ------------------------------------------------------------------ #
    # 内部 helper
    # ------------------------------------------------------------------ #

    def _running_instance(self, node_id: str) -> int:
        """当前正在跑的实例号 = 已 start 计数 - 1（on_chain_stream / _end 时该实例尚在计数内）。"""

        return max(self._node_instances.get(node_id, 0) - 1, 0)

    def _node_for_run(self, parent_ids: list[Any]) -> str | None:
        """据 ``parent_ids`` 找首个已登记的节点 run，返回其 node_id。"""

        for rid in parent_ids:
            node = self._node_runs.get(rid)
            if node is not None:
                return node
        return None

    def _ensure_drainer(self) -> None:
        if self._drainer is None or self._drainer.done():
            self._drainer = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """串行 await store.append；写失败降级记错、不抛（ADR-0023 不变量）。"""

        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            try:
                await self._store.append(item)
            except Exception:
                _logger.exception(
                    "trace_events append 失败（trace_id=%s event_seq=%s type=%s）——降级、不阻塞图",
                    item.trace_id,
                    item.event_seq,
                    item.event_type.value,
                )
