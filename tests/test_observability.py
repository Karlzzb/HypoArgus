"""Langfuse 观测 seam 的离线测试（不触网）。

断言 :func:`build_langfuse_callback` 的「可选、零硬依赖」语义：
- 缺三变量任一 → 返回 ``None``（不注入 callback，流水线行为不变）。
- 三变量齐全且 ``langfuse`` 已安装 → 返回 handler 实例（不验证网络）。
- ``_build_session_config`` 在未配置时返回空 dict、配置齐全时含 callbacks + trace metadata。
"""

from __future__ import annotations

import pytest

from infra.observability import build_langfuse_callback, langfuse_env_present


def test_langfuse_env_present_false_when_any_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # 隔离 cwd，不读仓库根 .env
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert not langfuse_env_present()
    assert build_langfuse_callback() is None


def test_langfuse_env_present_false_when_only_partial(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-x")
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    assert not langfuse_env_present()
    assert build_langfuse_callback() is None


def test_build_callback_returns_handler_when_env_present(monkeypatch, tmp_path):
    """三变量齐全 + SDK 已安装 → 返回 handler（不验证网络，仅构造成功）。"""

    pytest.importorskip("langfuse")  # SDK 未装则 skip，不破坏离线门
    monkeypatch.chdir(tmp_path)
    # 关闭 tracing：仅验证构造语义，避免后台 exporter 向死端口 localhost:13000 重试刷日志。
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:13000")
    handler = build_langfuse_callback()
    assert handler is not None


def test_build_session_config_empty_when_unconfigured(monkeypatch, tmp_path):
    """未配置 Langfuse → ``_build_session_config`` 返回空 dict（零侵入，不注入 callbacks）。"""

    from runtime.run_real import _build_session_config

    monkeypatch.chdir(tmp_path)
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert _build_session_config("sess-1") == {}


def test_build_session_config_has_callbacks_and_metadata_when_configured(
    monkeypatch, tmp_path
):
    """配置齐全 → ``_build_session_config`` 返回 callbacks + trace 聚合 metadata。"""

    pytest.importorskip("langfuse")
    from runtime.run_real import _build_session_config

    monkeypatch.chdir(tmp_path)
    # 关闭 tracing：仅验证装配语义，避免后台 exporter 向死端口重试刷日志。
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:13000")
    cfg = _build_session_config("sess-42")
    assert "callbacks" in cfg
    assert isinstance(cfg["callbacks"], list) and len(cfg["callbacks"]) == 1
    md = cfg["metadata"]
    assert md["langfuse_session_id"] == "sess-42"
    assert md["langfuse_user_id"] == "hypoargus"
    assert "real-run" in md["langfuse_tags"]
