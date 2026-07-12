"""影响传导 Agent 测试（issue #7、PRD §8、ADR-0003/0013）。

行为级黑盒测试（PRD «Testing Decisions»）：通过纯函数 seam（``compute_residual_support``
/``verdict_for_ratio``/``impact``）驱动「剩余支撑率 → 失效/弱化/不受影响」判决，断言：

- 失效判定公式可复算、可解释（ADR-0013）：给定子节点权重与存活情况，``ratio`` 与
  ``verdict`` 唯一确定。
- ``< 0.5`` → ``invalid``；``0.5–0.7`` → 贴「弱化」批注不失效；``≥ 0.7`` 不受影响。
- 影响传导在合并之后**串行**运行、读取标注完成的树、**不产任何替代文本**（不改
  ``content``、不新建假设）。
- ``error``（叶子论据自证其伪，体检判决）与 ``invalid``（上层论点被拖垮，影响传导判决）
  状态分工正确；``error`` 上层论点不再改判 ``invalid``。
- 失效逐层上推（后序）：``invalid`` 子节点对父论点计为不存活，使失效传至根。
- 影子子节点不参与传导；塌方时复用节点已有成立假设去激活，仅复用、绝不新建。
- 纯函数：不改输入树、不改 ``content``、不置 ``adopted``/``corrected``。

``impact`` 无 LLM / 检索依赖（确定性纯函数），故无需注入桩——与 ``merge`` 同形。
"""

from __future__ import annotations

import pytest

from hypoargus.domain import (
    ArgumentationNode,
    MergeAction,
    MergeDecision,
    NodeStatus,
    NodeType,
)
from hypoargus.hypothesis import Hypothesis, HypothesisRelation, HypothesisStatus
from hypoargus.impact import (
    INVALID_RATIO_THRESHOLD,
    WEAKEN_RATIO_THRESHOLD,
    WEAKENING_TAG,
    ImpactVerdict,
    compute_residual_support,
    impact,
    verdict_for_ratio,
)

# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #


def _evidence(
    node_id: str,
    *,
    status: NodeStatus = NodeStatus.CREDIBLE,
    weight: int = 50,
    parent_id: str | None = None,
    content: str = "e",
) -> ArgumentationNode:
    """构造一个 evidence 子节点（默认存活、权重 50）。"""

    return ArgumentationNode(
        node_id=node_id,
        node_type=NodeType.EVIDENCE,
        parent_id=parent_id,
        paragraph_id="p0001",
        content=content,
        argument_weight=weight,
        status=status,
    )


def _claim(
    node_id: str,
    *,
    node_type: NodeType = NodeType.SUB_CLAIM,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    weight: int = 60,
    status: NodeStatus = NodeStatus.CREDIBLE,
    content: str = "c",
    hypotheses: list[Hypothesis] | None = None,
    issue_tags: list[str] | None = None,
) -> ArgumentationNode:
    """构造一个上层论点节点（main_claim / sub_claim）。"""

    return ArgumentationNode(
        node_id=node_id,
        node_type=node_type,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        paragraph_id="p0001",
        content=content,
        argument_weight=weight,
        status=status,
        candidate_hypotheses=list(hypotheses or []),
        issue_tags=list(issue_tags or []),
    )


def _hyp(
    hid: str,
    *,
    relation: HypothesisRelation = HypothesisRelation.OPPOSE,
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
    confidence: float = 0.5,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        text="h",
        relation=relation,
        status=status,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# 失效判定公式（纯函数 seam · 可复算、可解释 · ADR-0013）
# --------------------------------------------------------------------------- #


class TestFormula:
    """``compute_residual_support`` 与 ``verdict_for_ratio`` 的纯函数契约。"""

    def test_ratio_all_surviving_is_one_unaffected(self):
        """全部存活 → ratio = 1.0 → 不受影响。"""

        children = [_evidence("c1", weight=40), _evidence("c2", weight=60)]
        support = compute_residual_support(children)
        assert support.total_weight == 100
        assert support.surviving_weight == 100
        assert support.ratio == 1.0
        assert support.participating_children == 2
        assert verdict_for_ratio(support.ratio) is ImpactVerdict.UNAFFECTED

    def test_ratio_all_dead_is_zero_invalid(self):
        """全部 error → ratio = 0 → invalid。"""

        children = [
            _evidence("c1", status=NodeStatus.ERROR, weight=40),
            _evidence("c2", status=NodeStatus.ERROR, weight=60),
        ]
        support = compute_residual_support(children)
        assert support.surviving_weight == 0
        assert support.total_weight == 100
        assert support.ratio == 0.0
        assert verdict_for_ratio(support.ratio) is ImpactVerdict.INVALID

    def test_ratio_below_half_invalid_boundary(self):
        """存活权重 30/100 = 0.3 < 0.5 → invalid；恰好 0.5 不失效。"""

        children = [_evidence("c1", weight=30), _evidence("dead", status=NodeStatus.ERROR, weight=70)]
        assert compute_residual_support(children).ratio == pytest.approx(0.3)
        assert verdict_for_ratio(0.3) is ImpactVerdict.INVALID
        # 边界：0.5 属弱化下界、不判 invalid（ADR-0013「0.5~0.7 弱化」含 0.5）。
        assert verdict_for_ratio(INVALID_RATIO_THRESHOLD) is ImpactVerdict.WEAKEN

    def test_ratio_weaken_band_inclusive_lower_exclusive_upper(self):
        """0.5 ≤ ratio < 0.7 → 弱化；0.7 恰好不受影响。"""

        assert verdict_for_ratio(0.5) is ImpactVerdict.WEAKEN
        assert verdict_for_ratio(0.6) is ImpactVerdict.WEAKEN
        # 0.7 为不受影响下界（「≥ 0.7 不受影响」）。
        assert verdict_for_ratio(WEAKEN_RATIO_THRESHOLD) is ImpactVerdict.UNAFFECTED
        assert verdict_for_ratio(0.99) is ImpactVerdict.UNAFFECTED

    def test_doubtful_child_counts_as_surviving(self):
        """doubtful 子节点仍计存活（ADR-0013 失效判定·TS 先验语义）。"""

        children = [
            _evidence("doubt", status=NodeStatus.DOUBTFUL, weight=60),
            _evidence("dead", status=NodeStatus.ERROR, weight=40),
        ]
        support = compute_residual_support(children)
        assert support.surviving_weight == 60
        assert support.ratio == pytest.approx(0.6)
        assert verdict_for_ratio(support.ratio) is ImpactVerdict.WEAKEN

    def test_invalid_child_counts_as_non_surviving(self):
        """已被判 invalid 的子节点计为不存活（使失效逐层上推）。"""

        children = [
            _evidence("inv", status=NodeStatus.INVALID, weight=60),
            _evidence("dead", status=NodeStatus.ERROR, weight=40),
        ]
        support = compute_residual_support(children)
        assert support.surviving_weight == 0
        assert support.ratio == 0.0

    def test_shadow_children_excluded_from_ratio(self):
        """影子子节点（background/evaluation）不参与传导——既不计入分母也不计入分子。"""

        bg = ArgumentationNode(
            node_id="bg",
            node_type=NodeType.BACKGROUND,
            paragraph_id="p0001",
            argument_weight=0,
            status=NodeStatus.CREDIBLE,
        )
        ev = _evidence("c1", weight=50)
        support = compute_residual_support([bg, ev])
        assert support.participating_children == 1  # 仅 evidence 参与
        assert support.total_weight == 50
        assert support.ratio == 1.0

    def test_zero_participating_weight_is_unaffected(self):
        """无参与传导的子节点（全影子 / 全权重 0）→ 不崩盘、不受影响（0/0 守为 1.0）。"""

        assert compute_residual_support([]).ratio == 1.0
        assert compute_residual_support([]).participating_children == 0
        all_zero = [_evidence("z", weight=0)]
        assert compute_residual_support(all_zero).ratio == 1.0
        assert verdict_for_ratio(1.0) is ImpactVerdict.UNAFFECTED

    def test_ratio_is_recomputable_and_explainable(self):
        """给定权重与存活，ratio 与 rationale 可复算、可解释（ADR-0013）。"""

        children = [
            _evidence("a", weight=30),
            _evidence("b", status=NodeStatus.ERROR, weight=70),
        ]
        support = compute_residual_support(children)
        # 同输入再算一次：结果一致（纯函数、可复算）。
        again = compute_residual_support(
            [_evidence("a", weight=30), _evidence("b", status=NodeStatus.ERROR, weight=70)]
        )
        assert support == again
        rationale = support.rationale()
        assert "0.3" in rationale or "30" in rationale
        assert "invalid" in rationale.lower()


# --------------------------------------------------------------------------- #
# 失效上推（impact 主入口 · 串行·不产文本·后序逐层上推）
# --------------------------------------------------------------------------- #


def _by_id(tree: list[ArgumentationNode]) -> dict[str, ArgumentationNode]:
    return {n.node_id: n for n in tree}


class TestImpactInvalidation:
    """``impact`` 对上层论点的 ``invalid`` / 弱化 / 不受影响判决。"""

    def test_sub_claim_below_half_becomes_invalid(self):
        """存活权重 30/100 = 0.3 < 0.5 → sub_claim 置 ``invalid``；子节点不动。"""

        tree = [
            _claim("n0", children_ids=["c1", "c2"], status=NodeStatus.CREDIBLE),
            _evidence("c1", status=NodeStatus.ERROR, weight=70, parent_id="n0"),
            _evidence("c2", status=NodeStatus.CREDIBLE, weight=30, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.INVALID
        # 子节点状态不动（影响传导不判叶子、不改体检结论）。
        assert out["c1"].status is NodeStatus.ERROR
        assert out["c2"].status is NodeStatus.CREDIBLE

    def test_main_claim_below_half_becomes_invalid(self):
        """``main_claim`` 同样可被判 ``invalid``（上层论点不限于 sub_claim）。"""

        tree = [
            _claim("m", node_type=NodeType.MAIN_CLAIM, children_ids=["c"], status=NodeStatus.CREDIBLE),
            _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="m"),
        ]
        assert _by_id(impact(tree))["m"].status is NodeStatus.INVALID

    def test_unaffected_when_ratio_above_seven(self):
        """ratio ≥ 0.7 → 不受影响：status 不变、不贴弱化标签。"""

        tree = [
            _claim("n0", children_ids=["c1", "c2"], status=NodeStatus.CREDIBLE),
            _evidence("c1", status=NodeStatus.CREDIBLE, weight=80, parent_id="n0"),
            _evidence("c2", status=NodeStatus.ERROR, weight=20, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.CREDIBLE
        assert WEAKENING_TAG not in out["n0"].issue_tags

    def test_weaken_band_appends_tag_without_invalidation(self):
        """0.5 ≤ ratio < 0.7 → 贴 ``weakening`` 批注、status 不失效。"""

        # 60/100 = 0.6 → 弱化。
        tree = [
            _claim("n0", children_ids=["c1", "c2"], status=NodeStatus.CREDIBLE),
            _evidence("c1", status=NodeStatus.CREDIBLE, weight=60, parent_id="n0"),
            _evidence("c2", status=NodeStatus.ERROR, weight=40, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.CREDIBLE  # 不失效
        assert WEAKENING_TAG in out["n0"].issue_tags

    def test_weaken_does_not_clobber_existing_tags(self):
        """弱化批注追加进 ``issue_tags``、保留既有标签、去重。"""

        tree = [
            _claim(
                "n0",
                children_ids=["c1", "c2"],
                status=NodeStatus.CREDIBLE,
                issue_tags=["conflict", WEAKENING_TAG],
            ),
            _evidence("c1", status=NodeStatus.CREDIBLE, weight=60, parent_id="n0"),
            _evidence("c2", status=NodeStatus.ERROR, weight=40, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        # 既有 conflict 保留、weakening 不重复。
        assert out["n0"].issue_tags == ["conflict", WEAKENING_TAG]

    def test_error_upper_claim_not_re_judged_invalid(self):
        """自身已 ``error`` 的上层论点不再改判 ``invalid``（error 是更直接的自证其伪判决）。"""

        # n0(sub_claim, ERROR) 的子节点全 error → ratio 0，但 n0 已 error → 维持 error。
        tree = [
            _claim("n0", children_ids=["c1"], status=NodeStatus.ERROR),
            _evidence("c1", status=NodeStatus.ERROR, weight=100, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.ERROR
        assert out["n0"].status is not NodeStatus.INVALID

    def test_invalid_propagates_up_to_grandparent_post_order(self):
        """失效逐层上推（后序）：子 evidence error → sub_claim invalid → main_claim invalid。"""

        tree = [
            _claim("g", node_type=NodeType.MAIN_CLAIM, children_ids=["s"], status=NodeStatus.CREDIBLE),
            _claim("s", node_type=NodeType.SUB_CLAIM, parent_id="g", children_ids=["e"], status=NodeStatus.CREDIBLE),
            _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s"),
        ]
        out = _by_id(impact(tree))
        assert out["e"].status is NodeStatus.ERROR  # 叶子不动
        assert out["s"].status is NodeStatus.INVALID  # 子全死 → 失效
        assert out["g"].status is NodeStatus.INVALID  # 子失效 → 上推失效

    def test_error_child_propagates_as_non_surviving_to_parent(self):
        """``error`` 子节点对父论点计为不存活（无需子先变 invalid 即可上推）。"""

        # 父 credible、唯一子 evidence error → ratio 0 → 父 invalid。
        tree = [
            _claim("n0", children_ids=["c"], status=NodeStatus.CREDIBLE),
            _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="n0"),
        ]
        assert _by_id(impact(tree))["n0"].status is NodeStatus.INVALID

    def test_partial_collapse_does_not_invalidate_unchanged_sibling_chain(self):
        """一个分支塌方不波及另一独立存活分支（树形隔离）。"""

        # 根 main 有两子：左子 sub_a 全死 → invalid；右子 sub_b 全活 → 不变。
        tree = [
            _claim("root", node_type=NodeType.MAIN_CLAIM, children_ids=["a", "b"], status=NodeStatus.CREDIBLE),
            _claim("a", node_type=NodeType.SUB_CLAIM, parent_id="root", children_ids=["a1"], status=NodeStatus.CREDIBLE),
            _claim("b", node_type=NodeType.SUB_CLAIM, parent_id="root", children_ids=["b1"], status=NodeStatus.CREDIBLE),
            _evidence("a1", status=NodeStatus.ERROR, weight=100, parent_id="a"),
            _evidence("b1", status=NodeStatus.CREDIBLE, weight=100, parent_id="b"),
        ]
        out = _by_id(impact(tree))
        assert out["a"].status is NodeStatus.INVALID
        assert out["b"].status is NodeStatus.CREDIBLE
        # 根：a(invalid, 100 权重) + b(credible, 100 权重) → 0.5 → 弱化、不失效。
        assert out["root"].status is NodeStatus.CREDIBLE
        assert WEAKENING_TAG in out["root"].issue_tags


class TestImpactScope:
    """影响传导只判上层论点；叶子与影子节点不参与。"""

    def test_evidence_with_dead_children_not_judged(self):
        """``evidence`` 即使有子节点也不被影响传导判 ``invalid``（叶子不参与）。"""

        tree = [
            _claim("n0", node_type=NodeType.EVIDENCE, children_ids=["c"], status=NodeStatus.CREDIBLE),
            _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.CREDIBLE
        assert out["n0"].status is not NodeStatus.INVALID

    def test_qualification_not_judged(self):
        """``qualification`` 不参与传导——即便子节点全死也不判 ``invalid``。"""

        tree = [
            _claim("n0", node_type=NodeType.QUALIFICATION, children_ids=["c"], status=NodeStatus.CREDIBLE),
            _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="n0"),
        ]
        assert _by_id(impact(tree))["n0"].status is NodeStatus.CREDIBLE

    def test_shadow_child_does_not_count_nor_save_parent(self):
        """影子子节点不参与传导：高权重影子救不了全死 evidence 的父论点。"""

        bg = ArgumentationNode(
            node_id="bg",
            node_type=NodeType.BACKGROUND,
            parent_id="n0",
            paragraph_id="p0001",
            argument_weight=100,  # 影子权重即便高也不参与
            status=NodeStatus.CREDIBLE,
        )
        tree = [
            _claim("n0", children_ids=["bg", "ev"], status=NodeStatus.CREDIBLE),
            bg,
            _evidence("ev", status=NodeStatus.ERROR, weight=100, parent_id="n0"),
        ]
        out = _by_id(impact(tree))
        # 仅 ev 参与（100 全死）→ ratio 0 → invalid；影子不计入分母。
        assert out["n0"].status is NodeStatus.INVALID

    def test_no_children_upper_claim_unaffected(self):
        """无子节点的上层论点：无可失支撑、不判失效（0/0 守为 1.0）。"""

        tree = [_claim("n0", children_ids=[], status=NodeStatus.CREDIBLE)]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.CREDIBLE
        assert WEAKENING_TAG not in out["n0"].issue_tags


# --------------------------------------------------------------------------- #
# 塌方时复用既有成立假设激活（ADR-0003 · 仅复用、绝不新建）
# --------------------------------------------------------------------------- #


def _node_with_decision(
    node_id: str,
    *,
    status: NodeStatus,
    children_ids: list[str],
    hypotheses: list[Hypothesis],
    decision: MergeDecision | None,
    node_type: NodeType = NodeType.SUB_CLAIM,
) -> ArgumentationNode:
    """构造一个带合并裁决 + 候选假设的上层论点（模拟合并算子 #6 产出）。"""

    return ArgumentationNode(
        node_id=node_id,
        node_type=node_type,
        children_ids=children_ids,
        paragraph_id="p0001",
        status=status,
        candidate_hypotheses=list(hypotheses),
        merge_decision=decision,
    )


class TestImpactReactivation:
    """塌方（``invalid``）时复用节点已有成立假设去激活——仅复用、绝不新建。"""

    def test_unverified_upper_claim_activates_existing_supported_oppose(self):
        """未裁决 sub_claim（合并判 KEEP、假设未激活）塌方 → 复用 supported-oppose 激活为 REPLACE。"""

        h = _hyp("h1", relation=HypothesisRelation.OPPOSE)
        # 模拟合并产出：unverified + 保守 KEEP（假设保留但未激活）。
        node = _node_with_decision(
            "s",
            status=NodeStatus.UNVERIFIED,
            children_ids=["e"],
            hypotheses=[h],
            decision=MergeDecision(action=MergeAction.KEEP),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        out = _by_id(impact(tree))
        assert out["s"].status is NodeStatus.INVALID
        assert out["s"].merge_decision is not None
        assert out["s"].merge_decision.action is MergeAction.REPLACE
        assert out["s"].merge_decision.activated_hypothesis_ids == ["h1"]

    @pytest.mark.parametrize(
        "relation,action",
        [
            (HypothesisRelation.OPPOSE, MergeAction.REPLACE),
            (HypothesisRelation.ADVANCE, MergeAction.REWRITE),
            (HypothesisRelation.EXPAND, MergeAction.SUPPLEMENT),
        ],
    )
    def test_reactivation_dispatches_by_relation(self, relation, action):
        """复用激活的动作由假设关系决定（与合并算子 #6 同源映射）。"""

        h = _hyp("h", relation=relation)
        node = _node_with_decision(
            "s",
            status=NodeStatus.UNVERIFIED,
            children_ids=["e"],
            hypotheses=[h],
            decision=MergeDecision(action=MergeAction.KEEP),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        out = _by_id(impact(tree))
        assert out["s"].merge_decision.action is action
        assert out["s"].merge_decision.activated_hypothesis_ids == ["h"]

    def test_reactivation_multiple_supported_all_activated_primary_by_confidence(self):
        """多条 supported 假设：全部激活为候选，节点级动作取最高 confidence 者的关系。"""

        lo = _hyp("lo", relation=HypothesisRelation.OPPOSE, confidence=0.2)
        hi = _hyp("hi", relation=HypothesisRelation.EXPAND, confidence=0.9)
        mid = _hyp("mid", relation=HypothesisRelation.ADVANCE, confidence=0.5)
        node = _node_with_decision(
            "s",
            status=NodeStatus.UNVERIFIED,
            children_ids=["e"],
            hypotheses=[lo, hi, mid],
            decision=MergeDecision(action=MergeAction.KEEP),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        out = _by_id(impact(tree))
        # primary = 最高 confidence（expand 0.9）→ SUPPLEMENT；三条 supported 均激活。
        assert out["s"].merge_decision.action is MergeAction.SUPPLEMENT
        assert set(out["s"].merge_decision.activated_hypothesis_ids) == {"lo", "hi", "mid"}

    def test_reactivation_no_supported_keeps_keep_no_candidates(self):
        """塌方节点无 supported 假设 → ``KEEP``、无候选、原文入 HITL-2 无药。"""

        # 仅有一条 refuted 假设（合并未激活）。
        h = _hyp("h", status=HypothesisStatus.REFUTED)
        node = _node_with_decision(
            "s",
            status=NodeStatus.UNVERIFIED,
            children_ids=["e"],
            hypotheses=[h],
            decision=MergeDecision(action=MergeAction.KEEP),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        out = _by_id(impact(tree))
        assert out["s"].status is NodeStatus.INVALID
        assert out["s"].merge_decision.action is MergeAction.KEEP
        assert out["s"].merge_decision.activated_hypothesis_ids == []

    def test_invalid_preserves_already_activating_merge_decision(self):
        """合并算子已判 REPLACE（doubtful 行 supported 列）的节点塌方 → 保留裁决、仅翻 status。"""

        h = _hyp("h1", relation=HypothesisRelation.OPPOSE)
        # 模拟合并产出：doubtful + REPLACE（假设已激活）。
        node = _node_with_decision(
            "s",
            status=NodeStatus.DOUBTFUL,
            children_ids=["e"],
            hypotheses=[h],
            decision=MergeDecision(
                action=MergeAction.REPLACE, activated_hypothesis_ids=["h1"]
            ),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        out = _by_id(impact(tree))
        assert out["s"].status is NodeStatus.INVALID
        # 裁决保留（不重新推导）。
        assert out["s"].merge_decision.action is MergeAction.REPLACE
        assert out["s"].merge_decision.activated_hypothesis_ids == ["h1"]

    def test_invalid_does_not_create_new_hypotheses(self):
        """塌方复用既有假设：``candidate_hypotheses`` 的 id 集合前后完全一致（绝不新建）。"""

        h1 = _hyp("h1", relation=HypothesisRelation.OPPOSE)
        h2 = _hyp("h2", relation=HypothesisRelation.EXPAND, status=HypothesisStatus.DOUBTFUL)
        node = _node_with_decision(
            "s",
            status=NodeStatus.UNVERIFIED,
            children_ids=["e"],
            hypotheses=[h1, h2],
            decision=MergeDecision(action=MergeAction.KEEP),
        )
        tree = [node, _evidence("e", status=NodeStatus.ERROR, weight=100, parent_id="s")]
        before_ids = {h.hypothesis_id for h in node.candidate_hypotheses}
        out = _by_id(impact(tree))
        after_ids = {h.hypothesis_id for h in out["s"].candidate_hypotheses}
        assert after_ids == before_ids  # 仅复用、绝不新建


# --------------------------------------------------------------------------- #
# 纯函数约束：不改输入树、不改 content、不置 adopted/corrected、返回新实例
# --------------------------------------------------------------------------- #


class TestImpactPurity:
    """影响传导是纯函数子缝：不产文本、不替人拍板、不改输入。"""

    def test_does_not_mutate_input_tree(self):
        """返回新树；输入节点（status/tags/merge_decision/children）原样不变。"""

        node = _claim(
            "n0",
            children_ids=["c1", "c2"],
            status=NodeStatus.CREDIBLE,
            content="原文论点",
        )
        tree = [
            node,
            _evidence("c1", status=NodeStatus.ERROR, weight=70, parent_id="n0"),
            _evidence("c2", status=NodeStatus.CREDIBLE, weight=30, parent_id="n0"),
        ]
        before = {n.node_id: n.model_copy(deep=True) for n in tree}

        impact(tree)

        for n in tree:
            assert n == before[n.node_id], f"输入树被改写：{n.node_id}"
            assert n.merge_decision is None  # 输入节点本无 merge_decision

    def test_preserves_content_never_adopts_or_corrects(self):
        """影响传导不改 ``content``、不置 ``adopted``/``corrected``（替人拍板是 HITL-2/回写职责）。"""

        node = _claim(
            "n0",
            children_ids=["c"],
            status=NodeStatus.CREDIBLE,
            content="不可改的原文",
        )
        tree = [node, _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="n0")]
        out = _by_id(impact(tree))
        assert out["n0"].status is NodeStatus.INVALID
        assert out["n0"].content == "不可改的原文"  # 不产文本
        assert out["n0"].status is not NodeStatus.ADOPTED
        assert out["n0"].status is not NodeStatus.CORRECTED

    def test_returns_new_node_instances(self):
        """输出节点是新实例、与输入不同对象（纯函数、可独立单测）。"""

        node = _claim("n0", children_ids=["c"], status=NodeStatus.CREDIBLE)
        tree = [node, _evidence("c", status=NodeStatus.ERROR, weight=100, parent_id="n0")]
        out = _by_id(impact(tree))
        for n in tree:
            assert out[n.node_id] is not n

    def test_no_change_tree_is_still_new_instances_byte_identity(self):
        """全可信树：影响传导不动任何节点，但仍返回新实例（与合并 #6 同形）。"""

        tree = [
            _claim("n0", children_ids=["c"], status=NodeStatus.CREDIBLE, content="C"),
            _evidence("c", status=NodeStatus.CREDIBLE, weight=100, parent_id="n0", content="E"),
        ]
        out = impact(tree)
        assert [n.node_id for n in out] == [n.node_id for n in tree]
        assert all(o is not n for o, n in zip(out, tree, strict=True))
        # 内容与状态完全不变。
        assert all(o.content == n.content for o, n in zip(out, tree, strict=True))
        assert all(o.status == n.status for o, n in zip(out, tree, strict=True))
