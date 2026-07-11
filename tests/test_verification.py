"""线路 1 · 体检 Agent 测试（issue #4、PRD §5、ADR-0011）。

行为级黑盒测试（PRD «Testing Decisions»）：通过公共 seam（``verify`` 纯函数 +
``VerifyLlmClient`` + ``RetrievalLayer``）驱动 ReAct 循环，断言节点落 ``credible /
doubtful / error``、不重写原文、异常/超时不卡死流程。

``FakeVerifyLlmClient`` 与 ``create_mock_retrieval_layer`` 共同保证离线、确定、可断言。
"""

from __future__ import annotations

from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.retrieval import (
    RetrievalKind,
    create_mock_retrieval_layer,
)
from hypoargus.verification import (
    ConcludeStep,
    FakeVerifyLlmClient,
    SearchStep,
    VerifyVerdict,
    verify,
)


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


def test_verify_single_evidence_search_then_conclude_credible():
    """ReAct：一次网络检索 → 结论 credible。节点状态写回 ``credible``。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(
        script=[
            SearchStep(
                query="论据 事实",
                channel=RetrievalKind.NETWORK,
                domain="stats.example.com",
            ),
            ConcludeStep(verdict=VerifyVerdict.CREDIBLE, reasoning="比对一致"),
        ]
    )
    retrieval = create_mock_retrieval_layer()

    updates = verify(tree, llm, retrieval)

    assert set(updates) == {"n0"}
    assert updates["n0"].status is NodeStatus.CREDIBLE


# --------------------------------------------------------------------------- #
# 终判三态
# --------------------------------------------------------------------------- #


def test_verify_doubtful_verdict():
    """LLM 检索后结论 doubtful → 节点落 ``doubtful``。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(
        script=[
            SearchStep(query="q", channel=RetrievalKind.NETWORK, domain="stats.example.com"),
            ConcludeStep(verdict=VerifyVerdict.DOUBTFUL, reasoning="证据不足"),
        ]
    )
    updates = verify(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].status is NodeStatus.DOUBTFUL


def test_verify_error_verdict_from_conclude():
    """LLM 检索后结论 error（论据自证其伪）→ 节点落 ``error``。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(
        script=[
            SearchStep(query="q", channel=RetrievalKind.NETWORK, domain="stats.example.com"),
            ConcludeStep(verdict=VerifyVerdict.ERROR, reasoning="比对相悖"),
        ]
    )
    updates = verify(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].status is NodeStatus.ERROR


# --------------------------------------------------------------------------- #
# 检索词自动调整 + 知识库通道 + observations 累积
# --------------------------------------------------------------------------- #


class _RecordingRetrieval:
    """记录请求、委托 Mock：让单测断言体检发出了哪些检索请求。"""

    def __init__(self, inner):
        self._inner = inner
        self.requests = []

    def retrieve(self, request):
        self.requests.append(request)
        return self._inner.retrieve(request)


def test_verify_adjusts_query_across_multiple_searches():
    """ReAct 自动调整检索词：两次检索 query 不同，observations 累积后结论。"""

    tree = [_node()]
    seen_lengths: list[int] = []

    def factory(node, observations):
        seen_lengths.append(len(observations))
        if len(observations) == 0:
            return SearchStep(
                query="第一版检索词", channel=RetrievalKind.NETWORK, domain="who.int"
            )
        if len(observations) == 2:  # 每次 Mock 返回 2 条
            return SearchStep(
                query="调整后的检索词", channel=RetrievalKind.NETWORK, domain="who.int"
            )
        return ConcludeStep(verdict=VerifyVerdict.CREDIBLE)

    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    updates = verify(tree, FakeVerifyLlmClient(factory=factory), retrieval)

    assert updates["n0"].status is NodeStatus.CREDIBLE
    assert len(retrieval.requests) == 2
    assert retrieval.requests[0].query == "第一版检索词"
    assert retrieval.requests[1].query == "调整后的检索词"
    # LLM 每轮看到前几轮检索累积的 observations（0 → 2 → 4）。
    assert seen_lengths == [0, 2, 4]


def test_verify_knowledge_base_channel():
    """知识库通道：用授权 user_id 构造请求、取证后结论。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(
        script=[
            SearchStep(
                query="内部资料",
                channel=RetrievalKind.KNOWLEDGE_BASE,
                user_id="analyst-1",
            ),
            ConcludeStep(verdict=VerifyVerdict.CREDIBLE),
        ]
    )
    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    updates = verify(tree, llm, retrieval)
    assert updates["n0"].status is NodeStatus.CREDIBLE
    req = retrieval.requests[0]
    assert req.kind is RetrievalKind.KNOWLEDGE_BASE
    assert req.user_id == "analyst-1"


# --------------------------------------------------------------------------- #
# 异常/超时兜底：绝不卡死流程（PRD §13、issue #4 验收末条）
# --------------------------------------------------------------------------- #


def test_verify_llm_exception_lands_error_no_hang():
    """LLM 抛异常 → 节点 ``error``、流程继续（不抛出、不卡死）。"""

    def factory(node, observations):
        raise RuntimeError("LLM 不可用")

    tree = [_node(), _node(node_id="n1")]
    updates = verify(tree, FakeVerifyLlmClient(factory=factory), create_mock_retrieval_layer())
    assert updates["n0"].status is NodeStatus.ERROR
    assert updates["n1"].status is NodeStatus.ERROR


def test_verify_retrieval_compliance_violation_lands_error():
    """检索合规违规（非白名单域名）→ ComplianceError → 节点 ``error``。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(
        script=[
            SearchStep(query="q", channel=RetrievalKind.NETWORK, domain="evil.example.com"),
            ConcludeStep(verdict=VerifyVerdict.CREDIBLE),
        ]
    )
    updates = verify(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].status is NodeStatus.ERROR


def test_verify_malformed_step_lands_error():
    """LLM 返回非 union 成员（结构非法）→ 节点 ``error``。"""

    tree = [_node()]
    llm = FakeVerifyLlmClient(factory=lambda node, obs: "garbage")  # type: ignore[arg-type]
    updates = verify(tree, llm, create_mock_retrieval_layer())
    assert updates["n0"].status is NodeStatus.ERROR


def test_verify_iteration_cap_lands_error_and_is_bounded():
    """LLM 永不结论（一直检索）→ 迭代硬上限触发 → ``error``；有界、不卡死。"""

    tree = [_node()]
    always_search = lambda node, obs: SearchStep(  # noqa: E731
        query=f"q{len(obs)}", channel=RetrievalKind.NETWORK, domain="stats.example.com"
    )
    retrieval = _RecordingRetrieval(create_mock_retrieval_layer())
    updates = verify(tree, FakeVerifyLlmClient(factory=always_search), retrieval, max_iterations=3)
    assert updates["n0"].status is NodeStatus.ERROR
    assert len(retrieval.requests) == 3  # 恰好 max_iterations 次，不无限


# --------------------------------------------------------------------------- #
# 覆盖范围 + 不改原文 + 不改输入树
# --------------------------------------------------------------------------- #


def _full_tree() -> list[ArgumentationNode]:
    """main_claim / sub_claim / evidence / qualification / background 各一。"""

    return [
        ArgumentationNode(
            node_id="m",
            node_type=NodeType.MAIN_CLAIM,
            paragraph_id="p0001",
            content="主论点",
        ),
        ArgumentationNode(
            node_id="s",
            node_type=NodeType.SUB_CLAIM,
            paragraph_id="p0002",
            content="分论点",
        ),
        ArgumentationNode(
            node_id="e",
            node_type=NodeType.EVIDENCE,
            paragraph_id="p0003",
            content="论据",
        ),
        ArgumentationNode(
            node_id="q",
            node_type=NodeType.QUALIFICATION,
            paragraph_id="p0004",
            content="限定条件",
        ),
        ArgumentationNode(
            node_id="b",
            node_type=NodeType.BACKGROUND,
            paragraph_id="p0005",
            content="背景",
        ),
    ]


def test_verify_covers_claims_and_evidence_skips_qualification_and_shadow():
    """覆盖 main_claim/sub_claim/evidence；qualification 与影子节点不在 updates 中。"""

    tree = _full_tree()
    # 每个核心节点：一次检索后 credible。
    state: dict[str, int] = {}

    def factory(node, observations):
        c = state.get(node.node_id, 0)
        state[node.node_id] = c + 1
        if c == 0:
            return SearchStep(
                query=node.content,
                channel=RetrievalKind.NETWORK,
                domain="stats.example.com",
            )
        return ConcludeStep(verdict=VerifyVerdict.CREDIBLE)

    updates = verify(tree, FakeVerifyLlmClient(factory=factory), create_mock_retrieval_layer())
    assert set(updates) == {"m", "s", "e"}
    for nid in ("m", "s", "e"):
        assert updates[nid].status is NodeStatus.CREDIBLE


def test_verify_does_not_rewrite_content():
    """体检绝不改写原文：节点 ``content`` 与输入逐字节一致。"""

    tree = _full_tree()
    before = {n.node_id: n.content for n in tree}
    updates = verify(
        tree,
        FakeVerifyLlmClient(
            script=[
                SearchStep(query="q", channel=RetrievalKind.NETWORK, domain="stats.example.com"),
                ConcludeStep(verdict=VerifyVerdict.CREDIBLE),
            ]
        ),
        create_mock_retrieval_layer(),
    )
    for nid, node in updates.items():
        assert node.content == before[nid]


def test_verify_does_not_mutate_input_tree():
    """返回新实例；输入树节点状态不变（``unverified``）、内容不变。"""

    tree = _full_tree()
    originals = {n.node_id: n.model_copy(deep=True) for n in tree}
    verify(
        tree,
        FakeVerifyLlmClient(
            script=[
                SearchStep(query="q", channel=RetrievalKind.NETWORK, domain="stats.example.com"),
                ConcludeStep(verdict=VerifyVerdict.CREDIBLE),
            ]
        ),
        create_mock_retrieval_layer(),
    )
    for n in tree:
        assert n == originals[n.node_id], f"输入树被改写：{n.node_id}"
