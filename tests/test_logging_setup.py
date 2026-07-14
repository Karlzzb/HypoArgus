"""结构化 JSON 日志单测（T-08·PRD §11 / §3.3）。

stdlib + JSON formatter（不引新依赖）：每条日志带 ``session_id`` / ``trace_id`` / ``user_id``
（脱敏后）+ 时间戳 / 级别 / logger / message；凭证字段仅记哈希、正则脱敏手机号 / 身份证。
contextvars 注入运行身份，由 :class:`api_layer.run.RunService` 在请求入口设置。
"""

from __future__ import annotations

import io
import json
import logging
import re

from api_layer.logging_setup import (
    JsonFormatter,
    configure_logging,
    log_context,
)
from api_layer.redaction import RedactionConfig, RedactionRule, Redactor


def _capture(logger: logging.Logger, level: int = logging.DEBUG) -> tuple[io.StringIO, logging.Handler]:
    """给 logger 挂 JsonFormatter + StringIO handler，返回 buffer。"""

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter(redactor=Redactor(RedactionConfig())))
    logger.addHandler(h)
    logger.setLevel(level)
    return buf, h


def test_json_formatter_basic_fields() -> None:
    """基础字段：ts / level / logger / message 成 JSON。"""

    logger = logging.getLogger("t_basic")
    buf, h = _capture(logger)
    try:
        with log_context(session_id="s1", trace_id="t1", user_id="u1"):
            logger.info("hello %s", "world")
    finally:
        logger.removeHandler(h)
    rec = json.loads(buf.getvalue())
    assert rec["level"] == "INFO"
    assert rec["logger"] == "t_basic"
    assert rec["message"] == "hello world"
    assert rec["session_id"] == "s1"
    assert rec["trace_id"] == "t1"
    assert rec["user_id"] == "u1"
    assert "ts" in rec


def test_json_formatter_redacts_phone_in_message() -> None:
    """message 含手机号 + 启用规则 → 脱敏。"""

    red = Redactor(
        RedactionConfig(
            rules=(RedactionRule(re.compile(r"1[3-9]\d{9}"), "[PHONE]"),)
        )
    )
    logger = logging.getLogger("t_phone")
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter(redactor=red))
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("联系 13800138000")
    finally:
        logger.removeHandler(h)
    rec = json.loads(buf.getvalue())
    assert rec["message"] == "联系 [PHONE]"


def test_json_formatter_hashes_sensitive_extra_field() -> None:
    """敏感键名（password）的 extra 值仅记哈希、不泄漏原值。"""

    red = Redactor(RedactionConfig())  # 默认关规则，但敏感键名哈希独立生效
    logger = logging.getLogger("t_cred")
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter(redactor=red))
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("login", extra={"password": "supersecret", "ok": True})
    finally:
        logger.removeHandler(h)
    rec = json.loads(buf.getvalue())
    assert rec["password"] != "supersecret"
    assert len(rec["password"]) == 16  # hash_credential 前 16 hex
    assert rec["ok"] is True


def test_log_context_restores_contextvars() -> None:
    """log_context 退出后恢复（嵌套 / 退出清空）。"""

    logger = logging.getLogger("t_ctx")
    buf, h = _capture(logger)
    try:
        with log_context(session_id="s1", trace_id="t1", user_id="u1"):
            logger.info("inside")
        logger.info("outside")
    finally:
        logger.removeHandler(h)
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    inside = lines[0]
    outside = lines[1]
    assert inside["session_id"] == "s1"
    assert "session_id" not in outside or outside.get("session_id") in (None, "")


def test_configure_logging_installs_json_formatter_on_root() -> None:
    """configure_logging 给 root 装上 JsonFormatter（结构化生效、不引新依赖）。"""

    handlers_before = list(logging.getLogger().handlers)
    try:
        configure_logging(level=logging.INFO)
        root = logging.getLogger()
        # 至少有一个 handler 的 formatter 是 JsonFormatter。
        assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
    finally:
        logging.getLogger().handlers = handlers_before
