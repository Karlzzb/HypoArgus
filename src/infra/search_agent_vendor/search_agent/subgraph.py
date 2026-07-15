"""LangGraph search-retrieval subgraph.

``build_search_subgraph`` compiles a deterministic map-reduce retrieval subgraph:

    decompose -> fan_out_search -> consolidate

All nodes are async. ``decompose`` uses the resolved LLM to split the task into
1..N subqueries (degrading to a single subquery when no LLM is available),
``fan_out_search`` searches every subquery concurrently, and ``consolidate``
renders the merged results with the template path (LLM consolidation is Slice 4).

The subgraph exposes a narrow contract to a parent graph via input/output
schemas: it consumes ``{"task": str}`` and returns ``{"consolidated": str,
"citations": list[Citation]}``. The intermediate keys (``subqueries`` /
``raw_results``) stay internal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from .config import SearchConfig
from .consolidation import AnswerConsolidator
from .llm import default_chat_model
from .providers import get_provider
from .providers.searxng import _validate_base_url
from .tracing import get_langfuse_callback
from .contracts import Citation, WebSearchResponse

logger = logging.getLogger(__name__)

_DECOMPOSE_SYSTEM_PROMPT = (
    "You split a research task into focused web-search subqueries. "
    "Return between 1 and 5 subqueries, one per line, no numbering or extra text."
)


def _decompose_prompt(task: str) -> str:
    return (
        "Split the following task into 1-5 focused web-search subqueries, "
        "one per line:\n\n"
        f"{task}"
    )


def _parse_subqueries(text: str, task: str) -> list[str]:
    """Parse the LLM's decompose output into a subquery list.

    Accepts either a JSON array of strings or a newline/bullet list. Falls back
    to ``[task]`` when nothing usable is produced.
    """
    text = (text or "").strip()
    if not text:
        return [task]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            items = [str(item).strip() for item in parsed if str(item).strip()]
            if items:
                return items
        except (ValueError, TypeError):
            pass
    items: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-*0123456789.() ").strip()
        if cleaned:
            items.append(cleaned)
    return items or [task]


class SearchSubgraphState(TypedDict):
    """Internal state of the retrieval subgraph."""

    task: str
    subqueries: list[str]
    raw_results: list[WebSearchResponse]
    consolidated: str
    citations: list[Citation]


class SearchInput(TypedDict):
    """Contract the parent graph passes in."""

    task: str


class SearchOutput(TypedDict):
    """Contract the parent graph reads back."""

    consolidated: str
    citations: list[Citation]


def _effective_provider(config: SearchConfig) -> str:
    """Resolve the provider actually used, applying the searxng fallback.

    ``searxng`` needs a ``base_url``; when none is configured the subgraph falls
    back to the zero-config ``duckduckgo`` provider (the historical
    ``web_search()`` behavior).
    """
    if config.provider == "searxng" and not config.base_url:
        return "duckduckgo"
    return config.provider


def _merge_responses(task: str, raw_results: list[WebSearchResponse]) -> WebSearchResponse:
    """Merge the per-subquery responses into one, renumbering citations."""
    provider = raw_results[0].provider if raw_results else "searxng"
    merged = WebSearchResponse(query=task, answer="", provider=provider)
    # Simple url-based dedup across fan-out paths (no semantic fusion / rerank).
    # Results without a url are always kept.
    seen: set[str] = set()
    for response in raw_results:
        for result in response.search_results:
            if result.url and result.url in seen:
                continue
            if result.url:
                seen.add(result.url)
            merged.search_results.append(result)
    for index, result in enumerate(merged.search_results, 1):
        merged.citations.append(
            Citation(
                id=index,
                reference=f"[{index}]",
                url=result.url,
                title=result.title,
                snippet=result.snippet,
                source=result.source,
            )
        )
    return merged


def build_search_subgraph(config: SearchConfig, llm: BaseChatModel | None = None):
    """Build and compile the retrieval subgraph for *config*.

    Args:
        config: Injected retrieval configuration (provider, base_url, ...).
        llm: Optional LangChain chat model shared by decompose/consolidate. When
            omitted, the built-in default model is used if configured, else
            decompose degrades to a single subquery.

    Returns:
        A compiled LangGraph subgraph with input ``{"task": str}`` and output
        ``{"consolidated": str, "citations": list[Citation]}``. When Langfuse is
        configured, a callback handler is baked into the graph so every node and
        LLM generation is traced.
    """

    # Validate an explicit searxng base_url up front so a malformed URL surfaces
    # clearly at build time instead of being swallowed by per-path degradation.
    if config.provider == "searxng" and config.base_url:
        _validate_base_url(config.base_url)

    resolved_llm = llm if llm is not None else default_chat_model()

    # Resolve the consolidate-side model: an explicit override targets only the
    # consolidate step; decompose always keeps the base model. For the injected
    # case we bind the model kwarg; for the default case we build a fresh model.
    consolidate_llm = resolved_llm
    if resolved_llm is not None and config.consolidation_llm_model:
        if llm is not None:
            consolidate_llm = resolved_llm.bind(model=config.consolidation_llm_model)
        else:
            consolidate_llm = (
                default_chat_model(model=config.consolidation_llm_model) or resolved_llm
            )

    async def decompose(state: SearchSubgraphState) -> dict:
        task = state["task"]
        if resolved_llm is None:
            return {"subqueries": [task]}
        message = await resolved_llm.ainvoke(
            [
                SystemMessage(content=_DECOMPOSE_SYSTEM_PROMPT),
                HumanMessage(content=_decompose_prompt(task)),
            ]
        )
        text = message.content if isinstance(message.content, str) else str(message.content)
        return {"subqueries": _parse_subqueries(text, task)}

    async def fan_out_search(state: SearchSubgraphState) -> dict:
        provider = get_provider(
            _effective_provider(config), api_key=config.api_key, proxy=config.proxy
        )

        async def _search(subquery: str) -> WebSearchResponse:
            return await asyncio.to_thread(
                provider.search,
                subquery,
                base_url=config.base_url or "",
                max_results=config.max_results,
            )

        subqueries = state["subqueries"]
        # Degrade per-path: a single subquery failure must not abort the whole
        # fan-out (unlike the old single-shot web_search()). Failed paths are
        # logged and skipped; the surviving paths still consolidate.
        settled = await asyncio.gather(
            *(_search(sq) for sq in subqueries), return_exceptions=True
        )
        raw_results: list[WebSearchResponse] = []
        for subquery, result in zip(subqueries, settled):
            if isinstance(result, BaseException):
                logger.warning("search failed for subquery %r: %s", subquery, result)
                continue
            raw_results.append(result)
        return {"raw_results": raw_results}

    async def consolidate(state: SearchSubgraphState) -> dict:
        merged = _merge_responses(state["task"], state["raw_results"])
        provider = get_provider(
            _effective_provider(config), api_key=config.api_key, proxy=config.proxy
        )
        # Only providers that return raw SERP results (supports_answer=False, e.g.
        # SearXNG) need consolidation. Providers that already synthesize an answer
        # skip the extra LLM pass.
        use_llm = config.consolidation_use_llm and not provider.supports_answer
        consolidator = AnswerConsolidator(
            use_llm=use_llm,
            custom_template=config.consolidation_custom_template,
            max_results=config.max_results,
            llm=consolidate_llm,
        )
        await consolidator.consolidate(merged)
        return {"consolidated": merged.answer, "citations": merged.citations}

    builder = StateGraph(
        SearchSubgraphState,
        input_schema=SearchInput,
        output_schema=SearchOutput,
    )
    builder.add_node("decompose", decompose)
    builder.add_node("fan_out_search", fan_out_search)
    builder.add_node("consolidate", consolidate)
    builder.add_edge(START, "decompose")
    builder.add_edge("decompose", "fan_out_search")
    builder.add_edge("fan_out_search", "consolidate")
    builder.add_edge("consolidate", END)
    graph = builder.compile()

    # Bake the Langfuse callback into the compiled graph so every ainvoke is
    # traced with no extra arguments. Non-fatal: absent creds/pkg -> no handler.
    handler = get_langfuse_callback()
    if handler is not None:
        graph = graph.with_config({"callbacks": [handler]})
    return graph


__all__ = ["build_search_subgraph", "SearchSubgraphState", "SearchInput", "SearchOutput"]
