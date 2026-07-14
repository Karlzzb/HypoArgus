"""结构化 JSON 日志（T-08·PRD §11 / §3.3）。

stdlib ``logging`` + JSON formatter（不引新依赖）：每条日志为单行 JSON，含 ``ts`` / ``level``
/ ``logger`` / ``message`` + 运行身份 ``session_id`` / ``trace_id`` / ``user_id``（脱敏后）+ 调用方
``extra`` 字段。凭证字段（``password`` / ``secret`` / ``token`` / ``key`` 等）仅记 SHA-256 哈希，
正则脱敏（手机号 / 身份证等）经 :class:`api_layer.redaction.Redactor`。

运行身份经 :mod:`contextvars` 注入：:func:`log_context` 上下文管理器在请求入口（:meth:`api_layer.run.RunService.run`）
设置 ``session_id`` / ``trace_id`` / ``user_id``，:class:`ContextFilter` 在每条记录格式化前回填，
请求结束自动恢复——无需每处 logger 调用手传身份。
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from api_layer.redaction import (
    DEFAULT_SENSITIVE_KEY_PATTERNS,
    Redactor,
    default_redaction_config,
    hash_credential,
)

__all__ = [
    "JsonFormatter",
    "ContextFilter",
    "log_context",
    "set_trace_id",
    "configure_logging",
]

#: 运行身份 contextvars（请求入口注入、:class:`ContextFilter` 回填）。
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)

#: 标准库 ``LogRecord`` 属性名集合——序列化 extra 时排除（避免与顶层字段重复）。
_RECORD_BUILTINS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)

#: 预编译敏感键名正则（日志中这些键的值仅记哈希）。
_SENSITIVE_KEY_RES = tuple(
    re.compile(p, re.IGNORECASE) for p in DEFAULT_SENSITIVE_KEY_PATTERNS
)


class ContextFilter(logging.Filter):
    """回填运行身份（session_id / trace_id / user_id）到每条 LogRecord。

    请求入口经 :func:`log_context` 设 contextvars；本 filter 在格式化前把当前值写回 record，
    使 :class:`JsonFormatter` 可序列化。无上下文时写空串（便于前端 / 运维统一 schema）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id.get() or ""
        record.trace_id = _trace_id.get() or ""
        record.user_id = _user_id.get() or ""
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志 formatter。

    - ``ts``：ISO8601 tz-aware 落库时刻；
    - ``level`` / ``logger`` / ``message``；
    - ``session_id`` / ``trace_id`` / ``user_id``（由 :class:`ContextFilter` 回填、脱敏后）；
    - 其余 ``extra`` 字段经 :class:`Redactor` 脱敏 + 敏感键名哈希；
    - ``exc_info``（异常栈）以 ``exception`` 字段附 ``type`` / ``message`` + traceback 文本。
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor if redactor is not None else Redactor(default_redaction_config())

    def format(self, record: logging.LogRecord) -> str:
        sid = getattr(record, "session_id", "") or _session_id.get() or ""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redact_text(record.getMessage()),
            "session_id": self._redact_text(sid),
            "trace_id": getattr(record, "trace_id", "") or _trace_id.get() or "",
            "user_id": self._redact_text(
                getattr(record, "user_id", "") or _user_id.get() or ""
            ),
        }
        for key, value in record.__dict__.items():
            if key in _RECORD_BUILTINS or key in payload:
                continue
            if key.startswith("_"):
                continue
            payload[key] = self._redact_field(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _redact_text(self, text: str) -> str:
        if not isinstance(text, str) or not self._redactor.enabled:
            return text
        return self._redactor.redact_text(text)

    def _redact_field(self, key: str, value: Any) -> Any:
        """敏感键名 → 哈希；其余 string → 正则脱敏；非 string 递归脱敏（enabled 才生效）。"""

        if isinstance(value, str):
            if self._is_sensitive_key(key):
                return hash_credential(value)
            return self._redact_text(value)
        if self._redactor.enabled:
            return self._redactor.redact_obj(value)
        return value

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return any(p.search(key) for p in _SENSITIVE_KEY_RES)


def log_context(
    *, session_id: str, trace_id: str, user_id: str
) -> AbstractContextManager[None]:
    """设置运行身份 contextvars（请求入口）；退出恢复。

    用上下文管理器——``RunService.run`` 在入口 ``with`` 之，使该请求期间所有 logger 调用
    （含 translator drainer / sweep 降级记错）自动带身份。
    """

    class _Ctx(AbstractContextManager[None]):
        def __enter__(self) -> None:
            self._t1 = _session_id.set(session_id)
            self._t2 = _trace_id.set(trace_id)
            self._t3 = _user_id.set(user_id)

        def __exit__(self, *_exc: object) -> None:
            _session_id.reset(self._t1)
            _trace_id.reset(self._t2)
            _user_id.reset(self._t3)

    return _Ctx()


def set_trace_id(trace_id: str) -> None:
    """设置当前 trace_id（请求 mint / resume 复用 trace_id 后调）。

    ``session_id`` / ``user_id`` 由外层 :func:`log_context` 管生命周期；``trace_id`` 在 fresh
    mint 后才知，故单独 set——外层 ``log_context`` 退出时统一 reset（清空），无需调用方还原。
    """

    _trace_id.set(trace_id)


def configure_logging(
    *,
    level: int = logging.INFO,
    redaction: Any | None = None,
) -> None:
    """装配 root logger：JSON formatter + ContextFilter（不引新依赖）。

    生产入口（:func:`api_layer.server.serve`）调一次即生效；测试态不调则用 pytest 默认 handler。
    ``redaction`` 缺省为默认配置（一期关，PRD §3.3）；部署可传启用规则集。
    """

    redactor = redaction if isinstance(redaction, Redactor) else Redactor(default_redaction_config())
    root = logging.getLogger()
    # 清掉默认 stderr handler（避免双输出），换 JSON handler。
    root.handlers = []
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter(redactor=redactor))
    h.addFilter(ContextFilter())
    root.addHandler(h)
    root.setLevel(level)
