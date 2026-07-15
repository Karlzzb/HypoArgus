"""端到端骨架测试（tracer bullet #1）。

黑盒外部行为验证（PRD «Testing Decisions»）：输入纯文本 → 流水线流转 → 终稿文本。
核心断言：无采纳改动时，终稿与原始输入逐字节完全一致（含空行/缩进/换行/末尾空格）。
并验证流水线单向推进、桩环节不产生打回或重调度（每个桩仅被调用一次）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agents.assembly import Agents, create_real_agents, create_stub_agents
from agents.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    ReparentOp,
)
from agents.hitl2 import (
    ConfirmRewriteOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
)
from agents.hypothesis import (
    FakeHypothesisLlmClient,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisStatus,
)
from agents.judgment import (
    ArgumentVerdictEntry,
    FakeJudgmentLlmClient,
    HypothesisVerdictEntry,
    JudgmentArgumentVerdict,
    JudgmentHypothesisVerdict,
    JudgmentResult,
)
from agents.parser import (
    FakeLlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
)
from agents.rewrite_loop import FakeRewriteLlmClient
from domain import ArgumentType
from runtime.orchestrator import Orchestrator


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
        "hypothesis_propose": 0,
        "retrieval": 0,
        "judgment": 0,
        "rewrite_loop": 0,
        "hitl2": 0,
    }

    def wrap(name, fn):
        def inner(*a, **kw):
            calls[name] += 1
            return fn(*a, **kw)

        return inner

    agents = Agents(
        parse=wrap("parse", base.parse),
        hitl1=wrap("hitl1", base.hitl1),
        hypothesis_propose=wrap("hypothesis_propose", base.hypothesis_propose),
        retrieval=wrap("retrieval", base.retrieval),
        judgment=wrap("judgment", base.judgment),
        rewrite_loop=wrap("rewrite_loop", base.rewrite_loop),
        hitl2=wrap("hitl2", base.hitl2),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)
    assert out == doc
    # 每个环节恰好一次：证明单向推进、无打回。
    assert calls == {k: 1 for k in calls}, f"调用次数异常：{calls}"


def test_e2e_final_doc_reaches_end():
    """流水线推进至终稿：final_document 非空且等于原文（无任何改动）。"""

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


# --------------------------------------------------------------------------- #
# 贯穿 state：session_context + query_time_range（ADR-0021 / PRD §17·Slice 1）
#
# session_context 由入口注入（与 original_doc 同入 START）、全链只读；query_time_range
# 由 parse+partition 注桩（默认 2025–2026）。二者须贯穿到 END、且不破坏「无触达段终稿
# 逐字节等于原文」的 tracer bullet 承诺。
# --------------------------------------------------------------------------- #


def test_e2e_default_session_context_keeps_byte_identity(sample_doc):
    """未显式注入 session_context 时用确定性桩，流水线仍逐字节等于原文。"""

    _name, doc = sample_doc
    orch = Orchestrator()
    assert orch.run(doc) == doc


def test_e2e_injected_session_context_reaches_end_and_keeps_byte_identity():
    """显式注入 session_context 不破坏字节级承诺，且贯穿到 END。"""

    import datetime

    from domain import DEFAULT_QUERY_TIME_RANGE, SessionContext

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    sc = SessionContext(
        session_id="s1",
        user_id="u1",
        current_time=datetime.datetime(2026, 7, 13, 9, 0, 0),
        user_prompt="精简冗余论据",
    )
    orch = Orchestrator()
    report = orch.run_with_report(doc, session_context=sc)
    assert report.final_document == doc  # 贯穿背景不触达原文 → 逐字节还原
    assert report.errors == []

    # 贯穿到 END：经装配图 invoke 取终态 state，断言两 channel 落地（state-channel seam）。
    state = orch.graph.invoke({"original_doc": doc, "session_context": sc})
    assert state["session_context"] == sc
    assert state["query_time_range"] == DEFAULT_QUERY_TIME_RANGE


# --------------------------------------------------------------------------- #
# 批量检索节点（PRD §8 / ADR-0019 · Slice 4 · 当前伪代码桩）
#
# retrieval 节点紧随 hypothesis_propose：批量接收 argument_tree + hypotheses +
# query_time_range + session_context，统一写回 citations channel。当前为伪代码桩——
# 不真实检索、产空 citations；session_context / query_time_range 被读取但不触发联网
# （真实后端后续切片接入，infra.retrieval 接口层不变）。桩路径下终稿对未触达段
# 逐字节等于原文。
# --------------------------------------------------------------------------- #


def test_e2e_retrieval_stub_empty_citations_byte_identity():
    """retrieval 桩：产空 citations、贯穿背景不触达原文 → 终稿逐字节等于原文、无 errors。

    citations channel 落地为空 dict（单写者=retrieval、reducer=_merge_dict）；
    桩不触达任何段，故 tracer bullet 的字节级承诺继续成立。
    """

    import datetime

    from domain import DEFAULT_QUERY_TIME_RANGE, SessionContext

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    sc = SessionContext(
        session_id="s1",
        user_id="u1",
        current_time=datetime.datetime(2026, 7, 13, 9, 0, 0),
        user_prompt="精简冗余论据",
    )
    orch = Orchestrator()  # 默认全套桩（retrieval 桩产空 citations）
    report = orch.run_with_report(doc, session_context=sc)
    assert report.final_document == doc  # 桩不触达原文 → 逐字节还原
    assert report.errors == []  # 桩路径无单点波动

    # citations channel 落地为空（state-channel seam）；背景仍贯穿到 END。
    state = orch.graph.invoke({"original_doc": doc, "session_context": sc})
    assert state["citations"] == {}
    assert state["session_context"] == sc
    assert state["query_time_range"] == DEFAULT_QUERY_TIME_RANGE


def test_e2e_retrieval_node_threads_context_and_query_time_range():
    """retrieval 节点把 session_context / query_time_range / argument_tree / hypotheses 穿给检索 fn。

    用记录型 retrieval fn 替换桩，断言 build 闭包从 state 读出四类输入并下传——
    「session_context / query_time_range 被读取」的 seam 诚实性由此守住（真实后端
    后续切片接入时这些背景已就位、拓扑不动）。
    """

    import datetime

    from domain import DEFAULT_QUERY_TIME_RANGE, SessionContext

    base = create_stub_agents()
    seen: dict[str, object] = {}

    def recording_retrieval(
        argument_tree, hypotheses, query_time_range, session_context
    ):
        seen["argument_tree"] = argument_tree
        seen["hypotheses"] = hypotheses
        seen["query_time_range"] = query_time_range
        seen["session_context"] = session_context
        return {}  # 仍产空 citations，不触达原文

    agents = replace(base, retrieval=recording_retrieval)
    orch = Orchestrator(agents=agents)

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    sc = SessionContext(
        session_id="s1",
        user_id="u1",
        current_time=datetime.datetime(2026, 7, 13, 9, 0, 0),
        user_prompt="精简冗余论据",
    )
    out = orch.run(doc, session_context=sc)

    assert out == doc  # 记录型 fn 仍不触达原文 → 逐字节还原
    # 四类输入自 state 穿至检索 fn。
    assert len(seen["argument_tree"]) == 3  # parse 桩产 3 段影子节点
    assert seen["hypotheses"] == {}  # hypothesis_propose 桩产空
    assert seen["query_time_range"] == DEFAULT_QUERY_TIME_RANGE  # parse+partition 注的桩
    assert seen["session_context"] == sc  # 入口注入、贯穿到 retrieval


# --------------------------------------------------------------------------- #
# 真实解析 + 真实 HITL-1 接入（issue #2 集成）
#
# create_real_agents 把解析桩、HITL-1 桩替换为真实实现，下游仍为桩。
# 故无采纳改动 → 终稿逐字节等于原文的承诺继续成立。
# --------------------------------------------------------------------------- #


def _skip_gate() -> FakeHitl1Gate:
    return FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))


def test_real_parse_empty_llm_skip_gate_byte_identity(sample_doc):
    """空 LLM（无提议）→ 全段归影子 → 跳过 HITL-1 → 终稿逐字节等于原文。"""

    _name, doc = sample_doc
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult()),
        hitl1_gate=_skip_gate(),
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def test_real_parse_cycle_proposal_does_not_crash_pipeline():
    """LLM 给出成环 parent_index → 解析器断环、流水线仍推进至终稿逐字节还原。"""

    doc = "# 标题\n\n第一段。\n\n第二段。\n".encode()
    cycle_factory = _cycle_factory()
    agents = create_real_agents(
        llm=FakeLlmClient(factory=cycle_factory),
        hitl1_gate=_skip_gate(),
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def test_real_parse_with_proposals_skip_gate_byte_identity():
    """真实提议产出含核心节点的论证树，但跳过 HITL-1 + 下游桩 → 终稿逐字节等于原文。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    proposals = [
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", argument_type=ArgumentType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", argument_type=ArgumentType.EVIDENCE, parent_index=1
        ),
    ]
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=proposals)),
        hitl1_gate=_skip_gate(),
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def test_real_parse_hitl1_edits_do_not_break_byte_identity():
    """HITL-1 结构编辑（reparent）改树形不改文本 → 终稿仍逐字节等于原文。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    proposals = [
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", argument_type=ArgumentType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", argument_type=ArgumentType.EVIDENCE, parent_index=0
        ),
    ]
    # reparent n0001（p0002 的分论点）提为根——树形变化、文本不动、无节点进入 adopted。
    edit_gate = FakeHitl1Gate(
        Hitl1Decision(
            action=Hitl1Action.EDIT,
            ops=[ReparentOp(argument_id="n0001", new_parent_id=None)],
        )
    )
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=proposals)),
        hitl1_gate=edit_gate,
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def _cycle_factory():
    """返回一个 factory：对每段都提议一个互相成环的节点。"""

    def factory(paragraphs: list[ParagraphView]) -> ParseResult:
        arguments = [
            ParsedNodeProposal(
                paragraph_id=p.paragraph_id,
                argument_type=ArgumentType.SUB_CLAIM,
                parent_index=(i + 1) % max(len(paragraphs), 1),
            )
            for i, p in enumerate(paragraphs)
        ]
        return ParseResult(proposals=arguments)

    return factory


# --------------------------------------------------------------------------- #
# 真实开药接入（issue #5 集成 · Slice 3 重定义为仅 propose）
#
# create_real_agents 在给出 hypothesis_llm 时把开药桩替换为真实「投机生成」实现。
# 开药读 paragraph_summaries 逐 argument 调 propose、产 pending 假说、只写回
# candidate_hypotheses、不改 content/status、无人采纳 → 终稿逐字节等于原文的承诺继续成立。
# 取证（吃 citations 判终态）属 Slice 5 的 judgment 节点，不在 hypothesis_propose 内。
# --------------------------------------------------------------------------- #


def _sub_claim_evidence_proposals() -> list[ParsedNodeProposal]:
    """分论点 → 论据（两段、各一核心节点，均在开药覆盖范围内）。"""

    return [
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.SUB_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", argument_type=ArgumentType.EVIDENCE, parent_index=0
        ),
    ]


def test_real_hypothesis_wired_arguments_get_pending_hypotheses_byte_identity():
    """真实开药接入：覆盖节点各产 pending 假说，终稿逐字节等于原文（无人采纳）。"""

    doc = "分论点。\n\n论据。\n".encode()
    record: dict = {}
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda argument, summary, original_content: [
                HypothesisProposal(
                    text=f"针对{argument.argument_id}的对立假设",
                    relation=HypothesisRelation.OPPOSE,
                )
            ],
        ),
    )

    def wrapped_hypothesis_propose(argument_tree, paragraph_list):
        updates = agents.hypothesis_propose(argument_tree, paragraph_list)
        record.update(updates)
        return updates

    orch = Orchestrator(
        agents=replace(agents, hypothesis_propose=wrapped_hypothesis_propose)
    )
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：开药只贴 pending candidate_hypotheses、不动文本。
    assert set(record) == {"n0000", "n0001"}  # 仅 sub_claim/evidence 被开药覆盖
    for hypotheses in record.values():
        assert len(hypotheses) == 1
        assert hypotheses[0].relation is HypothesisRelation.OPPOSE
        assert hypotheses[0].status is HypothesisStatus.PENDING  # propose 不取证、一律 pending


def test_real_hypothesis_propose_exception_still_byte_identity_no_hang():
    """开药 propose 全程异常 → 该节点无假设（空），流水线仍推进至终稿逐字节还原（不卡死）。

    Slice 3 后 hypothesis_propose 不再取证；propose 异常即「本轮无假设」，不卡死。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda argument, summary, original_content: (
                _ for _ in ()
            ).throw(RuntimeError("propose LLM 不可用"))
        ),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)

    assert out == doc  # propose 兜底：空假设不卡死、无人采纳 → 逐字节还原。


# --------------------------------------------------------------------------- #
# 真实裁决接入（issue #4 取证 + #5 取证 + #6 merge + #7 impact + #8 consistency
# 五合一·Slice 5）
#
# create_real_agents 在给出 judgment_llm 时把裁决桩替换为真实 :func:`judge_and_adjudicate`
# （吃 citations 判 per-argument / per-hypothesis 终态、再按序调 merge/impact/consistency
# 纯函数、整树写回 argument_tree）。裁决只写回 status/merge_decision/issue_tags、不改
# content、不置 adopted → 终稿逐字节等于原文的承诺继续成立。
# --------------------------------------------------------------------------- #


def _judge_argument_verdicts_factory(
    verdicts_by_id: dict[str, JudgmentArgumentVerdict],
):
    """构造 judge_factory：对指定 argument_id 返回对应终态裁决，其余不裁决。"""

    def factory(argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range):
        return JudgmentResult(
            argument_verdicts=[
                ArgumentVerdictEntry(argument_id=aid, verdict=v)
                for aid, v in verdicts_by_id.items()
            ]
        )

    return factory


def test_real_judgment_argument_verdicts_land_in_tree_byte_identity():
    """judgment 据 citations 判 per-argument 终态 → 树 status 落终态、merge 全 KEEP、
    终稿逐字节等于原文（无人采纳）。

    FakeJudgmentLlmClient(judge_factory) 产 sub_claim credible / evidence doubtful（无假说 →
    无 supported 列）。judgment 应用 ``argument_credibility`` 后 merge 矩阵全 KEEP、无激活候选；
    impact/consistency 不动（无 error / 无多 main_claim）；无人采纳 → 终稿逐字节等于原文。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        judgment_llm=FakeJudgmentLlmClient(
            judge_factory=_judge_argument_verdicts_factory(
                {
                    "n0000": JudgmentArgumentVerdict.CREDIBLE,
                    "n0001": JudgmentArgumentVerdict.DOUBTFUL,
                }
            )
        ),
    )

    captured: dict = {}

    def wrapped_judgment(
        argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
    ):
        outcome = agents.judgment(
            argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
        )
        captured["out"] = outcome
        return outcome

    orch = Orchestrator(agents=replace(agents, judgment=wrapped_judgment))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：裁决只动 status/merge_decision、不改文本。

    arguments = {n.argument_id: n for n in captured["out"].argument_tree}
    assert arguments["n0000"].status.value == "credible"  # sub_claim 落终态
    assert arguments["n0001"].status.value == "doubtful"  # evidence 落终态
    # 无 supported 假说 → merge 矩阵全 KEEP、无激活候选。
    for nid in ("n0000", "n0001"):
        assert arguments[nid].merge_decision.action.value == "keep"
        assert arguments[nid].merge_decision.activated_hypothesis_ids == []
    # 无人被自动采纳。
    assert all(n.status.value != "adopted" for n in captured["out"].argument_tree)


def test_real_judgment_hypothesis_verdicts_trigger_merge_action_byte_identity():
    """judgment 落假说终态 → merge 矩阵命中 REPLACE：evidence doubtful × oppose supported。

    - evidence (n0001) 体检 doubtful；其 oppose 假说 judgment 判 supported →
      ``HYPOTHESIS_RELATION_TO_MERGE_ACTION[oppose]=replace``，merge 贴
      ``merge_decision.action==replace``、``activated_hypothesis_ids==[h-id]``、假说
      ``status==supported``。
    - 保守 HITL-2 全驳回（绝不自动采纳，ADR-0010）→ 终稿逐字节等于原文。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda argument, summary, original_content: (
                [HypothesisProposal(text="对立证据", relation=HypothesisRelation.OPPOSE)]
                if argument.argument_id == "n0001"
                else []
            ),
        ),
        judgment_llm=FakeJudgmentLlmClient(
            judge_factory=lambda argument_tree, hypotheses, citations, pl, sc, qtr: JudgmentResult(
                argument_verdicts=[
                    ArgumentVerdictEntry(
                        argument_id="n0001",
                        verdict=JudgmentArgumentVerdict.DOUBTFUL,
                    )
                ],
                hypothesis_verdicts=[
                    HypothesisVerdictEntry(
                        hypothesis_id=h.hypothesis_id,
                        verdict=JudgmentHypothesisVerdict.SUPPORTED,
                    )
                    for h in hypotheses.get("n0001", [])
                ],
            )
        ),
    )

    captured: dict = {}

    def wrapped_judgment(
        argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
    ):
        outcome = agents.judgment(
            argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
        )
        captured["out"] = outcome
        return outcome

    orch = Orchestrator(agents=replace(agents, judgment=wrapped_judgment))
    out = orch.run(doc)

    assert out == doc  # 保守闸门全驳回 → 无采纳 → 逐字节还原。

    arguments = {n.argument_id: n for n in captured["out"].argument_tree}
    evidence = arguments["n0001"]
    # doubtful × oppose supported → REPLACE、激活该假说。
    assert evidence.merge_decision.action.value == "replace"
    activated = evidence.merge_decision.activated_hypothesis_ids
    assert len(activated) == 1
    # 该假说终态 supported、被激活保留在 candidate_hypotheses。
    updated_hyps = captured["out"].hypotheses.get("n0001", [])
    assert len(updated_hyps) == 1
    assert updated_hyps[0].hypothesis_id == activated[0]
    assert updated_hyps[0].status is HypothesisStatus.SUPPORTED
    assert len(evidence.candidate_hypotheses) == 1
    assert evidence.candidate_hypotheses[0].status is HypothesisStatus.SUPPORTED
    # 无人被自动采纳（保守闸门驳回）。
    assert all(n.status.value != "adopted" for n in captured["out"].argument_tree)


def _weighted_three_core_proposals() -> list[ParsedNodeProposal]:
    """主论点 → 分论点 → 论据（三段、各一核心节点，带非零权重以驱动影响传导）。"""

    return [
        ParsedNodeProposal(
            paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM, argument_weight=80
        ),
        ParsedNodeProposal(
            paragraph_id="p0002",
            argument_type=ArgumentType.SUB_CLAIM,
            parent_index=0,
            argument_weight=60,
        ),
        ParsedNodeProposal(
            paragraph_id="p0003",
            argument_type=ArgumentType.EVIDENCE,
            parent_index=1,
            argument_weight=100,
        ),
    ]


def test_real_judgment_impact_propagates_invalid_up_chain_byte_identity():
    """judgment 产 evidence error → impact 后序上推 sub_claim/main_claim invalid。

    - evidence (n0002)：judgment 判 error（叶子，impact 不动）。
    - sub_claim (n0001)：唯一子 evidence error → 剩余支撑率 0 < 0.5 → invalid。
    - main_claim (n0000)：唯一子 sub_claim invalid → 0 < 0.5 → invalid（后序上推）。
    - 终稿逐字节等于原文（impact 不改 content、不置 adopted）。
    """

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_weighted_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        judgment_llm=FakeJudgmentLlmClient(
            judge_factory=_judge_argument_verdicts_factory(
                {
                    "n0000": JudgmentArgumentVerdict.CREDIBLE,
                    "n0001": JudgmentArgumentVerdict.CREDIBLE,
                    "n0002": JudgmentArgumentVerdict.ERROR,
                }
            )
        ),
    )

    captured: dict = {}

    def wrapped_judgment(
        argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
    ):
        outcome = agents.judgment(
            argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
        )
        captured["out"] = outcome
        return outcome

    orch = Orchestrator(agents=replace(agents, judgment=wrapped_judgment))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：影响传导不产文本、不置 adopted。

    arguments = {n.argument_id: n for n in captured["out"].argument_tree}
    assert arguments["n0002"].status.value == "error"  # 叶子：judgment 判决，impact 不动
    assert arguments["n0001"].status.value == "invalid"  # 子全死 → 塌方
    assert arguments["n0000"].status.value == "invalid"  # 子塌方 → 上推
    # 影响传导不替人拍板、不新建假设。
    assert all(n.status.value != "adopted" for n in captured["out"].argument_tree)
    assert all(n.merge_decision is not None for n in captured["out"].argument_tree)


def test_real_judgment_consistency_tags_multi_main_claim_byte_identity():
    """judgment 调 consistency：两 main_claim → 贴 multi_main_claim、终稿逐字节还原。

    批注（issue_tags）不进回写文本，故终稿仍逐字节等于原文。空裁决（无 status 变更）
    仍触发 consistency 的多根主论点检测——证明 consistency 在 judgment 内被调用。
    """

    doc = "主论点一。\n\n主论点二。\n".encode()
    proposals = [
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM),
        ParsedNodeProposal(paragraph_id="p0002", argument_type=ArgumentType.MAIN_CLAIM),
    ]
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=proposals)),
        hitl1_gate=_skip_gate(),
        judgment_llm=FakeJudgmentLlmClient(),  # 空裁决：不改 status，仅触发 consistency
    )

    captured: dict = {}

    def wrapped_judgment(
        argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
    ):
        outcome = agents.judgment(
            argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
        )
        captured["out"] = outcome
        return outcome

    orch = Orchestrator(agents=replace(agents, judgment=wrapped_judgment))
    out = orch.run(doc)

    assert out == doc  # 批注不进文本 → 逐字节还原。
    arguments = {n.argument_id: n for n in captured["out"].argument_tree}
    mains = [n for n in arguments.values() if n.argument_type is ArgumentType.MAIN_CLAIM]
    assert len(mains) == 2
    assert all("multi_main_claim" in n.issue_tags for n in mains)
    # 不替人拍板：无人进入 adopted。
    assert all(n.status.value != "adopted" for n in captured["out"].argument_tree)


# --------------------------------------------------------------------------- #
# 终稿确认路径（rewrite_loop + hitl2 · Slice 6 · ADR-0017）
#
# judgment 落 supported 假说 → rewrite_loop 对触达段产提议重写 → hitl2 确认 → 终稿含
# 确认文本、未触达段逐字节原文。这是「人拍板采纳提议重写」的端到端路径；与既有
# 保守全驳回路径（ConservativeHitl2Gate + rewrite_loop 桩不提议）互为对偶。
# --------------------------------------------------------------------------- #


def test_e2e_touched_confirmed_rewrite_lands_in_final_document():
    """被触达段经确认 → 终稿含确认文本；未触达段逐字节原文（Slice 6）。

    - evidence (n0001, p0002) 配对立假说、judgment 判假说 supported → p0002 触达。
    - sub_claim (n0000, p0001) 无假说 / 无命中 citations → 不触达、逐字节原文。
    - rewrite_loop 对 p0002 产提议文本 ``论据[已修订]``；hitl2 (FakeHitl2Gate) 确认 p0002
      → 终稿 p0002 用确认文本、p0001 逐字节还原。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(proposals=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda argument, summary, original_content: (
                [HypothesisProposal(text="对立证据", relation=HypothesisRelation.OPPOSE)]
                if argument.argument_id == "n0001"
                else []
            ),
        ),
        judgment_llm=FakeJudgmentLlmClient(
            judge_factory=lambda argument_tree, hypotheses, citations, pl, sc, qtr: JudgmentResult(
                argument_verdicts=[
                    ArgumentVerdictEntry(
                        argument_id="n0001",
                        verdict=JudgmentArgumentVerdict.DOUBTFUL,
                    )
                ],
                hypothesis_verdicts=[
                    HypothesisVerdictEntry(
                        hypothesis_id=h.hypothesis_id,
                        verdict=JudgmentHypothesisVerdict.SUPPORTED,
                    )
                    for h in hypotheses.get("n0001", [])
                ],
            )
        ),
        rewrite_llm=FakeRewriteLlmClient(
            propose_factory=lambda paragraph_id, paragraph_summary, original_content, arguments, citations, sc, qtr: (
                "论据[已修订]" if paragraph_id == "p0002" else None
            ),
        ),
        hitl2_gate=FakeHitl2Gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[ConfirmRewriteOp(paragraph_id="p0002")],
            )
        ),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)

    # p0001 未触达 → 逐字节原文（含段间空行尾随字节）；p0002 触达确认 → 用确认文本。
    expected = "分论点。\n\n论据[已修订]".encode()
    assert out == expected
    assert out.startswith("分论点。\n\n".encode())  # 未触达段逐字节忠实
    assert out.endswith("论据[已修订]".encode())  # 确认文本落地
