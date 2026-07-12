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
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
)
from agents.hypothesis import (
    FakeHypothesisLlmClient,
    HypothesisConcludeStep,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisVerdict,
)
from agents.parser import (
    FakeLlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
)
from agents.verification import (
    ConcludeStep,
    FakeVerifyLlmClient,
    SearchStep,
    VerifyVerdict,
)
from domain import NodeType
from infra.retrieval import RetrievalKind, create_mock_retrieval_layer
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


# --------------------------------------------------------------------------- #
# 真实双轨合并接入（issue #6 集成）
#
# create_stub_agents 已把合并桩替换为真实纯函数（#6 无 LLM/检索依赖）。
# 双线路 partial 更新经 merge_tree reducer 合流到同一节点（status 与
# candidate_hypotheses 共存），合并读 12 格矩阵裁决、贴 merge_decision、
# 裁剪假设、贴 conflict；不改 content/status、不置 adopted → 终稿逐字节等于原文。
# --------------------------------------------------------------------------- #


def test_real_merge_wired_decisions_landed_byte_identity():
    """两线路并行 → reducer 合流 → 合并读矩阵：sub_claim 冲突、evidence 替换。

    - sub_claim (n0000)：体检 credible + 开药 supported-oppose → 冲突格（CONFLICT +
      conflict 标签、对立假设保留为候选）。
    - evidence (n0001)：体检 doubtful + 开药 supported-oppose → REPLACE（假设激活）。
    - 终稿逐字节等于原文（合并不改 content、不置 adopted）。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(
                verdict=VerifyVerdict.DOUBTFUL
                if node.node_id == "n0001"
                else VerifyVerdict.CREDIBLE
            )
        ),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda node: [
                HypothesisProposal(
                    text=f"对立假设-{node.node_id}", relation=HypothesisRelation.OPPOSE
                )
            ],
            verify_factory=lambda text, obs: HypothesisConcludeStep(
                verdict=HypothesisVerdict.SUPPORTED
            ),
        ),
        retrieval=create_mock_retrieval_layer(),
    )

    captured: dict = {}

    def wrapped_merge(tree):
        out = agents.merge(tree)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, merge=wrapped_merge))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：合并只标注、不改文本、无人采纳。

    nodes = {n.node_id: n for n in captured["out"]}
    # sub_claim：credible × 对立成立 → CONFLICT + conflict 标签，对立假设保留、原文不动。
    sub = nodes["n0000"]
    assert sub.merge_decision.action.value == "conflict"
    assert "conflict" in sub.issue_tags
    assert len(sub.merge_decision.activated_hypothesis_ids) == 1
    assert len(sub.candidate_hypotheses) == 1  # 对立假设保留并列推 HITL-2
    assert sub.status.value == "credible"
    # evidence：doubtful × 对立成立 → REPLACE，假设激活、原文/状态不动。
    evi = nodes["n0001"]
    assert evi.merge_decision.action.value == "replace"
    assert len(evi.merge_decision.activated_hypothesis_ids) == 1
    assert evi.status.value == "doubtful"
    # 全员流入 HITL-2、无人被自动采纳。
    assert all(n.status.value != "adopted" for n in captured["out"])


# --------------------------------------------------------------------------- #
# 真实影响传导接入（issue #7 集成）
#
# create_stub_agents 已把影响传导桩替换为真实纯函数（#7 无 LLM/检索依赖，串行·不产文本）。
# 合并后的树经影响传导按剩余支撑率判 invalid/贴 weakening、复用既有成立假设激活；
# 不改 content/不新建假设/不置 adopted → 终稿逐字节等于原文。本集成用桩开药（{}）
# 以隔离观察影响传导：体检把叶子 evidence 判 error → sub_claim 塌方 invalid →
# main_claim 上推 invalid（后序逐层）。
# --------------------------------------------------------------------------- #


def _weighted_three_core_proposals() -> list[ParsedNodeProposal]:
    """主论点 → 分论点 → 论据（三段、各一核心节点，带非零权重以驱动影响传导）。"""

    return [
        ParsedNodeProposal(
            paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM, argument_weight=80
        ),
        ParsedNodeProposal(
            paragraph_id="p0002",
            node_type=NodeType.SUB_CLAIM,
            parent_index=0,
            argument_weight=60,
        ),
        ParsedNodeProposal(
            paragraph_id="p0003",
            node_type=NodeType.EVIDENCE,
            parent_index=1,
            argument_weight=100,
        ),
    ]


def test_real_impact_propagates_invalid_up_chain_byte_identity():
    """叶子 evidence 体检 error → sub_claim 塌方 invalid → main_claim 上推 invalid。

    - evidence (n0002)：体检 error（叶子不动，影响传导不判叶子）。
    - sub_claim (n0001)：唯一子 evidence error → 剩余支撑率 0 < 0.5 → invalid。
    - main_claim (n0000)：唯一子 sub_claim invalid → 0 < 0.5 → invalid（后序上推）。
    - 终稿逐字节等于原文（影响传导不改 content、不置 adopted）。
    """

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_weighted_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(
                verdict=VerifyVerdict.ERROR
                if node.node_id == "n0002"
                else VerifyVerdict.CREDIBLE
            )
        ),
        retrieval=create_mock_retrieval_layer(),
    )

    captured: dict = {}

    def wrapped_impact(tree):
        out = agents.impact(tree)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, impact=wrapped_impact))
    out = orch.run(doc)

    assert out == doc  # 字节级承诺：影响传导不产文本、不置 adopted。

    nodes = {n.node_id: n for n in captured["out"]}
    assert nodes["n0002"].status.value == "error"  # 叶子：体检判决，影响传导不动
    assert nodes["n0001"].status.value == "invalid"  # 子全死 → 塌方
    assert nodes["n0000"].status.value == "invalid"  # 子塌方 → 上推
    # 影响传导不替人拍板、不新建假设。
    assert all(n.status.value != "adopted" for n in captured["out"])
    assert all(n.merge_decision is not None for n in captured["out"])


def test_real_impact_all_credible_unaffected_byte_identity():
    """全核心节点体检 credible → 影响传导不动任何节点、终稿逐字节等于原文。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_weighted_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE)
        ),
        retrieval=create_mock_retrieval_layer(),
    )
    orch = Orchestrator(agents=agents)
    assert orch.run(doc) == doc  # 全可信 → 无失效/弱化 → 逐字节还原


# --------------------------------------------------------------------------- #
# 真实一致性校验接入（issue #8 集成）
#
# create_stub_agents 已把一致性校验桩替换为真实纯函数（#8 无 LLM/检索依赖，批注门禁·
# 单次扫描·只贴 issue_tags）。影响传导之后的树经一致性校验单次扫描、按段落级
# （自洽性/边界匹配）与全局（跨段论点一致/术语定义一致）规则贴 issue_tags；不改
# content/status/merge_decision、不置 adopted → 终稿逐字节等于原文（批注不影响回写）。
# --------------------------------------------------------------------------- #


def test_real_consistency_clean_tree_no_tags_byte_identity():
    """正常三核心节点树无一致性瑕疵 → 不贴批注、终稿逐字节等于原文。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_weighted_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE)
        ),
        retrieval=create_mock_retrieval_layer(),
    )
    captured: dict = {}

    def wrapped_consistency(tree):
        out = agents.consistency(tree)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, consistency=wrapped_consistency))
    out = orch.run(doc)
    assert out == doc  # 字节级承诺：一致性只贴 issue_tags、不动文本。
    # 三核心节点各占一段、单一主论点、无混段、无重复限定 → 不贴任何标签。
    assert all(n.issue_tags == [] for n in captured["out"])


def test_real_consistency_tags_issue_but_byte_identity_holds():
    """一致性校验贴批注（multi_main_claim）仍逐字节还原——批注不影响回写。

    构造两个 main_claim 的解析提议（跨段论点一致瑕疵），一致性校验在影响传导之后
    单次扫描、给两个 main_claim 贴 ``multi_main_claim``；但 issue_tags 不进回写文本，
    故终稿仍逐字节等于原文。
    """

    doc = "主论点一。\n\n主论点二。\n".encode()
    proposals = [
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.MAIN_CLAIM),
        ParsedNodeProposal(paragraph_id="p0002", node_type=NodeType.MAIN_CLAIM),
    ]
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=proposals)),
        hitl1_gate=_skip_gate(),
    )
    captured: dict = {}

    def wrapped_consistency(tree):
        out = agents.consistency(tree)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, consistency=wrapped_consistency))
    out = orch.run(doc)
    assert out == doc  # 批注不进文本 → 逐字节还原。
    nodes = {n.node_id: n for n in captured["out"]}
    mains = [n for n in nodes.values() if n.node_type is NodeType.MAIN_CLAIM]
    assert len(mains) == 2
    assert all("multi_main_claim" in n.issue_tags for n in mains)
    # 不替人拍板：无人进入 adopted。
    assert all(n.status.value != "adopted" for n in captured["out"])


# --------------------------------------------------------------------------- #
# 真实 HITL-2 修订确认硬闸门接入（issue #9 集成）
#
# create_stub_agents 的 HITL-2 桩已替换为真实 confirm + ConservativeHitl2Gate（#9）。
# 一致性校验之后的树经 HITL-2 呈现待决节点（doubtful/error/conflict + 激活候选），
# 保守闸门无待决→一键通过、有待决→全驳回（绝不自动采纳，ADR-0010）；采纳即置 adopted
# + 持久化 adopted_hypothesis_id（ADR-0011）。回写（#10）真实分流·幂等：采纳后按关系
# 缝合终稿、翻正 adopted→corrected。
# --------------------------------------------------------------------------- #


def _pending_merge_agents(hitl2_gate=None):
    """sub_claim credible（→conflict）+ evidence doubtful（→replace），均带激活候选。"""

    return create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_sub_claim_evidence_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(
                verdict=VerifyVerdict.DOUBTFUL
                if node.node_id == "n0001"
                else VerifyVerdict.CREDIBLE
            )
        ),
        hypothesis_llm=FakeHypothesisLlmClient(
            propose_factory=lambda node: [
                HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)
            ],
            verify_factory=lambda text, obs: HypothesisConcludeStep(
                verdict=HypothesisVerdict.SUPPORTED
            ),
        ),
        retrieval=create_mock_retrieval_layer(),
        hitl2_gate=hitl2_gate,
    )


def test_real_hitl2_conservative_default_rejects_all_byte_identity():
    """有待决内容 + 保守默认闸门 → 全驳回、无采纳 → 终稿逐字节等于原文。

    sub_claim（credible×对立成立→conflict）+ evidence（doubtful×对立成立→replace）均待决；
    保守闸门 DECIDE 空 ops（绝不自动采纳），HITL-2 仍被调用、收到待决呈现。
    """

    doc = "分论点。\n\n论据。\n".encode()
    agents = _pending_merge_agents()  # 默认保守闸门
    captured: dict = {}

    def wrapped_hitl2(tree, store):
        captured["in"] = tree
        out = agents.hitl2(tree, store)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, hitl2=wrapped_hitl2))
    out = orch.run(doc)

    assert out == doc  # 保守闸门全驳回 → 无采纳 → 逐字节还原。
    # HITL-2 收到待决内容（两个节点都有激活候选）。
    in_nodes = {n.node_id: n for n in captured["in"]}
    assert in_nodes["n0000"].merge_decision.activated_hypothesis_ids != []
    assert in_nodes["n0001"].merge_decision.activated_hypothesis_ids != []
    # 输出无采纳。
    assert all(n.status.value != "adopted" for n in captured["out"])
    assert all(n.adopted_hypothesis_id is None for n in captured["out"])


def test_real_hitl2_all_credible_pass_path_byte_identity():
    """全核心节点 credible → 无待决 → 保守闸门 PASS 一键通过 → 逐字节还原。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult(nodes=_weighted_three_core_proposals())),
        hitl1_gate=_skip_gate(),
        verify_llm=FakeVerifyLlmClient(
            factory=lambda node, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE)
        ),
        retrieval=create_mock_retrieval_layer(),
    )
    captured: dict = {}

    def wrapped_hitl2(tree, store):
        out = agents.hitl2(tree, store)
        captured["out"] = out
        return out

    orch = Orchestrator(agents=replace(agents, hitl2=wrapped_hitl2))
    assert orch.run(doc) == doc
    # 一键通过：无人采纳。
    assert all(n.status.value != "adopted" for n in captured["out"])


def test_real_hitl2_pass_on_pending_raises_hard_gate_e2e():
    """有待决内容时闸门返回 PASS → 硬闸门在流水线内拦截（绝不无人拍板自动采纳）。"""

    from agents.hitl2 import Hitl2GateError

    doc = "分论点。\n\n论据。\n".encode()
    # 显式注入一个越权 PASS 闸门。
    agents = _pending_merge_agents(
        hitl2_gate=FakeHitl2Gate(Hitl2Decision(action=Hitl2Action.PASS))
    )
    orch = Orchestrator(agents=agents)
    with pytest.raises(Hitl2GateError, match="硬闸门"):
        orch.run(doc)


def test_real_hitl2_adopting_gate_persists_adoption_in_pipeline():
    """采纳闸门在流水线内把激活候选置 adopted + 持久化 adopted_hypothesis_id，回写（#10）
    据此按关系分流缝合终稿、翻正 adopted→corrected（ADR-0011）。

    sub_claim（credible×对立成立→conflict）与 evidence（doubful×对立成立→replace）均被
    采纳对立假设（text="x"）；回写对立→替换原句，故终稿不再逐字节等于原文、原句消失、
    假设文本就位、两节点翻 corrected。
    """

    from agents.hitl2 import AdoptOp, Hitl2Action, Hitl2Decision, Hitl2Gate

    class _AdoptFirstGate(Hitl2Gate):
        """对每个有激活候选的待决节点，采纳其首条激活假设。"""

        def review(self, review):
            ops = []
            for n in review.nodes:
                if n.activated_hypothesis_ids:
                    ops.append(
                        AdoptOp(
                            node_id=n.node_id,
                            hypothesis_id=n.activated_hypothesis_ids[0],
                        )
                    )
            return Hitl2Decision(action=Hitl2Action.DECIDE, ops=ops)

    doc = "分论点。\n\n论据。\n".encode()
    agents = _pending_merge_agents(hitl2_gate=_AdoptFirstGate())
    captured: dict = {}

    def wrapped_hitl2(tree, store):
        out = agents.hitl2(tree, store)
        captured["out"] = out
        return out

    def wrapped_writeback(tree, store):
        out = agents.writeback(tree, store)
        captured["writeback"] = out
        return out

    orch = Orchestrator(
        agents=replace(
            agents, hitl2=wrapped_hitl2, writeback=wrapped_writeback
        )
    )
    final_doc = orch.run(doc)

    out_nodes = {n.node_id: n for n in captured["out"]}
    # HITL-2 采纳链：sub_claim（conflict）与 evidence（replace）均被采纳。
    for nid in ("n0000", "n0001"):
        assert out_nodes[nid].status.value == "adopted"
        assert out_nodes[nid].adopted_hypothesis_id is not None

    # 回写：对立→替换原句，两节点翻 corrected、终稿含假设文本、原句消失。
    wb_nodes = {n.node_id: n for n in captured["writeback"].tree}
    for nid in ("n0000", "n0001"):
        assert wb_nodes[nid].status.value == "corrected"
        assert wb_nodes[nid].adopted_hypothesis_id is not None
    assert b"x" in final_doc
    assert "分论点".encode() not in final_doc
    assert "论据".encode() not in final_doc
