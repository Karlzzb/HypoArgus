"""线路 2 · 开药 Agent 测试（issue #5、PRD §5、ADR-0002/0007/0008/0011）。

行为级黑盒测试（PRD «Testing Decisions»）：通过公共 seam（``hypothesize`` 纯函数 +
``HypothesisLlmClient`` + ``RetrievalLayer``）驱动「投机生成 → 逐条取证」流程，
断言假设落 ``supported / doubtful / refuted``、覆盖范围、不依赖体检结论、不重写原文、
异常/超时不卡死流程。

``FakeHypothesisLlmClient`` 与 ``create_mock_retrieval_layer`` 共同保证离线、确定、可断言。
"""

from __future__ import annotations

from agents.hypothesis import (
    FakeHypothesisLlmClient,
    HypothesisConcludeStep,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisSearchStep,
    HypothesisStatus,
    HypothesisVerdict,
    hypothesize,
)
from domain import ArgumentationNode, NodeType
from infra.retrieval import RetrievalKind, create_mock_retrieval_layer


def _node(
    node_id: str = "n0",
    node_type: NodeType = NodeType.EVIDENCE,
    paragraph_id: str = "p0001",
    content: str = "原文论据",
) -> ArgumentationNode:
    return ArgumentationNode(
        node_id=node_id,
        node_type=node_type,
        paragraph_id=paragraph_id,
        content=content,
    )


def test_hypothesize_single_evidence_propose_then_verify_supported():
    """生成一条对立假设 → 取证结论 supported → candidate_hypotheses 恰一条 supported。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [
            HypothesisProposal(
                text="对立假设：数据应取次年口径",
                relation=HypothesisRelation.OPPOSE,
                confidence=0.8,
            )
        ],
        verify_factory=lambda text, obs: HypothesisConcludeStep(
            verdict=HypothesisVerdict.SUPPORTED, reasoning="检索支撑假设"
        ),
    )
    retrieval = create_mock_retrieval_layer()

    updates = hypothesize(tree, llm, retrieval)

    assert set(updates) == {"n0"}
    node = updates["n0"]
    assert node.content == "原文论据"  # 不改原文
    assert len(node.candidate_hypotheses) == 1
    h = node.candidate_hypotheses[0]
    assert h.text == "对立假设：数据应取次年口径"
    assert h.relation is HypothesisRelation.OPPOSE
    assert h.status is HypothesisStatus.SUPPORTED
    assert h.confidence == 0.8
    assert h.hypothesis_id  # 稳定非空 id（供 #9/#10 采纳链引用）


# --------------------------------------------------------------------------- #
# 取证三态
# --------------------------------------------------------------------------- #


def test_hypothesize_doubtful_and_refuted_verdicts():
    """两条假设分别取证为 doubtful / refuted → candidate_hypotheses 状态正确。"""

    tree = [_node()]
    verdicts = {
        "递进假设": HypothesisVerdict.DOUBTFUL,
        "扩展假设": HypothesisVerdict.REFUTED,
    }
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [
            HypothesisProposal(text="递进假设", relation=HypothesisRelation.ADVANCE),
            HypothesisProposal(text="扩展假设", relation=HypothesisRelation.EXPAND),
        ],
        verify_factory=lambda text, obs: HypothesisConcludeStep(
            verdict=verdicts[text]
        ),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    hs = {h.text: h for h in updates["n0"].candidate_hypotheses}
    assert hs["递进假设"].status is HypothesisStatus.DOUBTFUL
    assert hs["扩展假设"].status is HypothesisStatus.REFUTED


# --------------------------------------------------------------------------- #
# 覆盖范围（ADR-0002 成本闸）
# --------------------------------------------------------------------------- #


def _full_tree() -> list[ArgumentationNode]:
    """main_claim / sub_claim / evidence / qualification / background 各一。"""

    return [
        ArgumentationNode(node_id="m", node_type=NodeType.MAIN_CLAIM, paragraph_id="p1", content="主论点"),
        ArgumentationNode(node_id="s", node_type=NodeType.SUB_CLAIM, paragraph_id="p2", content="分论点"),
        ArgumentationNode(node_id="e", node_type=NodeType.EVIDENCE, paragraph_id="p3", content="论据"),
        ArgumentationNode(node_id="q", node_type=NodeType.QUALIFICATION, paragraph_id="p4", content="限定"),
        ArgumentationNode(node_id="b", node_type=NodeType.BACKGROUND, paragraph_id="p5", content="背景"),
    ]


def test_hypothesize_covers_evidence_and_sub_claim_skips_rest():
    """只覆盖 evidence/sub_claim；main_claim/qualification/background 不在 updates 中。"""

    tree = _full_tree()
    proposed: list[str] = []

    def propose(node):
        proposed.append(node.node_id)
        return [HypothesisProposal(text=f"针对{node.node_id}", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(
        propose_factory=propose,
        verify_factory=lambda text, obs: HypothesisConcludeStep(
            verdict=HypothesisVerdict.SUPPORTED
        ),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert set(updates) == {"s", "e"}
    assert proposed == ["s", "e"]  # 未对 m/q/b 调用 propose


def test_hypothesize_confidence_does_not_affect_verdict():
    """confidence 仅排序展示、不参与裁决：低 confidence 仍可 supported、高仍可 refuted。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [
            HypothesisProposal(text="低置信但成立", relation=HypothesisRelation.OPPOSE, confidence=0.1),
            HypothesisProposal(text="高置信但被推翻", relation=HypothesisRelation.ADVANCE, confidence=0.9),
        ],
        verify_factory=lambda text, obs: HypothesisConcludeStep(
            verdict=HypothesisVerdict.SUPPORTED
            if text == "低置信但成立"
            else HypothesisVerdict.REFUTED
        ),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    hs = {h.text: h for h in updates["n0"].candidate_hypotheses}
    assert hs["低置信但成立"].status is HypothesisStatus.SUPPORTED
    assert hs["低置信但成立"].confidence == 0.1
    assert hs["高置信但被推翻"].status is HypothesisStatus.REFUTED
    assert hs["高置信但被推翻"].confidence == 0.9


# --------------------------------------------------------------------------- #
# 无假设 = 空数组（ADR-0008）+ 生成异常兜底
# --------------------------------------------------------------------------- #


def test_hypothesize_no_proposals_yields_empty_candidate_list():
    """LLM 生成 0 条假设 → candidate_hypotheses 为空数组（非 None、非第四态）。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient()  # 默认 propose → []
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].candidate_hypotheses == []


def test_hypothesize_propose_exception_yields_empty_no_hang():
    """propose 抛异常 → 该节点无假设（空列表）、流程继续（不抛出、不卡死）。"""

    tree = [_node(), _node(node_id="n1")]
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: (_ for _ in ()).throw(RuntimeError("LLM 不可用")),
        verify_factory=lambda text, obs: HypothesisConcludeStep(
            verdict=HypothesisVerdict.SUPPORTED
        ),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].candidate_hypotheses == []
    assert updates["n1"].candidate_hypotheses == []


# --------------------------------------------------------------------------- #
# 取证兜底：异常/合规/结构非法/迭代硬上限 → doubtful（≠ refuted）、有界不卡死
# --------------------------------------------------------------------------- #


def _single_propose(text: str = "假设", relation: HypothesisRelation = HypothesisRelation.OPPOSE):
    return lambda node: [HypothesisProposal(text=text, relation=relation)]


def test_hypothesize_verify_llm_exception_lands_doubtful():
    """取证 LLM 抛异常 → 假设 doubtful、流程继续（不卡死）。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(),
        verify_factory=lambda text, obs: (_ for _ in ()).throw(RuntimeError("LLM 不可用")),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.DOUBTFUL


def test_hypothesize_retrieval_compliance_violation_lands_doubtful():
    """取证检索合规违规（非白名单域名）→ ComplianceError → 假设 doubtful（非 refuted）。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(),
        verify_script=[
            HypothesisSearchStep(query="q", channel=RetrievalKind.NETWORK, domain="evil.example.com"),
            HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
        ],
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.DOUBTFUL


def test_hypothesize_malformed_verify_step_lands_doubtful():
    """取证返回非 union 成员（结构非法）→ 假设 doubtful。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(),
        verify_factory=lambda text, obs: "garbage",  # type: ignore[return-value]
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.DOUBTFUL


def test_hypothesize_iteration_cap_lands_doubtful_and_is_bounded():
    """取证永不结论（一直检索）→ 迭代硬上限触发 → doubtful；有界、不卡死。"""

    tree = [_node()]
    always_search = lambda text, obs: HypothesisSearchStep(  # noqa: E731
        query=f"q{len(obs)}", channel=RetrievalKind.NETWORK, domain="stats.example.com"
    )
    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(), verify_factory=always_search
    )
    updates = hypothesize(tree, llm, retrieval, max_iterations=3)
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.DOUBTFUL
    assert len(retrieval.requests) == 3  # 恰好 max_iterations 次，不无限


# --------------------------------------------------------------------------- #
# 取证检索词自动调整 + 知识库通道 + observations 累积
# --------------------------------------------------------------------------- #


class _RecordingRetrieval:
    """记录请求、委托 Mock：让单测断言取证发出了哪些检索请求。"""

    def __init__(self, inner):
        self._inner = inner
        self.requests = []

    def retrieve(self, request):
        self.requests.append(request)
        return self._inner.retrieve(request)


def test_hypothesize_verify_adjusts_query_and_accumulates_observations():
    """取证 ReAct 自动调整检索词：两次检索 query 不同，observations 累积后结论。"""

    tree = [_node()]
    seen_lengths: list[int] = []

    def verify(text, observations):
        seen_lengths.append(len(observations))
        if len(observations) == 0:
            return HypothesisSearchStep(query="第一版检索词", channel=RetrievalKind.NETWORK, domain="who.int")
        if len(observations) == 2:  # 每次 Mock 返回 2 条
            return HypothesisSearchStep(query="调整后的检索词", channel=RetrievalKind.NETWORK, domain="who.int")
        return HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED)

    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(), verify_factory=verify
    )
    updates = hypothesize(tree, llm, retrieval)
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.SUPPORTED
    assert [r.query for r in retrieval.requests] == ["第一版检索词", "调整后的检索词"]
    # LLM 每轮看到前几轮检索累积的 observations（0 → 2 → 4）。
    assert seen_lengths == [0, 2, 4]


def test_hypothesize_verify_knowledge_base_channel():
    """取证可用知识库通道：用授权 user_id 构造请求、取证后结论。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=_single_propose(),
        verify_script=[
            HypothesisSearchStep(query="内部资料", channel=RetrievalKind.KNOWLEDGE_BASE, user_id="analyst-1"),
            HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
        ],
    )
    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    updates = hypothesize(tree, llm, retrieval)
    assert updates["n0"].candidate_hypotheses[0].status is HypothesisStatus.SUPPORTED
    req = retrieval.requests[0]
    assert req.kind is RetrievalKind.KNOWLEDGE_BASE
    assert req.user_id == "analyst-1"


# --------------------------------------------------------------------------- #
# 不改原文 + 不改输入树 + 一假设一关系（结构保证）
# --------------------------------------------------------------------------- #


def test_hypothesize_does_not_rewrite_content_or_status():
    """开药绝不改原文与体检状态：content 与 status 与输入逐字节一致。"""

    tree = _full_tree()
    before = {n.node_id: (n.content, n.status) for n in tree}
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)],
        verify_factory=lambda text, obs: HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    for nid, node in updates.items():
        assert node.content == before[nid][0]
        assert node.status == before[nid][1]


def test_hypothesize_does_not_mutate_input_tree():
    """返回新实例；输入树节点不变（content/status/candidate_hypotheses 均原样）。"""

    tree = _full_tree()
    originals = {n.node_id: n.model_copy(deep=True) for n in tree}
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)],
        verify_factory=lambda text, obs: HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
    )
    hypothesize(tree, llm, create_mock_retrieval_layer())
    for n in tree:
        assert n == originals[n.node_id], f"输入树被改写：{n.node_id}"


def test_hypothesize_each_hypothesis_carries_single_relation():
    """一假设一关系（ADR-0007）：混合意图以多条假设表达，各钉定单一 relation。"""

    tree = [_node()]
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda node: [
            HypothesisProposal(text="对立项", relation=HypothesisRelation.OPPOSE),
            HypothesisProposal(text="递进项", relation=HypothesisRelation.ADVANCE),
            HypothesisProposal(text="扩展项", relation=HypothesisRelation.EXPAND),
        ],
        verify_factory=lambda text, obs: HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
    )
    updates = hypothesize(tree, llm, create_mock_retrieval_layer())
    relations = [h.relation for h in updates["n0"].candidate_hypotheses]
    assert relations == [
        HypothesisRelation.OPPOSE,
        HypothesisRelation.ADVANCE,
        HypothesisRelation.EXPAND,
    ]
    # hypothesis_id 稳定、同输入再跑一次结果一致（可重算、幂等链前提）。
    again = hypothesize(tree, FakeHypothesisLlmClient(
        propose_factory=lambda node: [
            HypothesisProposal(text="对立项", relation=HypothesisRelation.OPPOSE),
            HypothesisProposal(text="递进项", relation=HypothesisRelation.ADVANCE),
            HypothesisProposal(text="扩展项", relation=HypothesisRelation.EXPAND),
        ],
        verify_factory=lambda text, obs: HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED),
    ), create_mock_retrieval_layer())
    assert [h.hypothesis_id for h in again["n0"].candidate_hypotheses] == [
        h.hypothesis_id for h in updates["n0"].candidate_hypotheses
    ]


def test_hypothesize_does_not_read_verification_status():
    """乐观并行（ADR-0002）：生成不读体检结论——节点 status 不影响生成/取证。"""

    tree = [
        ArgumentationNode(node_id="ok", node_type=NodeType.EVIDENCE, paragraph_id="p1", content="论据"),
        ArgumentationNode(
            node_id="bad",
            node_type=NodeType.EVIDENCE,
            paragraph_id="p2",
            content="论据2",
            # 体检可能已判 error，但开药仍照常生成与取证（不依赖该结论）。
        ),
    ]
    seen_nodes: list[str] = []

    def propose(node):
        seen_nodes.append(node.node_id)
        return [HypothesisProposal(text=f"h-{node.node_id}", relation=HypothesisRelation.OPPOSE)]

    def verify(text, obs):
        # 取证只看假设文本与 observations，不看节点 status。
        return HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED)

    updates = hypothesize(
        tree,
        FakeHypothesisLlmClient(propose_factory=propose, verify_factory=verify),
        create_mock_retrieval_layer(),
    )
    assert seen_nodes == ["ok", "bad"]
    for nid in ("ok", "bad"):
        assert updates[nid].candidate_hypotheses[0].status is HypothesisStatus.SUPPORTED
