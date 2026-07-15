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
import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.types import Command

from agents.assembly import create_real_agents
from agents.hitl1 import Hitl1Action, Hitl1Question, Hitl1Reply
from agents.hitl2 import Hitl2Action, Hitl2Question, Hitl2Reply
from domain import SessionContext
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from infra.llm_provider import build_qwen_chat_model
from infra.observability import build_langfuse_callback
from runtime.checkpoint import build_async_checkpointer
from runtime.cli_gates import CliHitl1Gate, CliHitl2Gate, owner_paragraph_id
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator, RunResult

__all__ = ["run_real_pipeline", "run_resume_loop", "arun_real_pipeline", "main"]

_INPUT_FN = Callable[[str], str]
_OUT_FN = Callable[..., None]


def _is_interactive(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return sys.stdin.isatty()


def _build_session_config(session_id: str) -> dict[str, object]:
    """装配 ``RunnableConfig``：注入 Langfuse callback + trace 聚合 metadata。

    ``build_langfuse_callback`` 在未配置 Langfuse（缺环境变量）或未安装 SDK 时返回 ``None``——
    此时**不**注入 ``callbacks`` / ``metadata``，``Orchestrator.run_with_report`` 行为与离线态
    完全一致（零侵入，不破坏离线测试）。配置齐全时把 Langfuse handler 与
    ``langfuse_session_id`` / ``langfuse_user_id`` / ``langfuse_tags`` 放入 ``metadata``，
    Langfuse handler 据之落 trace 会话 / 用户 / 标签（见 :mod:`infra.observability`）。
    """

    handler = build_langfuse_callback()
    if handler is None:
        return {}
    return {
        "callbacks": [handler],
        "metadata": {
            "langfuse_session_id": session_id,
            "langfuse_user_id": os.environ.get("HYPOARGUS_USER_ID", "hypoargus"),
            "langfuse_tags": ["real-run"],
        },
    }


def run_real_pipeline(
    original_doc: bytes,
    *,
    chat_model: BaseChatModel | None = None,
    hitl1_gate: object | None = None,
    hitl2_gate: object | None = None,
    session_context: SessionContext | None = None,
    session_config: dict[str, object] | None = None,
) -> RunResult:
    """驱动「真实 LLM + 交互闸门」整条流水线。

    默认：``chat_model`` 经 :func:`build_qwen_chat_model` 读 ``DASHSCOPE_API_KEY``；两闸门用
    CLI 交互式。测试 / 程序化调用可注入 fake 替身。

    ``session_context`` 缺省时由入口构造（``current_time=datetime.now()``，真实运行时刻由入口
    注入、非节点内取时，ADR-0021；``user_prompt`` 取 ``--prompt``，``session_id``/``user_id``
    取环境或空），与 ``original_doc`` 同入 START、全链只读。

    ``session_config`` 缺省时由 :func:`_build_session_config` 装配 Langfuse callback（配置齐全）
    或空 dict（未配置，零侵入）；调用方可显式注入 fake callback / 覆盖 metadata。Langfuse handler
    经 ``RunnableConfig.callbacks`` 线程贯穿整条 Agent 链路（ADR-0016）。
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
    config = session_config if session_config is not None else _build_session_config(
        ctx.session_id or str(uuid.uuid4())
    )
    return Orchestrator(agents=agents).run_with_report(
        original_doc, session_config=config, session_context=ctx
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


# --------------------------------------------------------------------------- #
# 异步 resume 循环（T-03·ADR-0022 spine）
#
# 图注入 InterruptHitl*Gate + AsyncPostgresSaver：ainvoke → 若 aget_state.next 非空
# （interrupt 暂停）→ 渲染 interrupt 载荷（Hitl*Question）到终端 → 读输入 → 构造
# Hitl*Reply → ainvoke(Command(resume=reply)) → 循环，直至 state.next 空（终态 final_document）。
# thread_id = session_id：跨进程以同 session_id 启动即复用断点（见 test 跨进程续跑）。
#
# 与同步 ``run_real_pipeline`` 的关系：本驱动是 ADR-0022 spine 的 CLI 驱动者；同步
# ``run_real_pipeline``（CliHitl*Gate、无 checkpointer）保留为程序化 / 离线全保真路径
# （Fake / Conservative 闸门、含 ops，供既有 wiring 测试）。
# --------------------------------------------------------------------------- #


def _interrupt_payload(state: object) -> object:
    """从 aget_state 结果取首个 interrupt 的 value（Hitl*Question）；无则 None。"""

    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return getattr(intr, "value", None)
    return None


def _render_hitl1_question(question: Hitl1Question, out: _OUT_FN) -> None:
    out("=== HITL-1 切分确认：解析树 ===")
    if not question.argument_tree:
        out("（空树）")
        return
    for n in question.argument_tree:
        para = owner_paragraph_id(question.paragraph_list, n.argument_id)
        out(
            f"{n.argument_id}\ttype={n.argument_type.value}\tweight={n.argument_weight}"
            f"\tpara={para or '?'}\tparent={n.parent_id}\tstatus={n.status.value}"
        )
    out("[s]kip / [a]ccept / [r]eplay（按 prompt 重跑 parse+partition，有界）")


def _render_hitl2_question(question: Hitl2Question, out: _OUT_FN) -> None:
    review = question.review
    if not review.has_pending:
        out("=== HITL-2：无提议重写，一键通过。 ===")
        return
    out("=== HITL-2 终稿确认：逐段原文 × 提议重写 ===")
    for p in review.paragraphs:
        out(f"\n--- {p.paragraph_id} ---")
        out(f"原文：{p.original_text}")
        out(f"提议：{p.proposed_text}")
    out("一期仅 action-only：[r]eject all（全驳回、原文逐字节保留，默认）")


def _prompt_hitl1(
    question: Hitl1Question,
    *,
    input_fn: _INPUT_FN,
    out_fn: _OUT_FN,
    interactive: bool,
) -> Hitl1Reply:
    _render_hitl1_question(question, out_fn)
    if not interactive:
        out_fn("[非交互] HITL-1 保守 SKIP（不改结构、原文不动）。")
        return Hitl1Reply(action=Hitl1Action.SKIP)
    while True:
        raw = input_fn("[HITL-1] [s]kip/[a]ccept/[r]eplay: ").strip().lower()
        if raw in ("s", "skip"):
            return Hitl1Reply(action=Hitl1Action.SKIP)
        if raw in ("a", "accept"):
            return Hitl1Reply(action=Hitl1Action.ACCEPT)
        if raw in ("r", "replay"):
            return Hitl1Reply(action=Hitl1Action.REPLAY)
        out_fn("未知选项，请输入 s/a/r。")


def _prompt_hitl2(
    question: Hitl2Question,
    *,
    input_fn: _INPUT_FN,
    out_fn: _OUT_FN,
    interactive: bool,
) -> Hitl2Reply:
    review = question.review
    if not review.has_pending:
        # 无待决：一键通过（confirm 的硬闸门校验放行 PASS）。
        return Hitl2Reply(action=Hitl2Action.PASS)
    _render_hitl2_question(question, out_fn)
    if not interactive:
        out_fn("[非交互] HITL-2 有提议重写但无人拍板 → 全驳回、原文逐字节保留。")
        return Hitl2Reply(action=Hitl2Action.DECIDE)
    # 一期 action-only：DECIDE = 全驳回（结构化逐段 confirm/edit 推后）。
    input_fn("[HITL-2] 回车=reject all：")
    return Hitl2Reply(action=Hitl2Action.DECIDE)


async def run_resume_loop(
    orch: Orchestrator,
    original_doc: bytes,
    *,
    session_id: str,
    session_context: SessionContext,
    input_fn: _INPUT_FN = input,
    out_fn: _OUT_FN = print,
    interactive: bool | None = None,
    session_config: dict[str, object] | None = None,
) -> RunResult:
    """驱动 ``interrupt + PostgresSaver`` 图的本地 resume 循环至终态。

    图须已 ``compile(checkpointer=...)``（``Orchestrator(checkpointer=...)``），hitl1/hitl2
    注入 :class:`InterruptHitl1Gate` / :class:`InterruptHitl2Gate`。``session_id`` 作
    ``thread_id``；进程重启后以同 ``session_id`` 重建 Orchestrator + saver、调本循环即可
    见 checkpoint 暂停点并续跑（跨进程续跑）。

    循环：先 ``aget_state`` 判定 fresh vs resume——无 checkpoint（``values`` 空）即 fresh，
    ``ainvoke(input)`` 喂入 ``original_doc`` / ``session_context`` 至首个 interrupt；否则 resume
    （已暂停的断点仍在）、不重喂 input。随后循环：``aget_state``：``next`` 空 → 终态返
    :class:`RunResult`；非空 → 据 ``next`` 节点名（``hitl1`` / ``hitl2``）渲染 interrupt 载荷、
    读输入、构造 ``Hitl*Reply`` → ``ainvoke(Command(resume=reply))`` → 续循环。
    """

    is_interactive = _is_interactive(interactive)
    config: dict[str, Any] = dict(session_config or {})
    configurable: dict[str, Any] = dict(config.get("configurable") or {})
    configurable["thread_id"] = session_id
    config["configurable"] = configurable
    config.setdefault("recursion_limit", orch._recursion_limit)

    # fresh vs resume：同 thread_id 已有 checkpoint（values 非空）即续跑、不重喂 input；
    # 无 checkpoint 即 fresh run、喂入 original_doc + session_context 至首个 interrupt。
    initial = await orch.graph.aget_state(config)
    if not initial.values:
        await orch.graph.ainvoke(
            {"original_doc": bytes(original_doc), "session_context": session_context},
            config=config,
        )
    while True:
        state = await orch.graph.aget_state(config)
        if not state.next:
            final: bytes = state.values.get("final_document", b"")
            errors: list[str] = list(state.values.get("errors", []))
            return RunResult(final_document=final, errors=errors)
        node = state.next[0]
        payload = _interrupt_payload(state)
        reply: Hitl1Reply | Hitl2Reply
        if node == "hitl1" and isinstance(payload, Hitl1Question):
            reply = _prompt_hitl1(
                payload, input_fn=input_fn, out_fn=out_fn, interactive=is_interactive
            )
        elif node == "hitl2" and isinstance(payload, Hitl2Question):
            reply = _prompt_hitl2(
                payload, input_fn=input_fn, out_fn=out_fn, interactive=is_interactive
            )
        else:
            raise RuntimeError(
                f"resume 循环遇到未知 interrupt 节点 {node!r}（payload={payload!r}）"
            )
        await orch.graph.ainvoke(Command(resume=reply), config=config)


async def arun_real_pipeline(
    original_doc: bytes,
    *,
    chat_model: BaseChatModel | None = None,
    session_context: SessionContext | None = None,
    session_config: dict[str, object] | None = None,
    input_fn: _INPUT_FN = input,
    out_fn: _OUT_FN = print,
    interactive: bool | None = None,
) -> RunResult:
    """异步 resume 循环入口：真实 LLM + InterruptHitl*Gate + AsyncPostgresSaver。

    默认 ``chat_model`` 经 :func:`build_qwen_chat_model`；两闸门用 :class:`InterruptHitl1Gate`
    / :class:`InterruptHitl2Gate`（``interrupt`` 暂停、本驱动 resume）。``input_fn`` /
    ``out_fn`` 可注入供单测脚本化驱动。
    """

    chat = chat_model or build_qwen_chat_model()
    agents = create_real_agents(
        llm=QwenParseLlmClient(chat),
        hitl1_gate=InterruptHitl1Gate(),
        hypothesis_llm=QwenHypothesisLlmClient(chat),
        judgment_llm=QwenJudgmentLlmClient(chat),
        rewrite_llm=QwenRewriteLlmClient(chat),
        hitl2_gate=InterruptHitl2Gate(),
    )
    ctx = session_context or SessionContext(
        session_id=os.environ.get("HYPOARGUS_SESSION_ID", ""),
        user_id=os.environ.get("HYPOARGUS_USER_ID", ""),
        current_time=datetime.now(),
        user_prompt="",
    )
    sid = ctx.session_id or str(uuid.uuid4())
    config = session_config if session_config is not None else _build_session_config(sid)
    async with build_async_checkpointer() as saver:
        await saver.setup()
        orch = Orchestrator(agents=agents, checkpointer=saver)
        return await run_resume_loop(
            orch,
            original_doc,
            session_id=sid,
            session_context=ctx,
            input_fn=input_fn,
            out_fn=out_fn,
            interactive=interactive,
            session_config=config,
        )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m runtime.run_real [input] [-o output]``（异步 resume 循环）。"""

    parser = argparse.ArgumentParser(
        prog="runtime.run_real",
        description="HypoArgus 真实 LLM 端到端流水线（DashScope / qwen-max · interrupt+PostgresSaver resume 循环）。",
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

    from dotenv import load_dotenv

    load_dotenv()  # 加载 .env（DASHSCOPE_API_KEY / HYPOARGUS_PG_DSN 等）
    raw = _read_input(args.input)
    session_id = os.environ.get("HYPOARGUS_SESSION_ID") or str(uuid.uuid4())
    ctx = SessionContext(
        session_id=session_id,
        user_id=os.environ.get("HYPOARGUS_USER_ID", ""),
        current_time=datetime.now(),
        user_prompt=args.prompt,
    )
    report = asyncio.run(
        arun_real_pipeline(
            raw,
            chat_model=build_qwen_chat_model(args.model),
            session_context=ctx,
            session_config=_build_session_config(session_id),
        )
    )
    _write_output(args.output, report.final_document)
    for err in report.errors:
        print(err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
