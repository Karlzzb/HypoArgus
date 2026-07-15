"""Environment / ``.env`` loading for the search package.

The package auto-loads a package-local ``.env`` on import (see ``__init__``) so
the configuration style below works out of the box:

    LLM_KEY / LLM_BASE_URL / LLM_MODEL   -> the LangChain ChatOpenAI model
    SEARXNG_BASE_URL                     -> SearchConfig.base_url default
    LANGFUSE_PUBLIC_KEY / _SECRET_KEY / _BASE_URL -> Langfuse tracing

This deliberately relaxes the original "no implicit global state" invariant
(see DEVELOPMENT.md §7): loading is opt-out only by removing ``.env``, never
overrides variables already present in the process environment, and reads the
``.env`` that sits next to this package so imports resolve it regardless of the
caller's working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).with_name(".env")


def load_env() -> None:
    """Load the package-local ``.env`` if present (never overriding real env)."""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)


def env_str(name: str) -> str | None:
    """Return a stripped, non-empty environment value, or ``None``."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


__all__ = ["load_env", "env_str"]
