"""Real end-to-end smoke test for the search subgraph.

Unlike the hermetic unit suite, this script hits the **live** services wired in
``.env``:

- SearXNG      (``SEARXNG_BASE_URL``)         — real web search
- qwen-plus    (``LLM_KEY`` / ``LLM_BASE_URL``) — real decompose + consolidation
- Langfuse     (``LANGFUSE_*``)               — real tracing

It builds the subgraph with the package's normal factory (which auto-loads
``.env``, resolves the default ``ChatOpenAI``, and bakes in the Langfuse
callback), runs one real task, prints the consolidated answer and citations, and
emits the Langfuse trace URL.

This script uses absolute imports and relies on the installed ``search_agent``
package, so it runs both as a plain script (e.g. the PyCharm default Run action)
and as a module:

    # direct script
    /Users/karlgua/miniconda3/envs/search_agent/bin/python search_agent/e2e_smoke.py

    # module
    cd /Users/karlgua/repos/sandbox
    /Users/karlgua/miniconda3/envs/search_agent/bin/python -m search_agent.e2e_smoke
"""

from __future__ import annotations

import asyncio

from search_agent import SearchConfig, build_search_subgraph
from search_agent.env import env_str

TASK = "研究当前中国高职院校的汽修专业的发展趋势和后续发展动态"


def _require_env() -> None:
    missing = [
        name
        for name in ("SEARXNG_BASE_URL", "LLM_KEY", "LLM_BASE_URL")
        if not env_str(name)
    ]
    if missing:
        raise SystemExit(f"Missing required .env values: {', '.join(missing)}")


async def _run() -> dict:
    config = SearchConfig(provider="searxng", consolidation_use_llm=True, max_results=5)
    graph = build_search_subgraph(config)
    return await graph.ainvoke({"task": TASK})


def main() -> None:
    _require_env()

    langfuse_enabled = bool(env_str("LANGFUSE_PUBLIC_KEY") and env_str("LANGFUSE_SECRET_KEY"))
    trace_url = None

    if langfuse_enabled:
        from langfuse import get_client

        client = get_client()
        with client.start_as_current_observation(name="search_agent_e2e", as_type="span"):
            out = asyncio.run(_run())
            trace_url = client.get_trace_url()
        client.flush()
    else:
        out = asyncio.run(_run())

    print("=" * 72)
    print(f"TASK: {TASK}")
    print("=" * 72)
    print("\n--- CONSOLIDATED ANSWER ---\n")
    print(out["consolidated"])
    print("\n--- CITATIONS ---\n")
    for citation in out["citations"]:
        print(f"{citation.reference} {citation.title}\n     {citation.url}")
    if not out["citations"]:
        print("(none)")

    print("\n" + "=" * 72)
    if trace_url:
        print(f"Langfuse trace: {trace_url}")
    elif langfuse_enabled:
        print(f"Langfuse traces flushed to {env_str('LANGFUSE_BASE_URL')}")
    else:
        print("Langfuse not configured; ran untraced.")


if __name__ == "__main__":
    main()
