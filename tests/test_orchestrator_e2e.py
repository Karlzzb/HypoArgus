"""端到端骨架测试（tracer bullet #1）。

黑盒外部行为验证（PRD «Testing Decisions»）：输入纯文本 → 流水线流转 → 终稿文本。
核心断言：无采纳改动时，终稿与原始输入逐字节完全一致（含空行/缩进/换行/末尾空格）。
并验证流水线单向推进、桩环节不产生打回或重调度（每个桩仅被调用一次）。
"""

from __future__ import annotations

import pytest

from hypoargus.agents import Agents, create_stub_agents
from hypoargus.orchestrator import Orchestrator


def test_e2e_byte_identical_no_adoptions(sample_doc):
    """无采纳改动时，终稿逐字节等于原始输入（tracer bullet 核心承诺）。"""

    _name, doc = sample_doc
    orch = Orchestrator()
    assert orch.run(doc) == doc


def test_e2e_pipeline_single_direction_no_reschedule(sample_doc):
    """流水线单向推进：每个桩智能体恰好被调用一次（无打回、无重调度）。"""

    _name, doc = sample_doc
    base = create_stub_agents()
    calls: dict[str, int] = {
        "parse": 0,
        "hitl1": 0,
        "verification": 0,
        "hypothesis": 0,
        "merge": 0,
        "impact": 0,
        "consistency": 0,
        "hitl2": 0,
        "writeback": 0,
    }

    def wrap(name, fn):
        def inner(*a, **kw):
            calls[name] += 1
            return fn(*a, **kw)

        return inner

    agents = Agents(
        parse=wrap("parse", base.parse),
        hitl1=wrap("hitl1", base.hitl1),
        verification=wrap("verification", base.verification),
        hypothesis=wrap("hypothesis", base.hypothesis),
        merge=wrap("merge", base.merge),
        impact=wrap("impact", base.impact),
        consistency=wrap("consistency", base.consistency),
        hitl2=wrap("hitl2", base.hitl2),
        writeback=wrap("writeback", base.writeback),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)
    assert out == doc
    # 每个环节恰好一次：证明单向推进、无打回。
    assert calls == {k: 1 for k in calls}, f"调用次数异常：{calls}"


def test_e2e_final_doc_reaches_end():
    """流水线推进至终稿：final_doc 非空且等于原文（无任何改动）。"""

    doc = "# 标题\n\n正文段落一。\n\n- 要点\n\n```python\nx=1\n```\n\n末段。\n".encode()
    orch = Orchestrator()
    out = orch.run(doc)
    assert out == doc
    assert len(out) == len(doc)


def test_e2e_rejects_str_input():
    """原始文本以 bytes 流转（字节级保护原文的前提）。"""

    orch = Orchestrator()
    with pytest.raises(TypeError):
        orch.run("not bytes")  # type: ignore[arg-type]
