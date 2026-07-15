"""一致性校验 Agent（PRD §9、issue #8、ADR-0012）：批注门禁·只贴 issue_tags·不打回。

在影响传导（#7）之后、修订确认（#9）之前对**那棵已标注完成的树**扫描**一次**，
执行段落级校验（自洽性、边界匹配、段落原文重复）与全局校验（跨段论点一致）。
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

T-02：段落归属改由 ``paragraph_list.argument_tree_ids`` 正向分组（不再扫全树按
``Argument.paragraph_id`` 反向 join），段落原文重复改用 ``ParagraphRecord.original_content``
去重（不再读 ``Argument.content``）——数据访问路径迁移、扫描结构不动。

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
3. ``duplicate_paragraph_content``（段落原文重复）：两个段落的归一化 ``original_content``
   完全相同——同一段原文被逐字重复，是潜在冗余瑕疵。重复段的全部节点贴标签。
   （T-02 前为逐 ``qualification`` 节点 ``content`` 去重；字段移除后按段原文去重。）

全局：

4. ``multi_main_claim``（跨段论点一致）：全树多于一个 ``main_claim`` 节点——
   多主论点是潜在跨段论点不一致。所有 ``main_claim`` 节点贴标签。
"""

from __future__ import annotations

from domain import Argument, ArgumentType, ParagraphRecord

__all__ = [
    "MIXED_PARAGRAPH_KIND_TAG",
    "MULTI_PRIMARY_PER_PARAGRAPH_TAG",
    "MULTI_MAIN_CLAIM_TAG",
    "DUPLICATE_PARAGRAPH_CONTENT_TAG",
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

DUPLICATE_PARAGRAPH_CONTENT_TAG = "duplicate_paragraph_content"
"""段落级·原文重复：两个段落归一化 ``original_content`` 完全相同（潜在冗余瑕疵）。"""


def consistency(
    argument_tree: list[Argument], paragraph_list: list[ParagraphRecord]
) -> list[Argument]:
    """对标注完成的树跑单次一致性扫描，返回贴批注后的新树（不修改输入）。

    T-02：段落归属经 ``paragraph_list.argument_tree_ids`` 正向分组、原文重复按
    ``original_content`` 去重（不再读 ``Argument.paragraph_id`` / ``.content``）。

    - **只追加** ``issue_tags``（去重，保留既有标签如 ``conflict`` / ``weakening``）。
    - 不改 ``content`` / ``status`` / ``merge_decision`` / ``candidate_hypotheses``。
    - 不置 ``adopted`` / ``corrected``（彼时不应存在；本 seam 亦不读此二者）。
    - 单次扫描、无打回、无重调度（ADR-0012）；幂等：重复调用不重复贴标签。
    """

    # 单次扫描收集每节点的新增标签，最后统一去重追加——天然单次、不打回。
    extra: dict[str, list[str]] = {n.argument_id: [] for n in argument_tree}
    by_id: dict[str, Argument] = {n.argument_id: n for n in argument_tree}
    _scan_paragraph_kind(paragraph_list, by_id, extra)
    _scan_multi_primary_per_paragraph(paragraph_list, by_id, extra)
    _scan_multi_main_claim(argument_tree, extra)
    _scan_duplicate_paragraph_content(paragraph_list, by_id, extra)

    out: list[Argument] = []
    for argument in argument_tree:
        tags = list(argument.issue_tags)
        for tag in extra.get(argument.argument_id, []):
            if tag not in tags:
                tags.append(tag)
        out.append(argument.model_copy(update={"issue_tags": tags}) if tags else argument.model_copy())
    return out


def _paragraph_nodes(
    record: ParagraphRecord, by_id: dict[str, Argument]
) -> list[Argument]:
    """按 ``argument_tree_ids`` 正向解析该段节点（T-02：取代 ``Argument.paragraph_id`` 反向 join）。"""

    return [by_id[aid] for aid in record.argument_tree_ids if aid in by_id]


# --------------------------------------------------------------------------- #
# 段落级：自洽性 —— 同段混影子与核心逻辑节点
# --------------------------------------------------------------------------- #


def _scan_paragraph_kind(
    paragraph_list: list[ParagraphRecord],
    by_id: dict[str, Argument],
    extra: dict[str, list[str]],
) -> None:
    """同一段落同时含影子与核心逻辑节点 → 该段全部节点贴 ``mixed_paragraph_kind``。"""

    for record in paragraph_list:
        nodes = _paragraph_nodes(record, by_id)
        has_shadow = any(n.argument_type.is_shadow for n in nodes)
        has_core = any(not n.argument_type.is_shadow for n in nodes)
        if has_shadow and has_core:
            for n in nodes:
                extra[n.argument_id].append(MIXED_PARAGRAPH_KIND_TAG)


# --------------------------------------------------------------------------- #
# 段落级：边界匹配 —— 同段多根
# --------------------------------------------------------------------------- #


def _scan_multi_primary_per_paragraph(
    paragraph_list: list[ParagraphRecord],
    by_id: dict[str, Argument],
    extra: dict[str, list[str]],
) -> None:
    """同一段落含多于一个根节点（``parent_id`` 为 ``None``）→ 这些根节点贴标签。"""

    for record in paragraph_list:
        roots = [
            n for n in _paragraph_nodes(record, by_id) if n.parent_id is None
        ]
        if len(roots) > 1:
            for n in roots:
                extra[n.argument_id].append(MULTI_PRIMARY_PER_PARAGRAPH_TAG)


# --------------------------------------------------------------------------- #
# 全局：跨段论点一致 —— 多主论点
# --------------------------------------------------------------------------- #


def _scan_multi_main_claim(
    argument_tree: list[Argument], extra: dict[str, list[str]]
) -> None:
    """全树多于一个 ``main_claim`` → 所有 ``main_claim`` 节点贴 ``multi_main_claim``。"""

    mains = [n for n in argument_tree if n.argument_type is ArgumentType.MAIN_CLAIM]
    if len(mains) > 1:
        for n in mains:
            extra[n.argument_id].append(MULTI_MAIN_CLAIM_TAG)


# --------------------------------------------------------------------------- #
# 段落级：原文重复 —— 段落 original_content 归一化相同（T-02：按段原文去重）
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """归一化文本以判定「同一段原文被逐字重复」（空白与大小写不敏感）。"""

    return " ".join(text.split()).casefold()


def _scan_duplicate_paragraph_content(
    paragraph_list: list[ParagraphRecord],
    by_id: dict[str, Argument],
    extra: dict[str, list[str]],
) -> None:
    """两个段落归一化 ``original_content`` 完全相同 → 双方段内全部节点贴标签。"""

    by_norm: dict[str, list[ParagraphRecord]] = {}
    for record in paragraph_list:
        if not record.original_content:
            continue
        by_norm.setdefault(_normalize(record.original_content), []).append(record)
    for group in by_norm.values():
        if len(group) > 1:
            for record in group:
                for n in _paragraph_nodes(record, by_id):
                    extra[n.argument_id].append(DUPLICATE_PARAGRAPH_CONTENT_TAG)
