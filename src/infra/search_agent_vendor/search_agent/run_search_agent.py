"""Single production CLI for the SearchAgent public contract.

Uses SearchAgentRuntime — no duplicated config/dependency/graph assembly.

Usage:
    python -u run_search_agent.py --input manual_inputs/v12_embodied_intelligence_4task.json --with-llm
    python -u run_search_agent.py --input ... --with-llm --output manual_outputs/output.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from search_agent import SearchAgentRuntime
from search_agent.evidence_retrieval.cli_io import emit_public_json


ROOT = Path(__file__).resolve().parent


async def run(input_path: Path, *, output_path: Path | None, with_llm: bool) -> dict:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    runtime = SearchAgentRuntime.from_env(with_llm=with_llm)
    try:
        result = await runtime.ainvoke(raw)
    finally:
        await runtime.aclose()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Output written to: {output_path.resolve()}", file=sys.stderr)

    emit_public_json(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the production SearchAgent graph")
    parser.add_argument(
        "--input",
        default=str(ROOT / "manual_inputs" / "v12_embodied_intelligence_4task.json"),
        help="search-agent-input/v1 JSON file",
    )
    parser.add_argument("--output", help="optional local file containing only search-agent-output/v1")
    parser.add_argument("--with-llm", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(run(
            Path(args.input).resolve(),
            output_path=Path(args.output).resolve() if args.output else None,
            with_llm=args.with_llm,
        ))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
