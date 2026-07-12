"""图相关工具：带「零侵入」历史自动裁剪的 prompt-llm 链路构建器。

设计目标
--------
1. 零侵入：业务节点无需手动处理 / 裁剪 ``messages``，只管把完整历史塞进去；
   裁剪发生在链路内部的预处理阶段。
2. 不区分主图 / 子图：不再有 ``scene`` 概念，只通过一个「压缩参数」
   （:class:`CompressionConfig`）控制行为，且带默认值，可省略。

用法
----
::

    from graph_utils import build_prompt_llm_chain, CompressionConfig

    # 1) 使用默认压缩策略
    chain = build_prompt_llm_chain(prompt, kimi_llm)

    # 2) 关闭历史，仅保留系统提示
    chain = build_prompt_llm_chain(
        prompt, kimi_think_llm,
        compression=CompressionConfig(enable_history=False),
    )

    # 3) 自定义压缩：仅保留最近 8 条 / 3000 token
    chain = build_prompt_llm_chain(
        prompt, kimi_llm,
        compression=CompressionConfig(max_messages=8, max_tokens=3000),
    )

    resp = chain.invoke({"messages": state.messages, "single_node_data": ...})
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

try:  # trim_messages 在较新版本的 langchain_core 提供，保证裁剪后序列合法
    from langchain_core.messages import trim_messages as _trim_messages
except ImportError:  # pragma: no cover - 兼容旧版本
    _trim_messages = None

logger = logging.getLogger(__name__)

# prompt 中承载历史消息的默认变量名（对应 ("placeholder", "{messages}")）
DEFAULT_HISTORY_KEY = "messages"

TokenCounter = Callable[[Sequence[BaseMessage]], int]


# ---------------------- 压缩参数（唯一对外配置） ----------------------
@dataclass(frozen=True)
class CompressionConfig:
    """历史消息压缩策略。

    属性
    ----
    enable_history:
        ``False`` 时丢弃全部历史，仅保留 prompt 中的系统提示。
    max_messages:
        保留的历史消息条数上限；``None`` 表示不按条数裁剪。
    max_tokens:
        保留的历史 token 上限；``None`` 表示不按 token 裁剪。
        与 ``max_messages`` 可同时生效（取更严格者）。
    strategy:
        裁剪方向，``"last"`` 保留最近、``"first"`` 保留最早。
    start_on:
        裁剪后允许的起始消息类型，避免以孤立的工具消息开头。
    include_system:
        裁剪时是否始终保留 system 消息。
    """

    enable_history: bool = True
    max_messages: Optional[int] = 20
    max_tokens: Optional[int] = None
    strategy: str = "last"
    start_on: str = "human"
    include_system: bool = True


# 省略 compression 参数时使用的默认压缩策略
DEFAULT_COMPRESSION = CompressionConfig()


# ---------------------- 内部工具 ----------------------
def _coerce_history(value: Any) -> List[BaseMessage]:
    """把入参里的历史字段规整成消息列表。"""
    if value is None:
        return []
    if isinstance(value, BaseMessage):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _default_token_counter(llm: Runnable) -> TokenCounter:
    """优先复用 llm 自带的 token 统计，退化为字符数近似。"""
    counter = getattr(llm, "get_num_tokens_from_messages", None)
    if callable(counter):

        def _count(messages: Sequence[BaseMessage]) -> int:
            try:
                return counter(list(messages))
            except Exception:  # pragma: no cover - 计数失败不应阻断主流程
                logger.debug("llm token 计数失败，退化为字符近似", exc_info=True)
                return _approx_token_count(messages)

        return _count

    return _approx_token_count


def _approx_token_count(messages: Sequence[BaseMessage]) -> int:
    """粗略 token 估算：约 4 字符 ≈ 1 token（中英文混排的保守值）。"""
    chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    return chars // 4 + len(messages)


def _trim_history(
    messages: List[BaseMessage],
    config: CompressionConfig,
    token_counter: TokenCounter,
) -> List[BaseMessage]:
    """按条数 / token 裁剪历史，尽量保证裁剪后消息序列合法。"""
    if not messages:
        return messages

    # 先按条数裁剪（若配置了 max_messages）
    if config.max_messages is not None and len(messages) > config.max_messages:
        messages = _apply_trim(
            messages,
            token_counter=len,
            max_tokens=config.max_messages,
            config=config,
        )

    # 再按 token 裁剪（若配置了 max_tokens）
    if config.max_tokens is not None and token_counter(messages) > config.max_tokens:
        messages = _apply_trim(
            messages,
            token_counter=token_counter,
            max_tokens=config.max_tokens,
            config=config,
        )

    return messages


def _apply_trim(
    messages: List[BaseMessage],
    token_counter: TokenCounter,
    max_tokens: int,
    config: CompressionConfig,
) -> List[BaseMessage]:
    """封装 trim_messages，不可用时退化为简单头 / 尾截断。"""
    if _trim_messages is not None:
        return _trim_messages(
            messages,
            token_counter=token_counter,
            max_tokens=max_tokens,
            strategy=config.strategy,
            start_on=config.start_on,
            include_system=config.include_system,
            allow_partial=False,
        )

    # 退化路径：仅在按条数裁剪时才有确定语义
    if token_counter is len:
        return messages[-max_tokens:] if config.strategy == "last" else messages[:max_tokens]
    return messages


# ---------------------- 对外构建函数 ----------------------
def build_prompt_llm_chain(
    prompt: ChatPromptTemplate,
    llm: Runnable,
    compression: CompressionConfig = DEFAULT_COMPRESSION,
    *,
    history_key: str = DEFAULT_HISTORY_KEY,
    token_counter: Optional[TokenCounter] = None,
) -> Runnable:
    """构建「零侵入」自动裁剪历史的 ``prompt | llm`` 链路。

    参数
    ----
    prompt:
        含 ``("placeholder", "{messages}")`` 占位符的对话模板。
    llm:
        任意可运行的 LLM（如 ``kimi_llm`` / ``kimi_think_llm``）。
    compression:
        压缩参数，见 :class:`CompressionConfig`；省略即用 :data:`DEFAULT_COMPRESSION`。
    history_key:
        prompt 中承载历史消息的变量名，默认 ``"messages"``。
    token_counter:
        自定义 token 计数器；省略时复用 llm 自带计数并回退到字符近似。

    返回
    ----
    ``Runnable``：``invoke`` 入参为 prompt 所需的变量字典，业务侧无需手动裁剪。
    """
    counter = token_counter or _default_token_counter(llm)

    def _preprocess(inputs: Dict[str, Any]) -> Dict[str, Any]:
        new_inputs = dict(inputs)  # 不原地修改调用方字典

        if not compression.enable_history:
            new_inputs[history_key] = []
            return new_inputs

        history = _coerce_history(new_inputs.get(history_key))
        new_inputs[history_key] = _trim_history(history, compression, counter)
        return new_inputs

    preprocess = RunnableLambda(_preprocess, name="compress_history")
    return preprocess | prompt | llm
