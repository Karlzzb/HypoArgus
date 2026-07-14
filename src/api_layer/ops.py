"""运维加固服务（T-08·PRD §9 / §11）：/health、/metrics、后台 sweep、告警。

把 §9 / §11 的兜底 / 可观测从「惰性命中」补成「后台主动兜底 + 可见」，不新增控制流语义：
``stream_abort`` 仍只由锁 TTL 孤儿 / PauseMeta TTL 孤儿 / 显式 cancel 触发（ADR-0023 不变量）。

- :meth:`health` → ``{db, active_sessions, active_locks, ws_connections}``（PRD §11）。
- :meth:`metrics_text` → Prometheus 文本（:func:`api_layer.metrics.render_prometheus`），
  合并 :class:`OpsMetrics` 自有指标 + :class:`api_layer.ws.WsMetrics` 队列快照 + 会话 / 锁 / 连接
  实时计数（单一真相、不双计）。
- :meth:`sweep` → 后台单次清扫（PRD §9.2 / §9.6 / §9.7）：
  - Phase A：过期 ``pause_meta``（30min+）→ 推 ``stream_abort``（``abort_reason="pause_expired"``）
    + 删 pause_meta + 删对应 ``session_locks`` 行（§9.2）；
  - Phase B：过期 ``session_locks``（heartbeat + TTL）且**无活跃 pause_meta** → 孤儿 run，
    推 ``stream_abort``（``abort_reason="lock_orphan"``）+ 删锁行 + cancel 在跑请求任务（§9.6）；
    有活跃 pause_meta → 跳过（HITL 暂停期不写心跳、由 pause_meta 30min TTL 管辖，不误杀）；
  - 活跃会话数达 ``session_limit`` 80% → 结构化日志 warning 告警（§9.7）。

后台周期由调用方（:func:`api_layer.server.serve`）起 ``asyncio.create_task`` 循环调 :meth:`sweep`；
单实例一期（PRD §4.4 跨实例扇出属二期）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from api_layer.errors import PAUSE_TTL_SECONDS
from api_layer.metrics import MetricSample, OpsMetrics, render_prometheus
from api_layer.session_cache import DEFAULT_LOCK_TTL_SECONDS, SessionCacheBase
from api_layer.trace_store import EventType, TraceEvent, TraceEventStoreBase
from api_layer.ws import WSSenderService

__all__ = ["OpsConfig", "SweepReport", "OpsService", "DEFAULT_SWEEP_INTERVAL_SECONDS"]

_logger = logging.getLogger(__name__)

#: 后台 sweep 默认周期（秒）。单实例一期保守 60s（PRD §11）。
DEFAULT_SWEEP_INTERVAL_SECONDS: float = 60.0


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class OpsConfig:
    """运维可调参数（PRD §9.2 / §9.6 / §9.7）。"""

    session_limit: int = 100
    session_limit_warn_ratio: float = 0.8
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
    pause_ttl_seconds: int = PAUSE_TTL_SECONDS
    sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS


@dataclass
class SweepReport:
    """单次 sweep 清扫报告。"""

    pause_expired: int = 0
    lock_orphan: int = 0
    lock_skipped_active_pause: int = 0
    session_warn_ratio: float = 0.0


@dataclass
class OpsService:
    """运维加固服务：健康 / 指标 / 后台 sweep。

    注入 :class:`SessionCacheBase`（会话 / 锁 / pause 扫描 + ping）、:class:`TraceEventStoreBase`
    （推 ``stream_abort`` 落库 → live WS 尾随者 + 重放）、:class:`OpsMetrics`、可选
    :class:`api_layer.ws.WSSenderService`（ws_connections + 队列指标）与 :class:`api_layer.run.RunService`
    （孤儿 cancel seam）。``clock`` 可注入以测 TTL 边界。
    """

    session_cache: SessionCacheBase
    trace_store: TraceEventStoreBase
    metrics: OpsMetrics = field(default_factory=OpsMetrics)
    ws_service: WSSenderService | None = None
    run_service: Any = None  # RunService | None（避免硬 import 致 ops↔run 循环）
    config: OpsConfig = field(default_factory=OpsConfig)
    clock: Callable[[], datetime] = field(default=_utcnow)

    # ------------------------------------------------------------------ #
    # /health
    # ------------------------------------------------------------------ #

    async def health(self) -> dict[str, Any]:
        """``{db, active_sessions, active_locks, ws_connections}``（PRD §11）。"""

        moment = self.clock()
        db_ok = await self.session_cache.ping()
        if not db_ok:
            return {
                "db": "down",
                "active_sessions": 0,
                "active_locks": 0,
                "ws_connections": self._ws_connections(),
            }
        try:
            active_sessions = await self.session_cache.get_active_count(now=moment)
            active_locks = await self.session_cache.count_active_locks(now=moment)
        except Exception:
            _logger.exception("health 计数失败——db 标记 down")
            return {
                "db": "down",
                "active_sessions": 0,
                "active_locks": 0,
                "ws_connections": self._ws_connections(),
            }
        return {
            "db": "ok",
            "active_sessions": active_sessions,
            "active_locks": active_locks,
            "ws_connections": self._ws_connections(),
        }

    # ------------------------------------------------------------------ #
    # /metrics
    # ------------------------------------------------------------------ #

    async def metrics_text(self) -> str:
        """Prometheus 文本 exposition（合并 OpsMetrics + WS 队列 + 会话 / 锁 / 连接计数）。"""

        return render_prometheus(await self._samples())

    async def _samples(self) -> list[MetricSample]:
        moment = self.clock()
        samples: list[MetricSample] = list(self.metrics.samples())
        # WS 队列指标（单一真相：来自 WsMetrics，不双计）。
        if self.ws_service is not None:
            snap = self.ws_service.metrics.snapshot()
            samples.append(
                MetricSample(
                    "ws_event_queue_size", "gauge",
                    "WS-sender 背压缓冲当前深度合计", snap["ws_event_queue_size"],
                )
            )
            samples.append(
                MetricSample(
                    "ws_event_queue_full_total", "counter",
                    "WS-sender 背压缓冲满次数（合并 / 丢弃）", snap["ws_event_queue_full_total"],
                )
            )
            ws_conn = self.ws_service.active_connection_count()
        else:
            samples.append(
                MetricSample("ws_event_queue_size", "gauge", "WS-sender 背压缓冲当前深度合计", 0)
            )
            samples.append(
                MetricSample("ws_event_queue_full_total", "counter", "WS-sender 背压缓冲满次数", 0)
            )
            ws_conn = 0
        samples.append(
            MetricSample("ws_connections", "gauge", "活跃 WS 连接数", ws_conn)
        )
        # 会话 / 锁实时计数（db 不可达记 0、不抛）。
        try:
            samples.append(
                MetricSample(
                    "active_sessions", "gauge",
                    "活跃会话数（session_owner.last_seen 近 30min）",
                    await self.session_cache.get_active_count(now=moment),
                )
            )
            samples.append(
                MetricSample(
                    "active_locks", "gauge",
                    "活跃执行锁数（未过期 session_locks 行）",
                    await self.session_cache.count_active_locks(now=moment),
                )
            )
        except Exception:
            _logger.exception("metrics 会话 / 锁计数失败")
            samples.append(MetricSample("active_sessions", "gauge", "活跃会话数", 0))
            samples.append(MetricSample("active_locks", "gauge", "活跃执行锁数", 0))
        return samples

    # ------------------------------------------------------------------ #
    # 后台 sweep
    # ------------------------------------------------------------------ #

    async def sweep(self) -> SweepReport:
        """单次后台清扫（PRD §9.2 / §9.6 / §9.7）。顺序：pause 过期 → 锁孤儿 → 80% 告警。"""

        report = SweepReport()
        moment = self.clock()

        # Phase A：过期 pause_meta → stream_abort + 删 pause + 删锁（§9.2）。
        for p in await self.session_cache.list_expired_pauses(now=moment):
            await self._emit_stream_abort(p.session_id, p.trace_id, "pause_expired")
            await self.session_cache.delete_pause_meta(p.session_id)
            await self.session_cache.unlock_session(p.session_id)
            await self._cancel_orphan(p.session_id)
            report.pause_expired += 1

        # Phase B：过期锁且无活跃 pause_meta → 孤儿 run → stream_abort + 删锁 + cancel（§9.6）。
        for li in await self.session_cache.list_expired_locks(now=moment):
            pause = await self.session_cache.get_pause_meta(li.session_id)
            if pause is not None and not self._pause_expired(pause, moment):
                # 合法 HITL 暂停（heartbeat 由 pause_meta 30min TTL 管辖、不误杀）。
                report.lock_skipped_active_pause += 1
                continue
            await self._emit_stream_abort(li.session_id, li.trace_id, "lock_orphan")
            await self.session_cache.unlock_session(li.session_id)
            await self._cancel_orphan(li.session_id)
            report.lock_orphan += 1

        # 80% 上限告警（§9.7）。
        try:
            count = await self.session_cache.get_active_count(now=moment)
        except Exception:
            count = 0
        ratio = count / self.config.session_limit if self.config.session_limit > 0 else 0.0
        report.session_warn_ratio = ratio
        if ratio >= self.config.session_limit_warn_ratio:
            _logger.warning(
                "活跃会话数 %d 达上限 %d 的 %.0f%%（%s 阈值）——资源告警",
                count,
                self.config.session_limit,
                ratio * 100,
                f"{int(self.config.session_limit_warn_ratio * 100)}%",
            )
        return report

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _ws_connections(self) -> int:
        return self.ws_service.active_connection_count() if self.ws_service is not None else 0

    def _pause_expired(self, pause: Any, moment: datetime) -> bool:
        pause_time: datetime = pause.pause_time
        return bool(pause_time + timedelta(seconds=self.config.pause_ttl_seconds) < moment)

    async def _emit_stream_abort(self, session_id: str, trace_id: str, reason: str) -> None:
        """mint 一条 ``stream_abort`` 事件落 :class:`TraceEventStoreBase`（→ live WS 尾随 + 重放）。

        ``event_seq`` 取该 trace 已落库 ``max_seq + 1``（与翻译层续跑同形）；append 失败降级记错、
        不抛（ADR-0023 不变量：显示侧落库失败不阻塞 sweep / 图）。
        """

        try:
            seq = await self.trace_store.max_seq(trace_id) + 1
            await self.trace_store.append(
                TraceEvent(
                    session_id=session_id,
                    trace_id=trace_id,
                    event_seq=seq,
                    event_type=EventType.STREAM_ABORT,
                    payload={"abort_reason": reason},
                    ts=self.clock(),
                )
            )
        except Exception:
            _logger.exception(
                "sweep stream_abort 落库失败（session=%s trace=%s reason=%s）——降级",
                session_id,
                trace_id,
                reason,
            )

    async def _cancel_orphan(self, session_id: str) -> None:
        """委托 :meth:`RunService.cancel_orphan` 取消在跑请求任务（防御病理性长挂）。"""

        cancel = getattr(self.run_service, "cancel_orphan", None)
        if cancel is None:
            return
        try:
            await cancel(session_id)
        except Exception:
            _logger.exception("sweep cancel_orphan 失败（session=%s）", session_id)
