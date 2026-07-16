"""DashScope（阿里通义千问）OpenAI-compatible LLM provider 工厂——LLM seam 的第二 adapter。

contract 层（parser / verification / hypothesis 的 ``*LlmClient`` Protocol）provider-free、
不绑任何 provider；本模块是「第二个 adapter」（deep-module 两-adapter 原则）：把
``langchain_openai.ChatOpenAI`` 指向 DashScope 的 OpenAI-compatible 端点，复用其
``with_structured_output`` 满足结构化输出契约（DEVELOPMENT.md §11）。provider 无关——
注入其它 ``BaseChatModel`` 即指向别的网关。

安全红线（见 CLAUDE.md）：API key **只**读环境变量 ``DASHSCOPE_API_KEY``——
绝不硬编码、不进配置、不进日志、不进 commit。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

__all__ = [
    "DASHSCOPE_BASE_URL",
    "DEFAULT_MODEL",
    "build_qwen_chat_model",
]


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-max"


def _load_env_file(path: Path) -> None:
    """无依赖的极简 ``.env`` 加载器：只对**未设置**的环境变量 setdefault。

    格式：``KEY=VALUE`` 一行一个；``#`` 注释；值两侧的引号被剥去。仅读 cwd 下 ``.env``
    （已被 ``.gitignore`` 忽略，不会提交）——方便本地填 key 后直接跑，无需 ``export``。
    """

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def build_qwen_chat_model(
    model: str | None = None,
    *,
    temperature: float = 0.0,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = 120.0,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """返回指向 DashScope 的 ``ChatOpenAI`` 实例（OpenAI-compatible 调用面）。

    :param model: DashScope 模型名；缺省读 ``DASHSCOPE_MODEL`` 环境变量、再缺省
        :data:`DEFAULT_MODEL` ``qwen-max``。
    :param base_url: 默认 :data:`DASHSCOPE_BASE_URL`；可指向自建网关。
    :param api_key: 默认读环境变量 ``DASHSCOPE_API_KEY``（先从 cwd 下 ``.env`` 加载）；
        缺则抛 :class:`RuntimeError`——绝不把字面 key 写进代码 / 配置 / 日志。
    :param timeout: 单次请求超时秒；默认 120。
        结构化输出（function-calling 多步）在多段文档上合法耗时 10–60s，
        60s 不留余量且曾导致冷启动 ``APITimeoutError``，故拉长至 120s。
    :param max_tokens: 输出 token 上限；默认 None 走 provider 默认。
        显式设置时透传给 ``ChatOpenAI``，用于缓解大文档输出截断。
    """

    _load_env_file(Path.cwd() / ".env")
    resolved_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_KEY：请在 .env 或环境变量设置 DASHSCOPE_API_KEY"
            "（绝不硬编码 key）"
        )
    resolved_model = model or os.environ.get("DASHSCOPE_MODEL") or DEFAULT_MODEL
    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "base_url": base_url or DASHSCOPE_BASE_URL,
        "api_key": SecretStr(resolved_key),
        "temperature": temperature,
        "timeout": timeout,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)
