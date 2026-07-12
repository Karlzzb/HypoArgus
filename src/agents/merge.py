"""双轨合并算子（PRD §7、issue #6、ADR-0006/0002）：确定性 12 格矩阵。

把体检（#4）与开药（#5）两线路结果标注到**同一棵树**（系统全程只有一棵树；此处
「合并」指合并两线路结果，非合并两棵树）。裁决按「原文状态 × 假设状态」全 12 格
矩阵执行；「成立(supported)」列按假设与原文的语义关系分流——对立→替换、递进→改写、
扩展→补充（段尾追加带审计标识）。

两条关键边界（ADR-0006）：

- ``credible × 对立成立`` → 贴 ``conflict`` 把原文与对立假设并列推 HITL-2 人判，
  系统**不自动裁决**。
- ``credible × 递进/扩展成立`` → 严格冻结、原文不动（以静制动，即便存在成立的扩展型
  假设也不擅自增补）。

本函数是纯函数子缝（PRD «Testing Decisions»）：``标注前的树 → 标注后的同一棵树``，
确定性、可独立单测。**绝不替人拍板**：不置 ``adopted``、不改 ``content`` 与
``status``（那是 HITL-2 #9 与回写 #10 的职责）。所有节点（含无候选者、影子节点、
未裁决节点）均标注 ``merge_decision`` 后流入 HITL-2，无独立人工兜底分支。

裁剪规则（ADR-0002「合并算子是唯一裁剪点」）：

- ``credible`` 非「成立」格 → 假设全 Drop（保留原文、丢弃假说）；「对立成立」格 →
  仅保留对立 supported 假设（推 HITL-2），其余丢弃。
- ``doubtful`` / ``error`` 行 → 保留 supported（激活）+ doubtful（弱呈现「供参考」），
  丢弃 refuted。
- 未裁决节点（``unverified`` / ``pending_verification``，体检未覆盖的 qualification /
  影子节点、或并行未跑完）→ 保守 KEEP、不裁剪假设、不激活。
"""

from __future__ import annotations

from collections.abc import Callable

from agents.hypothesis import Hypothesis, HypothesisRelation, HypothesisStatus
from domain import (
    HYPOTHESIS_RELATION_TO_MERGE_ACTION,
    ArgumentationNode,
    MergeAction,
    MergeDecision,
    NodeStatus,
)

__all__ = ["apply_partial_updates", "merge", "merge_with_partials"]


# --------------------------------------------------------------------------- #
# 双线路 partial 字段级合流（合并前置·ADR-0006「合并两线路结果」的工程落点）
# --------------------------------------------------------------------------- #


def apply_partial_updates(
    tree: list[ArgumentationNode],
    verification_updates: dict[str, ArgumentationNode],
    hypothesis_updates: dict[str, ArgumentationNode],
) -> list[ArgumentationNode]:
    """把体检 / 开药两线路的 partial 更新**字段级合流**到同一棵树。

    体检（#4）partial 只改 ``status``、开药（#5）partial 只改 ``candidate_hypotheses``
    （二者字段不交叠，故字段级合流无歧义、无 last-writer-wins 丢字段）。其余字段从原树
    节点保留。返回新树（不修改输入）。

    之所以独立为纯函数：并行两线路各从同一棵 HITL-1 输出树出发、互不见对方写入，
    若让二者直接写同一 ``tree`` channel、由整节点 upsert reducer 折叠，则后写者会整节点
    覆盖先写者——``status`` 与 ``candidate_hypotheses`` 无法在同节点共存，合并算子无从
    读 ``原文.status × 假设.status`` 矩阵。本函数按字段归属合流，消除该竞态。
    """

    out: list[ArgumentationNode] = []
    for node in tree:
        update: dict[str, object] = {}
        partial = verification_updates.get(node.node_id)
        if partial is not None:
            update["status"] = partial.status
        partial = hypothesis_updates.get(node.node_id)
        if partial is not None:
            update["candidate_hypotheses"] = partial.candidate_hypotheses
        out.append(node.model_copy(update=update))
    return out


# 合并算子对外形态：``(tree, tree, tree) -> tree``（将两线路 partial 视为同棵树的不同字段切片）。
MergeLike = Callable[[list[ArgumentationNode]], list[ArgumentationNode]]
"""合并算子可调用形态（与 :class:`agents.assembly.MergeFn` 结构同构）。

merge 模块不依赖 agents 契约（agents 是装配层、merge 是纯领域算子），故以本地别名
表达该可调用形态，避免反向导入。"""


def merge_with_partials(
    tree: list[ArgumentationNode],
    verification_updates: dict[str, ArgumentationNode],
    hypothesis_updates: dict[str, ArgumentationNode],
    merge_fn: MergeLike,
) -> list[ArgumentationNode]:
    """字段级合流两线路 partial 后跑矩阵裁决——合并的两步 staging 收口于此。

    合并 = 「先字段级合流两线路 partial、再矩阵裁决」两步；本入口把该 staging 收口在
    merge 模块内（之前由调度层 :mod:`runtime.orchestrator` 显式串两步，使合并的内部
    步骤漏到调度层）。返回裁决后的新树（不修改输入）。

    :func:`apply_partial_updates` 仍为公开纯函数，供调度层在合并算子异常时取「已合流、
    未裁决」的中间态兜底——那是兜底语义需要、非 staging 串接，故不构成内部步骤泄漏。
    """

    combined = apply_partial_updates(tree, verification_updates, hypothesis_updates)
    return merge_fn(combined)


# --------------------------------------------------------------------------- #
# 矩阵查表
# --------------------------------------------------------------------------- #

# 「成立」列动作由假设关系决定（ADR-0006/0007）；映射定义于 domain，与影响传导 #7 共用。
_RELATION_TO_ACTION = HYPOTHESIS_RELATION_TO_MERGE_ACTION

_VERDICT_ROWS: frozenset[NodeStatus] = frozenset(
    {NodeStatus.CREDIBLE, NodeStatus.DOUBTFUL, NodeStatus.ERROR}
)
"""体检已下终判的三行（矩阵原文侧）。其余状态视为未裁决、保守 KEEP。"""


def _is_supported(h: Hypothesis) -> bool:
    return h.status is HypothesisStatus.SUPPORTED


def _is_oppose_supported(h: Hypothesis) -> bool:
    return _is_supported(h) and h.relation is HypothesisRelation.OPPOSE


def _decide_credible(hypotheses: list[Hypothesis]) -> tuple[MergeDecision, list[Hypothesis], list[str]]:
    """credible 行：一律保持原文不动。

    - 有对立 supported → 冲突格：贴 ``conflict``、保留对立 supported、丢弃其余。
    - 否则有递进/扩展 supported → 冻结：假设全丢弃、原文不动。
    - 否则（无 supported / 仅 doubtful·refuted）→ 保留原文、丢弃全部假设。
    """

    oppose_supported = [h for h in hypotheses if _is_oppose_supported(h)]
    if oppose_supported:
        kept_ids = [h.hypothesis_id for h in oppose_supported]
        return (
            MergeDecision(
                action=MergeAction.CONFLICT,
                activated_hypothesis_ids=kept_ids,
            ),
            oppose_supported,
            ["conflict"],
        )

    has_advance_expand_supported = any(
        _is_supported(h) and h.relation is not HypothesisRelation.OPPOSE
        for h in hypotheses
    )
    if has_advance_expand_supported:
        return MergeDecision(action=MergeAction.FREEZE), [], []
    return MergeDecision(action=MergeAction.KEEP), [], []


def _decide_flawed(hypotheses: list[Hypothesis]) -> tuple[MergeDecision, list[Hypothesis], list[str]]:
    """doubtful / error 行：原文有缺陷、按假设状态分流。

    - 有 supported → 按关系 REPLACE/REWRITE/SUPPLEMENT；supported 全激活为候选。
      多条 supported 时，节点级动作取**最高 confidence** 者的关系（ADR-0008：
      confidence 仅用于 supported 假设排序），其余 supported 仍作候选保留。
    - 无 supported → KEEP（原文入 HITL-2，doubtful 假设弱呈现供参考、refuted 丢弃）。
    """

    # 裁剪：保留 supported（激活）+ doubtful（弱呈现），丢弃 refuted；保持原顺序。
    kept = [h for h in hypotheses if h.status in (HypothesisStatus.SUPPORTED, HypothesisStatus.DOUBTFUL)]
    supported = [h for h in hypotheses if _is_supported(h)]
    if not supported:
        return MergeDecision(action=MergeAction.KEEP), kept, []

    # max 在 confidence 并列时返回首个（稳定、可复算）。
    primary = max(supported, key=lambda h: h.confidence)
    action = _RELATION_TO_ACTION[primary.relation]
    return (
        MergeDecision(
            action=action,
            activated_hypothesis_ids=[h.hypothesis_id for h in supported],
        ),
        kept,
        [],
    )


def _decide(
    node: ArgumentationNode,
) -> tuple[MergeDecision, list[Hypothesis], list[str]]:
    """单节点矩阵裁决 → (decision, 裁剪后 candidate_hypotheses, 追加 issue_tags)。

    返回的假设列表为**裁剪后存活者**（保持原顺序）；追加的 issue_tags 仅 ``conflict``。
    """

    hypotheses = list(node.candidate_hypotheses)

    # 未裁决节点（体检未覆盖、或并行未跑完）→ 保守 KEEP、不裁剪、不激活。
    if node.status not in _VERDICT_ROWS:
        return MergeDecision(action=MergeAction.KEEP), hypotheses, []

    if node.status is NodeStatus.CREDIBLE:
        return _decide_credible(hypotheses)
    return _decide_flawed(hypotheses)


def merge(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """对每个节点跑 12 格矩阵裁决，返回标注后的新树（不修改输入）。

    - 每节点附 ``merge_decision``；冲突格在 ``issue_tags`` 追加 ``conflict``（去重）。
    - ``candidate_hypotheses`` 按矩阵裁剪（Drop refuted / frozen，保留 activated / weak）。
    - 不改 ``content`` 与 ``status``，不置 ``adopted``——绝不替人拍板（HITL-2 #9 职责）。
    - 所有节点（含无候选者、影子、未裁决）均标注后流入 HITL-2，无独立人工兜底分支。
    """

    out: list[ArgumentationNode] = []
    for node in tree:
        decision, kept_hypotheses, extra_tags = _decide(node)
        new_tags = list(node.issue_tags)
        for tag in extra_tags:
            if tag not in new_tags:
                new_tags.append(tag)
        out.append(
            node.model_copy(
                update={
                    "candidate_hypotheses": kept_hypotheses,
                    "issue_tags": new_tags,
                    "merge_decision": decision,
                }
            )
        )
    return out
