"""Deprecated compatibility runner. Use run_search_agent.py."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from search_agent import EvidenceRetrievalConfig, EvidenceRetrievalDependencies, get_langfuse_callback
from search_agent.evidence_retrieval.providers.openai_compatible_chat import OpenAICompatibleChatClient
from search_agent.evidence_retrieval.public_contracts import SearchAgentInputState, SearchAgentOutputState
from search_agent.evidence_retrieval.v11_graph import build_search_agent_v11_graph
from search_agent.tracing import get_langfuse_export_status


ROOT = Path(__file__).resolve().parent


async def run(input_path: Path, output_path: Path, *, with_llm: bool) -> dict:
    request = SearchAgentInputState.model_validate_json(input_path.read_text(encoding="utf-8"))
    config = EvidenceRetrievalConfig.from_env()
    callbacks = []
    callback = get_langfuse_callback()
    if callback is not None:
        callbacks.append(callback)
    if with_llm:
        llm = OpenAICompatibleChatClient.from_env(
            model=config.judge_model,
            timeout_seconds=config.parallel_batch_judge_timeout_ms / 1000,
        )
        if llm is None:
            raise RuntimeError("LLM_KEY/LLM_BASE_URL/LLM_MODEL is not configured")
        dependencies = EvidenceRetrievalDependencies.with_llm(config, llm)
    else:
        dependencies = EvidenceRetrievalDependencies.defaults(config)
    graph = build_search_agent_v11_graph(config, dependencies, callbacks=callbacks or None)
    started = time.perf_counter()
    try:
        state = await graph.ainvoke({"input": request.model_dump(mode="json")})
    finally:
        await dependencies.aclose()
    public = SearchAgentOutputState.model_validate(state["public_output"])
    payload = {
        "public_output": public.model_dump(mode="json"),
        "diagnostic_output": state["diagnostic_output"],
        "run_evidence": {
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "langfuse_export": get_langfuse_export_status(),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output_path.resolve()),
        "request_id": public.request_id,
        "task_count": len(public.results),
        "citation_count": len(public.citations),
        "status": public.run_status.status,
        "trace_id": public.trace.trace_id,
        "elapsed_ms": payload["run_evidence"]["elapsed_ms"],
    }, ensure_ascii=False, indent=2))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "manual_inputs" / "v11_embodied_intelligence_4task.json"))
    parser.add_argument("--output", default=str(ROOT / "manual_outputs" / "v11_real_e2e_output.json"))
    parser.add_argument("--with-llm", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(Path(args.input).resolve(), Path(args.output).resolve(), with_llm=args.with_llm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
