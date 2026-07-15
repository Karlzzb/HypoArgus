"""``search_many`` — EDRE's sole retrieval seam over the search_agent layer.

EDRE does not drive the full ``build_search_subgraph`` subgraph. Instead it calls
this pure-function adapter, which reuses the existing provider registry and the
searxng->duckduckgo failover plus the concurrent fan-out pattern, but returns
URL-deduplicated documents that retain *query attribution* (a document records
every query in the batch that surfaced it) rather than the subgraph's merged
single-answer output. No LLM answer synthesis happens here (see ADR-0001).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import SearchConfig
from .contracts import Citation, SearchResult, WebSearchResponse
from .providers import get_provider
from .subgraph import _effective_provider

logger = logging.getLogger(__name__)


@dataclass
class RetrievedDocument:
    """A deduplicated search result with its citation and query attribution.

    ``source_queries`` records every query in the current batch whose results
    surfaced this document (matched by URL), preserving traceability when the
    same page is hit by multiple queries. ``normalized`` holds EDRE's per-document
    distilled fact fragment once the normalization node has run (``None`` until
    then), keeping the fragment bound to its citation for downstream grounding.
    """

    result: SearchResult
    citation: Citation
    source_queries: list[str] = field(default_factory=list)
    normalized: str | None = None
    task_match: float = 0.0
    query_match: float = 0.0


async def _fan_out(
    config: SearchConfig, queries: list[str]
) -> list[tuple[str, WebSearchResponse]]:
    """Search every query concurrently, degrading per-path on failure.

    A single query's failure is logged and skipped (never fatal); the surviving
    paths still produce documents. Provider selection (including the searxng ->
    duckduckgo failover) happens here, inside the adapter, so no failover
    decision leaks to the caller.
    """
    provider = get_provider(
        _effective_provider(config), api_key=config.api_key, proxy=config.proxy
    )

    async def _search(query: str) -> WebSearchResponse:
        return await asyncio.to_thread(
            provider.search,
            query,
            base_url=config.base_url or "",
            max_results=config.max_results,
        )

    settled = await asyncio.gather(
        *(_search(q) for q in queries), return_exceptions=True
    )
    survivors: list[tuple[str, WebSearchResponse]] = []
    for query, result in zip(queries, settled):
        if isinstance(result, BaseException):
            logger.warning("search_many: query %r failed: %s", query, result)
            continue
        survivors.append((query, result))
    return survivors


async def search_many(
    config: SearchConfig, queries: list[str]
) -> list[RetrievedDocument]:
    """Fan out *queries* and return URL-deduplicated documents with attribution.

    Documents are deduplicated by URL across all fan-out paths (results without a
    URL are always kept). Each surviving document carries the list of queries that
    surfaced it and a citation renumbered contiguously (``[1]``, ``[2]``, ...).
    """
    survivors = await _fan_out(config, queries)

    documents: list[RetrievedDocument] = []
    by_url: dict[str, RetrievedDocument] = {}
    for query, response in survivors:
        for result in response.search_results:
            existing = by_url.get(result.url) if result.url else None
            if existing is not None:
                if query not in existing.source_queries:
                    existing.source_queries.append(query)
                continue
            doc = RetrievedDocument(
                result=result,
                citation=Citation(
                    id=0,
                    reference="",
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet,
                    source=result.source,
                ),
                source_queries=[query],
            )
            documents.append(doc)
            if result.url:
                by_url[result.url] = doc

    for index, doc in enumerate(documents, 1):
        doc.citation.id = index
        doc.citation.reference = f"[{index}]"
    return documents


__all__ = ["search_many", "RetrievedDocument"]
