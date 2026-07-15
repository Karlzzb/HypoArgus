"""Langfuse 观测 seam：把 LangChain/LangGraph callback handler 接到流水线（ADR-0017 回调线程）。

``Orchestrator.run_with_report`` 已透传 ``RunnableConfig`` 到 ``graph.invoke``，其 ``callbacks``
线程贯穿整条 Agent 链路、业务节点零侵入（见 :mod:`runtime.orchestrator` 的 ``session_config``
docstring）。本模块只负责构造那个 callback handler——把 Langfuse 的 LangChain handler 装进
``session_config["callbacks"]`` 即可让全链路 LLM 调用 / 图节点在 Langfuse 落 trace。

**provider 无关、可选、零硬依赖**：
- Langfuse v4 客户端直接读环境变量 ``LANGFUSE_BASE_URL`` / ``LANGFUSE_SECRET_KEY`` /
  ``LANGFUSE_PUBLIC_KEY``（``client.py:317-319``，``LANGFUSE_BASE_URL`` 优先级高于
  ``LANGFUSE_HOST``）——与本项目 ``.env`` 既有变量名一致，无需映射。
- 三变量任一缺失或 ``langfuse`` 未安装 → 返回 ``None``：调用方据此**不**注入 callback，
  流水线行为与离线 / 测试态完全一致（不破坏 387-green 门、不引入硬依赖）。这与
  :func:`infra.llm_provider.build_qwen_chat_model`「缺 key 抛错、绝不硬编码」的安全红线同形：
  密钥**只**读环境变量、不进配置 / 日志 / commit。

特殊 trace 字段（``CallbackHandler`` 读 run ``metadata`` 中以下键写入 trace 属性，
``CallbackHandler.py:496-520``）：
- ``langfuse_session_id`` → trace.session_id（按会话聚合）
- ``langfuse_user_id``    → trace.user_id（可被 ``GET /api/public/traces?userId=`` 查询）
- ``langfuse_tags``       → trace.tags
调用方把这些键放进 ``session_config["metadata"]``，即可在 Langfuse 服务端按会话 / 用户检索 trace。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = ["build_langfuse_callback", "langfuse_env_present"]


def _load_env_file(path: Path) -> None:
    """无依赖的极简 ``.env`` 加载器：只对**未设置**的环境变量 setdefault。

    与 :func:`infra.llm_provider._load_env_file` 同形同义——本模块独立加载 ``.env`` 以便在
    未构造 chat model 时亦可独立构造 handler（离线测试直接调 :func:`build_langfuse_callback`
    而无需先触 LLM provider）。``.env`` 已被 ``.gitignore`` 忽略，不会提交。
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


def langfuse_env_present() -> bool:
    """三只 Langfuse 环境变量是否齐全（缺任一即视为未配置 → 不注入 callback）。"""

    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and os.environ.get("LANGFUSE_BASE_URL")
    )


def build_langfuse_callback() -> Any | None:
    """构造 Langfuse LangChain callback handler；未配置 / 未安装时返回 ``None``。

    先 ``setdefault`` 加载 cwd 下 ``.env``（不覆盖已导出的环境变量），再检查三变量齐全；
    齐全则懒导入 ``langfuse.langchain.CallbackHandler`` 构造无参实例（v4 自动读上述环境变量）。
    返回 ``None`` 时调用方应**不**向 ``session_config`` 注入 ``callbacks``——保持流水线零侵入、
    行为与离线态一致。
    """

    _load_env_file(Path.cwd() / ".env")
    if not langfuse_env_present():
        return None
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:
        return None
    return CallbackHandler()
