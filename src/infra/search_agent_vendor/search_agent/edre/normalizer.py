"""EDRE document normalizer (Slice F) — per-document distillation.

Deepens the skeleton's inert passthrough into the grounding step between the
first-layer gate and the second-layer claim scorer. It reuses the LLM
normalization *capability* of ``AnswerConsolidator`` (dense factual + preserve
citation + conflict annotation, explicitly "grounding context for another LLM")
but changes granularity from "merge one query's whole result set into a single
answer" to **one distilled fragment per document** (see ADR-0001). Only the
reranker's surviving documents are normalized, so cost stays bounded, and each
fragment keeps its citation marker so the downstream signed-support scorer (and
its refutation path, ADR-0005) can trace and ground its judgement.

``make_llm_normalizer(llm)`` returns a ``Normalizer`` closure, keeping the
component replaceable without touching the up/downstream nodes.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..retrieval import RetrievedDocument

# The normalizer seam: distill each surviving document into a dense fact
# fragment, returning one fragment per input document (aligned 1:1).
Normalizer = Callable[[list[RetrievedDocument]], Awaitable[list[str]]]

_SYSTEM_PROMPT = (
    "You normalize a single web document into dense grounding context for "
    "another LLM that will judge whether the document supports or refutes "
    "specific claims. Distill only this one document.\n\n"
    "Output format:\n"
    "- A tight list of verifiable facts: numbers, dates, names, definitions, "
    "findings. Omit navigation, boilerplate, and filler.\n"
    "- Keep the document's citation marker (e.g. [1]) on the facts drawn from "
    "it, so every fact stays traceable to its source.\n"
    "- Explicitly flag any hedges, caveats, or statements that conflict with the "
    "document's own assertions — these matter for refutation.\n\n"
    "Be factual and dense. Do not merge in outside knowledge."
)

_MAX_CONTENT_CHARS = 5000


def _prompt(doc: RetrievedDocument) -> str:
    """Build the per-document user prompt, carrying the citation reference.

    Includes the document's citation marker so the distilled fragment can retain
    citation attribution, and its raw title/snippet/body as the material to
    distill (truncated to bound cost, mirroring the consolidator).
    """
    result = doc.result
    reference = doc.citation.reference or "[1]"
    body = result.content or ""
    if len(body) > _MAX_CONTENT_CHARS:
        body = body[:_MAX_CONTENT_CHARS] + "..."
    parts = [f"Document {reference}: {result.title}", f"URL: {result.url}"]
    if result.snippet:
        parts.append(result.snippet)
    if body:
        parts.append(body)
    document_block = "\n".join(parts)
    return (
        "Normalize the single document below into dense, citation-preserving "
        f"grounding notes. Tag facts with its marker {reference} and flag any "
        "internal caveats or conflicts.\n\n"
        f"Document:\n---\n{document_block}\n---"
    )


def make_llm_normalizer(llm: BaseChatModel) -> Normalizer:
    """Build a ``Normalizer`` closure backed by *llm*.

    Issues one LLM call per document (concurrently), so granularity is strictly
    per-document — never a query-merged answer.
    """

    async def normalizer(docs: list[RetrievedDocument]) -> list[str]:
        async def distill(doc: RetrievedDocument) -> str:
            message = await llm.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=_prompt(doc)),
                ]
            )
            content = message.content
            return content if isinstance(content, str) else str(content)

        if not docs:
            return []
        return list(await asyncio.gather(*(distill(doc) for doc in docs)))

    return normalizer


__all__ = ["Normalizer", "make_llm_normalizer"]
