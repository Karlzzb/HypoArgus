"""段落原子回写（ADR-0001、ADR-0005、ADR-0011、PRD §11、issue #10）。

遍历修订确认（#9）后的终版树，以**段落为唯一原子单位**产出修订版文档，坚决杜绝
全量重写。

- 未被任何采纳改动命中的段落：按 ``paragraph_id`` 从只读原文表**逐字节流式拷回**，
  100% 保护原文风格与格式（字节级无损）。
- 被命中的段落：按被采纳假设的关系分流（ADR-0006）——对立→替换原句、递进→局部改写
  原句、扩展→段尾追加带专属审计标识的内容。

回写按段落**幂等**（ADR-0011）：成功则节点置 ``corrected``；失败则停留 ``adopted``
并贴 ``writeback_error`` 错误标签。重试时「``adopted`` 且未 ``corrected``」的节点据
``adopted_hypothesis_id`` 重做回写、不重复注入——幂等性源于「始终从只读原文表的原始
bytes 出发重新推导整篇终稿」，故同一棵树再跑得同一份 bytes，supplement 永不累积。

本函数是纯函数子缝（PRD «Testing Decisions» 三个纯函数子缝之一）：
``终版树 + 只读段落表 → { 终稿文本, 状态翻正后的树 }``，确定性、无 LLM / 检索依赖、
可独立单测。**绝不替人拍板**：仅对已被 HITL-2 #9 采纳（``adopted_hypothesis_id`` 非空）
的节点做分流缝合，绝不自动采纳、绝不改写未被命中的段落。
"""

from __future__ import annotations

from dataclasses import dataclass

from hypoargus.domain import (
    HYPOTHESIS_RELATION_TO_MERGE_ACTION,
    ArgumentationNode,
    Hypothesis,
    MergeAction,
    NodeStatus,
)
from hypoargus.raw_store import RawParagraphStore
from hypoargus.status_machine import transition_node

__all__ = [
    "SUPPLEMENT_AUDIT_MARKER",
    "WRITEBACK_ERROR_TAG",
    "WritebackResult",
    "writeback",
]


SUPPLEMENT_AUDIT_MARKER = "<!-- ha-supplement"
"""扩展型补充内容的专属审计标识前缀（PRD §11、ADR-0006）。

合规审查一眼识别本次修订新注入的内容：扩展型假设的补充文本以
``<!-- ha-supplement:{hypothesis_id} -->`` 标记领起，附 ``hypothesis_id`` 便于回溯到
被采纳假设。前缀常量供测试与审计扫描按子串探测。
"""

WRITEBACK_ERROR_TAG = "writeback_error"
"""回写失败标签：``adopted`` 节点回写失败时贴入 ``issue_tags``（ADR-0011）。

失败即「无法据 ``adopted_hypothesis_id`` 从 ``candidate_hypotheses`` 解析出被采纳假设」
或「对立/递进分流需替换的 ``content`` 子串不在段落中」——前者为数据缺失、后者为解析与
段落错位。失败时原文逐字节保留（保护原文底线）、节点停留 ``adopted`` 待重试。
"""


@dataclass(frozen=True)
class WritebackResult:
    """回写结果：终稿 bytes + 状态翻正后的树（ADR-0011）。

    ``final_doc`` 为按 ``paragraph_id`` 规范顺序缝合的终稿；``tree`` 为同序新树——
    成功采纳节点翻 ``corrected``、失败者停留 ``adopted`` 并贴 ``writeback_error``。
    二者同源推导，故重跑（重试）得同一份 ``final_doc``、状态收敛至 ``corrected``。
    """

    final_doc: bytes
    tree: list[ArgumentationNode]


# --------------------------------------------------------------------------- #
# 内部：编码/解码（与 raw_store / parser 的 surrogateescape 往返一致）
# --------------------------------------------------------------------------- #


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="surrogateescape")


def _encode(s: str) -> bytes:
    return s.encode("utf-8", errors="surrogateescape")


def _supplement_block(hypothesis: Hypothesis) -> str:
    """扩展型补充块：审计标识（附 hypothesis_id）领起假设文本，作为段尾追加内容。

    形如 ``\\n<!-- ha-supplement:{hid} -->\\n{text}``：审计标识独占一行、假设文本紧随其后。
    幂等性靠「始终从原始段落 bytes 推导」保证——同一段重跑只追加一次、不累积。
    """

    return f"\n{SUPPLEMENT_AUDIT_MARKER}:{hypothesis.hypothesis_id} -->\n{hypothesis.text}"


def _resolve_adopted_hypothesis(node: ArgumentationNode) -> Hypothesis | None:
    """据 ``adopted_hypothesis_id`` 从 ``candidate_hypotheses`` 解析被采纳假设。

    HITL-2（#9）采纳即持久化 ``adopted_hypothesis_id``，并保证该 id 在候选集内
    （``AdoptOp.edited_text`` 时已落回 ``candidate_hypotheses[].text``）。返回 ``None``
    即数据缺失——回写失败、原文逐字节保留。
    """

    hid = node.adopted_hypothesis_id
    if hid is None:
        return None
    for h in node.candidate_hypotheses:
        if h.hypothesis_id == hid:
            return h
    return None


def _apply_relation(
    paragraph: str,
    node: ArgumentationNode,
    hypothesis: Hypothesis,
) -> str | None:
    """对单段落应用一条被采纳假设的关系分流；返回新段落文本或 ``None``（失败）。

    - 对立（oppose → replace）：以假设文本替换节点原句（``content`` 子串）。原句消失。
    - 递进（advance → rewrite）：保留原句、假设文本内联合并（``content + text``）。原句留。
    - 扩展（expand → supplement）：段尾追加带审计标识的补充块；不动原句。

    对立/递进需在段落中定位 ``content`` 子串；定位失败（解析与段落错位）返回 ``None`` →
    回写失败、原文保留。扩展恒成功（不动既有文本）。
    """

    action = HYPOTHESIS_RELATION_TO_MERGE_ACTION[hypothesis.relation]
    if action is MergeAction.SUPPLEMENT:
        return paragraph + _supplement_block(hypothesis)
    # replace / rewrite：定位原句子串。
    if node.content == "":
        # 空原句无法定位——视作错位失败，保护原文。
        return None
    idx = paragraph.find(node.content)
    if idx < 0:
        return None
    if action is MergeAction.REPLACE:
        replacement = hypothesis.text
    else:  # MergeAction.REWRITE：保留原句、合并假设（局部改写）。
        replacement = node.content + hypothesis.text
    return paragraph[:idx] + replacement + paragraph[idx + len(node.content):]


def _rewrite_paragraph(
    paragraph_id: str,
    nodes: list[ArgumentationNode],
    store: RawParagraphStore,
) -> tuple[bytes, list[ArgumentationNode]]:
    """重写一条被采纳改动命中的段落，返回 (终版 bytes, 翻正后的节点列表)。

    段内多个被采纳节点依次施加关系分流（ADR-0001：整段进入重写通道，段内其他句子不作
    硬字节承诺）。任一节点解析/定位失败 → 该节点停留 ``adopted`` + 贴 ``writeback_error``；
    成功节点翻 ``corrected``。失败节点的变换跳过（其原句保留），其余节点照常缝合——
    单点失败不阻塞同段其他节点，单向向前推进（PRD §13）。
    """

    paragraph = _decode(store.get(paragraph_id))
    out_nodes: list[ArgumentationNode] = []
    for node in nodes:
        touched = node.adopted_hypothesis_id is not None
        if not touched:
            out_nodes.append(node.model_copy())
            continue
        hypothesis = _resolve_adopted_hypothesis(node)
        if hypothesis is None:
            out_nodes.append(_tag_writeback_error(node))
            continue
        new_paragraph = _apply_relation(paragraph, node, hypothesis)
        if new_paragraph is None:
            out_nodes.append(_tag_writeback_error(node))
            continue
        paragraph = new_paragraph
        # 成功：adopted → corrected（经集中状态机子缝校验合法迁移）；已 corrected 者保持
        # （终态，幂等重跑不动）；adopted_hypothesis_id 持久保留。
        if node.status is NodeStatus.ADOPTED:
            out_nodes.append(transition_node(node, NodeStatus.CORRECTED))
        else:
            out_nodes.append(node.model_copy())
    return _encode(paragraph), out_nodes


def _tag_writeback_error(node: ArgumentationNode) -> ArgumentationNode:
    """回写失败：停留 ``adopted``、追加 ``writeback_error`` 标签（去重）。"""

    tags = list(node.issue_tags)
    if WRITEBACK_ERROR_TAG not in tags:
        tags.append(WRITEBACK_ERROR_TAG)
    return node.model_copy(update={"issue_tags": tags})


def writeback(
    tree: list[ArgumentationNode],
    store: RawParagraphStore,
) -> WritebackResult:
    """产出终稿 bytes + 状态翻正后的树（不修改输入）。

    按 :meth:`RawParagraphStore.paragraph_ids` 的规范顺序遍历每段：

    - 该段无被采纳节点（``adopted_hypothesis_id`` 全为空）→ 从只读表逐字节拷回（字节级
      无损）；该段节点浅拷入新树。
    - 该段有被采纳节点 → 整段进入重写通道（ADR-0001）：据关系分流缝合，成功节点翻
      ``corrected``、失败节点停留 ``adopted`` 并贴 ``writeback_error``。

    规范顺序遍历保证：只要无采纳改动，``final_doc`` 与原始输入逐字节相等（分区不变式）。
    幂等：重跑同一棵树得同一份 ``final_doc``；含 ``corrected`` 节点的段同样从原始 bytes
    重新推导，故 supplement 永不累积、状态收敛至 ``corrected``。
    """

    # 先按 paragraph_id 索引节点一次，避免逐段全量扫描树（O(N) 而非 O(N×P)）。
    nodes_by_paragraph: dict[str, list[ArgumentationNode]] = {}
    for node in tree:
        nodes_by_paragraph.setdefault(node.paragraph_id, []).append(node)

    out = bytearray()
    new_tree: list[ArgumentationNode] = []
    for paragraph_id in store.paragraph_ids():
        nodes = nodes_by_paragraph.get(paragraph_id, [])
        touched = any(n.adopted_hypothesis_id is not None for n in nodes)
        if not touched:
            out += store.get(paragraph_id)
            new_tree.extend(n.model_copy() for n in nodes)
        else:
            chunk, rewritten_nodes = _rewrite_paragraph(paragraph_id, nodes, store)
            out += chunk
            new_tree.extend(rewritten_nodes)

    # 树中可能出现 paragraph_id 不在只读表的游离节点（结构异常）——守势：浅拷附尾、不动。
    seen = set(store.paragraph_ids())
    for node in tree:
        if node.paragraph_id not in seen:
            new_tree.append(node.model_copy())

    return WritebackResult(final_doc=bytes(out), tree=new_tree)
