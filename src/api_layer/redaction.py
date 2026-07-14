"""敏感信息脱敏钩子（T-08·PRD §3.3）。

工具调用入参 / 返回、LLM 思考内容在**推送前端和落库前**经可配置脱敏钩子；日志禁止记录
完整凭证、仅记哈希。本模块提供单一 :class:`Redactor`，供翻译层落库前（:class:`api_layer.translator.EventTranslator`）
与日志 formatter（:class:`api_layer.logging_setup.JsonFormatter`）复用，避免脱敏规则在多处漂移。

一期默认**关闭**（空规则集 → :attr:`Redactor.enabled` 为 False → ``redact_obj`` 原样返回、零开销），
留正则接口如手机号 / 身份证（PRD §3.3「一期默认关闭，留正则接口」）。调用方按部署 override 注入规则。
凭证哈希用 SHA-256 前 16 hex（:func:`hash_credential`），供日志记录敏感键名对应值时仅留摘要。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RedactionRule",
    "RedactionConfig",
    "Redactor",
    "default_redaction_config",
    "hash_credential",
    "DEFAULT_SENSITIVE_KEY_PATTERNS",
]


#: 默认敏感键名片段（小写匹配，标识日志中应**仅记哈希**的凭证字段）。
#: 覆盖 password / secret / token / credential / api_key / access_key / private_key 常见命名变体；
#: 显式列名而非裸 ``key``，避免误伤 ``paragraph_id`` / ``argument_*`` 等 domain 字段。
DEFAULT_SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    r"password",
    r"secret",
    r"token",
    r"credential",
    r"api[_-]?key",
    r"access[_-]?key",
    r"private[_-]?key",
)


@dataclass(frozen=True)
class RedactionRule:
    """单条脱敏规则：编译后的正则 + 替换串。"""

    pattern: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class RedactionConfig:
    """脱敏配置（规则集）。空 → 关闭（``enabled`` False）。"""

    rules: tuple[RedactionRule, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.rules)


def default_redaction_config() -> RedactionConfig:
    """默认配置：空规则集（一期默认关，PRD §3.3）。"""

    return RedactionConfig()


class Redactor:
    """应用 :class:`RedactionConfig` 的脱敏器。

    ``enabled`` False 时所有 ``redact_*`` 均为透传（``redact_obj`` 直接返回原对象引用、
    不构造副本），使一期默认态零开销。规则按序对每个 string 叶应用（前一条替换后后一条仍扫描
    剩余明文）；非 string 原样过。
    """

    def __init__(self, config: RedactionConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def redact_text(self, text: str) -> str:
        if not self._config.enabled:
            return text
        out = text
        for rule in self._config.rules:
            out = rule.pattern.sub(rule.replacement, out)
        return out

    def redact_obj(self, obj: Any) -> Any:
        """递归脱敏 dict / list / tuple 中的 string 叶；非 string 原样过。

        关闭时直接返回原对象（identity），避免一期默认态对每条事件 payload 做深拷贝。
        """

        if not self._config.enabled:
            return obj
        return self._redact(obj)

    def _redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            return {str(k): self._redact(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._redact(v) for v in obj]
        return obj


def hash_credential(value: str) -> str:
    """凭证哈希：SHA-256 前 16 hex（稳定、不泄漏原值）。供日志记敏感键值时仅留摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
