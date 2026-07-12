"""拓扑 seam 测试：PipelineSpec 数据驱动布线 + 自定义拓扑（issue #11 后置重构 · B）。

验证默认 :func:`default_pipeline` 复刻原固定拓扑（行为零变化），且传入另一种 spec
（省略 ``hypothesis`` / ``consistency`` 并相应重连 deps）形成「第二种 adapter」——使
拓扑 seam 为**真 seam** 而非假 seam（deep-module 原则：two adapters means a real seam）。
"""

from __future__ import annotations

from dataclasses import replace

from agents.assembly import create_stub_agents
from runtime.orchestrator import Orchestrator, default_pipeline

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _spec_without_hypothesis():
    """省略 hypothesis：merge 仅依赖 verification（开药线路不参与流水线）。"""

    s = {st.name: st for st in default_pipeline()}
    return (
        s["partition"], s["parse"], s["hitl1"], s["verification"],
        replace(s["merge"], deps=("verification",)),
        s["impact"], s["consistency"], s["hitl2"], s["writeback"],
    )


def _spec_without_consistency():
    """省略 consistency：hitl2 直接接 impact（一致性批注线路不参与流水线）。"""

    s = {st.name: st for st in default_pipeline()}
    return (
        s["partition"], s["parse"], s["hitl1"],
        s["verification"], s["hypothesis"], s["merge"], s["impact"],
        replace(s["hitl2"], deps=("impact",)),
        s["writeback"],
    )


def _wrap(name, fn, calls):
    def inner(*a, **kw):
        calls[name] += 1
        return fn(*a, **kw)

    return inner


def test_default_spec_replicates_fixed_topology_byte_identity():
    """默认 spec 与原硬编码拓扑行为同构：无采纳 → 终稿逐字节等于原文。"""

    orch = Orchestrator()  # 默认 agents + 默认 spec
    assert orch.run(_DOC) == _DOC


def test_default_pipeline_is_immutable_tuple_of_frozen_specs():
    """default_pipeline 返回不可变 tuple、各 StageSpec frozen——拓扑数据不可变。"""

    spec = default_pipeline()
    assert isinstance(spec, tuple)
    assert len(spec) == 10
    names = [s.name for s in spec]
    assert names == [
        "partition", "parse", "hitl1", "verification", "hypothesis",
        "merge", "impact", "consistency", "hitl2", "writeback",
    ]
    # merge 同时依赖 verification 与 hypothesis（并行 join）
    merge = next(s for s in spec if s.name == "merge")
    assert set(merge.deps) == {"verification", "hypothesis"}


def test_custom_spec_dropping_hypothesis_skips_it():
    """省略 hypothesis 的 spec：merge 仅依赖 verification，开药智能体绝不被调用、
    流水线仍推进至终稿逐字节还原。"""

    base = create_stub_agents()
    calls = {"hypothesis": 0, "merge": 0, "consistency": 0}
    agents = replace(
        base,
        hypothesis=_wrap("hypothesis", base.hypothesis, calls),
        merge=_wrap("merge", base.merge, calls),
        consistency=_wrap("consistency", base.consistency, calls),
    )
    orch = Orchestrator(agents=agents, spec=_spec_without_hypothesis())
    out = orch.run(_DOC)

    assert out == _DOC  # 无人采纳 → 逐字节还原
    assert calls["hypothesis"] == 0  # 开药线路被拓扑省略、绝不被调用
    assert calls["merge"] == 1
    assert calls["consistency"] == 1


def test_custom_spec_dropping_consistency_skips_it():
    """省略 consistency 的 spec：hitl2 直接接 impact，一致性校验绝不被调用、
    流水线仍推进至终稿逐字节还原。"""

    base = create_stub_agents()
    calls = {"consistency": 0, "impact": 0, "hitl2": 0}
    agents = replace(
        base,
        consistency=_wrap("consistency", base.consistency, calls),
        impact=_wrap("impact", base.impact, calls),
        hitl2=_wrap("hitl2", base.hitl2, calls),
    )
    orch = Orchestrator(agents=agents, spec=_spec_without_consistency())
    out = orch.run(_DOC)

    assert out == _DOC
    assert calls["consistency"] == 0
    assert calls["impact"] == 1
    assert calls["hitl2"] == 1
