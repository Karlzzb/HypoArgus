"""E2E 确定性 LLM fake（T-07）——一个可流式的 ``BaseChatModel``。

真实 Qwen（``ChatOpenAI``）经 ``with_structured_output(...).invoke()`` 调用；本 fake 在
``with_structured_output`` 里把传入 schema 的**默认 JSON** 逐字符 ``_stream``/``_astream``，
使 ``astream_events(version="v2")`` 产出 ``on_chat_model_stream`` → 翻译层产 ``llm_thinking``
（前端实时 CoT 数据源），同时 ``with_structured_output`` 末尾把整段 JSON 解析回 schema 实例，
故语义结果与离线 ``Fake*``（空 proposals → 全段 background 影子 → 终稿逐字节原文）一致——
完整 HITL 流程 + 流式 token，但无网络 / 无 token 消耗、确定性。

设计为「一实例多用」：``with_structured_output(schema)`` 内按 schema 默认 JSON 派生子实例，
故同一 ``StreamingFakeChat`` 可注入四条 Qwen*LlmClient seam。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

__all__ = ["StreamingFakeChat"]


class StreamingFakeChat(BaseChatModel):
    """逐字符流式吐出 ``canned`` JSON 的确定性 chat 模型。"""

    canned: str = ""

    def _llm_type(self) -> str:
        return "streaming-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.canned))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        for ch in self.canned:
            yield ChatGenerationChunk(message=AIMessageChunk(content=ch))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        for ch in self.canned:
            yield ChatGenerationChunk(message=AIMessageChunk(content=ch))

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any) -> Any:
        """返回 ``子模型 | 解析``：子模型流式吐该 schema 的默认 JSON，末尾解析回实例。"""

        del kwargs
        canned = schema().model_dump_json()
        sub = StreamingFakeChat(canned=canned)

        def _parse(msg: Any) -> BaseModel:
            content = msg.content if isinstance(msg, BaseMessage) else str(msg)
            return schema.model_validate_json(content)

        return sub | RunnableLambda(_parse)
