"""编排中枢回写幂等续跑 seam 测试（issue #11 · 衔接 #10 · ADR-0011）。

回写中断（崩溃 / 进程退出）后，按持久化树中「``adopted`` 且未 ``corrected``」的节点续跑：
据 ``adopted_hypothesis_id`` 重新推导终稿、不重复注入、最终状态收敛为 ``corrected``。
本 seam 是编排中枢暴露的崩溃恢复入口（纯函数 ``writeback`` 幂等性的编排层封装）。
"""

from __future__ import annotations

from hypoargus.domain import (
    ArgumentationNode,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    NodeStatus,
    NodeType,
)
from hypoargus.orchestrator import Orchestrator
from hypoargus.raw_store import RawParagraphStore

_DOC = "分论点。\n\n论据。\n".encode()


def _adopted_evidence_node() -> ArgumentationNode:
    """一条已采纳待回写的论据节点（oppose 假设 text="x"，模拟崩溃前未翻正）。"""

    hyp = Hypothesis(
        hypothesis_id="h1",
        text="x",
        relation=HypothesisRelation.OPPOSE,
        status=HypothesisStatus.SUPPORTED,
    )
    return ArgumentationNode(
        node_id="n-p0002",
        node_type=NodeType.EVIDENCE,
        paragraph_id="p0002",
        content="论据",
        status=NodeStatus.ADOPTED,
        candidate_hypotheses=[hyp],
        adopted_hypothesis_id="h1",
    )


def _shadow_tree(store: RawParagraphStore) -> list[ArgumentationNode]:
    return [
        ArgumentationNode(
            node_id=f"n-{pid}",
            node_type=NodeType.BACKGROUND,
            paragraph_id=pid,
            content=store.get(pid).decode("utf-8", errors="surrogateescape"),
        )
        for pid in store.paragraph_ids()
    ]


def test_resume_writeback_converges_adopted_to_corrected():
    """持久化树含 adopted 未 corrected 节点 → 续跑翻正 corrected、终稿含假设文本。"""

    store = RawParagraphStore.from_text(_DOC)
    tree = _shadow_tree(store)
    # 替换 p0002 的影子节点为「已采纳待回写」的论据节点（模拟崩溃后落盘的树）。
    tree = [n for n in tree if n.paragraph_id != "p0002"]
    tree.append(_adopted_evidence_node())

    orch = Orchestrator()
    result = orch.resume_writeback(tree, store)

    by_id = {n.node_id: n for n in result.tree}
    assert by_id["n-p0002"].status is NodeStatus.CORRECTED
    assert by_id["n-p0002"].adopted_hypothesis_id == "h1"  # 持久保留（幂等重取依据）
    # 对立→替换原句：终稿含假设文本、原句消失。
    assert b"x" in result.final_doc
    assert "论据".encode() not in result.final_doc
    # 未变更段逐字节还原。
    assert "分论点。".encode() in result.final_doc


def test_resume_writeback_idempotent_no_double_injection():
    """续跑幂等：对已 corrected 的结果树再续跑 → 同一份 bytes、不重复注入。"""

    store = RawParagraphStore.from_text(_DOC)
    tree = _shadow_tree(store)
    tree = [n for n in tree if n.paragraph_id != "p0002"]
    tree.append(_adopted_evidence_node())

    orch = Orchestrator()
    first = orch.resume_writeback(tree, store)
    second = orch.resume_writeback(first.tree, store)

    assert second.final_doc == first.final_doc  # 幂等：重跑得同一份 bytes
    by_id = {n.node_id: n for n in second.tree}
    assert by_id["n-p0002"].status is NodeStatus.CORRECTED  # 收敛
    # supplement 永不累积（oppose 替换无追加，但断言终稿稳定）。
    assert second.final_doc.count(b"x") == 1


def test_resume_writeback_unresolvable_stays_adopted_for_retry():
    """不可解析的 adopted 节点续跑仍停留 adopted + 贴 writeback_error，可再次续跑。"""

    store = RawParagraphStore.from_text(_DOC)
    tree = _shadow_tree(store)
    tree = [n for n in tree if n.paragraph_id != "p0002"]
    # adopted_hypothesis_id 指向不存在的假设 → 数据缺失，回写失败、停留 adopted。
    tree.append(
        ArgumentationNode(
            node_id="n-p0002",
            node_type=NodeType.EVIDENCE,
            paragraph_id="p0002",
            content="论据",
            status=NodeStatus.ADOPTED,
            candidate_hypotheses=[],  # 空 → 解析不出被采纳假设
            adopted_hypothesis_id="missing-hid",
        )
    )

    orch = Orchestrator()
    result = orch.resume_writeback(tree, store)

    by_id = {n.node_id: n for n in result.tree}
    assert by_id["n-p0002"].status is NodeStatus.ADOPTED  # 停留、待再次续跑
    assert "writeback_error" in by_id["n-p0002"].issue_tags
    # 原文逐字节保留（保护原文底线）。
    assert result.final_doc == _DOC
