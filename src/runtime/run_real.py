"""真实装配入口：把真实 LLM adapter + 交互式 CLI 闸门 + Mock 检索装进 Orchestrator 跑通。

把 :func:`agents.assembly.create_real_agents` 的全部缺口填上——解析 / 体检 / 开药 三条
LLM seam 接真实 provider（DashScope 等 OpenAI-compatible）、检索用 Mock（待真实后端）、
HITL-1 / HITL-2 用交互式 CLI 闸门。装配后 manifest 的 ``real`` 工厂自动替换桩。

用法：

.. code-block:: bash

    export DASHSCOPE_API_KEY=...        # 绝不硬编码
    python -m runtime.run_real input.txt -o final.md
    cat input.txt | python -m runtime.run_real > final.md

非交互环境（无 tty）两闸门退化为保守决策，详见 :mod:`runtime.cli_gates`。
"""

from __future__ import annotations

import argparse
import sys

from langchain_core.language_models import BaseChatModel

from agents.assembly import create_real_agents
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenParseLlmClient,
    QwenVerifyLlmClient,
)
from infra.llm_provider import build_qwen_chat_model
from infra.retrieval import RetrievalLayer, create_mock_retrieval_layer
from runtime.cli_gates import CliHitl1Gate, CliHitl2Gate
from runtime.orchestrator import Orchestrator, RunResult

__all__ = ["run_real_pipeline", "main"]


def run_real_pipeline(
    raw_text: bytes,
    *,
    chat_model: BaseChatModel | None = None,
    retrieval: RetrievalLayer | None = None,
    hitl1_gate: object | None = None,
    hitl2_gate: object | None = None,
    max_iterations: int = 8,
) -> RunResult:
    """驱动「真实 LLM + 交互闸门 + Mock 检索」整条流水线。

    默认：``chat_model`` 经 :func:`build_qwen_chat_model` 读 ``DASHSCOPE_API_KEY``；
    ``retrieval`` 用 Mock；两闸门用 CLI 交互式。测试 / 程序化调用可注入 fake 替身。
    """

    chat = chat_model or build_qwen_chat_model()
    retrieval_layer = retrieval or create_mock_retrieval_layer()
    agents = create_real_agents(
        llm=QwenParseLlmClient(chat),
        hitl1_gate=hitl1_gate or CliHitl1Gate(),  # type: ignore[arg-type]
        verify_llm=QwenVerifyLlmClient(chat),
        hypothesis_llm=QwenHypothesisLlmClient(chat),
        retrieval=retrieval_layer,
        hitl2_gate=hitl2_gate or CliHitl2Gate(),  # type: ignore[arg-type]
        max_iterations=max_iterations,
    )
    return Orchestrator(agents=agents).run_with_report(raw_text)


def _read_input(path: str | None) -> bytes:
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as fh:
        return fh.read()


def _write_output(path: str | None, data: bytes) -> None:
    if path is None or path == "-":
        sys.stdout.buffer.write(data)
        return
    with open(path, "wb") as fh:
        fh.write(data)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m runtime.run_real [input] [-o output]``。"""

    parser = argparse.ArgumentParser(
        prog="runtime.run_real",
        description="HypoArgus 真实 LLM 端到端流水线（DashScope / qwen-max）。",
    )
    parser.add_argument(
        "input", nargs="?", default="-", help="输入文档路径，默认 stdin"
    )
    parser.add_argument(
        "-o", "--output", default="-", help="输出终稿路径，默认 stdout"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="DashScope 模型名（默认 qwen-max；可被 .env 的 DASHSCOPE_MODEL 覆盖）",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=8,
        help="ReAct 迭代硬上限（体检 / 开药，默认 8）",
    )
    args = parser.parse_args(argv)

    raw = _read_input(args.input)
    report = run_real_pipeline(
        raw,
        chat_model=build_qwen_chat_model(args.model),
        max_iterations=args.max_iterations,
    )
    _write_output(args.output, report.final_doc)
    for err in report.errors:
        print(err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
