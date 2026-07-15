"""线路 2 · 开药 Agent 测试（issue #5、PRD §5、ADR-0002/0007/0008/0011、Slice 3 重构）。

行为级黑盒测试（PRD «Testing Decisions»）：通过公共 seam
（``propose_hypotheses`` 纯函数 + ``HypothesisLlmClient``）驱动「投机生成」流程，
断言假说落 ``pending``、覆盖范围、不依赖体检结论、不重写原文、propose 异常不卡死。

Slice 3 重构：hypothesis 节点重定义为 **hypothesis_propose**——仅 ``propose``、不取证，
产 ``list[Hypothesis]``（status=pending）。取证移至 Slice 5 的 judgment 节点。
T-02：``propose`` 经 ``paragraph_list`` 反查该段 ``original_content`` + ``summary``
（不再读 ``Argument.content`` / ``paragraph_summaries`` dict），逐 argument 调用。

``FakeHypothesisLlmClient`` 保证离线、确定、可断言。
"""

from __future__ import annotations

import threading
import time

from agents.hypothesis import (
    FakeHypothesisLlmClient,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisStatus,
    propose_hypotheses,
)
from domain import Argument, ArgumentStatus, ArgumentType, ParagraphRecord


def _argument(
    argument_id: str = "n0",
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    paragraph_id: str = "p0001",
    content: str = "原文论据",
) -> Argument:
    arg = Argument(argument_id=argument_id, argument_type=argument_type)
    object.__setattr__(arg, "_test_paragraph_id", paragraph_id)
    object.__setattr__(arg, "_test_content", content)
    return arg


def _summaries(*pairs: tuple[str, str]) -> dict[str, str]:
    """``paragraph_id → 摘要`` 构造助手。"""

    return {pid: summary for pid, summary in pairs}


def _paragraph_list(
    argument_tree: list[Argument],
    summaries: dict[str, str] | None = None,
) -> list[ParagraphRecord]:
    """从树 + 摘要构造 paragraph_list。

    测试树每段单节点，故 ``original_content`` 取该段节点的 ``content``（与 parse 同源：每段一份
    原文）。production 路径由 parse 从 ``OriginalParagraphs`` 解码产出，结构一致。
    """

    summaries = summaries or {}
    by_para: dict[str, list[Argument]] = {}
    for a in argument_tree:
        by_para.setdefault(getattr(a, "_test_paragraph_id", "p0001"), []).append(a)
    return [
        ParagraphRecord(
            paragraph_id=pid,
            summary=summaries.get(pid, ""),
            original_content=getattr(by_para[pid][0], "_test_content", ""),
            argument_tree_ids=[a.argument_id for a in by_para[pid]],
        )
        for pid in by_para
    ]


# --------------------------------------------------------------------------- #
# propose 产 pending 假说 + 喂段落摘要
# --------------------------------------------------------------------------- #


def test_propose_single_evidence_yields_pending_hypothesis():
    """生成一条对立假设 → candidate_hypotheses 恰一条、status=pending。"""

    argument_tree = [_argument()]
    seen: list[tuple[str, str, str]] = []

    def propose(argument, paragraph_summary, original_content):
        seen.append((argument.argument_id, paragraph_summary, original_content))
        return [
            HypothesisProposal(
                text="对立假设：数据应取次年口径",
                relation=HypothesisRelation.OPPOSE,
                confidence=0.8,
            )
        ]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    summaries = _summaries(("p0001", "论据摘要"))
    updates = propose_hypotheses(argument_tree, _paragraph_list(argument_tree, summaries), llm)

    assert set(updates) == {"n0"}
    hypotheses = updates["n0"]
    assert len(hypotheses) == 1
    h = hypotheses[0]
    assert h.text == "对立假设：数据应取次年口径"
    assert h.relation is HypothesisRelation.OPPOSE
    assert h.status is HypothesisStatus.PENDING  # propose 期一律 pending（取证属 judgment·Slice 5）
    assert h.confidence == 0.8
    assert h.hypothesis_id  # 稳定非空 id（供 #9/#10 采纳链引用）
    # propose 收到段落摘要与段原文（非整段 content 的旧路径）。
    assert seen == [("n0", "论据摘要", "原文论据")]


# --------------------------------------------------------------------------- #
# 覆盖范围（ADR-0002 成本闸）
# --------------------------------------------------------------------------- #


def _full_tree() -> list[Argument]:
    """main_claim / sub_claim / evidence / qualification / background 各一。"""

    nodes = [
        Argument(argument_id="m", argument_type=ArgumentType.MAIN_CLAIM),
        Argument(argument_id="s", argument_type=ArgumentType.SUB_CLAIM),
        Argument(argument_id="e", argument_type=ArgumentType.EVIDENCE),
        Argument(argument_id="q", argument_type=ArgumentType.QUALIFICATION),
        Argument(argument_id="b", argument_type=ArgumentType.BACKGROUND),
    ]
    for n, pid, content in zip(
        nodes,
        ["p1", "p2", "p3", "p4", "p5"],
        ["主论点", "分论点", "论据", "限定", "背景"],
        strict=True,
    ):
        object.__setattr__(n, "_test_paragraph_id", pid)
        object.__setattr__(n, "_test_content", content)
    return nodes


def test_propose_covers_evidence_and_sub_claim_skips_rest():
    """只覆盖 evidence/sub_claim；main_claim/qualification/background 不在 updates 中。"""

    argument_tree = _full_tree()
    proposed: list[str] = []

    def propose(argument, paragraph_summary, original_content):
        proposed.append(argument.argument_id)
        return [
            HypothesisProposal(
                text=f"针对{argument.argument_id}", relation=HypothesisRelation.OPPOSE
            )
        ]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    summaries = _summaries(
        ("p1", "主论点摘要"), ("p2", "分论点摘要"), ("p3", "论据摘要"),
        ("p4", "限定摘要"), ("p5", "背景摘要"),
    )
    updates = propose_hypotheses(argument_tree, _paragraph_list(argument_tree, summaries), llm)
    assert set(updates) == {"s", "e"}
    assert set(proposed) == {"s", "e"}  # 未对 m/q/b 调用 propose（并发下调用顺序不保证）


def test_propose_passes_per_node_paragraph_summary():
    """每节点拿到自己 paragraph_id 对应的摘要（非整段 content、非空对齐）。"""

    argument_tree = _full_tree()
    seen: dict[str, str] = {}

    def propose(argument, paragraph_summary, original_content):
        seen[argument.argument_id] = paragraph_summary
        return [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    summaries = _summaries(("p2", "分论点摘要"), ("p3", "论据摘要"))
    propose_hypotheses(argument_tree, _paragraph_list(argument_tree, summaries), llm)
    assert seen == {"s": "分论点摘要", "e": "论据摘要"}


def test_propose_missing_summary_fed_as_empty_no_hang():
    """paragraph_list 缺该段时喂空串、不抛、流程继续。"""

    argument_tree = [_argument()]
    seen: list[str] = []

    def propose(argument, paragraph_summary, original_content):
        seen.append(paragraph_summary)
        return [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    updates = propose_hypotheses(argument_tree, [], llm)  # 空 paragraph_list → 缺段喂空
    assert seen == [""]
    assert len(updates["n0"]) == 1
    assert updates["n0"][0].status is HypothesisStatus.PENDING


# --------------------------------------------------------------------------- #
# 无假设 = 空数组（ADR-0008）+ 生成异常兜底
# --------------------------------------------------------------------------- #


def test_propose_no_proposals_yields_empty_candidate_list():
    """LLM 生成 0 条假设 → candidate_hypotheses 为空数组（非 None、非第四态）。"""

    argument_tree = [_argument()]
    llm = FakeHypothesisLlmClient()  # 默认 propose → []
    updates = propose_hypotheses(argument_tree, _paragraph_list(argument_tree, _summaries(("p0001", "摘要"))), llm)
    assert updates["n0"] == []


def test_propose_exception_yields_empty_no_hang():
    """propose 抛异常 → 该节点无假设（空列表）、流程继续（不抛出、不卡死）。"""

    argument_tree = [_argument(), _argument(argument_id="n1", paragraph_id="p2")]
    llm = FakeHypothesisLlmClient(
        propose_factory=lambda argument, summary, original_content: (
            _ for _ in ()
        ).throw(RuntimeError("LLM 不可用"))
    )
    updates = propose_hypotheses(
        argument_tree, _paragraph_list(argument_tree, _summaries(("p0001", "摘要"), ("p2", "摘要2"))), llm
    )
    assert updates["n0"] == []
    assert updates["n1"] == []


# --------------------------------------------------------------------------- #
# 不改原文 + 不改输入树 + 一假设一关系（结构保证）+ 幂等 id
# --------------------------------------------------------------------------- #


def _oppose_proposals(argument: Argument, summary: str, original_content: str) -> list[HypothesisProposal]:
    return [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)]


def test_propose_does_not_rewrite_content_or_status():
    """开药绝不改原文与体检状态：partial 只存候选假设、不携节点，输入树 content/status 不变。"""

    argument_tree = _full_tree()
    before = {n.argument_id: (getattr(n, "_test_content", ""), n.status) for n in argument_tree}
    llm = FakeHypothesisLlmClient(propose_factory=_oppose_proposals)
    summaries = _summaries(("p2", "分论点摘要"), ("p3", "论据摘要"))
    propose_hypotheses(argument_tree, _paragraph_list(argument_tree, summaries), llm)
    for n in argument_tree:
        assert (getattr(n, "_test_content", ""), n.status) == before[n.argument_id]


def test_propose_does_not_mutate_input_tree():
    """返回新实例；输入树节点不变（content/status/candidate_hypotheses 均原样）。"""

    argument_tree = _full_tree()
    originals = {n.argument_id: n.model_copy(deep=True) for n in argument_tree}
    llm = FakeHypothesisLlmClient(propose_factory=_oppose_proposals)
    summaries = _summaries(("p2", "分论点摘要"), ("p3", "论据摘要"))
    propose_hypotheses(argument_tree, _paragraph_list(argument_tree, summaries), llm)
    for n in argument_tree:
        assert n == originals[n.argument_id], f"输入树被改写：{n.argument_id}"


def test_propose_each_hypothesis_carries_single_relation():
    """一假设一关系（ADR-0007）：混合意图以多条假设表达，各钉定单一 relation。"""

    argument_tree = [_argument()]

    def proposals(argument, summary, original_content):
        return [
            HypothesisProposal(text="对立项", relation=HypothesisRelation.OPPOSE),
            HypothesisProposal(text="递进项", relation=HypothesisRelation.ADVANCE),
            HypothesisProposal(text="扩展项", relation=HypothesisRelation.EXPAND),
        ]

    llm = FakeHypothesisLlmClient(propose_factory=proposals)
    updates = propose_hypotheses(argument_tree, _paragraph_list(argument_tree, _summaries(("p0001", "摘要"))), llm)
    relations = [h.relation for h in updates["n0"]]
    assert relations == [
        HypothesisRelation.OPPOSE,
        HypothesisRelation.ADVANCE,
        HypothesisRelation.EXPAND,
    ]
    # hypothesis_id 稳定、同输入再跑一次结果一致（可重算、幂等链前提）。
    again = propose_hypotheses(
        argument_tree, _paragraph_list(argument_tree, _summaries(("p0001", "摘要"))), FakeHypothesisLlmClient(propose_factory=proposals)
    )
    assert [h.hypothesis_id for h in again["n0"]] == [
        h.hypothesis_id for h in updates["n0"]
    ]


def test_propose_pending_status_regardless_of_proposals():
    """propose 产出的假说一律 pending——propose 不取证、不落终态。"""

    argument_tree = [_argument()]

    def proposals(argument, summary, original_content):
        return [
            HypothesisProposal(text="a", relation=HypothesisRelation.OPPOSE, confidence=0.9),
            HypothesisProposal(text="b", relation=HypothesisRelation.ADVANCE, confidence=0.1),
            HypothesisProposal(text="c", relation=HypothesisRelation.EXPAND),
        ]

    llm = FakeHypothesisLlmClient(propose_factory=proposals)
    updates = propose_hypotheses(argument_tree, _paragraph_list(argument_tree, _summaries(("p0001", "摘要"))), llm)
    assert all(h.status is HypothesisStatus.PENDING for h in updates["n0"])


def test_propose_does_not_read_verification_status():
    """乐观并行（ADR-0002）：生成不读体检结论——节点 status 不影响生成。"""

    argument_tree = [
        Argument(argument_id="ok", argument_type=ArgumentType.EVIDENCE),
        Argument(
            argument_id="bad",
            argument_type=ArgumentType.EVIDENCE,
            status=ArgumentStatus.ERROR,  # 体检可能已判 error，但开药仍照常生成（不依赖该结论）
        ),
    ]
    for n, pid, content in zip(
        argument_tree, ["p1", "p2"], ["论据", "论据2"],
        strict=True,
    ):
        object.__setattr__(n, "_test_paragraph_id", pid)
        object.__setattr__(n, "_test_content", content)
    seen_arguments: list[str] = []

    def propose(argument, paragraph_summary, original_content):
        seen_arguments.append(argument.argument_id)
        return [
            HypothesisProposal(
                text=f"h-{argument.argument_id}", relation=HypothesisRelation.OPPOSE
            )
        ]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    updates = propose_hypotheses(
        argument_tree, _paragraph_list(argument_tree, _summaries(("p1", "摘要1"), ("p2", "摘要2"))), llm
    )
    assert set(seen_arguments) == {"ok", "bad"}  # 并发下调用顺序不保证，仅断言集合
    for nid in ("ok", "bad"):
        assert updates[nid][0].status is HypothesisStatus.PENDING


# --------------------------------------------------------------------------- #
# T-02 回归锁：propose seam 收到该段 original_content
# --------------------------------------------------------------------------- #


def test_propose_seam_receives_paragraph_original_content():
    """Fake-LLM spy：propose 收到该段 ``original_content``（经 paragraph_list 反查，T-02 回归锁）。

    引发本重构的原始问题在假设侧同样被回答：节点产假设时拿到的是所在段原文（而非仅摘要）。
    本测试把「原文到达假设 seam」锁为回归。
    """

    argument_tree = [
        Argument(argument_id="e", argument_type=ArgumentType.EVIDENCE),
    ]
    object.__setattr__(argument_tree[0], "_test_paragraph_id", "p1")
    object.__setattr__(argument_tree[0], "_test_content", "论据原文ABC")
    captured: dict[str, str] = {}

    def propose(argument, paragraph_summary, original_content):
        captured[argument.argument_id] = original_content
        return [HypothesisProposal(text="对立", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    propose_hypotheses(argument_tree, _paragraph_list(argument_tree), llm)
    assert captured == {"e": "论据原文ABC"}  # 段原文经 paragraph_list 反查传入 seam


# --------------------------------------------------------------------------- #
# 并发执行（串行 → 并发重构）：propose 调用在时间上重叠 + 有界
# --------------------------------------------------------------------------- #


def test_propose_fanout_overlaps_in_time():
    """并发：多节点 propose 调用在时间上重叠（非串行）。

    串行实现下任一时刻只有一个 propose 在途，``Barrier(2)`` 永不满足 → 超时抛
    ``BrokenBarrierError`` → 异常兜底置空 → 全节点空假设 → 断言失败（RED）。
    并发实现下 ≥2 个调用同时到达 barrier → 即刻释放 → 各产 1 条假设（GREEN）。
    """

    argument_tree = [
        _argument(argument_id=f"n{i}", paragraph_id=f"p{i}", content=f"c{i}")
        for i in range(6)
    ]
    barrier = threading.Barrier(2, timeout=2.0)

    def propose(argument, paragraph_summary, original_content):
        barrier.wait()  # 阻塞至 ≥2 个调用同时到达（证明重叠）
        return [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    updates = propose_hypotheses(
        argument_tree,
        _paragraph_list(argument_tree, _summaries(*[(f"p{i}", "s") for i in range(6)])),
        llm,
    )
    assert set(updates) == {f"n{i}" for i in range(6)}
    for hypotheses in updates.values():
        assert len(hypotheses) == 1
        assert hypotheses[0].status is HypothesisStatus.PENDING


def test_propose_respects_max_concurrency_bound():
    """有界并发：同一时刻在途 propose 数 ≤ max_concurrency（防 LLM 限流风暴）。

    ``max_concurrency=4``、16 节点：``ThreadPoolExecutor`` 硬上限保证在途 ≤ 4。
    无界实现（16 在途）会让 ``max_in_flight == 16`` → ``≤ 4`` 断言失败（RED）；
    串行实现（``max_in_flight == 1``）→ ``≥ 2`` 断言失败（RED）。
    """

    argument_tree = [
        _argument(argument_id=f"n{i}", paragraph_id=f"p{i}", content=f"c{i}")
        for i in range(16)
    ]
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def propose(argument, paragraph_summary, original_content):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)  # 持有调用使在途计数可测
        with lock:
            in_flight -= 1
        return [HypothesisProposal(text="x", relation=HypothesisRelation.OPPOSE)]

    llm = FakeHypothesisLlmClient(propose_factory=propose)
    updates = propose_hypotheses(
        argument_tree,
        _paragraph_list(argument_tree, _summaries(*[(f"p{i}", "s") for i in range(16)])),
        llm,
        max_concurrency=4,
    )
    assert set(updates) == {f"n{i}" for i in range(16)}
    assert max_in_flight <= 4  # 不超上限（有界）
    assert max_in_flight >= 2  # 且确有并发（未退化为串行）
