"""EDRE second-layer claim scorer (Slice G) — signed support, refutation-aware.

The first layer (rerank) only gates on topicality; this second layer is where the
engine actually decides whether each surviving document *supports* or *refutes*
each claim. It issues **one** LLM call per document (never per claim), returning
that document's signed support for *all* current claims at once:

- ``support in [-1, 1]``: positive = supports the hypothesis, negative = refutes /
  falsifies it, near-zero = no opinion (see ADR-0005). The sign is what lets a
  claim resolve as REFUTED (a successful discovery) rather than collapsing into
  ABANDONED.

The scorer only emits raw support values; the VERIFIED / REFUTED / ABANDONED
verdict is derived downstream from ``confidence`` (max-by-absolute-value support)
against the injected thresholds — the scorer never reads thresholds itself.

``make_llm_scorer(llm)`` returns a ``Scorer`` closure, keeping the component
replaceable without touching the up/downstream nodes.
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .models import EvidenceClaim
from ..retrieval import RetrievedDocument

_SYSTEM_PROMPT = (
    "You judge whether a single web document supports or refutes each of several "
    "falsifiable claims. For every claim, assign a SIGNED support score in the "
    "range [-1, 1]:\n"
    "- positive (up to +1): the document's evidence SUPPORTS the claim being TRUE;\n"
    "- negative (down to -1): the document's evidence REFUTES the claim / shows it "
    "is FALSE;\n"
    "- near 0: the document is irrelevant to the claim or offers no opinion.\n\n"
    "Magnitude reflects how decisive the evidence is. Base the judgement ONLY on "
    "the provided document; do not use outside knowledge. Respond with ONLY a JSON "
    'object mapping each claim id to its numeric support, e.g. {"c1": 0.9, '
    '"c2": -0.8}.'
)

_MAX_GROUNDING_CHARS = 5000


def _grounding(doc: RetrievedDocument) -> str:
    """The text the claim judgement is grounded on.

    Prefers the normalizer's distilled, citation-preserving fragment when present
    (Slice F); otherwise falls back to the document's raw body or snippet so the
    scorer still has material when normalization was a passthrough.
    """
    if doc.normalized:
        text = doc.normalized
    else:
        text = doc.result.content or doc.result.snippet or doc.result.title or ""
    if len(text) > _MAX_GROUNDING_CHARS:
        text = text[:_MAX_GROUNDING_CHARS] + "..."
    return text


def _prompt(claims: list[EvidenceClaim], doc: RetrievedDocument) -> str:
    reference = doc.citation.reference or "[1]"
    claim_lines = "\n".join(f"- {c.id}: {c.hypothesis}" for c in claims)
    return (
        "Score how the single document below supports or refutes each claim. "
        "Return ONLY a JSON object keyed by claim id with signed support in "
        '[-1, 1], e.g. {"c1": 0.9}.\n\n'
        f"Claims:\n{claim_lines}\n\n"
        f"Document {reference} (URL: {doc.result.url}):\n---\n{_grounding(doc)}\n---"
    )


def _parse_support(text: str, claims: list[EvidenceClaim]) -> dict[str, float]:
    """Parse the LLM's JSON into a ``{claim_id: support}`` dict.

    Only known claim ids with numeric values are kept, each clamped to [-1, 1];
    an omitted claim means "no opinion". Malformed output degrades to ``{}`` (no
    opinion) so one unparseable document never crashes the loop.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    valid_ids = {c.id for c in claims}
    support: dict[str, float] = {}
    for key, value in parsed.items():
        if key not in valid_ids or isinstance(value, bool):
            continue
        try:
            score = float(value)
        except (ValueError, TypeError):
            continue
        support[key] = max(-1.0, min(1.0, score))
    return support


def make_llm_scorer(llm: BaseChatModel):
    """Build a ``Scorer`` closure backed by *llm*.

    Issues one LLM call per document (concurrently), each returning that
    document's signed support vector over all claims.
    """

    async def scorer(
        claims: list[EvidenceClaim], docs: list[RetrievedDocument]
    ) -> list[dict[str, float]]:
        async def score_one(doc: RetrievedDocument) -> dict[str, float]:
            message = await llm.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=_prompt(claims, doc)),
                ]
            )
            content = message.content
            text = content if isinstance(content, str) else str(content)
            return _parse_support(text, claims)

        if not docs:
            return []
        return list(await asyncio.gather(*(score_one(doc) for doc in docs)))

    return scorer


__all__ = ["make_llm_scorer"]
