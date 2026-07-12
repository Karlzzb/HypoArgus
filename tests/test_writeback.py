"""回写纯函数子缝测试（ADR-0001、ADR-0005、ADR-0011、PRD §11、«Testing Decisions»）。

回写按段落原子缝合：未变更段逐字节拷回、变更段按关系正确替换或改写或段尾追加。
本切片覆盖「逐字节拷回」（#1）与「采纳改动分流」（#10）：oppose→替换原句、
advance→局部改写原句、expand→段尾追加带专属审计标识内容；成功置 ``corrected``、
失败停留 ``adopted`` 并贴错误标签、重试幂等不重复注入。

行为级黑盒测试（PRD «Testing Decisions»）。无 LLM / 检索依赖（确定性纯函数），
故无需注入桩。
"""

from __future__ import annotations

import pytest

from agents.writeback import (
    SUPPLEMENT_AUDIT_MARKER,
    WRITEBACK_ERROR_TAG,
    WritebackResult,
    writeback,
)
from domain import (
    ArgumentationNode,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    NodeStatus,
    NodeType,
)
from raw_store import RawParagraphStore

# --------------------------------------------------------------------------- #
# 构造小工具
# --------------------------------------------------------------------------- #


def _shadow_tree(store: RawParagraphStore):
    """每段一个只读影子节点（与解析桩一致）。"""

    return [
        ArgumentationNode(
            node_id=f"n-{pid}",
            node_type=NodeType.BACKGROUND,
            paragraph_id=pid,
            content=store.get(pid).decode("utf-8", errors="surrogateescape"),
        )
        for pid in store.paragraph_ids()
    ]


def _hyp(
    hid: str,
    *,
    text: str,
    relation: HypothesisRelation,
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
) -> Hypothesis:
    return Hypothesis(hypothesis_id=hid, text=text, relation=relation, status=status)


def _adopted_node(
    *,
    node_id: str,
    paragraph_id: str,
    content: str,
    hypothesis: Hypothesis,
    status: NodeStatus = NodeStatus.ADOPTED,
) -> ArgumentationNode:
    """构造一个已采纳节点（adopted 或 corrected），其候选假设含被采纳者。"""

    return ArgumentationNode(
        node_id=node_id,
        node_type=NodeType.EVIDENCE,
        paragraph_id=paragraph_id,
        content=content,
        status=status,
        candidate_hypotheses=[hypothesis],
        adopted_hypothesis_id=hypothesis.hypothesis_id,
    )


# --------------------------------------------------------------------------- #
# #1 逐字节拷回通道（无采纳改动 → 终稿逐字节等于原文）
# --------------------------------------------------------------------------- #


def test_writeback_no_adoptions_byte_identical(sample_doc):
    """无采纳改动时，终稿逐字节等于原始输入（含空行/缩进/末尾空格）。"""

    _name, doc = sample_doc
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    assert writeback(tree, store).final_doc == doc


def test_writeback_uses_store_canonical_order_not_tree_order():
    """回写按只读表规范顺序遍历，而非树遍历顺序——保证字节级确定。"""

    doc = b"aaa\n\nbbb\n\nccc\n"
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    # 故意打乱树顺序，回写仍按 store 规范顺序输出。
    tree.reverse()
    assert writeback(tree, store).final_doc == doc


def test_writeback_preserves_code_fence_block_bytes():
    """代码块段逐字节无损（含栅栏内空行）。"""

    doc = b"intro\n\n```python\na = 1\n\nb = 2\n```\n\noutro\n"
    store = RawParagraphStore.from_text(doc)
    tree = _shadow_tree(store)
    assert writeback(tree, store).final_doc == doc


# --------------------------------------------------------------------------- #
# #10 采纳改动分流（oppose→替换、advance→改写、expand→段尾追加）
# --------------------------------------------------------------------------- #


def test_writeback_oppose_replaces_original_sentence():
    """对立型假设 → 替换原句：节点 content 子串被假设文本取代，原句消失。

    成功置 ``corrected``、``adopted_hypothesis_id`` 持久保留（幂等重取依据）。
    """

    doc = b"keep\n\nchange\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    node = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="change",
        hypothesis=_hyp("h1", text="fixed", relation=HypothesisRelation.OPPOSE),
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]

    result = writeback(tree, store)

    assert isinstance(result, WritebackResult)
    # 未变更段逐字节还原。
    assert store.get("p0001") in result.final_doc
    assert store.get("p0003") in result.final_doc
    # 对立假设替换原句：新文本在、原句消失。
    assert b"fixed" in result.final_doc
    assert b"change" not in result.final_doc
    # 成功置 corrected、adopted_hypothesis_id 保留。
    out_by_id = {n.node_id: n for n in result.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.CORRECTED
    assert out_by_id["n-p0002"].adopted_hypothesis_id == "h1"


def test_writeback_advance_rewrites_merging_hypothesis():
    """递进型假设 → 局部改写原句：原句保留、假设文本内联合并。

    与对立（原句消失）的区别：递进保留原句、在其位置合并假设文本。
    """

    doc = b"keep\n\nchange\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    node = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="change",
        hypothesis=_hyp("h1", text="more", relation=HypothesisRelation.ADVANCE),
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]

    result = writeback(tree, store)

    # 原句保留、假设文本内联合并。
    assert b"change" in result.final_doc
    assert b"more" in result.final_doc
    # 变更段缝合后原句与假设同段共存。
    assert b"changemore" in result.final_doc
    out_by_id = {n.node_id: n for n in result.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.CORRECTED


def test_writeback_expand_supplements_with_audit_marker():
    """扩展型假设 → 段尾追加带专属审计标识的内容。

    原段逐字节保留、补充内容（审计标识 + 假设文本）追加在段尾；标识含 hypothesis_id
    便于合规审计回溯。
    """

    doc = b"keep\n\nbase\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    node = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="base",
        hypothesis=_hyp("h9", text="addendum", relation=HypothesisRelation.EXPAND),
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]

    result = writeback(tree, store)

    # 原段逐字节保留。
    assert store.get("p0002") in result.final_doc
    # 补充内容在段尾：审计标识（含 hypothesis_id）+ 假设文本。
    assert SUPPLEMENT_AUDIT_MARKER.encode() in result.final_doc
    assert b"ha-supplement:h9" in result.final_doc
    assert b"addendum" in result.final_doc
    # 标识领起补充文本（标识在文本之前）。
    marker_pos = result.final_doc.find(SUPPLEMENT_AUDIT_MARKER.encode())
    assert result.final_doc.find(b"addendum") > marker_pos
    out_by_id = {n.node_id: n for n in result.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.CORRECTED


# --------------------------------------------------------------------------- #
# 字节级对齐与失败模式
# --------------------------------------------------------------------------- #


def test_writeback_non_changed_paragraphs_byte_identical(sample_doc):
    """非变更段文本/标点/换行与原始输入逐字节完全一致（字节级对齐断言）。

    仅变更中间一段（oppose→替换），断言其余段逐字节无损还原、变更段正确缝合。
    """

    _name, doc = sample_doc
    store = RawParagraphStore.from_text(doc)
    pids = store.paragraph_ids()
    # 无至少两段时可改的样例直接跳过（本测试要求可改中间段）。
    if len(pids) < 3:
        pytest.skip("样例不足三段，无法改中间段")
    target_pid = pids[len(pids) // 2]
    target_bytes = store.get(target_pid)
    # content 取该段一个非空子串作为可定位原句。
    content = target_bytes.rstrip(b"\n").decode("utf-8", errors="surrogateescape")
    if content == "":
        pytest.skip("目标段无可用原句子串")

    node = _adopted_node(
        node_id=f"n-{target_pid}",
        paragraph_id=target_pid,
        content=content,
        hypothesis=_hyp("h1", text="REPLACED", relation=HypothesisRelation.OPPOSE),
    )
    tree = [
        ArgumentationNode(
            node_id=f"n-{pid}",
            node_type=NodeType.BACKGROUND,
            paragraph_id=pid,
            content=store.get(pid).decode("utf-8", errors="surrogateescape"),
        )
        for pid in pids
        if pid != target_pid
    ]
    tree.append(node)
    # 故意打乱顺序，回写仍按 store 规范顺序、非变更段逐字节还原。
    tree.reverse()

    result = writeback(tree, store)

    # 每个非变更段逐字节还原（含空行/缩进/末尾空格）。
    for pid in pids:
        if pid == target_pid:
            continue
        assert store.get(pid) in result.final_doc
    # 变更段：原句消失、替换文本就位。
    assert b"REPLACED" in result.final_doc
    assert content.encode("utf-8", errors="surrogateescape") not in result.final_doc.replace(
        b"REPLACED", b""
    )
    # 整体字节长度仍与原文同阶（替换段长度变化，但未变更段零增减）。
    others_len = sum(len(store.get(pid)) for pid in pids if pid != target_pid)
    assert others_len == sum(len(store.get(pid)) for pid in pids if pid != target_pid)


def test_writeback_unresolvable_hypothesis_stays_adopted_with_error_tag():
    """失败：adopted_hypothesis_id 在 candidate_hypotheses 中解析不出 → 停留 adopted
    + 贴 writeback_error、原文逐字节保留（保护原文底线）。"""

    doc = b"keep\n\noriginal\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    # adopted_hypothesis_id 指向不存在的假设——数据缺失（HITL-2 异常 / 树损坏）。
    node = ArgumentationNode(
        node_id="n-p0002",
        node_type=NodeType.EVIDENCE,
        paragraph_id="p0002",
        content="original",
        status=NodeStatus.ADOPTED,
        candidate_hypotheses=[
            _hyp("h1", text="x", relation=HypothesisRelation.OPPOSE),
        ],
        adopted_hypothesis_id="missing-hid",
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]

    result = writeback(tree, store)

    # 失败：原文逐字节保留、未注入假设文本。
    assert store.get("p0002") in result.final_doc
    assert b"x" not in result.final_doc
    out_by_id = {n.node_id: n for n in result.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.ADOPTED
    assert WRITEBACK_ERROR_TAG in out_by_id["n-p0002"].issue_tags


# --------------------------------------------------------------------------- #
# 幂等与不修改输入（ADR-0011、PRD §11 回写幂等测试）
# --------------------------------------------------------------------------- #


def test_writeback_idempotent_re_run_produces_same_bytes():
    """重跑同一棵（已翻正的）树得同一份 final_doc——supplement 永不累积。"""

    doc = b"keep\n\nbase\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    node = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="base",
        hypothesis=_hyp("h9", text="addendum", relation=HypothesisRelation.EXPAND),
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]

    first = writeback(tree, store)
    # 重跑：输入 first.tree（含 corrected 节点，仍携 adopted_hypothesis_id）。
    second = writeback(first.tree, store)

    assert first.final_doc == second.final_doc
    # 补充块只出现一次（不重复注入）。
    assert second.final_doc.count(SUPPLEMENT_AUDIT_MARKER.encode()) == 1
    # 状态收敛：被采纳节点翻正为 corrected（影子节点保持原状、未被触及）。
    out_by_id = {n.node_id: n for n in second.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.CORRECTED


def test_writeback_resumes_from_partial_run_no_double_injection():
    """模拟中断后重试：树含 adopted + corrected 混合（前次部分翻正）。

    断言：corrected 段不重复注入、adopted 段续跑翻正、final_doc 与全量重跑一致、
    状态收敛至 corrected。
    """

    doc = b"keep\n\nseg-a\n\nseg-b\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    h_a = _hyp("ha", text="AddA", relation=HypothesisRelation.EXPAND)
    h_b = _hyp("hb", text="AddB", relation=HypothesisRelation.EXPAND)
    # 前次已翻正 n-p0002（corrected）、n-p0003 仍 adopted（中断）。
    seg_a = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="seg-a",
        hypothesis=h_a,
        status=NodeStatus.CORRECTED,
    )
    seg_b = _adopted_node(
        node_id="n-p0003",
        paragraph_id="p0003",
        content="seg-b",
        hypothesis=h_b,
        status=NodeStatus.ADOPTED,
    )
    tree_partial = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        seg_a,
        seg_b,
        ArgumentationNode(node_id="n-p0004", node_type=NodeType.BACKGROUND, paragraph_id="p0004", content="keep2"),
    ]

    # 全量基线：两段皆 adopted。
    tree_fresh = [
        tree_partial[0],
        _adopted_node(
            node_id="n-p0002",
            paragraph_id="p0002",
            content="seg-a",
            hypothesis=h_a,
            status=NodeStatus.ADOPTED,
        ),
        _adopted_node(
            node_id="n-p0003",
            paragraph_id="p0003",
            content="seg-b",
            hypothesis=h_b,
            status=NodeStatus.ADOPTED,
        ),
        tree_partial[3],
    ]

    baseline = writeback(tree_fresh, store)
    resumed = writeback(tree_partial, store)

    # 重试与全量重跑终稿一致（corrected 段不重复注入、adopted 段续跑缝合）。
    assert resumed.final_doc == baseline.final_doc
    # 每条补充块恰好一次。
    assert resumed.final_doc.count(b"ha-supplement:ha") == 1
    assert resumed.final_doc.count(b"ha-supplement:hb") == 1
    # 状态收敛至 corrected。
    out_by_id = {n.node_id: n for n in resumed.tree}
    assert out_by_id["n-p0002"].status is NodeStatus.CORRECTED
    assert out_by_id["n-p0003"].status is NodeStatus.CORRECTED


def test_writeback_does_not_mutate_input_tree():
    """回写不修改输入树：输入节点 status / issue_tags / content 不变。"""

    doc = b"keep\n\nchange\n\nkeep2\n"
    store = RawParagraphStore.from_text(doc)
    node = _adopted_node(
        node_id="n-p0002",
        paragraph_id="p0002",
        content="change",
        hypothesis=_hyp("h1", text="fixed", relation=HypothesisRelation.OPPOSE),
    )
    tree = [
        ArgumentationNode(node_id="n-p0001", node_type=NodeType.BACKGROUND, paragraph_id="p0001", content="keep"),
        node,
        ArgumentationNode(node_id="n-p0003", node_type=NodeType.BACKGROUND, paragraph_id="p0003", content="keep2"),
    ]
    before_statuses = [n.status for n in tree]
    before_tags = [list(n.issue_tags) for n in tree]
    before_contents = [n.content for n in tree]

    result = writeback(tree, store)

    # 输入未变；输出是新实例。
    assert [n.status for n in tree] == before_statuses
    assert [list(n.issue_tags) for n in tree] == before_tags
    assert [n.content for n in tree] == before_contents
    assert all(out is not inp for out, inp in zip(result.tree, tree, strict=True))
    # 输入 adopted 节点仍 adopted；输出对应节点已 corrected。
    assert tree[1].status is NodeStatus.ADOPTED
    assert result.tree[1].status is NodeStatus.CORRECTED
