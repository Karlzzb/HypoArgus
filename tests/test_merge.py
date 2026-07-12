"""双轨合并算子测试（issue #6、PRD §7、ADR-0006/0002）。

行为级黑盒测试（PRD «Testing Decisions»）：通过纯函数 seam（``merge``）驱动「原文
status × 假设 status」全 12 格矩阵裁决，断言每格动作（保留 / 替换 / 改写 / 补充 /
贴 ``conflict`` / 冻结）、credible 行一律保持原文不动、冲突不自动裁决、所有节点
（含无候选者、影子节点、未裁决节点）均标注 ``merge_decision`` 流入 HITL-2，且合并
绝不替人拍板（不置 ``adopted``、不改 ``content``/``status``）。

``merge`` 无 LLM / 检索依赖（确定性纯函数），故无需注入桩。
"""

from __future__ import annotations

import pytest

from agents.hypothesis import Hypothesis, HypothesisRelation, HypothesisStatus
from agents.merge import apply_partial_updates, merge
from domain import (
    Argument,
    ArgumentStatus,
    ArgumentType,
    MergeAction,
)

# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #

_HID = 0


def _hyp(
    relation: HypothesisRelation,
    status: HypothesisStatus,
    *,
    text: str = "h",
    confidence: float = 0.5,
) -> Hypothesis:
    """构造一条带稳定 id 的假设（id 唯一即可，取值不参与矩阵裁决）。"""

    global _HID
    _HID += 1
    return Hypothesis(
        hypothesis_id=f"h{_HID}",
        text=text,
        relation=relation,
        status=status,
        confidence=confidence,
    )


def _argument(
    status: ArgumentStatus,
    hypotheses: list[Hypothesis] | None = None,
    *,
    argument_id: str = "n0",
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    content: str = "原文",
    issue_tags: list[str] | None = None,
) -> Argument:
    return Argument(
        argument_id=argument_id,
        argument_type=argument_type,
        paragraph_id="p0001",
        content=content,
        status=status,
        candidate_hypotheses=list(hypotheses or []),
        issue_tags=list(issue_tags or []),
    )


# --------------------------------------------------------------------------- #
# 12 格矩阵（「成立」列按关系拆为 oppose/advance/expand 三路）
# --------------------------------------------------------------------------- #

MatrixCase = tuple[
    ArgumentStatus,  # 原文 status
    HypothesisRelation | None,  # 假设关系（None = 无假设 / 不适用）
    HypothesisStatus | None,  # 假设 status（None = 无假设）
    MergeAction,  # 期望节点裁决动作
    bool,  # 期望 issue_tags 含 'conflict'
    int,  # 期望裁剪后 candidate_hypotheses 条数
    int,  # 期望 activated_hypothesis_ids 条数
]

MATRIX_CASES: list[MatrixCase] = [
    # ---- credible 行：一律保持原文不动 ----
    (ArgumentStatus.CREDIBLE, None, None, MergeAction.KEEP, False, 0, 0),
    (ArgumentStatus.CREDIBLE, HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, MergeAction.CONFLICT, True, 1, 1),
    (ArgumentStatus.CREDIBLE, HypothesisRelation.ADVANCE, HypothesisStatus.SUPPORTED, MergeAction.FREEZE, False, 0, 0),
    (ArgumentStatus.CREDIBLE, HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED, MergeAction.FREEZE, False, 0, 0),
    (ArgumentStatus.CREDIBLE, HypothesisRelation.OPPOSE, HypothesisStatus.DOUBTFUL, MergeAction.KEEP, False, 0, 0),
    (ArgumentStatus.CREDIBLE, HypothesisRelation.OPPOSE, HypothesisStatus.REFUTED, MergeAction.KEEP, False, 0, 0),
    # ---- doubtful 行 ----
    (ArgumentStatus.DOUBTFUL, None, None, MergeAction.KEEP, False, 0, 0),
    (ArgumentStatus.DOUBTFUL, HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, MergeAction.REPLACE, False, 1, 1),
    (ArgumentStatus.DOUBTFUL, HypothesisRelation.ADVANCE, HypothesisStatus.SUPPORTED, MergeAction.REWRITE, False, 1, 1),
    (ArgumentStatus.DOUBTFUL, HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED, MergeAction.SUPPLEMENT, False, 1, 1),
    (ArgumentStatus.DOUBTFUL, HypothesisRelation.OPPOSE, HypothesisStatus.DOUBTFUL, MergeAction.KEEP, False, 1, 0),
    (ArgumentStatus.DOUBTFUL, HypothesisRelation.OPPOSE, HypothesisStatus.REFUTED, MergeAction.KEEP, False, 0, 0),
    # ---- error 行 ----
    (ArgumentStatus.ERROR, None, None, MergeAction.KEEP, False, 0, 0),
    (ArgumentStatus.ERROR, HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, MergeAction.REPLACE, False, 1, 1),
    (ArgumentStatus.ERROR, HypothesisRelation.ADVANCE, HypothesisStatus.SUPPORTED, MergeAction.REWRITE, False, 1, 1),
    (ArgumentStatus.ERROR, HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED, MergeAction.SUPPLEMENT, False, 1, 1),
    (ArgumentStatus.ERROR, HypothesisRelation.OPPOSE, HypothesisStatus.DOUBTFUL, MergeAction.KEEP, False, 1, 0),
    (ArgumentStatus.ERROR, HypothesisRelation.OPPOSE, HypothesisStatus.REFUTED, MergeAction.KEEP, False, 0, 0),
]


def _case_id(c: MatrixCase) -> str:
    status, relation, hstatus, *_ = c
    rel = relation.value if relation else "none"
    hs = hstatus.value if hstatus else "none"
    return f"{status.value}-{rel}-{hs}"


@pytest.mark.parametrize("case", MATRIX_CASES, ids=[_case_id(c) for c in MATRIX_CASES])
def test_merge_matrix_cell_verdict(case):
    """全 12 格（成立列按关系拆 3 路 = 18 例）裁决严格符合 ADR-0006 矩阵。"""

    status, relation, hstatus, action, conflict, kept, activated = case
    hypotheses: list[Hypothesis] = []
    if relation is not None and hstatus is not None:
        hypotheses = [_hyp(relation, hstatus)]
    argument = _argument(status, hypotheses)

    [out] = merge([argument])
    decision = out.merge_decision
    assert decision is not None
    assert decision.action is action, f"动作期望 {action}，实际 {decision.action}"
    assert ("conflict" in out.issue_tags) is conflict
    assert len(out.candidate_hypotheses) == kept
    assert len(decision.activated_hypothesis_ids) == activated
    # credible 行一律保持原文不动：status 与 content 不变、无激活假设。
    if status is ArgumentStatus.CREDIBLE:
        assert out.status is ArgumentStatus.CREDIBLE
        assert out.content == "原文"


# --------------------------------------------------------------------------- #
# 关键边界 1：credible × 对立成立 → 贴 conflict 并列推 HITL-2，不自动裁决
# --------------------------------------------------------------------------- #


def test_merge_credible_oppose_supported_tags_conflict_no_autodecision():
    """credible × 对立 supported → 贴 conflict、保留对立假设、原文不动、不自动采纳。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="对立假设")
    argument = _argument(ArgumentStatus.CREDIBLE, [h], content="原文论点")

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.CONFLICT
    assert "conflict" in out.issue_tags
    # 原文与对立假设并列推 HITL-2：假设保留为候选。
    assert len(out.candidate_hypotheses) == 1
    assert out.candidate_hypotheses[0].hypothesis_id == h.hypothesis_id
    assert out.merge_decision.activated_hypothesis_ids == [h.hypothesis_id]
    # 不自动裁决：节点未进入 adopted、content/status 不动。
    assert out.status is ArgumentStatus.CREDIBLE
    assert out.content == "原文论点"


def test_merge_credible_multiple_oppose_supported_all_kept_as_conflict():
    """多条对立 supported 假设 → 均保留并列推 HITL-2（用户分条判断）。"""

    h1 = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="对立甲")
    h2 = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="对立乙")
    argument = _argument(ArgumentStatus.CREDIBLE, [h1, h2])

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.CONFLICT
    assert "conflict" in out.issue_tags
    assert {h.hypothesis_id for h in out.candidate_hypotheses} == {h1.hypothesis_id, h2.hypothesis_id}
    assert set(out.merge_decision.activated_hypothesis_ids) == {h1.hypothesis_id, h2.hypothesis_id}


def test_merge_credible_oppose_plus_expand_only_oppose_survives():
    """credible 节点同时有对立成立 + 扩展成立：冲突格主导，扩展假设被冻结丢弃。"""

    oppose = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="对立")
    expand = _hyp(HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED, text="扩展")
    argument = _argument(ArgumentStatus.CREDIBLE, [oppose, expand])

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.CONFLICT
    assert "conflict" in out.issue_tags
    assert [h.hypothesis_id for h in out.candidate_hypotheses] == [oppose.hypothesis_id]
    assert out.merge_decision.activated_hypothesis_ids == [oppose.hypothesis_id]


# --------------------------------------------------------------------------- #
# 关键边界 2：credible × 递进/扩展成立 → 严格冻结、原文不动
# --------------------------------------------------------------------------- #


def test_merge_credible_advance_or_expand_supported_freezes_original():
    """credible + 递进/扩展成立 → FREEZE：假设全丢弃、原文不动、无 conflict。"""

    advance = _hyp(HypothesisRelation.ADVANCE, HypothesisStatus.SUPPORTED, text="递进")
    expand = _hyp(HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED, text="扩展")
    argument = _argument(ArgumentStatus.CREDIBLE, [advance, expand], content="原文论点")

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.FREEZE
    assert "conflict" not in out.issue_tags
    assert out.candidate_hypotheses == []
    assert out.merge_decision.activated_hypothesis_ids == []
    assert out.status is ArgumentStatus.CREDIBLE
    assert out.content == "原文论点"


# --------------------------------------------------------------------------- #
# 「成立」列按关系分流（doubtful / error 同形）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [ArgumentStatus.DOUBTFUL, ArgumentStatus.ERROR])
def test_merge_supported_column_dispatches_by_relation(status):
    """doubtful/error × supported：oppose→REPLACE、advance→REWRITE、expand→SUPPLEMENT。"""

    for relation, action in (
        (HypothesisRelation.OPPOSE, MergeAction.REPLACE),
        (HypothesisRelation.ADVANCE, MergeAction.REWRITE),
        (HypothesisRelation.EXPAND, MergeAction.SUPPLEMENT),
    ):
        h = _hyp(relation, HypothesisStatus.SUPPORTED)
        argument = _argument(status, [h])
        [out] = merge([argument])
        assert out.merge_decision.action is action, f"{relation.value} 期望 {action}"
        assert out.candidate_hypotheses[0].hypothesis_id == h.hypothesis_id
        assert out.merge_decision.activated_hypothesis_ids == [h.hypothesis_id]
        # oppose→替换、advance→改写、expand→补充——但合并阶段只标注动作、不改 content。
        assert out.content == "原文"
        assert out.status is status

    # 三条同时存在：按最高 confidence 选 primary，其余仍作为候选保留。
    argument = _argument(
        status,
        [
            Hypothesis(hypothesis_id="lo", text="对立低置信", relation=HypothesisRelation.OPPOSE, status=HypothesisStatus.SUPPORTED, confidence=0.2),
            Hypothesis(hypothesis_id="hi", text="扩展高置信", relation=HypothesisRelation.EXPAND, status=HypothesisStatus.SUPPORTED, confidence=0.9),
            Hypothesis(hypothesis_id="mid", text="递进中置信", relation=HypothesisRelation.ADVANCE, status=HypothesisStatus.SUPPORTED, confidence=0.5),
        ],
    )
    [out] = merge([argument])
    # primary = 最高 confidence（扩展 0.9）→ SUPPLEMENT；三条 supported 均激活为候选。
    assert out.merge_decision.action is MergeAction.SUPPLEMENT
    assert set(out.merge_decision.activated_hypothesis_ids) == {"lo", "hi", "mid"}
    assert len(out.candidate_hypotheses) == 3


# --------------------------------------------------------------------------- #
# 弱呈现：doubtful/error × doubtful 假设 → 保留供参考、不计入 activated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [ArgumentStatus.DOUBTFUL, ArgumentStatus.ERROR])
def test_merge_doubtful_hypothesis_weakly_presented_not_activated(status):
    """doubtful 假设弱呈现「未证实·供参考」：留 candidate_hypotheses、不计 activated。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.DOUBTFUL, text="存疑假设")
    argument = _argument(status, [h])

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.KEEP
    assert out.merge_decision.activated_hypothesis_ids == []
    assert len(out.candidate_hypotheses) == 1
    assert out.candidate_hypotheses[0].hypothesis_id == h.hypothesis_id


def test_merge_supported_and_doubtful_mixed_both_kept_only_supported_activated():
    """doubtful 节点：supported 激活、doubtful 弱呈现——两者都留、仅 supported 入 activated。"""

    sup = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="成立")
    weak = _hyp(HypothesisRelation.EXPAND, HypothesisStatus.DOUBTFUL, text="存疑")
    argument = _argument(ArgumentStatus.DOUBTFUL, [sup, weak])

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.REPLACE
    assert out.merge_decision.activated_hypothesis_ids == [sup.hypothesis_id]
    assert {h.hypothesis_id for h in out.candidate_hypotheses} == {sup.hypothesis_id, weak.hypothesis_id}
    # refuted 应被丢弃。
    refuted = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.REFUTED, text="被推翻")
    argument2 = _argument(ArgumentStatus.DOUBTFUL, [sup, weak, refuted])
    [out2] = merge([argument2])
    assert {h.hypothesis_id for h in out2.candidate_hypotheses} == {sup.hypothesis_id, weak.hypothesis_id}


# --------------------------------------------------------------------------- #
# 所有节点（含无候选者、影子节点、未裁决节点）均标注后流入 HITL-2，无独立人工兜底
# --------------------------------------------------------------------------- #


def test_merge_every_argument_annotated_including_shadows_and_no_candidate():
    """所有节点均附 merge_decision（无候选者、影子、qualification、未裁决均在内）。"""

    argument_tree = [
        _argument(ArgumentStatus.CREDIBLE, [], argument_id="cred", argument_type=ArgumentType.EVIDENCE),
        _argument(ArgumentStatus.DOUBTFUL, [], argument_id="doubt", argument_type=ArgumentType.SUB_CLAIM),
        _argument(ArgumentStatus.ERROR, [], argument_id="err", argument_type=ArgumentType.EVIDENCE),
        _argument(ArgumentStatus.UNVERIFIED, [], argument_id="qual", argument_type=ArgumentType.QUALIFICATION),
        _argument(ArgumentStatus.UNVERIFIED, [], argument_id="bg", argument_type=ArgumentType.BACKGROUND),
        _argument(ArgumentStatus.UNVERIFIED, [], argument_id="ev", argument_type=ArgumentType.EVALUATION),
        _argument(ArgumentStatus.UNVERIFIED, [], argument_id="main", argument_type=ArgumentType.MAIN_CLAIM),
    ]
    out = merge(argument_tree)
    assert len(out) == len(argument_tree)
    for argument in out:
        assert argument.merge_decision is not None, f"{argument.argument_id} 未标注 merge_decision"
        assert argument.merge_decision.action is MergeAction.KEEP
    # 无独立人工兜底分支：无节点被丢弃或进入 adopted。
    assert {n.argument_id for n in out} == {n.argument_id for n in argument_tree}
    assert all(n.status is not ArgumentStatus.ADOPTED for n in out)


def test_merge_unverified_argument_with_hypotheses_conservative_keep():
    """未裁决节点（体检未覆盖、却带假设）→ 保守 KEEP、不裁剪假设、不自动激活。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED, text="假设")
    argument = _argument(ArgumentStatus.UNVERIFIED, [h], argument_type=ArgumentType.SUB_CLAIM)

    [out] = merge([argument])
    assert out.merge_decision.action is MergeAction.KEEP
    assert out.merge_decision.activated_hypothesis_ids == []
    # 保守：不丢假设（无裁决不可裁剪），交 HITL-2。
    assert len(out.candidate_hypotheses) == 1
    assert "conflict" not in out.issue_tags


# --------------------------------------------------------------------------- #
# 纯函数约束：不改输入树、不改 content/status、绝不替人拍板
# --------------------------------------------------------------------------- #


def test_merge_does_not_mutate_input_tree():
    """返回新实例；输入树节点（content/status/issue_tags/candidate/merge_decision）原样。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED)
    argument_tree = [
        _argument(ArgumentStatus.CREDIBLE, [h], argument_id="c", content="C"),
        _argument(ArgumentStatus.DOUBTFUL, [_hyp(HypothesisRelation.EXPAND, HypothesisStatus.SUPPORTED)], argument_id="d", content="D"),
    ]
    originals = {n.argument_id: n.model_copy(deep=True) for n in argument_tree}

    merge(argument_tree)

    for n in argument_tree:
        assert n == originals[n.argument_id], f"输入树被改写：{n.argument_id}"
        assert n.merge_decision is None  # 输入节点本无 merge_decision


def test_merge_preserves_content_and_status_never_adopts():
    """合并只标注、不改 content/status、绝不置 adopted（替人拍板）。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED)
    argument_tree = [
        _argument(ArgumentStatus.CREDIBLE, [h], argument_id="c", content="cred 原文"),
        _argument(ArgumentStatus.DOUBTFUL, [h], argument_id="d", content="doubt 原文"),
        _argument(ArgumentStatus.ERROR, [h], argument_id="e", content="err 原文"),
    ]
    before = {n.argument_id: (n.content, n.status) for n in argument_tree}

    out = merge(argument_tree)
    for argument in out:
        assert argument.content == before[argument.argument_id][0]
        assert argument.status is before[argument.argument_id][1]
        assert argument.status is not ArgumentStatus.ADOPTED
        assert argument.status is not ArgumentStatus.CORRECTED


def test_merge_does_not_drop_existing_issue_tags():
    """合并追加 conflict 时保留既有 issue_tags（不覆盖、不丢、去重）。"""

    h = _hyp(HypothesisRelation.OPPOSE, HypothesisStatus.SUPPORTED)
    argument = _argument(ArgumentStatus.CREDIBLE, [h], issue_tags=["stale-tag", "conflict"])

    [out] = merge([argument])
    # 既有 conflict 不重复追加；既有 stale-tag 保留。
    assert out.issue_tags == ["stale-tag", "conflict"]


def test_merge_returns_new_argument_instances():
    """输出节点是新实例、与输入节点不同对象（纯函数、可独立单测）。"""

    argument = _argument(ArgumentStatus.CREDIBLE, [], argument_id="x")
    [out] = merge([argument])
    assert out is not argument
    assert out.merge_decision is not None


# --------------------------------------------------------------------------- #
# 双线路 partial 字段级合流（apply_partial_updates）
# --------------------------------------------------------------------------- #


def test_apply_partial_updates_field_merges_disjoint_partial_channels():
    """体检 status 与开药 candidate_hypotheses 字段级合流到同节点、互不覆盖。"""

    base = _argument(ArgumentStatus.UNVERIFIED, [], argument_id="n0", content="原文")
    credibility = ArgumentStatus.DOUBTFUL  # 体检 partial 只产可信度裁决
    hypotheses = [  # 开药 partial 只产候选假设列表
        Hypothesis(
            hypothesis_id="h1",
            text="对立",
            relation=HypothesisRelation.OPPOSE,
            status=HypothesisStatus.SUPPORTED,
        )
    ]

    [out] = apply_partial_updates([base], {"n0": credibility}, {"n0": hypotheses})

    # 两字段共存（非 last-writer-wins 丢字段）。
    assert out.status is ArgumentStatus.DOUBTFUL
    assert len(out.candidate_hypotheses) == 1
    assert out.candidate_hypotheses[0].hypothesis_id == "h1"
    # 其余字段从原树保留。
    assert out.content == "原文"
    assert out.argument_type is ArgumentType.EVIDENCE


def test_apply_partial_updates_pure_does_not_mutate_input():
    """合流不修改输入树（返回新实例）；partial 为标量/列表、不携节点。"""

    base = _argument(ArgumentStatus.UNVERIFIED, [], argument_id="n0", content="原文")
    base_copy = base.model_copy(deep=True)

    apply_partial_updates([base], {"n0": ArgumentStatus.CREDIBLE}, {})

    assert base == base_copy  # 原树未变
    assert base.status is ArgumentStatus.UNVERIFIED


def test_apply_partial_updates_missing_partials_keeps_original():
    """无 partial 的节点原样保留（仅浅拷贝）；空 partial dict 无副作用。"""

    argument = _argument(ArgumentStatus.CREDIBLE, [], argument_id="solo", content="C")
    [out] = apply_partial_updates([argument], {}, {})
    assert out.status is ArgumentStatus.CREDIBLE
    assert out.content == "C"
    assert out is not argument
