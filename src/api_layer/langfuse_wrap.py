"""Langfuse callback 降级代理（T-08·PRD §11 / §3.3）。

Langfuse 的 LangChain ``CallbackHandler`` 在写 trace 失败时其内部本就会吞错记日志，但**不**外抛——
PRD §11 要求「不可用时降级、仅本地记错、不阻塞对话」并经 ``langfuse_errors_total`` 可见。本代理在
handler 之外再加一层显式计数：任一 ``on_*`` 回调异常 → counter +1 + 结构化日志、绝不外抛。

实现用 ``__getattr__``：代理是普通类（**不**继承 ``BaseCallbackHandler``，故 base 无 no-op 拦截
``__getattr__``），对任意 ``on_*`` 属性名返回一个委托包装器，使未来 langchain 新增回调方法也兜底，
无需逐一列名。handler 注入态为 ``Any``（:class:`api_layer.run.RunService.langfuse_handler` 亦是 ``Any``），
故无 mypy 签名约束。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from api_layer.metrics import OpsMetrics

__all__ = ["wrap_langfuse_handler"]

_logger = logging.getLogger(__name__)


class _CountingCallbackProxy:
    """委托 ``on_*`` 调用至被包 handler，异常计数 + 吞掉。"""

    def __init__(self, handler: Any, metrics: OpsMetrics) -> None:
        self._handler = handler
        self._metrics = metrics

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """``on_*`` 名 → 委托包装器；其余属性查找失败即 AttributeError。"""

        if not name.startswith("on_"):
            raise AttributeError(name)
        method = getattr(self._handler, name, None)
        metrics = self._metrics
        handler_name = type(self._handler).__name__

        def _invoke(*args: Any, **kwargs: Any) -> Any:
            if method is None:
                return None
            try:
                return method(*args, **kwargs)
            except Exception:
                metrics.langfuse_errors_total.inc()
                _logger.warning(
                    "Langfuse callback %s.%s 失败——降级、不阻塞对话",
                    handler_name,
                    name,
                    exc_info=True,
                )
                return None

        return _invoke


def wrap_langfuse_handler(handler: Any, metrics: OpsMetrics) -> Any:
    """包一个 Langfuse callback handler 为计数代理；handler 为 ``None`` 时直接回 ``None``。"""

    if handler is None:
        return None
    return _CountingCallbackProxy(handler, metrics)
