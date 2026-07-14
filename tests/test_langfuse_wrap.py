"""Langfuse 写失败降级代理单测（T-08·PRD §11.1 / §3.3）。

Langfuse LangChain callback handler 的写失败经 :func:`api_layer.langfuse_wrap.wrap_langfuse_handler`
包装代理：各 ``on_*`` 回调异常 → ``langfuse_errors_total`` +1 + 记结构化日志、**不外抛**（PRD §11：
Langfuse 不可用时降级、仅本地记错、不阻塞对话）。
"""

from __future__ import annotations

from typing import Any

from api_layer.langfuse_wrap import wrap_langfuse_handler
from api_layer.metrics import OpsMetrics


class _RaisingHandler:
    """伪 Langfuse handler：``on_llm_start`` 抛错，``on_llm_end`` 正常返回。"""

    def on_llm_start(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("langfuse network down")

    def on_llm_end(self, response: Any, **kwargs: Any) -> str:
        return "ok"


def test_callback_exception_increments_counter_and_swallows() -> None:
    """on_llm_start 抛错 → langfuse_errors_total +1、不外抛。"""

    m = OpsMetrics()
    proxy = wrap_langfuse_handler(_RaisingHandler(), m)
    # 不抛
    proxy.on_llm_start({"id": "x"}, ["p"], run_id="ru1")  # type: ignore[arg-type]
    assert m.langfuse_errors_total.value == 1


def test_callback_delegates_return_value_when_ok() -> None:
    """正常回调 → 透传返回值、计数不动。"""

    m = OpsMetrics()
    proxy = wrap_langfuse_handler(_RaisingHandler(), m)
    out = proxy.on_llm_end(response="resp", run_id="ru1")  # type: ignore[arg-type]
    assert out == "ok"
    assert m.langfuse_errors_total.value == 0


def test_unknown_callback_method_still_delegates() -> None:
    """代理经 ``__getattr__`` 覆盖任意 ``on_*`` 方法名（未来 langchain 新增回调也兜底）。"""

    class _H:
        def on_custom_event(self, *a: Any, **k: Any) -> str:
            return "custom"

        def on_chat_model_start(self, *a: Any, **k: Any) -> None:
            raise OSError("boom")

    m = OpsMetrics()
    proxy = wrap_langfuse_handler(_H(), m)
    assert proxy.on_custom_event() == "custom"  # type: ignore[attr-defined]
    proxy.on_chat_model_start(run_id="r")  # type: ignore[arg-type]
    assert m.langfuse_errors_total.value == 1
