"""Runtime configuration for the search subgraph.

``SearchConfig`` is the pure, injectable replacement for the monolith's
``resolve_search_runtime_config()``. It carries everything the subgraph needs to
select and drive a provider plus the consolidation strategy, with no hidden
global state, so it can be constructed and unit-tested in isolation.
"""

from dataclasses import dataclass, field

from .env import env_str


@dataclass
class SearchConfig:
    """Configuration passed to the search subgraph at construction time.

    Attributes:
        provider: Provider name. ``"searxng"`` is the primary provider; when it
            is selected without a ``base_url`` the subgraph falls back to
            ``"duckduckgo"`` (handled in a later slice).
        base_url: SearXNG instance URL. Required for searxng; other providers
            ignore it. Defaults to the ``SEARXNG_BASE_URL`` environment variable
            (loaded from ``.env``); an explicit value always wins.
        api_key: API key for providers that require one. SearXNG/DuckDuckGo do
            not need a key.
        proxy: Optional proxy URL forwarded to the provider.
        max_results: Desired number of results. The provider enforces its own
            hard cap (10).
        consolidation_use_llm: When True, consolidate via LLM instead of the
            Jinja2 template (LLM path wired in a later slice).
        consolidation_custom_template: Optional Jinja2 template overriding the
            provider default.
        consolidation_llm_model: Optional model name that overrides only the
            consolidate step's model; decompose keeps the default model.
    """

    provider: str = "searxng"
    base_url: str | None = field(default_factory=lambda: env_str("SEARXNG_BASE_URL"))
    api_key: str | None = None
    proxy: str | None = None
    max_results: int = 5
    # consolidation
    consolidation_use_llm: bool = False
    consolidation_custom_template: str | None = None
    consolidation_llm_model: str | None = None


__all__ = ["SearchConfig"]
