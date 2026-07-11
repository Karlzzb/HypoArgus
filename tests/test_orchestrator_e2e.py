"""端到端骨架测试（tracer bullet #1）。

黑盒外部行为验证（PRD «Testing Decisions»）：输入纯文本 → 流水线流转 → 终稿文本。
核心断言：无采纳改动时，终稿与原始输入逐字节完全一致（含空行/缩进/换行/末尾空格）。
并验证流水线单向推进、桩环节不产生打回或重调度（每个桩仅被调用一次）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hypoargus.agents import Agents, create_real_agents, create_stub_agents
from hypoargus.domain import NodeType
from hypoargus.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    ReparentOp,
)
from hypoargus.hypothesis import (
    FakeHypothesisLlmClient,
    HypothesisConcludeStep,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisVerdict,
)
from hypoargus.orchestrator import Orchestrator
from hypoargus.parser import (
    FakeLlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
)
from hypoargus.retrieval import RetrievalKind, create_mock_retrieval_layer
from hypoargus.verification import (
    ConcludeStep,
    FakeVerifyLlmClient,
    SearchStep,
    VerifyVerdict,
)


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
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", node_type=NodeType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", node_type=NodeType.EVIDENCE, parent_index=1
        ),
    ]
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=proposals)),
        hitl1_gate=_skip_gate(),
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def test_real_parse_hitl1_edits_do_not_break_byte_identity():
    """HITL-1 结构编辑（reparent）改树形不改文本 → 终稿仍逐字节等于原文。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    proposals = [
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", node_type=NodeType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", node_type=NodeType.EVIDENCE, parent_index=0
        ),
    ]
    # reparent n0001（p0002 的分论点）提为根——树形变化、文本不动、无节点进入 adopted。
    edit_gate = FakeHitl1Gate(
        Hitl1Decision(
            action=Hitl1Action.EDIT,
            ops=[ReparentOp(node_id="n0001", new_parent_id=None)],
        )
    )
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=proposals)),
        hitl1_gate=edit_gate,
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc


def _cycle_factory():
    """返回一个 factory：对每段都提议一个互相成环的节点。"""

    def factory(paragraphs: list[ParagraphView]) -> ParseResult:
        nodes = [
            ParsedNodeProposal(
                paragraph_id=p.paragraph_id,
                node_type=NodeType.SUB_CLAIM,
                parent_index=(i + 1) % max(len(paragraphs), 1),
            )
            for i, p in enumerate(paragraphs)
        ]
        return ParseResult(nodes=nodes)

    return factory


# --------------------------------------------------------------------------- #
# 真实体检接入（issue #4 集成）
#
# create_real_agents 在给出 verify_llm + retrieval 时把体检桩替换为真实 ReAct 实现。
# 体检只写回节点状态、不改 content、无人采纳 → 终稿逐字节等于原文的承诺继续成立。
# --------------------------------------------------------------------------- #


def _three_core_proposals() -> list[ParsedNodeProposal]:
    """主论点 → 分论点 → 论据（三段、各一核心节点）。"""

    return [
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", node_type=NodeType.SUB_CLAIM, parent_index=0
        ),
        ParsedNodeProposal(
            paragraph_id="p0003", node_type=NodeType.EVIDENCE, parent_index=1
        ),
    ]


def _search_then_credible_factory():
    """每节点：一次网络检索 → 结论 credible（按 node_id 记录调用次数）。"""

    state: dict[str, int] = {}

    def factory(node, observations):
        count = state.get(node.node_id, 0)
        state[node.node_id] = count + 1
        if count == 0:
            return SearchStep(
                query=node.content,
                channel=RetrievalKind.NETWORK,
                domain="stats.example.com",
            )
        return ConcludeStep(verdict=VerifyVerdict.CREDIBLE)

    return factory


def test_real_verify_wired_core_nodes_get_verdicts_byte_identity():
    """真实体检接入：三核心节点各落 credible，终稿逐字节等于原文（无采纳改动）。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    record: dict = {}
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(factory=_search_then_credible_factory()),
        retrieval=create_mock_retrieval_layer(),
    )

    def wrapped_verify(tree):
        updates = agents.verification(tree)
        record.update(updates)
        return updates

    orch = Orchestrator(agents=replace(agents, verification=wrapped_verify))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：体检只动状态、不动文本。
    assert set(record) == {"n0000", "n0001", "n0002"}
    assert all(v.status.value == "credible" for v in record.values())


def test_real_verify_all_error_still_byte_identity_no_hang():
    """体检全程异常 → 三核心节点落 error，流水线仍推进至终稿逐字节还原（不卡死）。"""

    def always_throws(node, observations):
        raise RuntimeError("体检 LLM 不可用")

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(factory=always_throws),
        retrieval=create_mock_retrieval_layer(),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)

    assert out == doc  # 异常兜底：节点落 error 不卡死、无人采纳 → 逐字节还原。


# --------------------------------------------------------------------------- #
# 真实开药接入（issue #5 集成）
#
# create_real_agents 在给出 hypothesis_llm + retrieval 时把开药桩替换为真实
# 「投机生成 + 逐条取证」实现。开药只写回 candidate_hypotheses、不改 content/status、
# 无人采纳 → 终稿逐字节等于原文的承诺继续成立。与体检乐观并行（不读体检结论，
# ADR-0002），本集成用桩体检（{}）以隔离观察开药线路行为。
# --------------------------------------------------------------------------- #


def _sub_claim_evidence_proposals() -> list[ParsedNodeProposal]:
    """分论点 → 论据（两段、各一核心节点，均在开药覆盖范围内）。"""

    return [
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.SUB_CLAIM),
        ParsedNodeProposal(
            paragraph_id="p0002", node_type=NodeType.EVIDENCE, parent_index=0
        ),
    ]


def test_real_hypothesis_wired_nodes_get_hypotheses_byte_identity():
    """真实开药接入：覆盖节点各产假设，终稿逐字节等于原文（无人采纳）。"""

    doc = "分论点。\n\n论据。\n".encode()
    record: dict = {}
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda node: [
                HypothesisProposal(
                    text=f"针对{node.node_id}的对立假设",
                    relation=HypothesisRelation.OPPOSE,
                )
            ],
            verify_factory=lambda text, obs: HypothesisConcludeStep(
                verdict=HypothesisVerdict.SUPPORTED
            ),
        ),
        retrieval=create_mock_retrieval_layer(),
    )

    def wrapped_hypothesis(tree):
        updates = agents.hypothesis(tree)
        record.update(updates)
        return updates

    orch = Orchestrator(agents=replace(agents, hypothesis=wrapped_hypothesis))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：开药只贴 candidate_hypotheses、不动文本。
    assert set(record) == {"n0000", "n0001"}  # 仅 sub_claim/evidence 被开药覆盖
    for node in record.values():
        assert len(node.candidate_hypotheses) == 1
        assert node.candidate_hypotheses[0].relation is HypothesisRelation.OPPOSE


def test_real_hypothesis_all_verify_failure_still_byte_identity_no_hang():
    """开药取证全程异常 → 假设落 doubtful，流水线仍推进至终稿逐字节还原（不卡死）。"""

    def verify_always_throws(text, observations):
        raise RuntimeError("取证 LLM 不可用")

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda node: [
                HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)
            ],
            verify_factory=verify_always_throws,
        ),
        retrieval=create_mock_retrieval_layer(),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)

    assert out == doc  # 取证兜底：假设 doubtful 不卡死、无人采纳 → 逐字节还原。


def test_real_hypothesis_parallel_with_verification_byte_identity():
    """体检 ∥ 开药 同时真实接入：乐观并行执行不卡死、终稿逐字节等于原文。

    开药不读体检结论（ADR-0002）：两线路各从同一棵 hitl-1 输出树出发、互不依赖。
    字段级合流（同节点 status 与 candidate_hypotheses 共存）由双轨合并算子 #6 负责，
    本测试只断言并行接入不破坏字节级承诺、不卡死。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE)
        ),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda node: [
                HypothesisProposal(text="x", relation=HypothesisRelation.EXPAND)
            ],
            verify_factory=lambda text, obs: HypothesisConcludeStep(
                verdict=HypothesisVerdict.SUPPORTED
            ),
        ),
        retrieval=create_mock_retrieval_layer(),
    )
    orch = Orchestrator(agents=agents)
    out = orch.run(doc)

    assert out == doc  # 两线路并行、无人采纳 → 逐字节还原。
