"""Stable public API for SearchAgent V12.

Usage (long-lived runtime):
    from search_agent import SearchAgentRuntime
    runtime = SearchAgentRuntime.from_env(with_llm=True)
    output = await runtime.ainvoke(input_dict)
    await runtime.aclose()

Usage (one-shot):
    from search_agent import ainvoke_search_agent
    output = await ainvoke_search_agent(input_dict, with_llm=True)
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SearchAgentContractError(Exception):
    """Raised when input/output contract validation fails."""


class SearchAgentClosedError(RuntimeError):
    """Raised when ainvoke is called after aclose."""


class SearchAgentConfigurationError(RuntimeError):
    """Raised when environment configuration is insufficient."""


class SearchAgentRuntime:
    """Reusable runtime that compiles the LangGraph once and serves multiple invokes."""

    def __init__(
        self,
        *,
        config: Any,
        dependencies: Any,
        graph: Any,
        callbacks: list[Any] | None = None,
    ) -> None:
        self._config = config
        self._dependencies = dependencies
        self._graph = graph
        self._callbacks = callbacks or []
        self._closed = False

    @classmethod
    def from_env(
        cls,
        *,
        with_llm: bool = True,
        callbacks: list[Any] | None = None,
    ) -> "SearchAgentRuntime":
        """Create runtime from environment configuration."""
        from .evidence_retrieval.config import EvidenceRetrievalConfig
        from .evidence_retrieval.dependencies import EvidenceRetrievalDependencies
        from .evidence_retrieval.search_agent_graph import build_search_agent_graph
        from .evidence_retrieval.providers.openai_compatible_chat import OpenAICompatibleChatClient
        from .tracing import get_langfuse_callback

        config = EvidenceRetrievalConfig.from_env()
        cb = list(callbacks or [])
        callback = get_langfuse_callback()
        if callback is not None:
            cb.append(callback)

        if with_llm:
            llm = OpenAICompatibleChatClient.from_env(
                model=config.judge_model,
                timeout_seconds=config.parallel_batch_judge_timeout_ms / 1000,
            )
            if llm is None:
                raise SearchAgentConfigurationError(
                    "LLM_KEY / LLM_BASE_URL / LLM_MODEL is not configured. "
                    "Set these in .env or pass with_llm=False."
                )
            dependencies = EvidenceRetrievalDependencies.with_llm(config, llm)
        else:
            dependencies = EvidenceRetrievalDependencies.defaults(config)

        graph = build_search_agent_graph(config, dependencies, callbacks=cb or None)
        return cls(config=config, dependencies=dependencies, graph=graph, callbacks=cb)

    @classmethod
    def create(
        cls,
        *,
        config: Any,
        dependencies: Any,
        callbacks: list[Any] | None = None,
    ) -> "SearchAgentRuntime":
        """Dependency-injection entry point for testing and host systems."""
        from .evidence_retrieval.search_agent_graph import build_search_agent_graph

        graph = build_search_agent_graph(config, dependencies, callbacks=callbacks or None)
        return cls(config=config, dependencies=dependencies, graph=graph, callbacks=callbacks)

    async def ainvoke(self, payload: Mapping[str, Any] | Any) -> dict[str, Any]:
        """Invoke the SearchAgent graph.

        Input: search-agent-input/v1 (dict or SearchAgentInputState).
        Output: search-agent-output/v1 (JSON-serializable dict).
        """
        if self._closed:
            raise SearchAgentClosedError("SearchAgentRuntime has been closed. Create a new instance.")

        from .evidence_retrieval.public_contracts import SearchAgentInputState, SearchAgentOutputState

        if isinstance(payload, Mapping):
            input_state = SearchAgentInputState.model_validate(dict(payload))
        else:
            input_state = SearchAgentInputState.model_validate(payload)

        state = await self._graph.ainvoke({"input": input_state.model_dump(mode="json")})

        raw_output = state.get("public_output") or state
        if isinstance(raw_output, Mapping) and "public_output" in raw_output:
            raw_output = raw_output["public_output"]

        output = SearchAgentOutputState.model_validate(raw_output)

        # Hard ID consistency check
        if output.request_id != input_state.request_id:
            raise SearchAgentContractError(
                f"request_id mismatch: input={input_state.request_id} output={output.request_id}"
            )
        if output.document_id != input_state.document_id:
            raise SearchAgentContractError(
                f"document_id mismatch: input={input_state.document_id} output={output.document_id}"
            )
        if output.paragraph_id != input_state.paragraph.paragraph_id:
            raise SearchAgentContractError(
                f"paragraph_id mismatch: input={input_state.paragraph.paragraph_id} output={output.paragraph_id}"
            )

        return output.model_dump(mode="json")

    async def aclose(self) -> None:
        """Idempotently close all dependency resources (HTTP/LLM/KB clients)."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._dependencies, "aclose", None)
        if close is not None:
            await close()

    async def __aenter__(self) -> "SearchAgentRuntime":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


async def ainvoke_search_agent(
    payload: Mapping[str, Any] | Any,
    *,
    with_llm: bool = True,
) -> dict[str, Any]:
    """One-shot call. Creates a Runtime, invokes, and closes."""
    async with SearchAgentRuntime.from_env(with_llm=with_llm) as runtime:
        return await runtime.ainvoke(payload)


__all__ = [
    "SearchAgentRuntime",
    "ainvoke_search_agent",
    "SearchAgentContractError",
    "SearchAgentClosedError",
    "SearchAgentConfigurationError",
]
