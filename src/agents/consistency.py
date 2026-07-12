"""一致性校验 Agent（PRD §9、issue #8、ADR-0012）：批注门禁·只贴 issue_tags·不打回。

在影响传导（#7）之后、修订确认（#9）之前对**那棵已标注完成的树**扫描**一次**，
执行段落级校验（自洽性、边界匹配）与全局校验（跨段论点一致、术语定义一致）。
所有冲突与瑕疵统一以批注标签 ``issue_tags`` 注入节点元数据，**单向推进到修订
确认**——完全不具备拒绝、打回、重新调度的权力。

边界（ADR-0012）：

- **只扫一次**：产出 ``issue_tags`` 喂给 HITL-2，不回炉、无 Rejection Loop。
- **看不到 ``adopted`` / ``corrected``**（那是 HITL-2 #9 之后才有的状态），
  亦不因用户采纳可能引入新冲突而回炉重扫。
- **无状态的一次性扫描**，不参与 HITL-2 之后的流程。

本函数是纯函数子缝（PRD «Testing Decisions»）：``标注后的树 → 贴批注后的同一棵树``，
确定性、无 LLM / 检索依赖、可独立单测。**绝不替人拍板**：不改 ``content`` 与
``status``、不置 ``adopted`` / ``corrected``、不动 ``merge_decision`` 与
``candidate_hypotheses``（那是体检 #4 / 合并 #6 / 影响传导 #7 / HITL-2 #9 / 回写
#10 的职责）。**只追加** ``issue_tags``（去重，保留既有标签）。

确定性检查规则（皆为代码级可判定；语义级术语 / 论点一致性需 LLM，本版为确定性
代理 + 已知缺口，遵循 ADR-0012「已知缺口换取单向稳健」哲学，后续版本可接入
LLM seam 做轻量语义校验）：

段落级：

1. ``mixed_paragraph_kind``（自洽性）：同一段落同时含影子节点（background /
   evaluation）与核心逻辑节点——PRD 结构原则「影子段落对应影子节点、实质段落
   对应论点节点」，混段是潜在解析边界瑕疵。该段全部节点贴标签。
2. ``multi_primary_per_paragraph``（边界匹配）：同一段落含多于一个根节点
   （``parent_id`` 为 ``None``）——段落是回写原子单位，一段多根是潜在边界瑕疵。
   这些根节点贴标签。

全局：

3. ``multi_main_claim``（跨段论点一致）：全树多于一个 ``main_claim`` 节点——
   多主论点是潜在跨段论点不一致。所有 ``main_claim`` 节点贴标签。
4. ``duplicate_qualification``（术语定义一致）：两个 ``qualification`` 节点
   的归一化内容完全相同——同一限定 / 术语定义被逐字重复，是潜在冗余瑕疵。
   重复的双方均贴标签。（语义级「同词不同义」需 LLM，本版只做确定性重复代理。）
"""

from __future__ import annotations

from domain import ArgumentationNode, NodeType

__all__ = [
    "MIXED_PARAGRAPH_KIND_TAG",
    "MULTI_PRIMARY_PER_PARAGRAPH_TAG",
    "MULTI_MAIN_CLAIM_TAG",
    "DUPLICATE_QUALIFICATION_TAG",
    "consistency",
]


# --------------------------------------------------------------------------- #
# 批注标签常量（与 merge 的 ``conflict``、impact 的 ``weakening`` 同形：去重追加）
# --------------------------------------------------------------------------- #

MIXED_PARAGRAPH_KIND_TAG = "mixed_paragraph_kind"
"""段落级·自洽性：同一段落混影子与核心逻辑节点（潜在解析边界瑕疵）。"""

MULTI_PRIMARY_PER_PARAGRAPH_TAG = "multi_primary_per_paragraph"
"""段落级·边界匹配：同一段落含多于一个根节点（一段多根·潜在边界瑕疵）。"""

MULTI_MAIN_CLAIM_TAG = "multi_main_claim"
"""全局·跨段论点一致：全树多于一个 ``main_claim``（潜在跨段论点不一致）。"""

DUPLICATE_QUALIFICATION_TAG = "duplicate_qualification"
"""全局·术语定义一致：两个 ``qualification`` 归一化内容完全相同（潜在冗余瑕疵）。"""


def consistency(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """对标注完成的树跑单次一致性扫描，返回贴批注后的新树（不修改输入）。

    - **只追加** ``issue_tags``（去重，保留既有标签如 ``conflict`` / ``weakening``）。
    - 不改 ``content`` / ``status`` / ``merge_decision`` / ``candidate_hypotheses``。
    - 不置 ``adopted`` / ``corrected``（彼时不应存在；本 seam 亦不读此二者）。
    - 单次扫描、无打回、无重调度（ADR-0012）；幂等：重复调用不重复贴标签。
    """

    # 单次扫描收集每节点的新增标签，最后统一去重追加——天然单次、不打回。
    extra: dict[str, list[str]] = {n.node_id: [] for n in tree}
    _scan_paragraph_kind(tree, extra)
    _scan_multi_primary_per_paragraph(tree, extra)
    _scan_multi_main_claim(tree, extra)
    _scan_duplicate_qualification(tree, extra)

    out: list[ArgumentationNode] = []
    for node in tree:
        tags = list(node.issue_tags)
        for tag in extra.get(node.node_id, []):
            if tag not in tags:
                tags.append(tag)
        out.append(node.model_copy(update={"issue_tags": tags}) if tags else node.model_copy())
    return out


# --------------------------------------------------------------------------- #
# 段落级：自洽性 —— 同段混影子与核心逻辑节点
# --------------------------------------------------------------------------- #


def _scan_paragraph_kind(
    tree: list[ArgumentationNode], extra: dict[str, list[str]]
) -> None:
    """同一段落同时含影子与核心逻辑节点 → 该段全部节点贴 ``mixed_paragraph_kind``。"""

    by_paragraph: dict[str, list[ArgumentationNode]] = {}
    for node in tree:
        by_paragraph.setdefault(node.paragraph_id, []).append(node)
    for nodes in by_paragraph.values():
        has_shadow = any(n.node_type.is_shadow for n in nodes)
        has_core = any(not n.node_type.is_shadow for n in nodes)
        if has_shadow and has_core:
            for n in nodes:
                extra[n.node_id].append(MIXED_PARAGRAPH_KIND_TAG)


# --------------------------------------------------------------------------- #
# 段落级：边界匹配 —— 同段多根
# --------------------------------------------------------------------------- #


def _scan_multi_primary_per_paragraph(
    tree: list[ArgumentationNode], extra: dict[str, list[str]]
) -> None:
    """同一段落含多于一个根节点（``parent_id`` 为 ``None``）→ 这些根节点贴标签。"""

    roots_by_paragraph: dict[str, list[ArgumentationNode]] = {}
    for node in tree:
        if node.parent_id is None:
            roots_by_paragraph.setdefault(node.paragraph_id, []).append(node)
    for roots in roots_by_paragraph.values():
        if len(roots) > 1:
            for n in roots:
                extra[n.node_id].append(MULTI_PRIMARY_PER_PARAGRAPH_TAG)


# --------------------------------------------------------------------------- #
# 全局：跨段论点一致 —— 多主论点
# --------------------------------------------------------------------------- #


def _scan_multi_main_claim(
    tree: list[ArgumentationNode], extra: dict[str, list[str]]
) -> None:
    """全树多于一个 ``main_claim`` → 所有 ``main_claim`` 节点贴 ``multi_main_claim``。"""

    mains = [n for n in tree if n.node_type is NodeType.MAIN_CLAIM]
    if len(mains) > 1:
        for n in mains:
            extra[n.node_id].append(MULTI_MAIN_CLAIM_TAG)


# --------------------------------------------------------------------------- #
# 全局：术语定义一致 —— 重复限定（归一化内容完全相同）
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """归一化文本以判定「同一限定被逐字重复」（空白与大小写不敏感）。"""

    return " ".join(text.split()).casefold()


def _scan_duplicate_qualification(
    tree: list[ArgumentationNode], extra: dict[str, list[str]]
) -> None:
    """两个 ``qualification`` 节点归一化内容完全相同 → 双方贴 ``duplicate_qualification``。"""

    by_norm: dict[str, list[ArgumentationNode]] = {}
    for node in tree:
        if node.node_type is NodeType.QUALIFICATION:
            by_norm.setdefault(_normalize(node.content), []).append(node)
    for group in by_norm.values():
        if len(group) > 1:
            for n in group:
                extra[n.node_id].append(DUPLICATE_QUALIFICATION_TAG)
