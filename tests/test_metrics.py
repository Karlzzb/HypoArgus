"""指标注册表单测（T-08·PRD §11.1）。

无依赖 Prometheus 文本 exposition：counter/gauge 原语 + ``render_prometheus`` 产 ``# HELP`` / ``# TYPE`` 行。
: class:`OpsMetrics` 聚合本切片自有的 counter / gauge（``langfuse_errors_total`` /
``event_push_latency_seconds`` / ``graph_execution_duration_seconds``）；WS / 会话 / 锁计数由
: class:`api_layer.ops.OpsService` 渲染时从各自源 snapshot 合并（单一真相、不双计）。
"""

from __future__ import annotations

from api_layer.metrics import (
    Counter,
    Gauge,
    MetricSample,
    OpsMetrics,
    render_prometheus,
)


def test_counter_increments() -> None:
    c = Counter("langfuse_errors_total", "Langfuse write failures")
    assert c.value == 0
    c.inc()
    c.inc(2)
    assert c.value == 3


def test_gauge_sets_value() -> None:
    g = Gauge("event_push_latency_seconds", "push latency")
    g.set(0.12)
    assert abs(g.value - 0.12) < 1e-9


def test_render_prometheus_shape() -> None:
    """exposition 文本含 HELP/TYPE 行 + ``name value`` 行。"""

    text = render_prometheus(
        [
            MetricSample("langfuse_errors_total", "counter", "Langfuse 写失败计数", 3),
            MetricSample("ws_connections", "gauge", "活跃 WS 连接", 2),
        ]
    )
    assert "# HELP langfuse_errors_total Langfuse 写失败计数" in text
    assert "# TYPE langfuse_errors_total counter" in text
    assert "langfuse_errors_total 3" in text
    assert "# TYPE ws_connections gauge" in text
    assert "ws_connections 2" in text


def test_render_prometheus_stable_order() -> None:
    """同名 metric 的 HELP/TYPE 只出现一次（即便重复样本）；输出按给定顺序稳定。"""

    text = render_prometheus(
        [
            MetricSample("active_sessions", "gauge", "活跃会话", 5),
            MetricSample("active_locks", "gauge", "活跃锁", 1),
        ]
    )
    assert text.index("active_sessions") < text.index("active_locks")
    assert text.count("# TYPE active_sessions gauge") == 1


def test_opsmetrics_defaults_and_samples() -> None:
    """OpsMetrics 默认含三个自有指标；samples() 含其名。"""

    m = OpsMetrics()
    m.langfuse_errors_total.inc(4)
    m.event_push_latency_seconds.set(0.05)
    m.graph_execution_duration_seconds.set(2.3)
    names = {s.name for s in m.samples()}
    assert {
        "langfuse_errors_total",
        "event_push_latency_seconds",
        "graph_execution_duration_seconds",
    } <= names
    text = render_prometheus(m.samples())
    assert "langfuse_errors_total 4" in text
    assert "event_push_latency_seconds 0.05" in text
    assert "graph_execution_duration_seconds 2.3" in text
