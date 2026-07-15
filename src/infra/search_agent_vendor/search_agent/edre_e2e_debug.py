from __future__ import annotations

import asyncio
import sys

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


def _safe(x: object) -> str:
    return str(x).replace("\u2022", "-").replace("\n", " ")


async def main_async() -> None:
    config = EDREConfig(
        search=SearchConfig(provider="searxng", max_results=5),
        max_loops=4,
    )

    graph = build_research_graph(config)

    print("=" * 100)
    print(f"TASK: {TASK}")
    print("=" * 100)

    async for update in graph.astream({"task": TASK}, stream_mode="updates"):
        for node_name, payload in update.items():
            print("\n" + "#" * 100)
            print(f"NODE: {node_name}")
            print("#" * 100)

            if node_name == "plan":
                claims = payload.get("evidence_plan", [])
                print("\n[Evidence Claims]")
                for claim in claims:
                    print(
                        f"- {claim.id} | {claim.importance.value} | "
                        f"{_safe(claim.hypothesis)}"
                    )

            elif node_name == "generate_queries":
                queries = payload.get("queries", {})
                print("\n[Generated Queries]")
                for claim_id, qs in queries.items():
                    print(f"\n{claim_id}:")
                    for q in qs:
                        print(f"  - {_safe(q)}")

            elif node_name == "search":
                docs = payload.get("documents", [])
                print(f"\n[Search Results] count={len(docs)}")
                for i, doc in enumerate(docs[:10], 1):
                    print(f"{i}. {doc.citation.reference} {_safe(doc.result.title)}")
                    print(f"   url={doc.result.url}")
                    print(f"   source_queries={doc.source_queries}")

            elif node_name == "rerank":
                docs = payload.get("documents", [])
                print(f"\n[Rerank Survivors] count={len(docs)}")
                for i, doc in enumerate(docs[:10], 1):
                    combined = (
                        config.rerank_w1 * doc.task_match
                        + config.rerank_w2 * doc.query_match
                    )
                    print(
                        f"{i}. combined={combined:.3f} "
                        f"task_match={doc.task_match:.3f} "
                        f"query_match={doc.query_match:.3f}"
                    )
                    print(f"   title={_safe(doc.result.title)}")
                    print(f"   url={doc.result.url}")

            elif node_name == "score_claims":
                scores = payload.get("doc_scores", [])
                print(f"\n[Support Scores] count={len(scores)}")
                for score in scores[:10]:
                    print(score)

            elif node_name == "update_evidence":
                claims = payload.get("evidence_plan", [])
                print("\n[Claim Status]")
                for claim in claims:
                    status = derive_status(claim, config)
                    print(
                        f"- {claim.id} | {claim.importance.value} | "
                        f"{status.value} | confidence={claim.confidence:+.2f} | "
                        f"attempts={claim.search_attempts} | "
                        f"{_safe(claim.hypothesis)}"
                    )

            elif node_name == "control":
                print("[Control Output]")
                print(payload)

            elif node_name == "finalize":
                out = payload.get("output")
                print("\n[Final Output]")
                summary = out.research_summary
                print(f"terminal={summary.terminal}")
                print(f"critical_all_resolved={summary.critical_all_resolved}")
                print(
                    f"VERIFIED={summary.verified} "
                    f"REFUTED={summary.refuted} "
                    f"ABANDONED={summary.abandoned}"
                )
                print(f"coverage={summary.coverage:.2f}")
                print(f"loop_count={summary.loop_count}")
                print(f"blocking_claims={summary.blocking_claim_ids}")


def main() -> None:
    _require_env()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()