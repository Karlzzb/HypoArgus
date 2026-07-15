"""
Web Search Base Provider - Abstract base class for all search providers

This module defines the BaseSearchProvider class that all search providers must inherit from.
Credentials are injected via the constructor; providers never resolve global config.
"""

from abc import ABC, abstractmethod
import logging
from typing import Any

from .contracts import WebSearchResponse

# Legacy name retained for provider metadata only.
SEARCH_API_KEY_ENV = "SEARCH_API_KEY"


class BaseSearchProvider(ABC):
    """Abstract base class for search providers.

    Credentials and connection settings (``api_key`` / ``base_url`` /
    ``max_results`` / ``proxy``) are supplied by the caller, typically from a
    ``SearchConfig``. No global configuration is resolved here.
    Each provider has its own BASE_URL defined as a class constant.
    """

    name: str = "base"
    display_name: str = "Base Provider"
    description: str = ""
    requires_api_key: bool = True
    supports_answer: bool = False  # Whether provider generates LLM answers
    BASE_URL: str = ""  # Each provider defines its own endpoint
    API_KEY_ENV_VARS: tuple[str, ...] = (SEARCH_API_KEY_ENV,)

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        """
        Initialize the provider.

        Args:
            api_key: API key for the provider, injected by the caller. Providers
                that set ``requires_api_key = True`` must be given a non-empty key.
            **kwargs: Additional configuration options (e.g. ``proxy``).
        """
        self.logger = logging.getLogger(__name__)
        self.api_key = api_key or self._get_api_key()
        self.config = kwargs
        self.proxy = kwargs.get("proxy")

    def _get_api_key(self) -> str:
        """Validate the injected API key.

        No global configuration is resolved: the key must be supplied via the
        constructor. Providers that do not require a key (``requires_api_key =
        False``) simply get an empty string.
        """
        if self.requires_api_key:
            raise ValueError(f"{self.name} requires an api_key to be supplied at construction.")
        return ""

    @abstractmethod
    def search(self, query: str, **kwargs: Any) -> WebSearchResponse:
        """
        Execute search and return standardized response.

        Args:
            query: The search query.
            **kwargs: Provider-specific options.

        Returns:
            WebSearchResponse: Standardized search response.
        """
        pass

    def is_available(self) -> bool:
        """
        Check if provider is available (dependencies installed, API key set).

        Returns:
            bool: True if provider is available, False otherwise.
        """
        if self.requires_api_key and not self.api_key:
            return False
        return True


__all__ = ["BaseSearchProvider", "SEARCH_API_KEY_ENV"]
