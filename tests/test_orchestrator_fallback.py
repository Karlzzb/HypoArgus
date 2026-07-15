"""编排中枢异常兜底与单向流控测试（issue #11 · PRD §13）。

黑盒验证：任一下游智能体异常 / 超时时，目标节点就地置错误状态并附日志、流水线单向
向前推进至终稿，绝不因单点波动卡死整篇（PRD §13）。硬约束：无复杂分布式重试降级与
跨模块挂起——异常即记日志、就地降级、继续向前。
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial

import pytest

from agents.assembly import create_real_agents, create_stub_agents
from agents.hitl1 import (
    DEFAULT_MAX_PARTITION_RETRIES,
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    confirm_partition,
)
from agents.parser import FakeLlmClient, ParsedNodeProposal, ParseResult
from domain import ArgumentType
from runtime.orchestrator import Orchestrator

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _raise(stage: str):
    raise RuntimeError(f"{stage} boom")


def _three_core_proposals() -> list[ParsedNodeProposal]:
    return [
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", argument_type=ArgumentType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", argument_type=ArgumentType.EVIDENCE, parent_index=1
        ),
    ]


def _skip_gate() -> FakeHitl1Gate:
    return FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))


@pytest.mark.parametrize(
    "stage,build_throwing",
    [
        ("parse", lambda: (lambda original_paragraphs: _raise("parse"))),
        ("hitl1", lambda: (lambda argument_tree, retry_count: _raise("hitl1"))),
        ("hitl2", lambda: (lambda original_paragraphs, proposed_rewrites: _raise("hitl2"))),
    ],
)
def test_tree_stage_exception_keeps_pipeline_alive_and_logs(stage, build_throwing):
    """任一主干 stage 抛异常 → 单点波动不卡死、终稿逐字节还原、errors 记日志（PRD §13）。

    hitl2 普通异常（非 :class:`Hitl2GateError`）由 ``_hitl2_node`` 的 :func:`_guarded`
    兜底：回退原文 bytes 拼接（保护原文底线）向前。硬闸门 :class:`Hitl2GateError`
    不兜底、原样上抛，见 :func:`test_hitl2_gate_error_is_hard_stop_not_swallowed`。
    """

    base = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
    )
    agents = replace(base, **{stage: build_throwing()})
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_document == _DOC  # stale 树向前、无人确认 → 逐字节还原
    assert any(stage in e for e in report.errors)


def test_rewrite_loop_exception_falls_back_to_empty_proposed_rewrites_and_logs():
    """rewrite_loop 整体异常 → 降级空 proposed_rewrites + errors 记日志、终稿逐字节还原（PRD §13）。

    rewrite_loop 节点经 ``_guarded`` 兜底：fn 抛异常即「本轮无提议重写」——不卡死，
    记日志、空 ``proposed_rewrites`` 向前，下游 hitl2 见无待决 → 一键通过、终稿逐字节
    等于原文。rewrite_loop 不碰 ``argument_tree``（信号在 errors channel、不写树）。
    """

    base = create_stub_agents()

    def throwing_rewrite_loop(
        argument_tree, citations, paragraph_list,
        session_context, query_time_range,
    ):
        raise RuntimeError("rewrite_loop boom")

    agents = replace(base, rewrite_loop=throwing_rewrite_loop)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_document == _DOC  # 空 proposed_rewrites 不触达原文 → 逐字节还原
    assert any("rewrite_loop" in e for e in report.errors)


def test_judgment_wholesale_exception_marks_in_scope_arguments_error():
    """judgment 整体抛异常 → 覆盖范围内未判决节点就地置 error（PRD §13）、流水线仍推进至终稿。

    五合一后 judgment 是检索之后的唯一裁决节点（吃 citations 判终态、再调 merge/impact/
    consistency）。整体异常时 ``_judgment_node`` 的 :func:`_guarded` fallback 经
    :func:`_mark_verify_scope_error(reason="judgment")`` 把 ``claim`` / ``evidence`` 范围内仍
    ``unverified`` 的节点就地置 ``error`` + 贴 ``orchestrator_error:judgment``；``hypotheses``
    保持 pending 不动。下游 HITL-2 见 ``error`` 待决 → 保守闸门全驳回 → 原文逐字节还原。
    """

    base = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
    )

    def throwing_judgment(argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range):
        raise RuntimeError("judgment boom")

    captured: dict = {}

    def wrapping_rewrite_loop(
        argument_tree, citations, paragraph_list,
        session_context, query_time_range,
    ):
        captured["argument_tree"] = argument_tree  # judgment 整体异常后写入 argument_tree 的标记树
        return base.rewrite_loop(
            argument_tree, citations, paragraph_list,
            session_context, query_time_range,
        )

    agents = replace(base, judgment=throwing_judgment, rewrite_loop=wrapping_rewrite_loop)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    # 三核心节点均在 judgment 覆盖范围内、均未判决 → 整体异常后就地置 error + 贴 orchestrator_error:judgment。
    marked = {n.argument_id: n for n in captured["argument_tree"]}
    for nid in ("n0000", "n0001", "n0002"):
        assert marked[nid].status.value == "error"
        assert any(t.startswith("orchestrator_error:judgment") for t in marked[nid].issue_tags)
    # 流水线仍推进至终稿、rewrite_loop 见无触达（无 supported 假说 / 无命中 citations）→
    # 空 proposed_rewrites → hitl2 一键通过 → 逐字节还原。
    assert report.final_document == _DOC
    assert any("judgment" in e for e in report.errors)


def test_hitl2_gate_error_is_hard_stop_not_swallowed():
    """Hitl2GateError 是硬闸门正确性硬停，不兜底、原样上抛（ADR-0010）。"""

    from agents.hitl2 import Hitl2GateError

    base = create_stub_agents()

    def gate_error_hitl2(original_paragraphs, proposed_rewrites):
        raise Hitl2GateError("硬闸门：有待决内容却 PASS")

    agents = replace(base, hitl2=gate_error_hitl2)
    orch = Orchestrator(agents=agents)
    with pytest.raises(Hitl2GateError, match="硬闸门"):
        orch.run_with_report(_DOC)


def test_retrieval_exception_falls_back_to_empty_citations_and_logs():
    """retrieval 整体异常 → 降级空 citations + errors 记日志、终稿逐字节还原（PRD §13）。

    批量检索节点经 ``_guarded`` 兜底：检索 fn 抛异常即「本轮无 citations」——不卡死，
    记日志、空 citations 向前，下游 judgment / rewrite_loop 见无素材、不触达任何段，
    终稿逐字节等于原文。真实后端后续切片接入时该兜底语义不变。
    """

    base = create_stub_agents()

    def throwing_retrieval(argument_tree, hypotheses, query_time_range, session_context, paragraph_list):
        raise RuntimeError("retrieval boom")

    agents = replace(base, retrieval=throwing_retrieval)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_document == _DOC  # 降级空 citations 不触达原文 → 逐字节还原
    assert any("retrieval" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# _guarded 不吞 langgraph 控制流异常（T-03·ADR-0022）
#
# interrupt() 经 GraphInterrupt（GraphBubbleUp 子类）解栈暂停。hitl1/hitl2 节点经
# ``_guarded`` 包裹 gate.review（其内 interrupt）；若 ``_guarded`` 的 ``except Exception``
# 吞掉 GraphBubbleUp，则 interrupt 被静默兜底、图不暂停——破坏整个异步 HITL spine。
# 故 ``_guarded`` 必须原样放行 GraphBubbleUp（与 Hitl2GateError 同级硬停）。
# --------------------------------------------------------------------------- #


def test_guarded_re_raises_graph_bubbleup_not_swallowed() -> None:
    """GraphBubbleUp（GraphInterrupt 基类）经 _guarded 原样上抛、不走 fallback。"""

    from langgraph.errors import GraphBubbleUp

    from agents.assembly import _guarded

    def _raise_bubble() -> dict[str, object]:
        raise GraphBubbleUp("interrupt")

    fallback_ran: list[bool] = []

    def _fallback() -> dict[str, object]:
        fallback_ran.append(True)
        return {"errors": ["fallback"]}

    with pytest.raises(GraphBubbleUp):
        _guarded("hitl1", _raise_bubble, _fallback)
    assert fallback_ran == []  # 不兜底


def test_guarded_still_swallows_plain_runtime_error() -> None:
    """普通异常仍经 _guarded 兜底（GraphBubbleUp 放行不影响既有降级语义）。"""

    from agents.assembly import _guarded

    def _raise() -> dict[str, object]:
        raise RuntimeError("boom")

    out = _guarded("hitl1", _raise, lambda: {"errors": ["fb"]})
    assert out == {"errors": ["fb"]}


# --------------------------------------------------------------------------- #
# hitl1 partition 确认闸门 + 有界打回（ADR-0017 §2/§4·Slice 2）
#
# hitl1 重定义为 partition 确认闸门：确认继续（skip/accept/edit）→ 下游；打回重跑
# （replay）→ 重跑 parse+partition（按 user prompt，当前伪代码桩——原样重切、字节级自检不变）。
# 打回有界（max retries 默认 3）；超限向前推进 + 贴 partition_retry_exhausted（受控分支、
# 非异常降级）。桩路径下终稿对未触达段逐字节等于原文。
# --------------------------------------------------------------------------- #


class _ReplayThenSkipGate:
    """打回一次后跳过：第 1 次 REPLAY，第 2 次 SKIP。"""

    def __init__(self) -> None:
        self._calls = 0

    def review(self, argument_tree, *, paragraph_list) -> Hitl1Decision:
        self._calls += 1
        if self._calls == 1:
            return Hitl1Decision(action=Hitl1Action.REPLAY)
        return Hitl1Decision(action=Hitl1Action.SKIP)


class _AlwaysReplayGate:
    """恒打回：每次 review 都返回 REPLAY（驱动打回超限分支）。"""

    def review(self, argument_tree, *, paragraph_list) -> Hitl1Decision:
        return Hitl1Decision(action=Hitl1Action.REPLAY)


def _wrap(name, fn, calls):
    def inner(*a, **kw):
        calls[name] += 1
        return fn(*a, **kw)

    return inner


def test_hitl1_replay_once_then_continue_reruns_parse_and_keeps_byte_identity():
    """打回一次后继续：parse+partition 与 hitl1 各被调用两次、终稿逐字节等于原文。"""

    base = create_stub_agents()
    calls = {"parse": 0, "hitl1": 0}
    gate = _ReplayThenSkipGate()
    agents = replace(
        base,
        parse=_wrap("parse", base.parse, calls),
        hitl1=_wrap("hitl1", partial(confirm_partition, gate=gate), calls),
    )
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_document == _DOC  # 桩重切不触达原文 → 逐字节还原
    assert report.errors == []  # 受控打回不计异常
    assert calls["parse"] == 2  # 初始 1 + 打回重跑 1
    assert calls["hitl1"] == 2  # REPLAY 1 + SKIP 1


def test_hitl1_replay_exhaustion_forwards_and_tags_partition_retry_exhausted():
    """恒打回 + 超限 → 向前推进、errors 含 partition_retry_exhausted、终稿逐字节等于原文。"""

    base = create_stub_agents()
    calls = {"parse": 0, "hitl1": 0}
    gate = _AlwaysReplayGate()
    agents = replace(
        base,
        parse=_wrap("parse", base.parse, calls),
        hitl1=_wrap("hitl1", partial(confirm_partition, gate=gate), calls),
    )
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_document == _DOC  # 桩重切不触达原文 → 逐字节还原
    # 受控分支贴标签（非异常降级）：errors 含 partition_retry_exhausted、且无 hitl1 异常日志。
    assert any("partition_retry_exhausted" in e for e in report.errors)
    assert not any(e.startswith("[hitl1]") and "partition_retry_exhausted" not in e for e in report.errors)
    # 打回有界：parse 与 hitl1 各被调用 max_retries + 1 次（初始 + 3 次打回重跑，第 4 次 hitl1 耗尽向前）。
    assert calls["hitl1"] == DEFAULT_MAX_PARTITION_RETRIES + 1
    assert calls["parse"] == DEFAULT_MAX_PARTITION_RETRIES + 1


def test_hitl1_replay_exhaustion_does_not_raise_partition_recursion():
    """打回超限不触发 GraphRecursionError——recursion 预算随 max_replays 缩放、有界。"""

    base = create_stub_agents()
    gate = _AlwaysReplayGate()
    agents = replace(base, hitl1=partial(confirm_partition, gate=gate))
    orch = Orchestrator(agents=agents)
    # 不抛 GraphRecursionError 即通过（run_with_report 正常返回）。
    report = orch.run_with_report(_DOC)
    assert report.final_document == _DOC
