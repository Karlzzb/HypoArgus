"""WS-sender：``trace_events`` 只读尾随 + 心跳 + 背压 + 重连回放（T-06·ADR-0023）。

每条 WS 连接一个 WS-sender，**只读尾随** ``trace_events``：建连首推 ``graph_static``、按
``event_seq`` 回放该会话已落库事件到最新、再接 :meth:`TraceEventStoreBase.subscribe` live 尾随。
WS 是只读显示视图——连、断、慢都**不启动、不中止、不阻塞** run（ADR-0023 不变量）：run 的唯一
控制输入是 ``/api/agent/run``；本模块只读 ``trace_events`` + **只读校验** T-04 所有权 seam
（WS 不登记 / 不绑定 / 不 touch ``session_owner``——所有权绑定由 run 控制面独占，
WS 首见未绑定会话仅允许显示先连，不 hijack 随后 run 的归属）。

架构（背压队列只作用于 WS-sender→WS 之间，不反压图 / 落库）：

- 翻译层（T-05）写 ``trace_events`` + ``NOTIFY`` 已非阻塞完成（durable）；本模块只消费。
- live 尾随 ``async for ev in sub`` 把新事件推入有界 :class:`_BackpressureBuffer`（``maxsize=256``）；
  独立 send 协程从缓冲出队、序列化为 §6.3 消息、``websocket.send_text``。
- 缓冲满 → ``llm_thinking`` token 合并到队尾同类事件（同 trace）；其余事件 live 丢弃安全
  （已在 ``trace_events`` durable，重连按 ``event_seq`` 回放必补）；关键事件
  （``human_pause`` / ``stream_finish`` / ``stream_abort``）durable 不真正丢失。
- 心跳：send 协程 ``wait_for(buffer.pop(), heartbeat_interval)``，超时即发 ``heartbeat``
  （``event_seq=-1``，前端丢弃）。
- 同 ``session_id`` 新连接建连时旧连接被取代（停 send 协程 + close WS，**不发** ``stream_abort``）。

WS 断开永不触发 ``stream_abort``——``stream_abort`` 仅由锁 TTL 孤儿 / PauseMeta TTL 孤儿 /
显式 HTTP cancel（孤儿扫描在 T-08）。本模块消费 ``trace_events`` 中已 durable 的 ``stream_abort``
行并原样下发，但不产 abort。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from api_layer.graph_view import GraphView, build_graph_view
from api_layer.session_cache import SessionCacheBase
from api_layer.trace_store import (
    EventType,
    TraceEvent,
    TraceEventStoreBase,
    TraceSubscription,
)

__all__ = [
    "WsMetrics",
    "WSSenderConfig",
    "WSSenderService",
    "WS_CLOSE_FORBIDDEN",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_WS_QUEUE_MAXSIZE",
]

_logger = logging.getLogger(__name__)

#: 归属校验失败 / 跨用户 / 缺 ``X-User-Id`` 的 WS close code（PRD §6.1）。
WS_CLOSE_FORBIDDEN: int = 4001

#: 心跳间隔：30s 无数据帧即发 ``heartbeat``（PRD §6.4）。
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 30.0

#: 背压队列上限（PRD §6.2）。
DEFAULT_WS_QUEUE_MAXSIZE: int = 256


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class _WebSocket(Protocol):
    """WS-sender 依赖的 WebSocket 接口子集（FastAPI ``WebSocket`` 满足；亦供单测 fake）。"""

    async def accept(self) -> None: ...
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = ...) -> None: ...


# --------------------------------------------------------------------------- #
# 指标（T-06 只埋点；/metrics 端点在 T-08 落地）
# --------------------------------------------------------------------------- #


@dataclass
class WsMetrics:
    """WS-sender 运行指标快照源（PRD §6.2 ``ws_event_queue_size`` / ``ws_event_queue_full_total``）。

    单线程 async 协作模型下 int 增减无锁安全。活跃发送缓冲经 :meth:`register` /
    :meth:`unregister` 登记，:meth:`snapshot` 汇总当前各缓冲深度之和供 T-08 ``/metrics`` 暴露。
    """

    queue_full_total: int = 0
    _buffers: list[_BackpressureBuffer] = field(default_factory=list)

    def record_queue_full(self) -> None:
        """背压队列满（合并 / 丢弃）一次计数 +1。"""

        self.queue_full_total += 1

    def register(self, buf: _BackpressureBuffer) -> None:
        self._buffers.append(buf)

    def unregister(self, buf: _BackpressureBuffer) -> None:
        while buf in self._buffers:
            self._buffers.remove(buf)

    def snapshot(self) -> dict[str, int]:
        """当前指标值（T-08 ``/metrics`` 读取）。"""

        size = sum(len(b) for b in self._buffers)
        return {
            "ws_event_queue_size": size,
            "ws_event_queue_full_total": self.queue_full_total,
        }


# --------------------------------------------------------------------------- #
# 背压缓冲
# --------------------------------------------------------------------------- #


class _BackpressureBuffer:
    """WS-sender→WS 之间的有界背压缓冲（``deque`` + ``asyncio.Event``，cap=256）。

    :meth:`push` 非阻塞：未满直入队；满则按 PRD §6.2 降级——``llm_thinking`` token 合并到队尾
    同 trace 的同类事件（更新 ``token`` / ``full_thought``），其余事件 live 丢弃
    （``trace_events`` 已 durable，重连回放必补）。关键事件 durable 不真正丢失。满次计数经
    :class:`WsMetrics`。

    :meth:`pop` 阻塞至有事件或被 :meth:`close` 唤醒；send 协程以 ``wait_for(pop, heartbeat)``
    超时发心跳。
    """

    def __init__(self, maxsize: int, metrics: WsMetrics) -> None:
        self._dq: deque[TraceEvent] = deque()
        self._cap = maxsize
        self._ev = asyncio.Event()
        self._metrics = metrics
        self._closed = False

    def __len__(self) -> int:
        return len(self._dq)

    def push(self, event: TraceEvent) -> None:
        if self._closed:
            return
        if len(self._dq) < self._cap:
            self._dq.append(event)
            self._ev.set()
            return
        # 满降级（ADR-0023：背压只作用 WS-sender→WS；落库已完成、重连必补）。
        self._metrics.record_queue_full()
        if event.event_type is EventType.LLM_THINKING:
            self._merge_llm_thinking(event)
            return
        # 其余事件 live 丢弃（durable 行经重连回放补齐）；关键事件永不真正丢失。
        _logger.debug(
            "ws 背压缓冲满，丢弃 live 事件（type=%s seq=%s）——durable，重连回放补齐",
            event.event_type.value,
            event.event_seq,
        )

    def _merge_llm_thinking(self, event: TraceEvent) -> None:
        """把溢出的 ``llm_thinking`` token 合并到队尾同 trace 的同类事件（PRD §6.2）。

        队尾无同 trace 的 ``llm_thinking`` 时丢弃该 token（``llm_thinking`` 非 durable 关键事件，
        丢失一个 live token 不影响终态；``full_thought`` 由翻译层在后续 token 累积重建）。
        """

        from dataclasses import replace

        for i in range(len(self._dq) - 1, -1, -1):
            prev = self._dq[i]
            if (
                prev.event_type is EventType.LLM_THINKING
                and prev.trace_id == event.trace_id
            ):
                merged_token = str(prev.payload.get("token", "")) + str(
                    event.payload.get("token", "")
                )
                # full_thought 取最新累积值（翻译层逐 token 累积，最新即最完整）。
                new_full = event.payload.get("full_thought") or prev.payload.get(
                    "full_thought", ""
                )
                self._dq[i] = replace(
                    prev,
                    payload={
                        **prev.payload,
                        "token": merged_token,
                        "full_thought": new_full,
                    },
                )
                return
        _logger.debug(
            "ws 背压缓冲满且队尾无同 trace llm_thinking，丢弃 token（trace=%s seq=%s）",
            event.trace_id,
            event.event_seq,
        )

    async def pop(self) -> TraceEvent:
        """阻塞至有事件；``close`` 唤醒后抛 ``StopAsyncIteration``。"""

        while not self._dq:
            if self._closed:
                raise StopAsyncIteration
            self._ev.clear()
            await self._ev.wait()
        return self._dq.popleft()

    def close(self) -> None:
        self._closed = True
        self._ev.set()


# --------------------------------------------------------------------------- #
# 消息序列化（PRD §6.3）
# --------------------------------------------------------------------------- #


def _message(
    session_id: str, trace_id: str, event_seq: int, event_type: EventType, payload: Any
) -> str:
    """§6.3 单条消息：``session_id`` / ``trace_id`` / ``event_seq`` / ``event_type`` / ``payload``。"""

    return json.dumps(
        {
            "session_id": session_id,
            "trace_id": trace_id,
            "event_seq": event_seq,
            "event_type": event_type.value,
            "payload": payload,
        },
        ensure_ascii=False,
    )


def _graph_static_message(session_id: str, gv: GraphView) -> str:
    """建连首推 ``graph_static``（``event_seq=-1``，来自 T-02 :func:`build_graph_view`）。"""

    return _message(
        session_id,
        "",
        -1,
        EventType.GRAPH_STATIC,
        {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "type": n.type,
                    "color": n.color,
                    "visible": n.visible,
                    "interrupt": n.interrupt,
                }
                for n in gv.nodes
            ],
            "edges": [
                {"source": e.source, "target": e.target, "cond": e.cond, "max": e.max}
                for e in gv.edges
            ],
            "warnings": list(gv.warnings),
        },
    )


def _heartbeat_message(session_id: str) -> str:
    """``heartbeat``（``event_seq=-1``，前端丢弃；PRD §6.4）。"""

    return _message(session_id, "", -1, EventType.HEARTBEAT, {})


# --------------------------------------------------------------------------- #
# 单会话连接句柄（同 session 新连接取代旧连接）
# --------------------------------------------------------------------------- #


@dataclass
class _ConnectionHandle:
    """同 ``session_id`` 当前活跃 WS-sender 句柄。

    新连接建连时调 :meth:`supersede`：置 stop + 取消 tail / send 协程 → 旧连接停 send 并 close
    （**不发** ``stream_abort``，仅停下发）。旧 serve 的 finally 仅当 ``self._active[sid] is self``
    才清登记，避免覆盖更新连接。
    """

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    tail_task: asyncio.Task[None] | None = None
    send_task: asyncio.Task[None] | None = None

    async def supersede(self) -> None:
        self.stop.set()
        for t in (self.tail_task, self.send_task):
            if t is not None and not t.done():
                t.cancel()


# --------------------------------------------------------------------------- #
# WSSenderService
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WSSenderConfig:
    """WS-sender 可调参数（PRD §6.2 / §6.4）。"""

    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    queue_maxsize: int = DEFAULT_WS_QUEUE_MAXSIZE


class WSSenderService:
    """每条 WS 连接的只读尾随服务（ADR-0023 不变量贯穿）。

    注入 :class:`SessionCacheBase`（所有权 seam，复用 T-04）、:class:`TraceEventStoreBase`
    （回放 + live 尾随）、``manifest`` + :class:`VisibilityConfig`（产 ``graph_static``，复用 T-02
    :func:`build_graph_view`）、:class:`WSSenderConfig` 与 :class:`WsMetrics`。
    """

    def __init__(
        self,
        session_cache: SessionCacheBase,
        trace_store: TraceEventStoreBase,
        *,
        manifest: tuple[Any, ...],
        visibility: Any,
        config: WSSenderConfig | None = None,
        metrics: WsMetrics | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._cache = session_cache
        self._store = trace_store
        self._manifest = manifest
        self._visibility = visibility
        self._config = config or WSSenderConfig()
        self._metrics = metrics or WsMetrics()
        self._clock = clock
        self._active: dict[str, _ConnectionHandle] = {}

    @property
    def metrics(self) -> WsMetrics:
        return self._metrics

    def active_connection_count(self) -> int:
        """当前活跃 WS-sender 连接数（同会话新连接取代旧连接，故 = 活跃句柄数）。供 /health。"""

        return len(self._active)

    async def serve(
        self, websocket: _WebSocket, session_id: str, user_id: str
    ) -> None:
        """一条 WS 连接的完整生命周期（所有权 → graph_static → 回放 → live → 心跳 / 背压）。

        任何路径均不抛 ``stream_abort``、不中止 run；异常 / 断开 / 取代均静默收尾。
        """

        await websocket.accept()
        if not await self._enforce_ownership(session_id, user_id, websocket):
            return  # 归属失败已 close 4001。

        # 取代同会话旧连接（停 send、不发 abort）。先登记新 handle 再 supersede 旧：
        # 跨 ``await old.supersede()`` 期间 ``_active`` 始终持有该会话的句柄，第三方并发
        # serve 不会见 None 而跳过 supersede、随后被覆盖以致孤儿（LISTEN 连接 + WS 泄漏）。
        handle = _ConnectionHandle()
        old = self._active.get(session_id)
        self._active[session_id] = handle
        if old is not None and old is not handle:
            await old.supersede()

        buf = _BackpressureBuffer(self._config.queue_maxsize, self._metrics)
        self._metrics.register(buf)
        sub: TraceSubscription | None = None
        try:
            # subscribe 在 try 内：若 ``subscribe`` 在开 LISTEN 连接后抛，_finalize 仍关连接 + WS。
            sub = await self._store.subscribe(session_id)
            await websocket.send_text(_graph_static_message(session_id, build_graph_view(self._manifest, self._visibility)))
            seen: dict[str, int] = {}
            await self._replay(websocket, session_id, seen)
            handle.tail_task = asyncio.create_task(self._tail(sub, buf, handle, seen))
            handle.send_task = asyncio.create_task(
                self._send(websocket, session_id, buf, handle)
            )
            done, pending = await asyncio.wait(
                {handle.tail_task, handle.send_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in done:
                if t.cancelled():
                    continue  # 被取代 / 断开取消；CancelledError 不算异常
                exc = t.exception()
                if exc and not isinstance(exc, StopAsyncIteration):
                    _logger.debug("ws 协程退出：%r", exc)
        finally:
            # 真实部署下 WS 客户端断开 → uvicorn 取消本 serve 协程；create_task 子任务不会自动取消，
            # 故此处显式取消并经 ``shield`` 排空（即便本协程被取消，_finalize 仍跑完——关 LISTEN 连接、
            # 释放订阅、关 WS；CancelledError 其后再抛，子任务无泄漏、PG LISTEN 连接不漏）。
            handle.stop.set()
            buf.close()
            await asyncio.shield(
                self._finalize(handle, buf, sub, session_id, websocket)
            )

    async def _finalize(
        self,
        handle: _ConnectionHandle,
        buf: _BackpressureBuffer,
        sub: TraceSubscription | None,
        session_id: str,
        websocket: _WebSocket,
    ) -> None:
        """收尾：取消并等 tail / send 子任务、关订阅、注销缓冲、关 WS。幂等、不抛。"""

        for t in (handle.tail_task, handle.send_task):
            if t is not None and not t.done():
                t.cancel()
        for t in (handle.tail_task, handle.send_task):
            if t is None:
                continue
            try:
                await t
            except BaseException:
                pass
        # ``except BaseException``（非 Exception）：cancellation 落在 sub.close() 上时
        # CancelledError 是 BaseException，须吞掉使后续 unregister / _active pop / _safe_close 必跑，
        # 否则 ws_event_queue_size 随重连向上漂移。
        if sub is not None:
            try:
                await sub.close()
            except BaseException:
                pass
        self._metrics.unregister(buf)
        if self._active.get(session_id) is handle:
            self._active.pop(session_id, None)
        await self._safe_close(websocket)

    # ------------------------------------------------------------------ #
    # 所有权（复用 T-04 SessionCacheBase seam）
    # ------------------------------------------------------------------ #

    async def _enforce_ownership(
        self, session_id: str, user_id: str, websocket: _WebSocket
    ) -> bool:
        """WS 归属**只读校验**（ADR-0023）：缺 / 跨用户 → close 4001；首见或匹配 → 允许。

        与 :meth:`RunService._enforce_ownership` 同源读 ``session_owner`` seam，但 WS 是
        只读显示视图——**不登记、不绑定、不 touch** ``session_owner``：既不 hijack 未绑定
        会话的归属（让随后 run 的 ``/api/agent/run`` 独占绑定），也不触达活跃上限计数
        （避免 WS-only 流量撑满 ``session_limit`` 致合法 run 被拒的 DoS）。

        - ``user_id`` 缺 → close 4001。
        - ``existing is None``（未绑定）→ 允许（显示可先于 run 到达；所有权由 run 绑定）。
        - ``existing == user_id`` → 允许（不 touch）。
        - ``existing != user_id`` → close 4001。
        """

        if not user_id:
            await websocket.close(code=WS_CLOSE_FORBIDDEN)
            return False
        existing = await self._cache.get_session_owner(session_id)
        if existing is None:
            return True  # 未绑定：显示可先于 run 连接；WS 不绑定（由 run 控制面独占）。
        if existing != user_id:
            await websocket.close(code=WS_CLOSE_FORBIDDEN)
            return False
        return True  # 匹配：允许；不 touch（不喂活跃计数）。

    # ------------------------------------------------------------------ #
    # 回放（重连按 event_seq 回放到最新）
    # ------------------------------------------------------------------ #

    async def _replay(
        self, websocket: _WebSocket, session_id: str, seen: dict[str, int]
    ) -> None:
        """按 ``event_seq`` 回放该会话已落库事件到最新（ADR-0023 重连即恢复）。

        ``seen`` 记 per-trace 已发最大 ``event_seq``，供 live 阶段去重交界竞态。
        """

        rows = await self._store.events_for_session(session_id)
        for ev in rows:
            cur = seen.get(ev.trace_id, -1)
            if ev.event_seq <= cur:
                continue
            await websocket.send_text(_message(ev.session_id, ev.trace_id, ev.event_seq, ev.event_type, ev.payload))
            seen[ev.trace_id] = ev.event_seq

    # ------------------------------------------------------------------ #
    # live 尾随（背压缓冲生产端）
    # ------------------------------------------------------------------ #

    async def _tail(
        self,
        sub: TraceSubscription,
        buf: _BackpressureBuffer,
        handle: _ConnectionHandle,
        seen: dict[str, int],
    ) -> None:
        """``async for`` 订阅新事件、推入背压缓冲（非阻塞 push；满则合并 / 丢弃）。

        live 事件按 per-trace ``event_seq`` 跳过已下发序号——``seen`` 由
        :meth:`_replay` 填充为该会话已回放到的最大序号。``subscribe``→``_replay`` 窗口内
        落库的事件既进 live 队列又被 ``_replay`` 读到，须仅发一次（ADR-0023 不重发）。
        ``seen`` 仅 ``_tail`` 在 ``_replay`` 完成后读写，无并发。
        """

        try:
            async for ev in sub:
                if handle.stop.is_set():
                    return
                if ev.event_seq <= seen.get(ev.trace_id, -1):
                    continue  # 回放已下发：交界竞态去重。
                seen[ev.trace_id] = ev.event_seq
                buf.push(ev)
        except StopAsyncIteration:
            return

    # ------------------------------------------------------------------ #
    # send 协程（背压缓冲消费端 + 心跳）
    # ------------------------------------------------------------------ #

    async def _send(
        self,
        websocket: _WebSocket,
        session_id: str,
        buf: _BackpressureBuffer,
        handle: _ConnectionHandle,
    ) -> None:
        """从背压缓冲出队、按序下发；``heartbeat_interval`` 无数据帧即发心跳。"""

        heartbeat = timedelta(seconds=self._config.heartbeat_interval_seconds)
        last_send = self._clock()
        while not handle.stop.is_set():
            try:
                ev = await asyncio.wait_for(
                    buf.pop(), timeout=self._config.heartbeat_interval_seconds
                )
            except TimeoutError:
                if self._clock() - last_send >= heartbeat:
                    try:
                        await websocket.send_text(_heartbeat_message(session_id))
                        last_send = self._clock()
                    except Exception:
                        return  # WS 已断开：静默退出（不中止 run）。
                continue
            except StopAsyncIteration:
                return
            try:
                await websocket.send_text(
                    _message(
                        ev.session_id, ev.trace_id, ev.event_seq, ev.event_type, ev.payload
                    )
                )
                last_send = self._clock()
            except Exception:
                return  # WS 已断开：静默退出（不中止 run）。

    # ------------------------------------------------------------------ #

    async def _safe_close(self, websocket: _WebSocket) -> None:
        try:
            await websocket.close(code=1000)
        except Exception:
            pass
