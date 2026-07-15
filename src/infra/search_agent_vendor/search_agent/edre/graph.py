"""EDRE flat LangGraph — the walking skeleton (Slice B).

A single flat graph (no nested subgraph, see ADR-0004):

    START -> plan -> generate_queries -> search -> rerank -> normalize
          -> score_claims -> update_evidence -> control
    control --CONTINUE--> generate_queries
    control --DONE/EXHAUSTED--> finalize -> END

The intelligent nodes (Planner, scorer) are injectable; their skeleton defaults
are deliberately inert (single-claim plan, no support) so the *spine* — fixed
claim set, the research loop, evidence aggregation, honest DONE-vs-EXHAUSTED
termination, and deterministic assembly — is what this slice actually builds.
Deepening each node is the job of Slices C–G. ``search`` calls Slice A's
``search_many`` adapter as an ordinary node.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from ..llm import default_chat_model
from ..retrieval import RetrievedDocument, search_many
from ..tracing import get_langfuse_callback
from .normalizer import Normalizer, make_llm_normalizer
from .planner import make_llm_planner
from .query_generator import make_llm_query_generator
from .reranker import RerankGate, default_local_reranker, make_rerank_gate
from .scorer import make_llm_scorer
from .models import (
    ClaimStatus,
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
    ClaimVerdict,
    derive_status,
    doc_relevance,
)

# Component seams. These deepen in later slices but the shapes are stable enough
# for the skeleton's fakes to plug in at the highest test seam.
Planner = Callable[[str, EDREConfig], Awaitable[list[EvidenceClaim]]]
QueryGenerator = Callable[[ResearchState, EDREConfig], Awaitable[dict[str, list[str]]]]
SearchFn = Callable[[object, list[str]], Awaitable[list[RetrievedDocument]]]
Scorer = Callable[
    [list[EvidenceClaim], list[RetrievedDocument]], Awaitable[list[dict[str, float]]]
]

_UNRESOLVED = {ClaimStatus.NOT_STARTED, ClaimStatus.PARTIAL}
_RESOLVED = {ClaimStatus.VERIFIED, ClaimStatus.REFUTED}


async def _default_planner(task: str, _config: EDREConfig) -> list[EvidenceClaim]:
    """Inert fallback plan: a single critical claim echoing the task.

    Used only when no planner is injected *and* no default LLM is configured;
    otherwise the real LLM planner (Slice C) is resolved.
    """
    return [EvidenceClaim(id="c1", hypothesis=task, importance=Importance.CRITICAL)]


def _resolve_planner(planner: "Planner | None") -> "Planner":
    """Injected planner wins; else a real LLM planner; else the inert fallback."""
    if planner is not None:
        return planner
    llm = default_chat_model()
    return make_llm_planner(llm) if llm is not None else _default_planner


async def _default_query_generator(
    state: ResearchState, config: EDREConfig
) -> dict[str, list[str]]:
    """Inert fallback: echo each unresolved claim's hypothesis as its lone query.

    Used only when no generator is injected *and* no default LLM is configured;
    otherwise the real LLM query generator (Slice D) is resolved.
    """
    queries: dict[str, list[str]] = {}
    for claim in state["evidence_plan"]:
        if derive_status(claim, config) in _UNRESOLVED:
            queries[claim.id] = [claim.hypothesis]
    return queries


def _resolve_query_generator(query_generator: "QueryGenerator | None") -> "QueryGenerator":
    """Injected generator wins; else a real LLM generator; else the inert fallback."""
    if query_generator is not None:
        return query_generator
    llm = default_chat_model()
    return make_llm_query_generator(llm) if llm is not None else _default_query_generator


def _resolve_reranker(
    reranker: "RerankGate | None", config: EDREConfig
) -> "RerankGate | None":
    """Injected gate wins; else the real local cross-encoder gate.

    Returns ``None`` (a passthrough, no gating) only when no default cross-encoder
    is available — e.g. neutralized in the hermetic test suite.
    """
    if reranker is not None:
        return reranker
    scorer = default_local_reranker(config)
    return make_rerank_gate(scorer, config) if scorer is not None else None


def _resolve_normalizer(normalizer: "Normalizer | None") -> "Normalizer | None":
    """Injected normalizer wins; else a real LLM normalizer; else passthrough.

    Returns ``None`` (no distillation; documents keep raw grounding) only when no
    default chat model is configured — e.g. neutralized in the hermetic suite.
    """
    if normalizer is not None:
        return normalizer
    llm = default_chat_model()
    return make_llm_normalizer(llm) if llm is not None else None


async def _default_scorer(
    _claims: list[EvidenceClaim], docs: list[RetrievedDocument]
) -> list[dict[str, float]]:
    """Inert fallback scorer: no support signal.

    Used only when no scorer is injected *and* no default LLM is configured;
    otherwise the real LLM signed-support scorer (Slice G) is resolved.
    """
    return [{} for _ in docs]


def _resolve_scorer(scorer: "Scorer | None") -> "Scorer":
    """Injected scorer wins; else a real LLM scorer; else the inert fallback."""
    if scorer is not None:
        return scorer
    llm = default_chat_model()
    return make_llm_scorer(llm) if llm is not None else _default_scorer


def _assemble_output(state: ResearchState, config: EDREConfig) -> ResearchOutput:
    """Pure, LLM-free assembly of the deterministic result snapshot."""
    claims = state["evidence_plan"]
    verdicts: list[ClaimVerdict] = []
    citations = []
    seen: set[str] = set()
    verified = refuted = abandoned = 0

    for claim in claims:
        status = derive_status(claim, config)
        if status is ClaimStatus.VERIFIED:
            verified += 1
        elif status is ClaimStatus.REFUTED:
            refuted += 1
        elif status is ClaimStatus.ABANDONED:
            abandoned += 1
        ranked = sorted(
            claim.supporting_documents,
            key=lambda ref: doc_relevance(ref, config),
            reverse=True,
        )
        verdicts.append(
            ClaimVerdict(
                claim_id=claim.id,
                hypothesis=claim.hypothesis,
                importance=claim.importance.value,
                status=status.value,
                confidence=claim.confidence,
                supporting_documents=ranked,
            )
        )
        for ref in ranked:
            key = ref.citation.url or ref.citation.reference
            if key in seen:
                continue
            seen.add(key)
            citations.append(ref.citation)

    total = len(claims)
    resolved = sum(1 for c in claims if derive_status(c, config) in _RESOLVED)
    blocking_claim_ids = [
        c.id
        for c in claims
        if c.importance is Importance.CRITICAL
        and derive_status(c, config) not in _RESOLVED
    ]
    critical_all_resolved = not blocking_claim_ids
    summary = ResearchSummary(
        terminal=state["terminal"] or "",
        verified=verified,
        refuted=refuted,
        abandoned=abandoned,
        critical_all_resolved=critical_all_resolved,
        coverage=(resolved / total) if total else 0.0,
        loop_count=state["loop_count"],
        blocking_claim_ids=blocking_claim_ids,
    )
    return ResearchOutput(
        research_summary=summary,
        evidence=verdicts,
        citations=citations,
        loop_count=state["loop_count"],
    )


def build_research_graph(
    config: EDREConfig,
    *,
    planner: Planner | None = None,
    query_generator: QueryGenerator | None = None,
    search_fn: SearchFn | None = None,
    reranker: RerankGate | None = None,
    normalizer: Normalizer | None = None,
    scorer: Scorer | None = None,
):
    """Build and compile the flat EDRE research graph for *config*.

    Injected components (all optional) let the highest test seam
    ``ResearchInput -> ResearchOutput`` drive the whole graph deterministically.
    When omitted, inert skeleton defaults are used and ``search`` falls back to
    the real ``search_many`` adapter.
    """
    _plan = _resolve_planner(planner)
    _gen = _resolve_query_generator(query_generator)
    _search = search_fn or search_many
    _rerank_gate = _resolve_reranker(reranker, config)
    _normalize = _resolve_normalizer(normalizer)
    _score = _resolve_scorer(scorer)

    async def plan(state: ResearchState) -> dict:
        claims = await _plan(state["task"], config)
        return {
            "evidence_plan": claims,
            "search_history": [],
            "loop_count": 0,
            "terminal": None,
        }

    async def generate_queries(state: ResearchState) -> dict:
        # loop_count advances at the top of each loop iteration.
        return {
            "queries": await _gen(state, config),
            "loop_count": state["loop_count"] + 1,
        }

    async def search(state: ResearchState) -> dict:
        flat: list[str] = []
        seen: set[str] = set()
        for queries in state["queries"].values():
            for query in queries:
                if query not in seen:
                    seen.add(query)
                    flat.append(query)
        documents = await _search(config.search, flat) if flat else []
        return {"documents": documents}

    async def rerank(state: ResearchState) -> dict:
        # First-layer gate: drop off-topic docs before normalization/scoring.
        # A ``None`` gate means passthrough (no default cross-encoder available).
        documents = state["documents"]
        if _rerank_gate is not None:
            documents = _rerank_gate(state, documents)
        return {"documents": documents}

    async def normalize(state: ResearchState) -> dict:
        # Second-layer grounding: distill each reranker-surviving document into a
        # dense, citation-preserving fragment bound to the document itself, so the
        # claim scorer scores against clean grounding. A ``None`` normalizer means
        # passthrough (no default chat model available).
        docs = state["documents"]
        if _normalize is None or not docs:
            return {}
        fragments = await _normalize(docs)
        for doc, fragment in zip(docs, fragments):
            doc.normalized = fragment
        return {"documents": docs}

    async def score_claims(state: ResearchState) -> dict:
        return {"doc_scores": await _score(state["evidence_plan"], state["documents"])}

    async def update_evidence(state: ResearchState) -> dict:
        claims = state["evidence_plan"]
        docs = state["documents"]
        scores = state["doc_scores"]
        attempted = set(state["queries"].keys())

        for claim in claims:
            if claim.id in attempted:
                claim.search_attempts += 1
            best = claim.confidence
            for doc, per_doc in zip(docs, scores):
                support = per_doc.get(claim.id)
                if support is None:
                    continue
                claim.supporting_documents.append(
                    DocumentRef(
                        citation=doc.citation,
                        support=support,
                        task_match=doc.task_match,
                        query_match=doc.query_match,
                    )
                )
                if abs(support) > abs(best):
                    best = support
            claim.confidence = best

        round_record = SearchRound(
            loop_index=state["loop_count"],
            queries=[q for qs in state["queries"].values() for q in qs],
            documents=docs,
            support_scores=scores,
            rerank_scores=[
                config.rerank_w1 * d.task_match + config.rerank_w2 * d.query_match
                for d in docs
            ],
            provider=config.search.provider,
        )
        return {
            "evidence_plan": claims,
            "search_history": state["search_history"] + [round_record],
        }

    async def control(state: ResearchState) -> dict:
        claims = state["evidence_plan"]
        critical_unresolved = [
            c
            for c in claims
            if c.importance is Importance.CRITICAL
            and derive_status(c, config) not in _RESOLVED
        ]
        if not critical_unresolved:
            terminal: str | None = Terminal.DONE.value
        elif state["loop_count"] >= config.max_loops or not any(
            derive_status(c, config) in _UNRESOLVED for c in claims
        ):
            # Hit the loop cap, or every unresolved claim is now ABANDONED with
            # no attemptable work left: stop honestly as EXHAUSTED.
            terminal = Terminal.EXHAUSTED.value
        else:
            terminal = None
        return {"terminal": terminal}

    def route(state: ResearchState) -> str:
        return "finalize" if state["terminal"] is not None else "generate_queries"

    async def finalize(state: ResearchState) -> dict:
        return {"output": _assemble_output(state, config)}

    builder = StateGraph(
        ResearchState, input_schema=ResearchInput, output_schema=ResearchResult
    )
    builder.add_node("plan", plan)
    builder.add_node("generate_queries", generate_queries)
    builder.add_node("search", search)
    builder.add_node("rerank", rerank)
    builder.add_node("normalize", normalize)
    builder.add_node("score_claims", score_claims)
    builder.add_node("update_evidence", update_evidence)
    builder.add_node("control", control)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "generate_queries")
    builder.add_edge("generate_queries", "search")
    builder.add_edge("search", "rerank")
    builder.add_edge("rerank", "normalize")
    builder.add_edge("normalize", "score_claims")
    builder.add_edge("score_claims", "update_evidence")
    builder.add_edge("update_evidence", "control")
    builder.add_conditional_edges(
        "control",
        route,
        {"generate_queries": "generate_queries", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)
    graph = builder.compile()

    # ``control`` returns EXHAUSTED at loop_count >= max_loops, so the semantic
    # cap fires first; recursion_limit is the raw backstop that guarantees the
    # loop can never spin forever (see ADR-0004).
    nodes_per_loop = 7
    run_config: dict = {"recursion_limit": config.max_loops * nodes_per_loop + 10}
    handler = get_langfuse_callback()
    if handler is not None:
        run_config["callbacks"] = [handler]
    return graph.with_config(run_config)


__all__ = ["build_research_graph"]
