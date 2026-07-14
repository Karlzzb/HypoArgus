"""拓扑 seam 测试：PipelineSpec 数据驱动布线 + 自定义拓扑（issue #11 后置重构 · B）。

验证默认 :func:`default_pipeline` 复刻新固定拓扑（行为零变化），且传入另一种 spec
（省略 ``hypothesis_propose`` / ``judgment`` 并相应重连 deps）形成「第二种 adapter」——使
拓扑 seam 为**真 seam** 而非假 seam（deep-module 原则：two adapters means a real seam）。
"""

from __future__ import annotations

from dataclasses import replace

from agents.assembly import create_stub_agents
from runtime.orchestrator import Orchestrator, default_pipeline

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _spec_without_hypothesis_propose():
    """省略 hypothesis_propose：retrieval 直接接 hitl1（开药线路不参与流水线）。

    judgment 仍依赖 retrieval，故 judgment 随 retrieval 下移；rewrite_loop 接 judgment、
    hitl2 接 rewrite_loop。开药智能体绝不被调用。
    """

    s = {st.name: st for st in default_pipeline()}
    return (
        s["parse+partition"], s["hitl1"],
        replace(s["retrieval"], deps=("hitl1",)),
        s["judgment"], s["rewrite_loop"], s["hitl2"],
    )


def _spec_without_judgment():
    """省略 judgment：rewrite_loop 直接接 retrieval（裁决线路不参与流水线）。

    retrieval 产空 citations、judgment 不跑 → 无裁决 → 无 supported 假说 → rewrite_loop
    不触达任何段 → proposed_rewrites 空 → hitl2 PASS → 逐字节还原。
    """

    s = {st.name: st for st in default_pipeline()}
    return (
        s["parse+partition"], s["hitl1"], s["hypothesis_propose"],
        s["retrieval"],
        replace(s["rewrite_loop"], deps=("retrieval",)),
        s["hitl2"],
    )


def _wrap(name, fn, calls):
    def inner(*a, **kw):
        calls[name] += 1
        return fn(*a, **kw)

    return inner


def test_default_spec_replicates_fixed_topology_byte_identity():
    """默认 spec 与固定拓扑行为同构：无触达 → 终稿逐字节等于原文。"""

    orch = Orchestrator()  # 默认 agents + 默认 spec
    assert orch.run(_DOC) == _DOC


def test_default_pipeline_is_immutable_tuple_of_frozen_specs():
    """default_pipeline 返回不可变 tuple、各 StageSpec frozen——拓扑数据不可变。

    Slice 6（ADR-0017）后，writeback 节点裁撤、终稿改由 rewrite_loop（逐段提议重写）+
    hitl2（确认 / 编辑 / 驳回后拼接）落地。故默认拓扑为 7 个 stage：

    ``parse+partition → hitl1 → hypothesis_propose → retrieval → judgment
    → rewrite_loop → hitl2``
    """

    spec = default_pipeline()
    assert isinstance(spec, tuple)
    assert len(spec) == 7
    names = [s.name for s in spec]
    assert names == [
        "parse+partition", "hitl1", "hypothesis_propose", "retrieval",
        "judgment", "rewrite_loop", "hitl2",
    ]
    # 首段 deps 为空（接 START）——partition+parse 合并后单节点起步。
    assert spec[0].name == "parse+partition"
    assert spec[0].deps == ()
    # hitl1 紧随 parse+partition。
    hitl1 = next(s for s in spec if s.name == "hitl1")
    assert hitl1.deps == ("parse+partition",)
    # hitl1 为 partition 确认闸门 + 有界打回（ADR-0018）：条件路由 seam 表达受控打回边
    # hitl1 → parse+partition；max_replays 为打回上限（驱动图 recursion 预算缩放）。
    assert hitl1.route is not None
    assert hitl1.max_replays == 3
    # hypothesis_propose 紧随 hitl1（仅 propose、pending；取证移至 judgment）。
    propose = next(s for s in spec if s.name == "hypothesis_propose")
    assert propose.deps == ("hitl1",)
    # retrieval 紧随 hypothesis_propose（批量检索桩·Slice 4）。
    retrieval = next(s for s in spec if s.name == "retrieval")
    assert retrieval.deps == ("hypothesis_propose",)
    # judgment 五合一节点（ADR-0019）紧随 retrieval。
    judgment = next(s for s in spec if s.name == "judgment")
    assert judgment.deps == ("retrieval",)
    # rewrite_loop 紧随 judgment（逐段提议重写·Slice 6）。
    rewrite_loop = next(s for s in spec if s.name == "rewrite_loop")
    assert rewrite_loop.deps == ("judgment",)
    # hitl2 接 rewrite_loop（终稿文本确认闸门·Slice 6 重定位）。
    hitl2 = next(s for s in spec if s.name == "hitl2")
    assert hitl2.deps == ("rewrite_loop",)
    # writeback 已裁撤（终稿在 hitl2 落地）。
    assert "writeback" not in names
    # 其余 stage 无条件路由（严格单向；唯 hitl1 有受控打回）。
    assert [s.name for s in spec if s.route is not None] == ["hitl1"]


def test_custom_spec_dropping_hypothesis_propose_skips_it():
    """省略 hypothesis_propose 的 spec：retrieval 直接接 hitl1，开药智能体绝不被调用、
    流水线仍推进至终稿逐字节还原。"""

    base = create_stub_agents()
    calls = {"hypothesis_propose": 0, "retrieval": 0, "judgment": 0}
    agents = replace(
        base,
        hypothesis_propose=_wrap("hypothesis_propose", base.hypothesis_propose, calls),
        retrieval=_wrap("retrieval", base.retrieval, calls),
        judgment=_wrap("judgment", base.judgment, calls),
    )
    orch = Orchestrator(agents=agents, spec=_spec_without_hypothesis_propose())
    out = orch.run(_DOC)

    assert out == _DOC  # 无人触达 → 逐字节还原
    assert calls["hypothesis_propose"] == 0  # 开药线路被拓扑省略、绝不被调用
    assert calls["retrieval"] == 1
    assert calls["judgment"] == 1


def test_custom_spec_dropping_judgment_skips_it():
    """省略 judgment 的 spec：rewrite_loop 直接接 retrieval，裁决智能体绝不被调用、
    流水线仍推进至终稿逐字节还原。"""

    base = create_stub_agents()
    calls = {"judgment": 0, "hitl2": 0, "retrieval": 0}
    agents = replace(
        base,
        judgment=_wrap("judgment", base.judgment, calls),
        hitl2=_wrap("hitl2", base.hitl2, calls),
        retrieval=_wrap("retrieval", base.retrieval, calls),
    )
    orch = Orchestrator(agents=agents, spec=_spec_without_judgment())
    out = orch.run(_DOC)

    assert out == _DOC
    assert calls["judgment"] == 0  # 裁决线路被拓扑省略、绝不被调用
    assert calls["hitl2"] == 1
    assert calls["retrieval"] == 1
