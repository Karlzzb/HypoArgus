"""EDRE Query Generator (Slice D) — unresolved claims -> fresh-angle queries.

Deepens the skeleton's inert passthrough into the component that replaces the
old subgraph ``decompose`` (see ADR-0001): one LLM call per *unresolved* claim
turns its falsifiable hypothesis into ``queries_per_claim`` focused web-search
queries. Resolved (VERIFIED/REFUTED) and ABANDONED claims are skipped entirely so
no budget is spent re-searching settled work. v1's only retry strategy is "new
angle" — the current retry round is passed to the LLM so each pass approaches the
hypothesis afresh (rewrite-vs-new is an internal detail, per ADR-0003).

``make_llm_query_generator(llm)`` returns a ``QueryGenerator`` closure, keeping
the component replaceable without touching the up/downstream nodes.
"""

from __future__ import annotations

import asyncio

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .models import ClaimStatus, EDREConfig, EvidenceClaim, ResearchState, derive_status

_UNRESOLVED = {ClaimStatus.NOT_STARTED, ClaimStatus.PARTIAL}

_SYSTEM_PROMPT = (
    "You generate focused web-search queries to gather evidence that could "
    "verify or refute a single hypothesis. Return one query per line, with no "
    "numbering, quoting, or extra prose. Each query should approach the "
    "hypothesis from a distinct angle."
)


def _prompt(claim: EvidenceClaim, config: EDREConfig, round_index: int) -> str:
    return (
        f"Generate {config.queries_per_claim} web-search queries that would "
        "surface evidence bearing on whether the following hypothesis is TRUE or "
        f"FALSE. This is retry round {round_index + 1}; approach the hypothesis "
        "from fresh angles that differ from the obvious phrasing. Return ONLY the "
        "queries, one per line.\n\n"
        f"Hypothesis:\n{claim.hypothesis}"
    )


def _parse_queries(text: str, fallback: str, limit: int) -> list[str]:
    """Parse the LLM's one-per-line queries, clamped to ``limit``.

    Strips list markers and blank lines; degrades to ``[fallback]`` (the raw
    hypothesis) when the model returns nothing usable, so a claim is never left
    without a query to drive its search.
    """
    queries: list[str] = []
    for line in (text or "").splitlines():
        cleaned = line.strip().lstrip("-*0123456789.() ").strip()
        if cleaned:
            queries.append(cleaned)
    queries = queries[: max(1, limit)]
    return queries or [fallback]


def make_llm_query_generator(llm: BaseChatModel):
    """Build a ``QueryGenerator`` closure backed by *llm*."""

    async def query_generator(
        state: ResearchState, config: EDREConfig
    ) -> dict[str, list[str]]:
        round_index = state["loop_count"]
        unresolved = [
            claim
            for claim in state["evidence_plan"]
            if derive_status(claim, config) in _UNRESOLVED
        ]

        async def generate(claim: EvidenceClaim) -> list[str]:
            message = await llm.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=_prompt(claim, config, round_index)),
                ]
            )
            text = (
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            )
            return _parse_queries(text, claim.hypothesis, config.queries_per_claim)

        results = await asyncio.gather(*(generate(c) for c in unresolved))
        return {claim.id: queries for claim, queries in zip(unresolved, results)}

    return query_generator


__all__ = ["make_llm_query_generator"]
