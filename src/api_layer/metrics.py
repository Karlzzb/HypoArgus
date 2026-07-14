"""运维指标注册表（T-08·PRD §11.1）。

无依赖 Prometheus 文本 exposition（不引 ``prometheus_client``）：手写 ``# HELP`` / ``# TYPE`` / ``name value``
三行格式。counter / gauge 原语供本切片自有指标（``langfuse_errors_total`` / ``event_push_latency_seconds``
/ ``graph_execution_duration_seconds``）；WS 队列指标、会话 / 锁 / 连接计数由 :class:`api_layer.ops.OpsService`
渲染时从各自源（:class:`api_layer.ws.WsMetrics` / :class:`api_layer.session_cache.SessionCacheBase` /
:class:`api_layer.ws.WSSenderService`）snapshot 合并——单一真相、不双计，避免漂移。

PRD §11.1 全量指标（``/metrics`` 暴露）：

- ``active_sessions`` / ``active_locks`` / ``ws_connections``（gauge）
- ``event_push_latency_seconds`` / ``graph_execution_duration_seconds``（gauge，最近观测值）
- ``ws_event_queue_size`` / ``ws_event_queue_full_total``（来自 :class:`WsMetrics`）
- ``langfuse_errors_total``（counter）
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Counter",
    "Gauge",
    "MetricSample",
    "OpsMetrics",
    "render_prometheus",
]


@dataclass
class Counter:
    """单调计数器（PRD §11.1 ``langfuse_errors_total`` 等）。"""

    name: str
    help: str
    value: int = 0

    def inc(self, n: int = 1) -> None:
        self.value += n


@dataclass
class Gauge:
    """可升可降的当前值（PRD §11.1 ``ws_connections`` / 延迟秒等）。"""

    name: str
    help: str
    value: float = 0.0

    def set(self, v: float) -> None:
        self.value = float(v)


@dataclass(frozen=True)
class MetricSample:
    """渲染用单指标样本：名 / 类型（``counter`` | ``gauge``）/ HELP 文 / 当前值。"""

    name: str
    type: str
    help: str
    value: float | int


def render_prometheus(samples: list[MetricSample]) -> str:
    """把样本列表渲染为 Prometheus 文本 exposition 格式。

    每个指标产三行：``# HELP`` / ``# TYPE`` / ``<name> <value>``。按给定顺序输出（稳定、可读）；
    同名重复样本只保留首条 HELP/TYPE（调用方应去重聚合后传入）。
    """

    seen: set[str] = set()
    lines: list[str] = []
    for s in samples:
        if s.name not in seen:
            seen.add(s.name)
            lines.append(f"# HELP {s.name} {s.help}")
            lines.append(f"# TYPE {s.name} {s.type}")
        lines.append(f"{s.name} {_fmt_value(s.value)}")
    return "\n".join(lines) + "\n"


def _fmt_value(v: float | int) -> str:
    """整数无小数点、浮点保留必要精度（Prometheus 文本接受 ``0.05`` / ``3``）。"""

    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return str(int(v))
    return repr(float(v))


@dataclass
class OpsMetrics:
    """本切片自有的运维指标集合（注入 :class:`api_layer.run.RunService` /
    :class:`api_layer.translator.EventTranslator` / Langfuse 代理）。

    WS 队列 / 会话 / 锁 / 连接计数**不**在此（由 ``OpsService`` 渲染时从源 snapshot），
    避免与 :class:`WsMetrics` / cache 双计。
    """

    langfuse_errors_total: Counter = field(
        default_factory=lambda: Counter(
            "langfuse_errors_total", "Langfuse 写失败次数（降级记错、不阻塞对话）"
        )
    )
    event_push_latency_seconds: Gauge = field(
        default_factory=lambda: Gauge(
            "event_push_latency_seconds",
            "trace_events 落库推送延迟（最近观测值，秒）",
        )
    )
    graph_execution_duration_seconds: Gauge = field(
        default_factory=lambda: Gauge(
            "graph_execution_duration_seconds",
            "单请求图执行墙钟时长（最近观测值，秒）",
        )
    )

    def samples(self) -> list[MetricSample]:
        """本指标集合的样本（供 :func:`render_prometheus`）。"""

        return [
            MetricSample(
                self.langfuse_errors_total.name,
                "counter",
                self.langfuse_errors_total.help,
                self.langfuse_errors_total.value,
            ),
            MetricSample(
                self.event_push_latency_seconds.name,
                "gauge",
                self.event_push_latency_seconds.help,
                self.event_push_latency_seconds.value,
            ),
            MetricSample(
                self.graph_execution_duration_seconds.name,
                "gauge",
                self.graph_execution_duration_seconds.help,
                self.graph_execution_duration_seconds.value,
            ),
        ]
