"""编排中枢异常兜底与单向流控测试（issue #11 · PRD §13）。

黑盒验证：任一下游智能体异常 / 超时时，目标节点就地置错误状态并附日志、流水线单向
向前推进至终稿，绝不因单点波动卡死整篇（PRD §13）。硬约束：无复杂分布式重试降级与
跨模块挂起——异常即记日志、就地降级、继续向前。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agents.assembly import create_real_agents, create_stub_agents
from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.parser import FakeLlmClient, ParsedNodeProposal, ParseResult
from domain import NodeType
from runtime.orchestrator import Orchestrator

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _raise(stage: str):
    raise RuntimeError(f"{stage} boom")


def _three_core_proposals() -> list[ParsedNodeProposal]:
    return [
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", node_type=NodeType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", node_type=NodeType.EVIDENCE, parent_index=1
        ),
    ]


def _skip_gate() -> FakeHitl1Gate:
    return FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))


def test_merge_stage_exception_does_not_hang_and_logs():
    """合并算子抛异常 → 流水线不卡死、终稿逐字节等于原文、errors 记日志（PRD §13）。"""

    base = create_stub_agents()

    def throwing_merge(tree):
        raise RuntimeError("merge boom")

    agents = replace(base, merge=throwing_merge)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_doc == _DOC  # 单点波动不卡死、无人采纳 → 逐字节还原
    assert report.errors  # 异常兜底日志非空
    assert any("merge" in e for e in report.errors)


def test_verification_wholesale_exception_marks_in_scope_nodes_error():
    """体检整体抛异常 → 覆盖范围内未判决节点就地置 error（PRD §13）、流水线仍推进至终稿。"""

    base = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
    )

    def throwing_verify(tree):
        raise RuntimeError("verify boom")

    captured: dict = {}

    def wrapping_merge(tree):
        captured["tree"] = tree  # 体检整体异常后写入 tree 的标记树
        return base.merge(tree)

    agents = replace(base, verification=throwing_verify, merge=wrapping_merge)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    # 三核心节点均在体检覆盖范围内、均未判决 → 整体异常后就地置 error + 贴 orchestrator_error。
    marked = {n.node_id: n for n in captured["tree"]}
    for nid in ("n0000", "n0001", "n0002"):
        assert marked[nid].status.value == "error"
        assert any(t.startswith("orchestrator_error:verify") for t in marked[nid].issue_tags)
    # 流水线仍推进至终稿、无人采纳 → 逐字节还原。
    assert report.final_doc == _DOC
    assert any("verification" in e for e in report.errors)


@pytest.mark.parametrize(
    "stage,build_throwing",
    [
        ("parse", lambda: (lambda store: _raise("parse"))),
        ("hitl1", lambda: (lambda tree: _raise("hitl1"))),
        ("impact", lambda: (lambda tree: _raise("impact"))),
        ("consistency", lambda: (lambda tree: _raise("consistency"))),
        ("hitl2", lambda: (lambda tree, store: _raise("hitl2"))),
    ],
)
def test_tree_stage_exception_keeps_pipeline_alive_and_logs(stage, build_throwing):
    """任一树形 stage 抛异常 → 单点波动不卡死、终稿逐字节还原、errors 记日志（PRD §13）。"""

    base = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
    )
    agents = replace(base, **{stage: build_throwing()})
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_doc == _DOC  # stale 树向前、无人采纳 → 逐字节还原
    assert any(stage in e for e in report.errors)


def test_hitl2_gate_error_is_hard_stop_not_swallowed():
    """Hitl2GateError 是硬闸门正确性硬停，不兜底、原样上抛（ADR-0010）。"""

    from agents.hitl2 import Hitl2GateError

    base = create_stub_agents()

    def gate_error_hitl2(tree, store):
        raise Hitl2GateError("硬闸门：有待决内容却 PASS")

    agents = replace(base, hitl2=gate_error_hitl2)
    orch = Orchestrator(agents=agents)
    with pytest.raises(Hitl2GateError, match="硬闸门"):
        orch.run_with_report(_DOC)


def test_writeback_stage_exception_falls_back_to_original_bytes():
    """回写整体异常 → 回退原文 bytes（保护原文底线）+ errors 记日志（PRD §13）。"""

    base = create_stub_agents()

    def throwing_writeback(tree, store):
        raise RuntimeError("writeback boom")

    agents = replace(base, writeback=throwing_writeback)
    orch = Orchestrator(agents=agents)
    report = orch.run_with_report(_DOC)

    assert report.final_doc == _DOC  # 回退原文逐字节拼接（分区不变式）
    assert any("writeback" in e for e in report.errors)
