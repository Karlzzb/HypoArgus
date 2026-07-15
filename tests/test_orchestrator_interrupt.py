"""持久化异步 HITL 集成测试（T-03·ADR-0022 spine）。

图注入 :class:`InterruptHitl1Gate` / :class:`InterruptHitl2Gate` + ``AsyncPostgresSaver``，
由 :func:`runtime.run_real.run_resume_loop` 驱动 ``ainvoke → aget_state → Command(resume)``。
覆盖验收：

- hitl1 / hitl2 经 ``interrupt()`` 两次暂停 + resume 续跑至终态（PRD §10.4 / ADR-0022）。
- 跨进程续跑：进程 1 跑至 hitl1 暂停（退出）、新进程同 ``session_id`` ``aget_state`` 见
  interrupt、resume 续跑至完成。
- 业务纯函数（``confirm`` / ``confirm_partition`` / ``resolve_rewrites`` /
  ``assemble_final_document``）零改动——经 ``interrupt`` 路径仍行为等价。

需共享 Postgres（``HYPOARGUS_PG_DSN``，见 ``.env``）；不可达由 ``pg_checkpointer`` 夹具 skip。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents.assembly import create_real_agents
from agents.parser import FakeLlmClient, ParseResult
from domain import SessionContext
from runtime.checkpoint import build_async_checkpointer
from runtime.gates import InterruptHitl1Gate, InterruptHitl2Gate
from runtime.orchestrator import Orchestrator, RunResult
from runtime.run_real import run_resume_loop

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _ctx(sid: str) -> SessionContext:
    return SessionContext(
        session_id=sid,
        user_id="u",
        current_time=datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC),
        user_prompt="",
    )


def _interrupt_agents() -> Any:
    """真实解析 + InterruptHitl*Gate，下游开药 / 裁决 / 重写为桩（无触达 → 终稿逐字节原文）。"""

    return create_real_agents(
        llm=FakeLlmClient(result=ParseResult()),  # 空 proposals → 全段 background 影子
        hitl1_gate=InterruptHitl1Gate(),
        hitl2_gate=InterruptHitl2Gate(),
    )


def _silent_out(*_args: Any, **_kw: Any) -> None:
    return None


async def test_resume_loop_drives_two_interrupts_to_terminal_byte_identical(
    pg_checkpointer: Any,
) -> None:
    """fresh run：hitl1 暂停（input ``s``=SKIP）→ hitl2 暂停（无待决→自动 PASS）→ 终态，
    终稿逐字节等于原文、无 errors。"""

    agents = _interrupt_agents()
    orch = Orchestrator(agents=agents, checkpointer=pg_checkpointer)
    inputs = iter(["s"])
    report = await run_resume_loop(
        orch,
        _DOC,
        session_id="sess-resume-full",
        session_context=_ctx("sess-resume-full"),
        input_fn=lambda _p: next(inputs),
        out_fn=_silent_out,
        interactive=True,
    )
    assert isinstance(report, RunResult)
    assert report.final_document == _DOC  # 无触达段 → 逐字节原文
    assert report.errors == []


async def test_resume_loop_hitl1_replay_then_skip_reruns_parse(pg_checkpointer: Any) -> None:
    """hitl1 REPLAY（有界打回）→ 重跑 parse+partition、再 hitl1 暂停 → SKIP 续跑至终态。

    打回经 interrupt 路径（REPLY resume value）、``confirm_partition`` 不改、有界循环预算
    不触发 GraphRecursionError。"""

    agents = _interrupt_agents()
    orch = Orchestrator(agents=agents, checkpointer=pg_checkpointer)
    inputs = iter(["r", "s"])  # 第 1 次 replay，第 2 次 skip
    report = await run_resume_loop(
        orch,
        _DOC,
        session_id="sess-resume-replay",
        session_context=_ctx("sess-resume-replay"),
        input_fn=lambda _p: next(inputs),
        out_fn=_silent_out,
        interactive=True,
    )
    assert report.final_document == _DOC
    assert report.errors == []


async def test_cross_process_resume_persists_interrupt_across_savers(pg_checkpointer: Any) -> None:
    """跨进程续跑：进程 1 跑至 hitl1 暂停（退出）；新进程新 saver + 同 session_id
    ``aget_state`` 见 interrupt、resume 续跑至终态。"""

    agents = _interrupt_agents()
    sid = "sess-xproc"
    ctx = _ctx(sid)

    # 进程 1：fresh run 至首个 interrupt，然后「退出」（不 resume）。
    orch1 = Orchestrator(agents=agents, checkpointer=pg_checkpointer)
    cfg1: dict[str, Any] = {
        "configurable": {"thread_id": sid},
        "recursion_limit": orch1._recursion_limit,
    }
    await orch1.graph.ainvoke(
        {"original_doc": _DOC, "session_context": ctx}, config=cfg1
    )
    st1 = await orch1.graph.aget_state(cfg1)
    assert st1.next  # 进程 1 在 hitl1 暂停、断点已落 PG
    assert "hitl1" in st1.next
    # 进程 1 到此为止——orch1 / saver1 不再使用（模拟进程退出）。

    # 进程 2：全新 saver（新 PG 连接）+ 全新 Orchestrator + 同 thread_id。
    async with build_async_checkpointer() as saver2:
        orch2 = Orchestrator(agents=agents, checkpointer=saver2)
        # resume 循环检测到既有 checkpoint（values 非空）→ 不重喂 input、直接续跑。
        st2 = await orch2.graph.aget_state(
            {
                "configurable": {"thread_id": sid},
                "recursion_limit": orch2._recursion_limit,
            }
        )
        assert st2.next  # 新进程见持久化的 interrupt 暂停点（ADR-0022 核心承诺）
        assert "hitl1" in st2.next
        report = await run_resume_loop(
            orch2,
            _DOC,
            session_id=sid,
            session_context=ctx,
            input_fn=lambda _p: "s",
            out_fn=_silent_out,
            interactive=True,
        )
        assert report.final_document == _DOC
        assert report.errors == []


async def test_interrupt_state_carries_original_paragraphs_through_checkpoint(
    pg_checkpointer: Any,
) -> None:
    """interrupt 暂停点的 state.values 含 original_paragraphs（经自定义编解码器落库），
    跨 saver 读回仍为 ``OriginalParagraphs``、段落序 + bytes 等价。"""

    from original_paragraphs import OriginalParagraphs

    agents = _interrupt_agents()
    sid = "sess-op-carry"
    ctx = _ctx(sid)
    orch1 = Orchestrator(agents=agents, checkpointer=pg_checkpointer)
    cfg: dict[str, Any] = {
        "configurable": {"thread_id": sid},
        "recursion_limit": orch1._recursion_limit,
    }
    await orch1.graph.ainvoke(
        {"original_doc": _DOC, "session_context": ctx}, config=cfg
    )
    # 进程 1 在 hitl1 暂停；op 已落 checkpoint。
    async with build_async_checkpointer() as saver2:
        orch2 = Orchestrator(agents=agents, checkpointer=saver2)
        st = await orch2.graph.aget_state(
            {
                "configurable": {"thread_id": sid},
                "recursion_limit": orch2._recursion_limit,
            }
        )
        op = st.values.get("original_paragraphs")
        assert isinstance(op, OriginalParagraphs)
        expected = OriginalParagraphs.from_text(_DOC)
        assert op.paragraph_ids() == expected.paragraph_ids()
        for pid in expected.paragraph_ids():
            assert op.get(pid) == expected.get(pid)


async def test_interrupt_payload_carries_paragraph_list_through_checkpoint(
    pg_checkpointer: Any,
) -> None:
    """hitl1 interrupt 载荷（``Hitl1Question``）经 PG checkpoint 往返仍携带 ``paragraph_list``
    （强类型 ``ParagraphRecord`` 列表）——T-03 resume 渲染反查所据不破。

    parse 产出 ``paragraph_list``（``FakeLlmClient`` 空 proposals → 全段 background 影子），
    经 ``_hitl1_node → confirm_partition → gate.review → formulate_question`` 入 interrupt 载荷、
    落 checkpoint；跨 saver 读回仍为强类型、段集合等价。
    """

    from agents.hitl1 import Hitl1Question
    from domain import ParagraphRecord
    from runtime.run_real import _interrupt_payload

    agents = _interrupt_agents()
    sid = "sess-pl-carry"
    ctx = _ctx(sid)
    orch1 = Orchestrator(agents=agents, checkpointer=pg_checkpointer)
    cfg: dict[str, Any] = {
        "configurable": {"thread_id": sid},
        "recursion_limit": orch1._recursion_limit,
    }
    await orch1.graph.ainvoke(
        {"original_doc": _DOC, "session_context": ctx}, config=cfg
    )
    st1 = await orch1.graph.aget_state(cfg)
    assert st1.next and "hitl1" in st1.next
    payload1 = _interrupt_payload(st1)
    assert isinstance(payload1, Hitl1Question)
    # parse 产出 paragraph_list、经 formulate_question 入载荷。
    assert payload1.paragraph_list
    assert all(isinstance(r, ParagraphRecord) for r in payload1.paragraph_list)
    pids_in_payload = {r.paragraph_id for r in payload1.paragraph_list}

    # 跨 saver（新 PG 连接）读回：paragraph_list 仍强类型、段集合等价。
    async with build_async_checkpointer() as saver2:
        orch2 = Orchestrator(agents=agents, checkpointer=saver2)
        st2 = await orch2.graph.aget_state(
            {
                "configurable": {"thread_id": sid},
                "recursion_limit": orch2._recursion_limit,
            }
        )
        payload2 = _interrupt_payload(st2)
        assert isinstance(payload2, Hitl1Question)
        assert all(isinstance(r, ParagraphRecord) for r in payload2.paragraph_list)
        assert (
            {r.paragraph_id for r in payload2.paragraph_list} == pids_in_payload
        )
