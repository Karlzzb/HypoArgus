"""脱敏钩子单测（T-08·PRD §3.3）。

默认关（空规则集 = 透传、零开销）；配置正则规则后递归 redact string 叶；凭证哈希仅记摘要。
翻译层落库前 + 日志 formatter 复用同一 :class:`Redactor`。
"""

from __future__ import annotations

import re

from api_layer.redaction import (
    DEFAULT_SENSITIVE_KEY_PATTERNS,
    RedactionConfig,
    RedactionRule,
    Redactor,
    default_redaction_config,
    hash_credential,
)


def test_default_config_is_off() -> None:
    """默认配置无规则 → enabled False → redact_text 原样返回。"""

    red = Redactor(default_redaction_config())
    assert not red.enabled
    assert red.redact_text("明文 13800138000 不动") == "明文 13800138000 不动"


def test_phone_redaction() -> None:
    """手机号正则 → 替换为掩码；非匹配段保留。"""

    red = Redactor(
        RedactionConfig(
            rules=(
                RedactionRule(
                    pattern=re.compile(r"1[3-9]\d{9}"),
                    replacement="[PHONE]",
                ),
            )
        )
    )
    assert red.enabled
    assert red.redact_text("联系 13800138000 即可") == "联系 [PHONE] 即可"
    assert red.redact_text("no phone here") == "no phone here"


def test_id_card_redaction() -> None:
    """身份证号（18 位末位 X）正则 → 替换。"""

    red = Redactor(
        RedactionConfig(
            rules=(
                RedactionRule(
                    pattern=re.compile(r"[1-9]\d{16}[0-9X]"),
                    replacement="[IDCARD]",
                ),
            )
        )
    )
    assert red.redact_text("id=11010119900307001X") == "id=[IDCARD]"


def test_redact_obj_recurses_strings_only() -> None:
    """redact_obj 递归 dict/list/string 叶；非字符串原样过。"""

    red = Redactor(
        RedactionConfig(
            rules=(
                RedactionRule(
                    pattern=re.compile(r"1[3-9]\d{9}"),
                    replacement="[PHONE]",
                ),
            )
        )
    )
    out = red.redact_obj(
        {
            "args": {"phone": "13800138000", "count": 3, "nested": ["x", "13800138000"]},
            "ok": True,
        }
    )
    assert out == {
        "args": {"phone": "[PHONE]", "count": 3, "nested": ["x", "[PHONE]"]},
        "ok": True,
    }


def test_redact_obj_disabled_is_identity() -> None:
    """enabled False 时 redact_obj 不构造新对象、直接返回原值（零开销）。"""

    red = Redactor(default_redaction_config())
    obj = {"a": "13800138000"}
    assert red.redact_obj(obj) is obj


def test_hash_credential_stable_and_truncated() -> None:
    """hash_credential：sha256 前 16 hex、稳定、不泄漏原值。"""

    h1 = hash_credential("supersecret")
    h2 = hash_credential("supersecret")
    assert h1 == h2
    assert h1 != "supersecret"
    assert len(h1) == 16
    assert hash_credential("other") != h1


def test_sensitive_key_patterns_match_common_names() -> None:
    """默认敏感键名集匹配 password/secret/token/key/credential（任意大小写）。"""

    pats = [re.compile(p, re.IGNORECASE) for p in DEFAULT_SENSITIVE_KEY_PATTERNS]
    keys = ["password", "api_key", "SECRET", "authToken", "credential", "access_token"]
    for k in keys:
        assert any(p.search(k) for p in pats), f"{k} 未被敏感键名集匹配"
    # 普通键不命中
    assert not any(p.search("paragraph_id") for p in pats)
