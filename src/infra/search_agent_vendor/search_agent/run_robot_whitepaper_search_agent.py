"""Run the SearchAgent public evidence-retrieval subgraph with a simulated upstream input.

Place this file in the search_agent-main project root.

Examples:
    python run_robot_whitepaper_search_agent.py --mode validate
    python run_robot_whitepaper_search_agent.py --mode offline
    python run_robot_whitepaper_search_agent.py --mode real --with-llm
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

PROCESS_BOOTSTRAP_STARTED = time.perf_counter()

from search_agent import (
    EvidenceRetrievalConfig,
    EvidenceRetrievalDependencies,
    build_evidence_retrieval_graph,
    get_langfuse_callback,
)
from search_agent.evidence_retrieval.providers.openai_compatible_chat import (
    OpenAICompatibleChatClient,
)
from search_agent.evidence_retrieval.providers.bisheng_retrieve import BishengRetrieveResult
from search_agent.evidence_retrieval.schemas import (
    EvidenceCandidate,
    EvidenceRelation,
    JudgeResult,
    QueryItem,
    SearchAgentBatchInput,
    SourceRef,
    SourceType,
    build_retrieval_tasks,
)
from search_agent.tracing import get_langfuse_export_status

PROCESS_IMPORTS_FINISHED = time.perf_counter()


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = PROJECT_ROOT / "manual_inputs" / "robot_whitepaper_upstream_mock_small.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "manual_outputs"


def load_request(path: Path) -> SearchAgentBatchInput:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SearchAgentBatchInput.model_validate(raw)


def show_task_plan(request: SearchAgentBatchInput) -> None:
    tasks = build_retrieval_tasks(request)
    print("=" * 88)
    print("UPSTREAM INPUT VALIDATED（上游输入校验通过）")
    print("=" * 88)
    print(f"request_id     : {request.request_id}")
    print(f"document_id    : {request.document_id}")
    print(f"paragraph_count: {len(request.paragraphs)}")
    print(f"task_count     : {len(tasks)}")
    print()
    for index, task in enumerate(tasks, 1):
        print(
            f"[{index:02d}] "
            f"task_id={task.task_id} | "
            f"paragraph_id={task.paragraph_id} | "
            f"line_type={task.line_type.value} | "
            f"item_id={task.item_id} | "
            f"node_id={task.node_id} | "
            f"hypothesis_id={task.hypothesis_id or '-'}"
        )
        print(f"     target_text: {task.target_text}")
        print(f"     slots      : {task.required_slots}")
    print()


# ---------------------------------------------------------------------------
# Offline controlled providers（离线受控工具）
# They verify Graph/Node/State/Loop/result mapping only.
# They do not prove the whitepaper statements are factually true.
# ---------------------------------------------------------------------------

class OfflineWebSearch:
    async def search(self, task_id: str, query: QueryItem) -> list[EvidenceCandidate]:
        await asyncio.sleep(0.01)
        return [
            EvidenceCandidate(
                candidate_id=f"{task_id}:{query.query_id}:web:{i}",
                task_id=task_id,
                source_type=SourceType.WEB,
                source_name="offline_fake_web",
                source_ref=SourceRef(
                    url=f"https://offline.example.org/{task_id.replace(':', '-')}/{i}",
                    query_id=query.query_id,
                ),
                title="Offline controlled evidence（离线受控证据）",
                content=f"Search query（检索词）: {query.query}. Candidate snippet（候选摘要）.",
                snippet_only=True,
            )
            for i in range(2)
        ]

    async def close(self) -> None:
        return None


class OfflineWebFetcher:
    async def fetch(self, candidate: EvidenceCandidate):
        content = (
            "This is an offline controlled full-text fixture（离线受控正文夹具）. "
            f"It is attached only to task {candidate.task_id}. "
            "It contains the target subject, time, metric and direct supporting wording."
        )
        return [
            candidate.model_copy(
                update={
                    "candidate_id": f"{candidate.candidate_id}:full",
                    "content": content,
                    "snippet_only": False,
                    "metadata": {"published_at": "2026-01-01"},
                }
            )
        ]

    async def close(self) -> None:
        return None


class OfflineKB:
    async def retrieve(self, **kwargs: Any) -> BishengRetrieveResult:
        return BishengRetrieveResult()

    async def close(self) -> None:
        return None


class OfflineStructured:
    async def healthy(self) -> bool:
        return True

    async def scenarios(self) -> dict[str, Any]:
        return {}

    async def scenario(self, name: str):
        return None

    async def query(self, scenario: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def close(self) -> None:
        return None


class OfflineJudge:
    async def judge(self, task, candidate, context) -> JudgeResult:
        return JudgeResult(
            relation=EvidenceRelation.SUPPORT,
            confidence=0.96,
            directness=0.95,
            reason="Offline controlled evidence directly supports the task.",
            quoted_spans=[candidate.content],
            covered_slots=task.required_slots,
            missing_slots=[],
        )


def offline_dependencies() -> EvidenceRetrievalDependencies:
    return EvidenceRetrievalDependencies(
        web_search=OfflineWebSearch(),
        web_fetcher=OfflineWebFetcher(),
        kb_client=OfflineKB(),
        structured_client=OfflineStructured(),
        judge=OfflineJudge(),
    )


def print_safe_config(config: EvidenceRetrievalConfig) -> None:
    print("=" * 88)
    print("REAL SERVICE CONFIG STATUS（真实服务配置状态）")
    print("=" * 88)
    print(f"Volcano Web（火山网络检索） : {bool(config.volcano_api_key)}")
    print(f"Bisheng KB（毕昇知识库）    : {bool(config.bisheng_base_url)}")
    print(f"Public KB IDs（公共知识库） : {config.public_knowledge_ids}")
    print(f"Structured（结构化服务）    : {bool(config.structured_base_url)}")
    print()


def summarize_output(output: dict[str, Any]) -> None:
    results = [
        item
        for paragraph in output.get("paragraph_results", [])
        for item in paragraph.get("results", [])
    ]
    print("=" * 88)
    print("SEARCHAGENT OUTPUT SUMMARY（SearchAgent 输出摘要）")
    print("=" * 88)
    print(json.dumps(output.get("execution_summary", {}), ensure_ascii=False, indent=2))
    print()
    for result in results:
        verification = result["verification"]
        print(
            f"{result['task_id']} | {result['line_type']} | {result['item_id']} | "
            f"{verification['verdict']}（{verification['upstream_status']}） | "
            f"{result['termination_reason']} | evidence={len(result.get('evidence_items', []))} | "
            f"errors={len(result.get('errors', []))}"
        )
        print(f"  target : {result['target_text']}")
        print(f"  reason : {verification['reason']}")
        if result.get("evidence_gap"):
            print(f"  gap    : {result['evidence_gap']}")
    print()


async def run_graph(
    request: SearchAgentBatchInput,
    *,
    mode: str,
    with_llm: bool,
) -> dict[str, Any]:
    callbacks = []
    # Each stage is timed exclusively: only one stage is "in flight" at a
    # time. The sum of stage timings should be <= cli.total_ms (within
    # measurement noise). Stages never overlap.
    stage_timings: dict[str, float] = {}
    wall_started = time.perf_counter()
    prev_mark = wall_started

    def mark(stage: str) -> None:
        nonlocal prev_mark
        now = time.perf_counter()
        stage_timings[stage] = round((now - prev_mark) * 1000, 3)
        prev_mark = now

    # No "imports" stage here: imports already happened before run_graph was
    # entered. The cli.imports_ms timing is captured by main() at the very
    # start of the process, see async_main.
    if mode == "offline":
        config = EvidenceRetrievalConfig(
            min_effective_evidence_count=1,
            min_direct_evidence_count=1,
            min_independent_document_count=1,
            min_independent_source_count=1,
            min_claim_coverage_score=0.10,
            min_final_evidence_score=0.10,
            max_noise_ratio=1.0,
            conflict_weight_threshold=10,
            task_hard_timeout_ms=60_000,
        )
        mark("cli.config_load_ms")
        dependencies = offline_dependencies()
        mark("cli.clients_init_ms")
    else:
        callback = get_langfuse_callback()
        mark("cli.langfuse_callback_init_ms")
        if callback is not None:
            callbacks.append(callback)
        config = EvidenceRetrievalConfig.from_env()
        print_safe_config(config)
        mark("cli.config_load_ms")
        if with_llm:
            llm = OpenAICompatibleChatClient.from_env(
                model=config.judge_model,
                timeout_seconds=config.parallel_batch_judge_timeout_ms / 1000,
            )
            if llm is None:
                raise RuntimeError(
                    "LLM is not configured. Check LLM_KEY, LLM_BASE_URL and LLM_MODEL in .env."
                )
            dependencies = EvidenceRetrievalDependencies.with_llm(config, llm)
        else:
            dependencies = EvidenceRetrievalDependencies.defaults(config)
        mark("cli.clients_init_ms")

    graph = build_evidence_retrieval_graph(
        config=config,
        dependencies=dependencies,
        callbacks=callbacks or None,
    )
    mark("cli.graph_build_ms")

    try:
        state = await graph.ainvoke({"request": request.model_dump(mode="json")})
    finally:
        mark("cli.graph_execute_ms")
        try:
            await dependencies.aclose()
        except Exception:
            pass
        mark("cli.clients_close_ms")

    output = state["output"]
    mark("cli.output_serialize_ms")
    output["_manual_run"] = {
        "mode": mode,
        "with_llm": with_llm,
        "elapsed_ms": int((time.perf_counter() - wall_started) * 1000),
        "langfuse_export": get_langfuse_export_status(),
        "stage_timings_ms": stage_timings,
        "cli_total_ms": round((time.perf_counter() - wall_started) * 1000, 3),
    }
    return output


async def async_main(
    args: argparse.Namespace,
    *,
    bootstrap_started: float | None = None,
    imports_finished: float | None = None,
    after_args: float | None = None,
) -> int:
    async_main_started = time.perf_counter()
    input_started = time.perf_counter()
    input_path = Path(args.input).resolve()
    request = load_request(input_path)
    show_task_plan(request)
    input_load_and_plan_ms = round((time.perf_counter() - input_started) * 1000, 3)

    if args.mode == "validate":
        print("Validation only（仅校验模式）: no Graph or external Tool was called.")
        return 0

    output = await run_graph(
        request, mode=args.mode, with_llm=args.with_llm,
    )
    output.setdefault("_manual_run", {}).setdefault("stage_timings_ms", {})[
        "cli.input_load_and_plan_ms"
    ] = input_load_and_plan_ms
    summarize_output(output)

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve() if args.output else (
        DEFAULT_OUTPUT_DIR / f"{input_path.stem}_output.json"
    )
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Output written to（结果已写入）: {output_path}")

    stage_timings = output.get("_manual_run", {}).get("stage_timings_ms", {})
    # Include true import/bootstrap timing from process start, so the
    # stage_timings sum is explainable and not missing 1-2 seconds of
    # Python startup.
    if bootstrap_started is not None and imports_finished is not None and after_args is not None:
        stage_timings = {
            "cli.imports_ms": round((imports_finished - bootstrap_started) * 1000, 3),
            "cli.args_parse_ms": round((after_args - imports_finished) * 1000, 3),
            **stage_timings,
        }
        output["_manual_run"]["stage_timings_ms"] = stage_timings
    if stage_timings:
        timing_path = output_path.with_name(output_path.stem + "_cli_timings.json")
        timing_payload = {
            "cli_total_ms": output.get("_manual_run", {}).get("cli_total_ms"),
            "async_main_wall_ms": round((time.perf_counter() - async_main_started) * 1000, 3),
            "process_wall_ms": (
                round((time.perf_counter() - bootstrap_started) * 1000, 3)
                if bootstrap_started is not None else None
            ),
            "stage_timings_ms": stage_timings,
            "stage_sum_ms": round(sum(stage_timings.values()), 3),
            "langfuse_export_status": output.get("_manual_run", {}).get("langfuse_export"),
            "flow_metrics": output.get("flow_metrics", {}),
        }
        timing_path.write_text(
            json.dumps(timing_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("=" * 88)
        print("CLI STAGE TIMINGS（CLI 分段耗时, 单位 ms, exclusive）")
        print("=" * 88)
        for stage in sorted(stage_timings):
            print(f"{stage:<40} {stage_timings[stage]:>12.3f}")
        print(f"{'stage_sum_ms':<40} {timing_payload['stage_sum_ms']:>12.3f}")
        print(f"{'cli.total_ms':<40} {timing_payload['cli_total_ms']:>12.3f}")
        print(f"{'async_main.wall_ms':<40} {timing_payload['async_main_wall_ms']:>12.3f}")
        print(f"Timings written to: {timing_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SearchAgent with a robot-whitepaper upstream mock input."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Path to upstream mock JSON.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output JSON path.",
    )
    parser.add_argument(
        "--mode",
        choices=["validate", "offline", "real"],
        default="validate",
        help=(
            "validate: Schema/task check only; "
            "offline: full Graph with controlled fake Tools; "
            "real: actual configured Web/KB/Structured services."
        ),
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="In real mode, use the configured real LLM for Batch Evidence Judge.",
    )
    args = parser.parse_args()
    return args


def main() -> int:
    # Capture process bootstrap before any heavy imports run. Reading this
    # module's start time is cheap; main() is invoked at the very end of the
    # import phase, so this measures Python startup + module imports.
    cli_bootstrap_started = PROCESS_BOOTSTRAP_STARTED
    args = parse_args()
    cli_after_args = time.perf_counter()
    try:
        rc = asyncio.run(async_main(
            args,
            bootstrap_started=cli_bootstrap_started,
            imports_finished=PROCESS_IMPORTS_FINISHED,
            after_args=cli_after_args,
        ))
        return rc
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
