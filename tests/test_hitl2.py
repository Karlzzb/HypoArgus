"""HITL-2 修订确认硬闸门单测（PRD §10 节点 2、issue #9、ADR-0010）。

在合并、影响传导、一致性校验全部完成后、回写（#10）前触发。界面并列呈现被标为
``doubtful``/``error`` 的段落原文、系统贴的 ``issue_tags``（含 ``conflict``）、
以及候选修订假设；用户逐条采纳或驳回、可手动修改节点内容。

**此节点为不可跳过的硬闸门**，系统绝不在无人拍板时自动采纳假设（ADR-0010）。
任务配置「是否启用人工确认」开关只作用于 HITL-1，对 HITL-2 无效。仅当全篇所有节点
均可信、无任何待决内容时，本节点呈现「无需修订」一键通过（属闸门内无待办，非跳过）。

用户采纳某条假设时，节点进入 ``adopted`` 状态并立即持久化 ``adopted_hypothesis_id``
（ADR-0011 采纳链），使回写失败重试再失败时用户决定不丢失。候选假设数量按实际生成
动态呈现，不固定为某个数字。

本切片 HITL-2 为同步注入闸门（``Hitl2Gate`` seam，``FakeHitl2Gate`` 供离线单测）；
真实 ``interrupt`` + ``Command(resume)`` + checkpointer 属 #11。
"""

from __future__ import annotations

import pytest

from hypoargus.domain import (
    ArgumentationNode,
    HypothesisRelation,
    HypothesisStatus,
    MergeAction,
    MergeDecision,
    NodeStatus,
    NodeType,
)
from hypoargus.hitl2 import (
    AdoptOp,
    EditContentOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Review,
    RejectOp,
    build_review,
    confirm,
)
from hypoargus.hypothesis import Hypothesis
from hypoargus.raw_store import RawParagraphStore

# --------------------------------------------------------------------------- #
# 测试夹具：一棵「主论点可信 + 分论点存疑带候选 + 论据错误」的树。
# --------------------------------------------------------------------------- #


def _hypothesis(
    hid: str,
    *,
    relation: HypothesisRelation = HypothesisRelation.OPPOSE,
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
    confidence: float = 0.8,
    text: str | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        text=text or f"假设-{hid}",
        relation=relation,
        status=status,
        confidence=confidence,
    )


def _node(
    node_id: str,
    *,
    paragraph_id: str,
    node_type: NodeType = NodeType.EVIDENCE,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    status: NodeStatus = NodeStatus.UNVERIFIED,
    argument_weight: int = 50,
    issue_tags: list[str] | None = None,
    candidates: list[Hypothesis] | None = None,
    merge_decision: MergeDecision | None = None,
    content: str = "",
) -> ArgumentationNode:
    return ArgumentationNode(
        node_id=node_id,
        node_type=node_type,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        paragraph_id=paragraph_id,
        argument_weight=argument_weight,
        status=status,
        issue_tags=list(issue_tags or []),
        candidate_hypotheses=list(candidates or []),
        merge_decision=merge_decision,
        content=content,
    )


def _store(*paragraphs: tuple[str, str]) -> RawParagraphStore:
    """从 (paragraph_id, text) 序列构造只读原文表（文本以 utf-8 编码固化）。"""

    from hypoargus.partition import Paragraph

    return RawParagraphStore(
        [Paragraph(pid, text.encode("utf-8")) for pid, text in paragraphs]
    )


def _pending_tree() -> tuple[list[ArgumentationNode], RawParagraphStore]:
    """主论点 credible（无候选）+ 分论点 doubtful（对立 supported 候选，激活）+ 论据 error。"""

    store = _store(
        ("p0001", "主论点。"),
        ("p0002", "分论点。"),
        ("p0003", "论据。"),
    )
    tree = [
        _node(
            "n0",
            paragraph_id="p0001",
            node_type=NodeType.MAIN_CLAIM,
            status=NodeStatus.CREDIBLE,
            argument_weight=80,
            children_ids=["n1"],
            content="主论点。",
        ),
        _node(
            "n1",
            paragraph_id="p0002",
            node_type=NodeType.SUB_CLAIM,
            status=NodeStatus.DOUBTFUL,
            argument_weight=60,
            parent_id="n0",
            children_ids=["n2"],
            content="分论点。",
            candidates=[_hypothesis("h1", relation=HypothesisRelation.OPPOSE)],
            merge_decision=MergeDecision(
                action=MergeAction.REPLACE, activated_hypothesis_ids=["h1"]
            ),
        ),
        _node(
            "n2",
            paragraph_id="p0003",
            node_type=NodeType.EVIDENCE,
            status=NodeStatus.ERROR,
            argument_weight=100,
            parent_id="n1",
            content="论据。",
        ),
    ]
    return tree, store


# --------------------------------------------------------------------------- #
# slice 1：build_review —— 并列呈现 doubtful/error 段落原文 + issue_tags + 候选
# --------------------------------------------------------------------------- #


def test_build_review_surfaces_pending_nodes_with_original_text():
    """build_review 呈现 doubtful/error 节点的段落原文、issue_tags、候选与激活集。"""

    tree, store = _pending_tree()
    review = build_review(tree, store)

    assert isinstance(review, Hitl2Review)
    assert review.has_pending is True
    by_node = {n.node_id: n for n in review.nodes}
    # 可信主论点无待决 → 不呈现。
    assert "n0" not in by_node
    # 分论点存疑 + 有激活候选 → 呈现，含段落原文、候选、激活集。
    sub = by_node["n1"]
    assert sub.original_text == "分论点。"
    assert sub.status is NodeStatus.DOUBTFUL
    assert sub.activated_hypothesis_ids == ["h1"]
    assert [c.hypothesis_id for c in sub.candidates] == ["h1"]
    assert sub.candidates[0].relation is HypothesisRelation.OPPOSE
    # 论据错误 → 呈现（无候选）。
    evi = by_node["n2"]
    assert evi.original_text == "论据。"
    assert evi.status is NodeStatus.ERROR
    assert evi.candidates == []


def test_build_review_no_pending_yields_empty_review():
    """全可信、无候选、无 conflict → 空呈现 + has_pending=False（一键通过口径）。"""

    store = _store(("p0001", "可信段。"))
    tree = [
        _node(
            "n0",
            paragraph_id="p0001",
            node_type=NodeType.MAIN_CLAIM,
            status=NodeStatus.CREDIBLE,
            argument_weight=80,
            content="可信段。",
        )
    ]
    review = build_review(tree, store)
    assert review.nodes == []
    assert review.has_pending is False


# --------------------------------------------------------------------------- #
# slice 2：confirm PASS（一键通过）+ 硬闸门禁自动采纳
# --------------------------------------------------------------------------- #


def _gate(decision: Hitl2Decision) -> Hitl2Gate:
    return FakeHitl2Gate(decision)


def test_confirm_pass_no_pending_returns_tree_unchanged():
    """无待决 → PASS 一键通过：树原样返回（深拷贝，不与输入同对象）。"""

    store = _store(("p0001", "可信段。"))
    tree = [
        _node(
            "n0",
            paragraph_id="p0001",
            node_type=NodeType.MAIN_CLAIM,
            status=NodeStatus.CREDIBLE,
            argument_weight=80,
            content="可信段。",
        )
    ]
    out = confirm(tree, store, _gate(Hitl2Decision(action=Hitl2Action.PASS)))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in tree]
    assert out is not tree
    assert out[0] is not tree[0]


def test_confirm_pass_with_pending_raises_hard_gate():
    """有待决内容时 gate 返回 PASS → 硬闸门拦截（绝不在无人拍板时自动采纳）。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Exception, match="硬闸门|PASS|待决|自动采纳"):
        confirm(tree, store, _gate(Hitl2Decision(action=Hitl2Action.PASS)))
    # 调用方原树不动。
    assert [n.model_dump() for n in tree] == snapshot


# --------------------------------------------------------------------------- #
# slice 3：DECIDE · 采纳假设（持久化 adopted_hypothesis_id + 状态机）
# --------------------------------------------------------------------------- #


def test_confirm_adopt_persists_adopted_hypothesis_id():
    """采纳激活候选 → 节点 adopted + adopted_hypothesis_id 持久化（回写重试不丢失）。"""

    tree, store = _pending_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[AdoptOp(node_id="n1", hypothesis_id="h1")],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n1"].status is NodeStatus.ADOPTED
    assert by_id["n1"].adopted_hypothesis_id == "h1"
    # 其余节点不受影响。
    assert by_id["n0"].status is NodeStatus.CREDIBLE
    assert by_id["n0"].adopted_hypothesis_id is None
    assert by_id["n2"].status is NodeStatus.ERROR
    # 调用方原树未被改。
    assert all(n.adopted_hypothesis_id is None for n in tree)
    assert all(n.status is not NodeStatus.ADOPTED for n in tree)


def test_confirm_adopt_with_edited_text_overrides_hypothesis_text():
    """采纳时手改假设文本 → candidate_hypotheses 中该假设文本被覆写（供回写幂等重取）。"""

    tree, store = _pending_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[
                    AdoptOp(
                        node_id="n1",
                        hypothesis_id="h1",
                        edited_text="手改后的假设文本",
                    )
                ],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    h1 = next(h for h in by_id["n1"].candidate_hypotheses if h.hypothesis_id == "h1")
    assert h1.text == "手改后的假设文本"
    assert by_id["n1"].status is NodeStatus.ADOPTED
    assert by_id["n1"].adopted_hypothesis_id == "h1"


def test_confirm_adopt_non_activated_hypothesis_raises():
    """采纳未激活的假设 → 越权 → 抛错，调用方原树不动。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    # h9 不在 n1 的激活集。
    with pytest.raises(Hitl2GateError, match="激活|activated|越权"):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[AdoptOp(node_id="n1", hypothesis_id="h9")],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_adopt_on_non_pending_node_raises():
    """对可信非冲突节点采纳 → 状态机非法变更 → 抛错（保护原文）。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Hitl2GateError, match="待决|pending|状态"):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    # n0 可信、无候选，无权采纳。
                    ops=[AdoptOp(node_id="n0", hypothesis_id="h1")],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_adopt_already_adopted_raises():
    """对已 adopted 节点再采纳 → 非法状态变更 → 抛错。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Hitl2GateError, match="adopted|状态|非法"):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[
                        AdoptOp(node_id="n1", hypothesis_id="h1"),
                        AdoptOp(node_id="n1", hypothesis_id="h1"),
                    ],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


# --------------------------------------------------------------------------- #
# slice 4：DECIDE · 驳回假设 + 手动修改节点内容
# --------------------------------------------------------------------------- #


def test_confirm_reject_removes_hypothesis_from_candidates():
    """驳回假设 → 从 candidate_hypotheses 移除；节点 status 不变（原文保留）。"""

    tree, store = _pending_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[RejectOp(node_id="n1", hypothesis_id="h1")],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n1"].candidate_hypotheses == []
    assert by_id["n1"].status is NodeStatus.DOUBTFUL  # 状态不变
    assert by_id["n1"].adopted_hypothesis_id is None


def test_confirm_reject_nonexistent_hypothesis_raises():
    """驳回不存在的假设 → 抛错，调用方原树不动。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Hitl2GateError, match="不存在|候选|hypothesis"):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[RejectOp(node_id="n1", hypothesis_id="h9")],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_edit_content_overrides_pending_node_content():
    """手动修改待决节点内容 → content 被覆写（状态不变）。"""

    tree, store = _pending_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[EditContentOp(node_id="n2", content="手动修订后的论据")],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n2"].content == "手动修订后的论据"
    assert by_id["n2"].status is NodeStatus.ERROR  # 状态不变


def test_confirm_edit_content_on_non_pending_node_raises():
    """对可信非冲突节点手改内容 → 抛错（保护原文，不呈现即不可改）。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Hitl2GateError, match="待决|pending|状态"):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[EditContentOp(node_id="n0", content="擅改可信内容")],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_decide_with_empty_ops_rejects_all_and_keeps_original():
    """DECIDE + 空 ops：人看过、全驳回 → 不采纳、原文保留（非自动跳过）。"""

    tree, store = _pending_tree()
    out = confirm(
        tree, store, _gate(Hitl2Decision(action=Hitl2Action.DECIDE, ops=[]))
    )
    # 全部未采纳 → 状态不变。
    assert all(n.status is not NodeStatus.ADOPTED for n in out)
    assert all(n.adopted_hypothesis_id is None for n in out)
    # 调用方原树不动。
    assert all(n.adopted_hypothesis_id is None for n in tree)


# --------------------------------------------------------------------------- #
# slice 5：conflict 格——可信原文 × 对立成立假设，人判采纳
# --------------------------------------------------------------------------- #


def _conflict_tree() -> tuple[list[ArgumentationNode], RawParagraphStore]:
    """单节点 credible + conflict 标签 + 对立 supported 假设（激活）。"""

    store = _store(("p0001", "可信但被对立假设挑战。"))
    tree = [
        _node(
            "n0",
            paragraph_id="p0001",
            node_type=NodeType.EVIDENCE,
            status=NodeStatus.CREDIBLE,
            argument_weight=70,
            issue_tags=["conflict"],
            candidates=[_hypothesis("h1", relation=HypothesisRelation.OPPOSE)],
            merge_decision=MergeDecision(
                action=MergeAction.CONFLICT, activated_hypothesis_ids=["h1"]
            ),
            content="可信但被对立假设挑战。",
        )
    ]
    return tree, store


def test_build_review_surfaces_conflict_node_with_original_text():
    """credible + conflict 节点虽可信但因贴 conflict → 呈现（人判对立假设）。"""

    tree, store = _conflict_tree()
    review = build_review(tree, store)
    assert review.has_pending is True
    assert len(review.nodes) == 1
    node = review.nodes[0]
    assert node.node_id == "n0"
    assert node.original_text == "可信但被对立假设挑战。"
    assert "conflict" in node.issue_tags
    assert node.activated_hypothesis_ids == ["h1"]


def test_confirm_adopt_on_conflict_node_resolves_conflict():
    """采纳对立假设 → credible 节点置 adopted（人判后可改可信原文，系统不自动裁决）。"""

    tree, store = _conflict_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[AdoptOp(node_id="n0", hypothesis_id="h1")],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n0"].status is NodeStatus.ADOPTED
    assert by_id["n0"].adopted_hypothesis_id == "h1"


def test_confirm_conflict_node_not_adopted_stays_credible():
    """conflict 节点未被采纳 → 维持 credible + conflict（人选择保留原文）。"""

    tree, store = _conflict_tree()
    out = confirm(
        tree, store, _gate(Hitl2Decision(action=Hitl2Action.DECIDE, ops=[]))
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n0"].status is NodeStatus.CREDIBLE
    assert "conflict" in by_id["n0"].issue_tags
    assert by_id["n0"].adopted_hypothesis_id is None


# --------------------------------------------------------------------------- #
# slice 6：多操作序列 + 原子性（非法步丢弃整个决策）
# --------------------------------------------------------------------------- #


def test_confirm_multi_op_sequence_applied_in_order():
    """采纳 n1 + 驳回 n2 的假设 + 手改 n2 内容：多 op 按序应用、各步校验。

    n1（doubtful + 激活 h1）→ 采纳；n2（error、无候选）→ 手改内容。
    """

    tree, store = _pending_tree()
    out = confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[
                    AdoptOp(node_id="n1", hypothesis_id="h1"),
                    EditContentOp(node_id="n2", content="手改论据"),
                ],
            )
        ),
    )
    by_id = {n.node_id: n for n in out}
    assert by_id["n1"].status is NodeStatus.ADOPTED
    assert by_id["n1"].adopted_hypothesis_id == "h1"
    assert by_id["n2"].content == "手改论据"
    assert by_id["n2"].status is NodeStatus.ERROR


def test_confirm_multi_op_invalid_step_rejects_wholesale():
    """序列中某步非法（采纳未激活假设）→ 整个决策丢弃，调用方原树不变。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    with pytest.raises(Hitl2GateError):
        confirm(
            tree,
            store,
            _gate(
                Hitl2Decision(
                    action=Hitl2Action.DECIDE,
                    ops=[
                        # 第一步合法：采纳 n1 的 h1。
                        AdoptOp(node_id="n1", hypothesis_id="h1"),
                        # 第二步非法：采纳 n1 不存在的 h9。
                        AdoptOp(node_id="n1", hypothesis_id="h9"),
                    ],
                )
            ),
        )
    assert [n.model_dump() for n in tree] == snapshot


def test_confirm_does_not_mutate_caller_tree_on_decide():
    """DECIDE 在深拷贝上工作，调用方树对象永不被改。"""

    tree, store = _pending_tree()
    snapshot = [n.model_dump() for n in tree]
    confirm(
        tree,
        store,
        _gate(
            Hitl2Decision(
                action=Hitl2Action.DECIDE,
                ops=[AdoptOp(node_id="n1", hypothesis_id="h1")],
            )
        ),
    )
    assert [n.model_dump() for n in tree] == snapshot
