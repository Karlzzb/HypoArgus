"""Real end-to-end smoke test for the EDRE research graph.

Unlike the hermetic unit suite, this script drives ``build_research_graph`` (the
full Evidence-Driven Research Engine) against the **live** services wired in
``.env``:

- SearXNG      (``SEARXNG_BASE_URL``)          — real web search via ``search_many``
- qwen-plus    (``LLM_KEY`` / ``LLM_BASE_URL``)  — real plan / query / normalize / score
- cross-encoder (``EDRE_RERANK_MODEL``)          — real local first-layer gate
- Langfuse     (``LANGFUSE_*``)                — real per-node / per-LLM tracing

It runs one real research task and prints the **deterministic result snapshot**
(terminal state, verdict counts, coverage, loop count), each claim's verdict with
its most relevant supporting documents, and the citation list — then emits the
Langfuse trace URL. It asserts nothing; it is a human-inspected smoke runner.

Run either way:

    /Users/karlgua/miniconda3/envs/search_agent/bin/python search_agent/edre_e2e_smoke.py

    cd /Users/karlgua/repos/sandbox
    /Users/karlgua/miniconda3/envs/search_agent/bin/python -m search_agent.edre_e2e_smoke
"""

from __future__ import annotations

import asyncio

from search_agent.config import SearchConfig
from search_agent.edre import EDREConfig, build_research_graph
from search_agent.edre.models import derive_status
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


async def _run():
    config = EDREConfig(
        search=SearchConfig(provider="searxng", max_results=5),
        max_loops=4,
    )
    graph = build_research_graph(config)
    result = await graph.ainvoke({"task": TASK})
    return config, result["output"]


def _print_output(config: EDREConfig, out) -> None:
    summary = out.research_summary
    print("=" * 72)
    print(f"TASK: {TASK}")
    print("=" * 72)

    print("\n--- RESULT SNAPSHOT (deterministic, no LLM) ---\n")
    print(f"terminal            : {summary.terminal}")
    print(f"critical_all_resolved: {summary.critical_all_resolved}")
    print(
        "verdict counts      : "
        f"VERIFIED={summary.verified} REFUTED={summary.refuted} "
        f"ABANDONED={summary.abandoned}"
    )
    print(f"coverage            : {summary.coverage:.2f}")
    print(f"loop_count          : {summary.loop_count}")
    if summary.blocking_claim_ids:
        print(f"blocking claims     : {', '.join(summary.blocking_claim_ids)}")

    print("\n--- EVIDENCE (per claim) ---\n")
    for verdict in out.evidence:
        print(
            f"[{verdict.importance}] {verdict.status} "
            f"(confidence={verdict.confidence:+.2f})"
        )
        print(f"    {verdict.hypothesis}")
        for ref in verdict.supporting_documents[:3]:
            print(
                f"      {ref.citation.reference} support={ref.support:+.2f} "
                f"{ref.citation.title}"
            )
        if not verdict.supporting_documents:
            print("      (no supporting documents)")

    print("\n--- CITATIONS ---\n")
    for citation in out.citations:
        print(f"{citation.reference} {citation.title}\n     {citation.url}")
    if not out.citations:
        print("(none)")


def main() -> None:
    _require_env()

    langfuse_enabled = bool(
        env_str("LANGFUSE_PUBLIC_KEY") and env_str("LANGFUSE_SECRET_KEY")
    )
    trace_url = None

    if langfuse_enabled:
        from langfuse import get_client

        client = get_client()
        with client.start_as_current_observation(name="edre_e2e", as_type="span"):
            config, out = asyncio.run(_run())
            trace_url = client.get_trace_url()
        client.flush()
    else:
        config, out = asyncio.run(_run())

    _print_output(config, out)

    print("\n" + "=" * 72)
    if trace_url:
        print(f"Langfuse trace: {trace_url}")
    elif langfuse_enabled:
        print(f"Langfuse traces flushed to {env_str('LANGFUSE_BASE_URL')}")
    else:
        print("Langfuse not configured; ran untraced.")


if __name__ == "__main__":
    main()
