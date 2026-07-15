"""Self-contained SearXNG search retrieval package.

This package was extracted from the deeptutor monolith and has no dependency on
it. It exposes the stable data contracts (``Citation`` / ``SearchResult`` /
``WebSearchResponse``), the provider registry, the injectable ``SearchConfig``,
the ``AnswerConsolidator``, the default LangChain chat model, the Langfuse
callback factory, and the LangGraph subgraph factory ``build_search_subgraph``.

A package-local ``.env`` (``LLM_*`` / ``SEARXNG_BASE_URL`` / ``LANGFUSE_*``) is
auto-loaded on import; process environment variables always take precedence.
"""

if not __package__:  # pragma: no cover - pytest collection through a hyphenated directory
    # The editable install exposes the same file under its proper package name.
    # Re-export it when pytest imports this file as a top-level ``__init__``.
    import search_agent as _installed
    __all__ = _installed.__all__
    def __getattr__(name):
        return getattr(_installed, name)
else:
    from .env import load_env

    load_env()

    from .config import SearchConfig  # noqa: E402
    from .providers import (  # noqa: E402
        get_available_providers,
        get_provider,
        get_providers_info,
        list_providers,
        register_provider,
    )
    from .tracing import get_langfuse_callback  # noqa: E402
    from .contracts import Citation, SearchResult, WebSearchResponse  # noqa: E402

    # New public retrieval layer. Kept separate from legacy retrieval.py/EDRE.
    from .evidence_retrieval import (  # noqa: E402
        EvidenceRetrievalConfig,
        EvidenceRetrievalDependencies,
        build_evidence_retrieval_graph,
        build_search_agent_graph,
        AtomicClaim, AtomicClaimGroup, ClaimLogicOperator, atomize_claim,
        normalize_reverse_hypothesis, apply_claim_logic, NumericRelationVerifier,
    )
    from .evidence_retrieval.public_contracts import (  # noqa: E402
        SearchAgentInputState,
        SearchAgentOutputState,
    )
    from .api import (  # noqa: E402
        SearchAgentRuntime,
        ainvoke_search_agent,
        SearchAgentContractError,
        SearchAgentClosedError,
        SearchAgentConfigurationError,
    )

    __all__ = [
        "SearchConfig",
        "AnswerConsolidator",
        "default_chat_model",
        "normalize_base_url",
        "get_langfuse_callback",
        "Citation",
        "SearchResult",
        "WebSearchResponse",
        "EvidenceRetrievalConfig",
        "EvidenceRetrievalDependencies",
        "build_evidence_retrieval_graph",
        "build_search_agent_graph",
        "AtomicClaim", "AtomicClaimGroup", "ClaimLogicOperator", "atomize_claim",
        "normalize_reverse_hypothesis", "apply_claim_logic", "NumericRelationVerifier",
        "SearchAgentRuntime",
        "ainvoke_search_agent",
        "SearchAgentInputState",
        "SearchAgentOutputState",
        "SearchAgentContractError",
        "SearchAgentClosedError",
        "SearchAgentConfigurationError",
        "register_provider",
        "get_provider",
        "list_providers",
        "get_available_providers",
        "get_providers_info",
        "build_search_subgraph",
    ]

    def __getattr__(name):
        # Avoid importing LangChain chat models (and their optional heavyweight
        # tokenizer/model stack) when callers only need evidence_retrieval.
        if name == "AnswerConsolidator":
            from .consolidation import AnswerConsolidator
            return AnswerConsolidator
        if name in {"default_chat_model", "normalize_base_url"}:
            from .llm import default_chat_model, normalize_base_url
            return {"default_chat_model": default_chat_model, "normalize_base_url": normalize_base_url}[name]
        if name == "build_search_subgraph":
            from .subgraph import build_search_subgraph
            return build_search_subgraph
        raise AttributeError(name)
