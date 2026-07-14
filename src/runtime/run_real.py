"""真实装配入口：把真实 LLM adapter + 交互式 CLI 闸门装进 Orchestrator 跑通。

把 :func:`agents.assembly.create_real_agents` 的缺口填上——解析 / 开药 / 裁决 / 重写 四条 LLM seam
接真实 provider（DashScope 等 OpenAI-compatible），HITL-1 / HITL-2 用交互式 CLI 闸门。装配后
manifest 的 ``real`` 工厂自动替换桩。

Slice 6 后 retrieval 节点仍为桩（产空 citations、真实后端 Out of Scope）；judgment 节点
吃空 citations 经 FakeJudgmentLlmClient 默认空裁决 → 全 KEEP → rewrite_loop 无触达段 →
终稿逐字节等于原文。真实后端检索接入后 citations 非空、judgment 据之判终态、rewrite_loop
据 supported 假说 / 命中 citations 逐段提议重写（拓扑不动）。

用法：

.. code-block:: bash

    export DASHSCOPE_API_KEY=...        # 绝不硬编码
    python -m runtime.run_real input.txt -o final.md
    cat input.txt | python -m runtime.run_real > final.md

非交互环境（无 tty）两闸门退化为保守决策，详见 :mod:`runtime.cli_gates`。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from langchain_core.language_models import BaseChatModel

from agents.assembly import create_real_agents
from domain import SessionContext
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from infra.llm_provider import build_qwen_chat_model
from runtime.cli_gates import CliHitl1Gate, CliHitl2Gate
from runtime.orchestrator import Orchestrator, RunResult

__all__ = ["run_real_pipeline", "main"]


def run_real_pipeline(
    original_doc: bytes,
    *,
    chat_model: BaseChatModel | None = None,
    hitl1_gate: object | None = None,
    hitl2_gate: object | None = None,
    session_context: SessionContext | None = None,
) -> RunResult:
    """驱动「真实 LLM + 交互闸门」整条流水线。

    默认：``chat_model`` 经 :func:`build_qwen_chat_model` 读 ``DASHSCOPE_API_KEY``；两闸门用
    CLI 交互式。测试 / 程序化调用可注入 fake 替身。

    ``session_context`` 缺省时由入口构造（``current_time=datetime.now()``，真实运行时刻由入口
    注入、非节点内取时，ADR-0021；``user_prompt`` 取 ``--prompt``，``session_id``/``user_id``
    取环境或空），与 ``original_doc`` 同入 START、全链只读。
    """

    chat = chat_model or build_qwen_chat_model()
    agents = create_real_agents(
        llm=QwenParseLlmClient(chat),
        hitl1_gate=hitl1_gate or CliHitl1Gate(),  # type: ignore[arg-type]
        hypothesis_llm=QwenHypothesisLlmClient(chat),
        judgment_llm=QwenJudgmentLlmClient(chat),
        rewrite_llm=QwenRewriteLlmClient(chat),
        hitl2_gate=hitl2_gate or CliHitl2Gate(),  # type: ignore[arg-type]
    )
    ctx = session_context or SessionContext(
        session_id=os.environ.get("HYPOARGUS_SESSION_ID", ""),
        user_id=os.environ.get("HYPOARGUS_USER_ID", ""),
        current_time=datetime.now(),
        user_prompt="",
    )
    return Orchestrator(agents=agents).run_with_report(
        original_doc, session_context=ctx
    )


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
        "--prompt",
        default="",
        help="用户修订提示词（写入 session_context.user_prompt，贯穿全链只读）",
    )
    args = parser.parse_args(argv)

    raw = _read_input(args.input)
    ctx = SessionContext(
        session_id=os.environ.get("HYPOARGUS_SESSION_ID", ""),
        user_id=os.environ.get("HYPOARGUS_USER_ID", ""),
        current_time=datetime.now(),
        user_prompt=args.prompt,
    )
    report = run_real_pipeline(
        raw,
        chat_model=build_qwen_chat_model(args.model),
        session_context=ctx,
    )
    _write_output(args.output, report.final_document)
    for err in report.errors:
        print(err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
