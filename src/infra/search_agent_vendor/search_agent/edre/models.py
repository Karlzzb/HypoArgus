"""EDRE data models — the spine the walking skeleton (Slice B) stands on.

All models are plain ``@dataclass`` (consistent with ``SearchConfig`` /
``Citation`` / ``RetrievedDocument``), never Pydantic. Per CONTEXT.md an
``EvidenceClaim``'s ``status`` is a *derived*, read-only view computed from
``confidence + search_attempts`` against the injected thresholds — it is never
stored on the claim. ``derive_status`` is that single derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict

from ..config import SearchConfig
from ..contracts import Citation
from ..retrieval import RetrievedDocument


class Importance(str, Enum):
    """Binary importance of an EvidenceClaim (no continuous weights)."""

    CRITICAL = "CRITICAL"
    OPTIONAL = "OPTIONAL"


class ClaimStatus(str, Enum):
    """Derived, read-only view of an EvidenceClaim.

    ``VERIFIED`` (strong positive) and ``REFUTED`` (strong negative — a
    *successful* discovery) are the two "resolved" states. ``ABANDONED`` (budget
    spent, still indecisive) must never be conflated with ``REFUTED``.
    """

    NOT_STARTED = "NOT_STARTED"
    PARTIAL = "PARTIAL"
    VERIFIED = "VERIFIED"
    REFUTED = "REFUTED"
    ABANDONED = "ABANDONED"


class Terminal(str, Enum):
    """The two terminal states of the research loop."""

    DONE = "DONE"
    EXHAUSTED = "EXHAUSTED"


@dataclass
class DocumentRef:
    """A document that contributed evidence to a claim, with its signed support.

    ``task_match`` / ``query_match`` are the first-layer cross-encoder scores that
    let the document survive the gate; together with ``support`` they feed
    ``doc_relevance`` when ordering a claim's evidence (see ``doc_relevance``).
    """

    citation: Citation
    support: float
    task_match: float = 0.0
    query_match: float = 0.0


@dataclass
class EvidenceClaim:
    """A falsifiable hypothesis the Planner produced for a task.

    ``confidence`` is the max-by-absolute-value signed support across all
    evidence (its sign decides VERIFIED vs REFUTED); it is the single source of
    truth for progress. ``status`` is *not* stored — see ``derive_status``.
    """

    id: str
    hypothesis: str
    importance: Importance
    confidence: float = 0.0
    supporting_documents: list[DocumentRef] = field(default_factory=list)
    search_attempts: int = 0


@dataclass
class EDREConfig:
    """Injected configuration container.

    Slice B carries only the knobs the skeleton's loop and termination need; the
    full config surface is completed in Slice I.
    """

    search: SearchConfig = field(default_factory=SearchConfig)
    verify_threshold: float = 0.7
    refute_threshold: float = 0.7
    max_attempts: int = 3
    max_loops: int = 6
    queries_per_claim: int = 2
    min_claims: int = 3
    max_claims: int = 6
    min_critical: int = 1
    # First-layer gate (Slice E): a document survives when
    # w1*TaskMatch + w2*QueryMatch >= rerank_threshold.
    rerank_threshold: float = 0.3
    rerank_w1: float = 0.5
    rerank_w2: float = 0.5
    # Evidence ordering (Slice I): a claim's supporting documents are ranked by
    # doc_relevance = w1*TaskMatch + w2*QueryMatch + w3*|support|.
    doc_relevance_w1: float = 1.0
    doc_relevance_w2: float = 1.0
    doc_relevance_w3: float = 1.0


@dataclass
class SearchRound:
    """One loop's retrieval + scoring record, supporting per-evidence traceability."""

    loop_index: int
    queries: list[str] = field(default_factory=list)
    documents: list[RetrievedDocument] = field(default_factory=list)
    support_scores: list[dict[str, float]] = field(default_factory=list)
    rerank_scores: list[float] = field(default_factory=list)
    provider: str = ""
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)


def derive_status(claim: EvidenceClaim, config: EDREConfig) -> ClaimStatus:
    """Derive a claim's read-only status from confidence + search_attempts.

    Resolution is checked first so a claim that resolves on its final allowed
    attempt reads as VERIFIED/REFUTED rather than ABANDONED.
    """
    if claim.confidence >= config.verify_threshold:
        return ClaimStatus.VERIFIED
    if claim.confidence <= -config.refute_threshold:
        return ClaimStatus.REFUTED
    if claim.search_attempts == 0:
        return ClaimStatus.NOT_STARTED
    if claim.search_attempts >= config.max_attempts:
        return ClaimStatus.ABANDONED
    return ClaimStatus.PARTIAL


def doc_relevance(ref: DocumentRef, config: EDREConfig) -> float:
    """Relevance of one evidence document, for ranking a claim's supporting docs.

    ``w1*TaskMatch + w2*QueryMatch + w3*|support|`` — combining the first-layer
    gate's topicality signals with the magnitude of the second-layer support.
    """
    return (
        config.doc_relevance_w1 * ref.task_match
        + config.doc_relevance_w2 * ref.query_match
        + config.doc_relevance_w3 * abs(ref.support)
    )


# --- Output contract ------------------------------------------------------


@dataclass
class ResearchSummary:
    """Deterministic result snapshot (no LLM): terminal + verdict counts.

    ``blocking_claim_ids`` makes the terminal decision explainable: the critical
    claims still unresolved when the loop stopped. It is empty for DONE (all
    critical resolved) and names the offenders for EXHAUSTED.
    """

    terminal: str
    verified: int
    refuted: int
    abandoned: int
    critical_all_resolved: bool
    coverage: float
    loop_count: int
    blocking_claim_ids: list[str] = field(default_factory=list)


@dataclass
class ClaimVerdict:
    """Per-claim verdict with the documents that supported/refuted it."""

    claim_id: str
    hypothesis: str
    importance: str
    status: str
    confidence: float
    supporting_documents: list[DocumentRef]


@dataclass
class ResearchOutput:
    """EDRE's v1 output contract."""

    research_summary: ResearchSummary
    evidence: list[ClaimVerdict]
    citations: list[Citation]
    loop_count: int


# --- Graph state (LangGraph channels) -------------------------------------


class ResearchInput(TypedDict):
    """The contract the caller passes in."""

    task: str


class ResearchResult(TypedDict):
    """The contract the caller reads back (the graph's output schema)."""

    output: "ResearchOutput | None"


class ResearchState(TypedDict, total=False):
    """In-memory flat-graph state (no checkpointer in v1, see ADR-0004).

    ``task``, ``evidence_plan``, ``search_history``, ``loop_count`` and
    ``terminal`` are the durable spine; ``queries`` / ``documents`` /
    ``doc_scores`` are per-loop transients passed between the sequential nodes.
    """

    task: str
    evidence_plan: list[EvidenceClaim]
    search_history: list[SearchRound]
    loop_count: int
    terminal: "str | None"
    queries: dict[str, list[str]]
    documents: list[RetrievedDocument]
    doc_scores: list[dict[str, float]]
    output: "ResearchOutput | None"


__all__ = [
    "Importance",
    "ClaimStatus",
    "Terminal",
    "DocumentRef",
    "EvidenceClaim",
    "EDREConfig",
    "SearchRound",
    "derive_status",
    "doc_relevance",
    "ResearchSummary",
    "ClaimVerdict",
    "ResearchOutput",
    "ResearchInput",
    "ResearchResult",
    "ResearchState",
]
