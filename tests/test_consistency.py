"""一致性校验 Agent 测试（issue #8、PRD §9、ADR-0012）。

行为级黑盒测试（PRD «Testing Decisions»）：通过纯函数 seam（:func:`consistency`）
驱动「标注完成的树 → 贴批注后的同一棵树」，断言：

- 在影响传导（#7）之后、HITL-2（#9）之前对标注完成的树**单次扫描**（ADR-0012）。
- 执行段落级（自洽性、边界匹配）与全局（跨段论点一致、术语定义一致）校验。
- **仅贴** ``issue_tags`` 批注，**无拒绝 / 打回 / 重调度权**（批注门禁）。
- 看不到 ``adopted`` / ``corrected``（彼时尚未产生），不因采纳引入新冲突而回炉重扫。
- 流水线单向推进，冲突作为批注留存不打回重算。

``consistency`` 无 LLM / 检索依赖（确定性纯函数），故无需注入桩——与 ``merge`` /
``impact`` 同形。语义级术语 / 论点一致性需 LLM，本版为确定性代理 + 已知缺口
（见 ``consistency.py`` 模块 docstring）。
"""

from __future__ import annotations

from agents.consistency import (
    DUPLICATE_PARAGRAPH_CONTENT_TAG,
    MIXED_PARAGRAPH_KIND_TAG,
    MULTI_MAIN_CLAIM_TAG,
    MULTI_PRIMARY_PER_PARAGRAPH_TAG,
    consistency,
)
from agents.hypothesis import Hypothesis, HypothesisRelation, HypothesisStatus
from domain import (
    Argument,
    ArgumentStatus,
    ArgumentType,
    MergeAction,
    MergeDecision,
    ParagraphRecord,
)

# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #


def _argument(
    argument_id: str,
    *,
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    paragraph_id: str = "p0001",
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    content: str = "",
    status: ArgumentStatus = ArgumentStatus.CREDIBLE,
    issue_tags: list[str] | None = None,
) -> Argument:
    """构造一个通用节点（默认 evidence / credible / 单段）。"""

    arg = Argument(
        argument_id=argument_id,
        argument_type=argument_type,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        status=status,
        issue_tags=list(issue_tags or []),
    )
    object.__setattr__(arg, "_test_paragraph_id", paragraph_id)
    object.__setattr__(arg, "_test_content", content)
    return arg


def _by_id(argument_tree: list[Argument]) -> dict[str, Argument]:
    return {n.argument_id: n for n in argument_tree}


def _paragraph_list(
    argument_tree: list[Argument],
    originals: dict[str, str] | None = None,
) -> list[ParagraphRecord]:
    """从树构造 paragraph_list：按 ``argument_id`` 所属段（``paragraph_id``）正向分组。

    T-02：consistency 按 ``argument_tree_ids`` 分组、按 ``original_content`` 去重。测试树各
    节点带 ``paragraph_id``（双读过渡态字段），故据此分组；``original_content`` 由 ``originals``
    显式给出（默认空——重复扫描跳过空原文，不影响其它扫描）。
    """

    originals = originals or {}
    by_para: dict[str, list[str]] = {}
    for a in argument_tree:
        by_para.setdefault(getattr(a, "_test_paragraph_id", "p0001"), []).append(a.argument_id)
    return [
        ParagraphRecord(
            paragraph_id=pid,
            original_content=originals.get(pid, ""),
            argument_tree_ids=ids,
        )
        for pid, ids in by_para.items()
    ]


# --------------------------------------------------------------------------- #
# 纯函数 seam：无瑕疵树原样通过、返回新实例（tracer bullet）
# --------------------------------------------------------------------------- #


class TestConsistencySeam:
    """``consistency`` 是纯函数子缝：标注后的树 → 贴批注后的同一棵树。"""

    def test_clean_tree_no_new_tags_and_new_instances(self):
        """无任何一致性瑕疵的树：不贴新批注、但返回新实例（与 merge/impact 同形）。"""

        argument_tree = [
            _argument(
                "m",
                argument_type=ArgumentType.MAIN_CLAIM,
                paragraph_id="p0001",
                children_ids=["s"],
                content="主论点",
            ),
            _argument(
                "s",
                argument_type=ArgumentType.SUB_CLAIM,
                paragraph_id="p0002",
                parent_id="m",
                content="分论点",
            ),
        ]
        out = consistency(argument_tree, _paragraph_list(argument_tree))
        assert [n.argument_id for n in out] == ["m", "s"]
        assert all(o is not n for o, n in zip(out, argument_tree, strict=True))
        assert all(o.issue_tags == [] for o in out)


# --------------------------------------------------------------------------- #
# 段落级 · 自洽性：同段混影子与核心逻辑节点
# --------------------------------------------------------------------------- #


class TestMixedParagraphKind:
    """``mixed_paragraph_kind``：影子段落与实质段落不应混同一段。"""

    def test_shadow_plus_core_in_same_paragraph_tags_all(self):
        """一段含 background + evidence → 该段全部节点贴 ``mixed_paragraph_kind``。"""

        argument_tree = [
            _argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0001"),
            _argument("ev", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MIXED_PARAGRAPH_KIND_TAG in out["bg"].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in out["ev"].issue_tags

    def test_evaluation_plus_sub_claim_in_same_paragraph_tags_all(self):
        """evaluation（影子）+ sub_claim（核心）同段亦贴标签。"""

        argument_tree = [
            _argument("ev1", argument_type=ArgumentType.EVALUATION, paragraph_id="p0001"),
            _argument("sc", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0001"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MIXED_PARAGRAPH_KIND_TAG in out["ev1"].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in out["sc"].issue_tags

    def test_pure_shadow_paragraph_not_tagged(self):
        """纯影子段（多个影子节点、无核心）不贴标签。"""

        argument_tree = [
            _argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0001"),
            _argument("ev1", argument_type=ArgumentType.EVALUATION, paragraph_id="p0001"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert all(MIXED_PARAGRAPH_KIND_TAG not in n.issue_tags for n in out.values())

    def test_pure_core_paragraph_not_tagged(self):
        """纯核心段（多个核心节点、无影子）不贴标签。"""

        argument_tree = [
            _argument("sc", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0001"),
            _argument("ev", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert all(MIXED_PARAGRAPH_KIND_TAG not in n.issue_tags for n in out.values())

    def test_mixed_in_one_paragraph_does_not_leak_to_other_paragraphs(self):
        """混段标签只贴在出问题的段；另一独立纯净段不受波及。"""

        argument_tree = [
            _argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0001"),
            _argument("ev", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001"),
            _argument("sc", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0002"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MIXED_PARAGRAPH_KIND_TAG in out["bg"].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in out["ev"].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG not in out["sc"].issue_tags


# --------------------------------------------------------------------------- #
# 段落级 · 边界匹配：同段多根
# --------------------------------------------------------------------------- #


class TestMultiPrimaryPerParagraph:
    """``multi_primary_per_paragraph``：一段不应含多于一个根节点。"""

    def test_two_roots_same_paragraph_tagged(self):
        """一段含两个 parent_id=None 的节点 → 两个根均贴标签。"""

        argument_tree = [
            _argument("r1", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _argument("r2", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MULTI_PRIMARY_PER_PARAGRAPH_TAG in out["r1"].issue_tags
        assert MULTI_PRIMARY_PER_PARAGRAPH_TAG in out["r2"].issue_tags

    def test_single_root_per_paragraph_not_tagged(self):
        """每段单根（即便全树多根、但分属不同段）不贴标签。"""

        argument_tree = [
            _argument("r1", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _argument("r2", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0002"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        # 每段各一根 → 边界正常（multi_main_claim 另算，见下）。
        assert all(MULTI_PRIMARY_PER_PARAGRAPH_TAG not in n.issue_tags for n in out.values())

    def test_child_with_parent_not_counted_as_root(self):
        """有 parent 的子节点不计为根，不影响「同段多根」判定。"""

        argument_tree = [
            _argument("r", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _argument("c", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001", parent_id="r"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MULTI_PRIMARY_PER_PARAGRAPH_TAG not in out["r"].issue_tags
        assert MULTI_PRIMARY_PER_PARAGRAPH_TAG not in out["c"].issue_tags


# --------------------------------------------------------------------------- #
# 全局 · 跨段论点一致：多主论点
# --------------------------------------------------------------------------- #


class TestMultiMainClaim:
    """``multi_main_claim``：全树不应多于一个主论点。"""

    def test_two_main_claims_tagged(self):
        """全树两个 main_claim → 两个均贴 ``multi_main_claim``。"""

        argument_tree = [
            _argument("m1", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _argument("m2", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0002"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert MULTI_MAIN_CLAIM_TAG in out["m1"].issue_tags
        assert MULTI_MAIN_CLAIM_TAG in out["m2"].issue_tags

    def test_single_main_claim_not_tagged(self):
        """全树仅一个 main_claim → 不贴标签。"""

        argument_tree = [
            _argument("m", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _argument("s", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0002", parent_id="m"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert all(MULTI_MAIN_CLAIM_TAG not in n.issue_tags for n in out.values())

    def test_no_main_claim_not_tagged(self):
        """全树无 main_claim（如全 sub_claim 森林）→ 不贴标签。"""

        argument_tree = [
            _argument("s1", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0001"),
            _argument("s2", argument_type=ArgumentType.SUB_CLAIM, paragraph_id="p0002"),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert all(MULTI_MAIN_CLAIM_TAG not in n.issue_tags for n in out.values())


# --------------------------------------------------------------------------- #
# 段落级 · 原文重复：两段 original_content 归一化相同（T-02：按段原文去重）
# --------------------------------------------------------------------------- #


class TestDuplicateParagraphContent:
    """``duplicate_paragraph_content``：两段原文逐字重复是潜在冗余瑕疵。

    T-02 前 consistency 按 ``qualification`` 节点 ``content`` 去重；``Argument.content`` 移除后
    改按段落聚合根 ``original_content`` 去重（每段一份原文）。重复段的全部节点贴标签。
    """

    def test_two_identical_paragraphs_tagged(self):
        """两段原文相同 → 双方段内节点贴 ``duplicate_paragraph_content``。"""

        argument_tree = [
            _argument("q1", argument_type=ArgumentType.QUALIFICATION, paragraph_id="p0001", content="限速 60"),
            _argument("q2", argument_type=ArgumentType.QUALIFICATION, paragraph_id="p0002", content="限速 60"),
        ]
        originals = {"p0001": "限速 60", "p0002": "限速 60"}
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree, originals)))
        assert DUPLICATE_PARAGRAPH_CONTENT_TAG in out["q1"].issue_tags
        assert DUPLICATE_PARAGRAPH_CONTENT_TAG in out["q2"].issue_tags

    def test_whitespace_and_case_insensitive_normalization(self):
        """空白与大小写差异视为同一段原文（归一化判定）。"""

        argument_tree = [
            _argument(
                "q1",
                argument_type=ArgumentType.QUALIFICATION,
                paragraph_id="p0001",
                content="  限速   60  ",
            ),
            _argument(
                "q2",
                argument_type=ArgumentType.QUALIFICATION,
                paragraph_id="p0002",
                content="限速 60",
            ),
        ]
        originals = {"p0001": "  限速   60  ", "p0002": "限速 60"}
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree, originals)))
        assert DUPLICATE_PARAGRAPH_CONTENT_TAG in out["q1"].issue_tags
        assert DUPLICATE_PARAGRAPH_CONTENT_TAG in out["q2"].issue_tags

    def test_distinct_paragraphs_not_tagged(self):
        """两段原文不同 → 不贴标签。"""

        argument_tree = [
            _argument("q1", argument_type=ArgumentType.QUALIFICATION, paragraph_id="p0001", content="限速 60"),
            _argument("q2", argument_type=ArgumentType.QUALIFICATION, paragraph_id="p0002", content="限速 80"),
        ]
        originals = {"p0001": "限速 60", "p0002": "限速 80"}
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree, originals)))
        assert all(DUPLICATE_PARAGRAPH_CONTENT_TAG not in n.issue_tags for n in out.values())

    def test_single_paragraph_not_tagged(self):
        """仅一段 → 不贴标签。"""

        argument_tree = [
            _argument("q", argument_type=ArgumentType.QUALIFICATION, paragraph_id="p0001", content="限速 60"),
        ]
        originals = {"p0001": "限速 60"}
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree, originals)))
        assert DUPLICATE_PARAGRAPH_CONTENT_TAG not in out["q"].issue_tags

    def test_empty_original_content_skipped(self):
        """空 ``original_content`` 的段不参与重复判定（不与其它空段误判）。"""

        argument_tree = [
            _argument("e1", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001", content="同一论据"),
            _argument("e2", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0002", content="同一论据"),
        ]
        # 两段均无原文（空）→ 重复扫描跳过、不贴标签。
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        assert all(DUPLICATE_PARAGRAPH_CONTENT_TAG not in n.issue_tags for n in out.values())


# --------------------------------------------------------------------------- #
# 批注门禁契约（ADR-0012）：只贴 issue_tags · 不打回 · 不替人拍板 · 单次幂等
# --------------------------------------------------------------------------- #


def _annotated_argument(
    argument_id: str,
    *,
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    paragraph_id: str = "p0001",
    parent_id: str | None = None,
    status: ArgumentStatus = ArgumentStatus.DOUBTFUL,
    issue_tags: list[str] | None = None,
    merge_decision: MergeDecision | None = None,
    candidate_hypotheses: list[Hypothesis] | None = None,
    content: str = "原文不可改",
) -> Argument:
    """构造一个带合并裁决 / 假设 / 既有批注的节点（模拟影响传导 #7 产出）。"""

    arg = Argument(
        argument_id=argument_id,
        argument_type=argument_type,
        parent_id=parent_id,
        status=status,
        issue_tags=list(issue_tags or []),
        candidate_hypotheses=list(candidate_hypotheses or []),
        merge_decision=merge_decision,
    )
    object.__setattr__(arg, "_test_paragraph_id", paragraph_id)
    object.__setattr__(arg, "_test_content", content)
    return arg


def _hyp(hid: str = "h1") -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        text="假设",
        relation=HypothesisRelation.OPPOSE,
        status=HypothesisStatus.SUPPORTED,
        confidence=0.5,
    )


class TestConsistencyGateMechanism:
    """批注门禁：只追加 ``issue_tags``，无拒绝 / 打回 / 重调度权。"""

    def test_only_appends_issue_tags_preserves_everything_else(self):
        """贴批注时：status/content/merge_decision/candidate_hypotheses 原样不动。"""

        decision = MergeDecision(
            action=MergeAction.REPLACE, activated_hypothesis_ids=["h1"]
        )
        h = _hyp("h1")
        # 段混影子+核心 → 触发 mixed_paragraph_kind，但只应改 issue_tags。
        argument_tree = [
            _annotated_argument(
                "bg",
                argument_type=ArgumentType.BACKGROUND,
                paragraph_id="p0001",
                status=ArgumentStatus.CREDIBLE,
                content="影子原文",
            ),
            _annotated_argument(
                "ev",
                argument_type=ArgumentType.EVIDENCE,
                paragraph_id="p0001",
                status=ArgumentStatus.DOUBTFUL,
                issue_tags=["conflict"],
                merge_decision=decision,
                candidate_hypotheses=[h],
                content="论据原文",
            ),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        bg, ev = out["bg"], out["ev"]
        # 唯一变化：追加 mixed_paragraph_kind。
        assert MIXED_PARAGRAPH_KIND_TAG in bg.issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in ev.issue_tags
        # status 不动。
        assert bg.status is ArgumentStatus.CREDIBLE
        assert ev.status is ArgumentStatus.DOUBTFUL
        # 节点纯推理字段不动（不产文本）。
        # merge_decision 不动。
        assert ev.merge_decision == decision
        # candidate_hypotheses 不动（id 集合一致）。
        assert [h2.hypothesis_id for h2 in ev.candidate_hypotheses] == ["h1"]

    def test_preserves_existing_tags_dedup_append(self):
        """既有批注（conflict / weakening）保留、新批注去重追加、不覆盖。

        bg+ev 同段、同为根、影子+核心 → 同时触发 ``mixed_paragraph_kind`` 与
        ``multi_primary_per_paragraph``；既有标签在前、新标签去重追加在后。
        """

        argument_tree = [
            _annotated_argument(
                "bg",
                argument_type=ArgumentType.BACKGROUND,
                paragraph_id="p0001",
                issue_tags=["weakening", "conflict"],
            ),
            _annotated_argument(
                "ev",
                argument_type=ArgumentType.EVIDENCE,
                paragraph_id="p0001",
                issue_tags=["conflict"],  # 既有 conflict
            ),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        # 既有标签全保留、顺序不变；两条新标签去重追加在后。
        assert out["bg"].issue_tags == [
            "weakening",
            "conflict",
            MIXED_PARAGRAPH_KIND_TAG,
            MULTI_PRIMARY_PER_PARAGRAPH_TAG,
        ]
        assert out["ev"].issue_tags == [
            "conflict",
            MIXED_PARAGRAPH_KIND_TAG,
            MULTI_PRIMARY_PER_PARAGRAPH_TAG,
        ]

    def test_does_not_mutate_input_tree(self):
        """返回新树；输入节点（issue_tags/status/content/merge_decision）原样不变。"""

        decision = MergeDecision(action=MergeAction.CONFLICT, activated_hypothesis_ids=["h1"])
        argument = _annotated_argument(
            "ev",
            argument_type=ArgumentType.EVIDENCE,
            paragraph_id="p0001",
            issue_tags=["conflict"],
            merge_decision=decision,
            candidate_hypotheses=[_hyp("h1")],
        )
        argument_tree = [
            _annotated_argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0001"),
            argument,
        ]
        before = {n.argument_id: n.model_copy(deep=True) for n in argument_tree}

        consistency(argument_tree, _paragraph_list(argument_tree))

        for n in argument_tree:
            assert n == before[n.argument_id], f"输入树被改写：{n.argument_id}"

    def test_returns_new_instances_even_when_no_tags_added(self):
        """无瑕疵树：不贴标签，但仍返回新实例（与 merge/impact 同形）。"""

        argument_tree = [
            _annotated_argument("m", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
        ]
        out = consistency(argument_tree, _paragraph_list(argument_tree))
        assert all(o is not n for o, n in zip(out, argument_tree, strict=True))
        assert all(o.issue_tags == [] for o in out)

    def test_idempotent_no_double_tagging_on_re_scan(self):
        """单次扫描 / 不回炉：重复调用结果一致、批注不翻倍（ADR-0012）。"""

        argument_tree = [
            _annotated_argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0001"),
            _annotated_argument("ev", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0001"),
        ]
        once = consistency(argument_tree, _paragraph_list(argument_tree))
        twice = consistency(once, _paragraph_list(once))
        # 再扫一次：每节点模型相等（批注集合与顺序完全一致，不重复追加）。
        assert [n.model_dump() for n in twice] == [n.model_dump() for n in once]
        for n in twice:
            # 无重复标签（幂等：再扫不翻倍）。
            assert len(n.issue_tags) == len(set(n.issue_tags))
            # 触发的两条标签都稳定在位。
            assert MIXED_PARAGRAPH_KIND_TAG in n.issue_tags
            assert MULTI_PRIMARY_PER_PARAGRAPH_TAG in n.issue_tags

    def test_does_not_act_on_adopted_or_corrected_status(self):
        """看不到 adopted/corrected（彼时不应存在）；即便误在也不据此贴标或改判。

        一致性校验只看结构 / 类型 / 段落 / 内容归一化，不读 status 来贴标签——
        故 adopted/corrected 节点不会被特殊处理，亦不会被改回（不替人拍板）。
        """

        argument_tree = [
            _annotated_argument(
                "ad",
                argument_type=ArgumentType.EVIDENCE,
                paragraph_id="p0001",
                status=ArgumentStatus.ADOPTED,
                content="已采纳",
            ),
            _annotated_argument(
                "co",
                argument_type=ArgumentType.EVIDENCE,
                paragraph_id="p0002",
                status=ArgumentStatus.CORRECTED,
                content="已回写",
            ),
        ]
        out = _by_id(consistency(argument_tree, _paragraph_list(argument_tree)))
        # 不改 status（绝不替人拍板：不回退 adopted/corrected）。
        assert out["ad"].status is ArgumentStatus.ADOPTED
        assert out["co"].status is ArgumentStatus.CORRECTED
        # 这两个节点分属不同段、各一核心节点、无主论点重复、无重复限定 → 不贴任何标签。
        assert out["ad"].issue_tags == []
        assert out["co"].issue_tags == []

    def test_no_reject_no_reschedule_single_pass(self):
        """门禁不打回：所有节点（含被贴标者）一律流入返回树，无节点被丢弃或重排。"""

        argument_tree = [
            _annotated_argument("m1", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0001"),
            _annotated_argument("m2", argument_type=ArgumentType.MAIN_CLAIM, paragraph_id="p0002"),
            _annotated_argument("bg", argument_type=ArgumentType.BACKGROUND, paragraph_id="p0003"),
            _annotated_argument(
                "ev", argument_type=ArgumentType.EVIDENCE, paragraph_id="p0003"
            ),  # p0003 混段
        ]
        out = consistency(argument_tree, _paragraph_list(argument_tree))
        # 节点数量与顺序不变（不打回、不丢弃、不重调度）。
        assert [n.argument_id for n in out] == ["m1", "m2", "bg", "ev"]
        # 有问题的节点被贴标、但仍留在树里交 HITL-2。
        assert MULTI_MAIN_CLAIM_TAG in out[0].issue_tags
        assert MULTI_MAIN_CLAIM_TAG in out[1].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in out[2].issue_tags
        assert MIXED_PARAGRAPH_KIND_TAG in out[3].issue_tags
