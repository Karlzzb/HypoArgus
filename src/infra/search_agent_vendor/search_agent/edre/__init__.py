"""EDRE — Evidence-Driven Research Engine (a flat LangGraph over search_agent)."""

from .graph import build_research_graph
from .normalizer import make_llm_normalizer
from .planner import make_llm_planner
from .query_generator import make_llm_query_generator
from .reranker import make_local_reranker, make_rerank_gate
from .scorer import make_llm_scorer
from .models import (
    ClaimStatus,
    ClaimVerdict,
    DocumentRef,
    EDREConfig,
    EvidenceClaim,
    Importance,
    ResearchInput,
    ResearchOutput,
    ResearchResult,
    ResearchState,
    ResearchSummary,
    SearchRound,
    Terminal,
    derive_status,
)

__all__ = [
    "build_research_graph",
    "make_llm_planner",
    "make_llm_query_generator",
    "make_llm_normalizer",
    "make_local_reranker",
    "make_rerank_gate",
    "make_llm_scorer",
    "EDREConfig",
    "EvidenceClaim",
    "Importance",
    "ClaimStatus",
    "Terminal",
    "DocumentRef",
    "SearchRound",
    "derive_status",
    "ResearchSummary",
    "ClaimVerdict",
    "ResearchOutput",
    "ResearchInput",
    "ResearchResult",
    "ResearchState",
]
