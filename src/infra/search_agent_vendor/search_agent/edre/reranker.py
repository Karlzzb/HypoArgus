"""EDRE first-layer gate (Slice E) — a local cross-encoder, Precision First.

Deepens the skeleton's inert passthrough into the first of the two evaluation
layers (see ADR-0002). A local cross-encoder scores every retrieved document
twice against the *same* model, changing only the left input:

- **Task Match** — document vs the original ``task``
- **Query Match** — document vs the query that surfaced it

The two scores are combined ``w1*TaskMatch + w2*QueryMatch`` and any document
below ``rerank_threshold`` is dropped *before* normalization or claim scoring —
the Precision First gate that keeps off-topic content out of the evidence. The
gate runs on *raw* document text; it never depends on the normalization node
(which runs downstream).

Two seams keep this replaceable (local or hosted, per PRD):

- ``Reranker`` — the low-level scorer ``rerank(left_text, docs) -> [scores]``.
  ``make_local_reranker`` returns one backed by ``sentence_transformers``.
- ``RerankGate`` — the node-level gate ``(state, docs) -> surviving docs``,
  built from a ``Reranker`` by ``make_rerank_gate``. This is what the graph
  injects; tests build the *real* gate around a fake ``Reranker`` so the gating
  logic is exercised deterministically and offline.
"""

from __future__ import annotations

import math
from typing import Callable

from ..env import env_str
from ..retrieval import RetrievedDocument
from .models import EDREConfig, ResearchState

# The cross-encoder abstraction: score each document against one left text.
Reranker = Callable[[str, list[RetrievedDocument]], list[float]]
# The node-level first-layer gate: filter documents, preserving order.
RerankGate = Callable[[ResearchState, list[RetrievedDocument]], list[RetrievedDocument]]

_DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _document_text(doc: RetrievedDocument) -> str:
    """The raw right-hand text a cross-encoder scores: title + body, unmodified."""
    result = doc.result
    body = result.content or result.snippet or ""
    return f"{result.title}\n{body}".strip() if result.title else body


def _sigmoid(x: float) -> float:
    """Squash a raw cross-encoder logit into ``[0, 1]`` so thresholds are stable."""
    return 1.0 / (1.0 + math.exp(-x))


def make_local_reranker(model_name: str | None = None) -> Reranker:
    """Build a ``Reranker`` backed by a local ``sentence_transformers`` cross-encoder.

    The heavy model is imported and loaded lazily on first use so importing EDRE
    never pulls in torch. Raw logits are sigmoid-normalized to ``[0, 1]``.
    """
    name = model_name or _DEFAULT_RERANK_MODEL
    model: object | None = None

    def rerank(left_text: str, docs: list[RetrievedDocument]) -> list[float]:
        nonlocal model
        if not docs:
            return []
        if model is None:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(name)
        pairs = [(left_text, _document_text(doc)) for doc in docs]
        scores = model.predict(pairs)  # type: ignore[attr-defined]
        return [_sigmoid(float(score)) for score in scores]

    return rerank


def default_local_reranker(config: EDREConfig) -> Reranker:
    """The production default cross-encoder (model overridable via env)."""
    return make_local_reranker(env_str("EDRE_RERANK_MODEL"))


def make_rerank_gate(reranker: Reranker, config: EDREConfig) -> RerankGate:
    """Build the first-layer gate from a ``Reranker``.

    For each document: ``TaskMatch`` scores it against the task, ``QueryMatch``
    against the best of the queries that surfaced it (``source_queries``), reusing
    the same ``reranker`` with a different left input. A document survives when
    ``w1*TaskMatch + w2*QueryMatch >= rerank_threshold``; order is preserved.
    """

    def gate(
        state: ResearchState, docs: list[RetrievedDocument]
    ) -> list[RetrievedDocument]:
        if not docs:
            return docs

        task_scores = reranker(state["task"], docs)

        unique_queries: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            for query in doc.source_queries:
                if query not in seen:
                    seen.add(query)
                    unique_queries.append(query)
        query_scores = {query: reranker(query, docs) for query in unique_queries}

        survivors: list[RetrievedDocument] = []
        for i, doc in enumerate(docs):
            task_match = task_scores[i]
            per_doc = [
                query_scores[query][i]
                for query in doc.source_queries
                if query in query_scores
            ]
            query_match = max(per_doc) if per_doc else 0.0
            combined = config.rerank_w1 * task_match + config.rerank_w2 * query_match
            if combined >= config.rerank_threshold:
                # Stamp the gate's scores on the survivor so downstream evidence
                # ordering (doc_relevance) can reuse them without re-scoring.
                doc.task_match = task_match
                doc.query_match = query_match
                survivors.append(doc)
        return survivors

    return gate


__all__ = [
    "Reranker",
    "RerankGate",
    "make_local_reranker",
    "default_local_reranker",
    "make_rerank_gate",
]
