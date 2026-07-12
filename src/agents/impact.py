"""影响传导 Agent（PRD §8、issue #7、ADR-0003/0013）：串行·不产文本。

在双轨合并（#6）之后**串行**运行，读取标注完成的树。**不生产任何替代文本**，只做两件事：

1. 把失效沿论证权重向上传导：按「剩余支撑率 = Σ 存活直接子节点权重 ÷ Σ 全部参与
   传导的直接子节点权重」判决上层论点（``main_claim`` / ``sub_claim``）——
   ``< 0.5`` → ``invalid``；``0.5 ~ 0.7`` → 贴「弱化」批注不失效；``≥ 0.7`` 不受影响
   （ADR-0013）。后序遍历使失效逐层上推至根：``invalid`` 子节点对父论点计为不存活。
2. 若某上层论点因此塌方（``invalid``），**复用该节点已有的成立假设**去激活——仅复用、
   绝不新建假设（修订假设唯一来源是开药 Agent #5，ADR-0003）。

状态分工（ADR-0011）：

- ``error``：体检（#4）对叶子论据的判决——论据自证其伪。
- ``invalid``：影响传导对上层论点的判决——失去支撑被拖垮。

故影响传导只判 ``main_claim`` / ``sub_claim``（有子节点可失支撑者）；叶子
（``evidence`` / ``qualification``）与影子节点不参与传导。自身已 ``error`` 的上层论点
不再改判 ``invalid``（``error`` 是自证其伪、比「失去支撑」更直接的判决，取前者）。

本函数是纯函数子缝（PRD «Testing Decisions»）：``标注后的树 → 标注后的同一棵树``，
确定性、无 LLM / 检索依赖、可独立单测。**绝不替人拍板**：不改 ``content``、不新建假设、
不置 ``adopted`` / ``corrected``（那是 HITL-2 #9 与回写 #10 的职责）。失效只改 ``status``
为 ``invalid``、贴 ``weakening`` 批注、并复用既有成立假设激活 ``merge_decision``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agents.hypothesis import HypothesisStatus
from domain import (
    HYPOTHESIS_RELATION_TO_MERGE_ACTION,
    ArgumentationNode,
    MergeAction,
    MergeDecision,
    NodeStatus,
    NodeType,
)
from status_machine import validate_transition

__all__ = [
    "ImpactVerdict",
    "ResidualSupport",
    "WEAKENING_TAG",
    "INVALID_RATIO_THRESHOLD",
    "WEAKEN_RATIO_THRESHOLD",
    "compute_residual_support",
    "verdict_for_ratio",
    "impact",
]


# --------------------------------------------------------------------------- #
# 可调参数（ADR-0013「阈值...为可调参数，后期回归调优」）
# --------------------------------------------------------------------------- #

INVALID_RATIO_THRESHOLD = 0.5
"""剩余支撑率低于此值 → 父论点 ``invalid``（ADR-0013）。"""

WEAKEN_RATIO_THRESHOLD = 0.7
"""剩余支撑率低于此值（且不低于失效阈值）→ 贴「弱化」批注不失效；``≥`` 此值不受影响。"""

WEAKENING_TAG = "weakening"
"""「弱化」批注标签（贴入 ``issue_tags``，对应 PRD §8「0.5–0.7 贴弱化批注」）。"""

_NON_SURVIVING: frozenset[NodeStatus] = frozenset({NodeStatus.ERROR, NodeStatus.INVALID})
"""不存活状态：``error``（自证其伪）与 ``invalid``（被拖垮）——对父论点不提供支撑。
``doubtful`` 仍计存活（存疑但立着），``unverified`` / ``pending_verification`` 计存活。"""

_UPPER_CLAIMS: frozenset[NodeType] = frozenset({NodeType.MAIN_CLAIM, NodeType.SUB_CLAIM})
"""影响传导只判上层论点；叶子（evidence/qualification）与影子节点不参与。"""


# --------------------------------------------------------------------------- #
# 失效判定公式（纯函数 seam · 可复算、可解释 · ADR-0013）
# --------------------------------------------------------------------------- #


class ImpactVerdict(StrEnum):
    """单节点剩余支撑率判决（ADR-0013）。"""

    INVALID = "invalid"
    WEAKEN = "weaken"
    UNAFFECTED = "unaffected"


@dataclass(frozen=True)
class ResidualSupport:
    """某上层论点的剩余支撑率计算结果（可复算、可解释）。

    ``ratio`` = ``surviving_weight`` / ``total_weight``（分母为 0 时守为 1.0——无权重证据
    不崩盘）；``participating_children`` 为参与传导的直接子节点数（影子子节点不计）。
    """

    ratio: float
    surviving_weight: int
    total_weight: int
    participating_children: int

    def rationale(self) -> str:
        """人类可读判决理由（可复算、可解释，ADR-0013）。"""

        verdict = verdict_for_ratio(self.ratio)
        if self.total_weight == 0:
            return (
                f"剩余支撑率 = {self.surviving_weight}/{self.total_weight}"
                f"（无参与传导子节点）→ {verdict.value}"
            )
        return (
            f"剩余支撑率 = {self.surviving_weight}/{self.total_weight}"
            f" = {self.ratio:.4f} → {verdict.value}"
        )


def compute_residual_support(
    children: list[ArgumentationNode],
) -> ResidualSupport:
    """计算父论点的剩余支撑率（ADR-0013）。

    影子子节点（``background`` / ``evaluation``）不参与传导——既不计入分母也不计入
    分子。``error`` / ``invalid`` 子节点计为不存活（提供 0 支撑）；其余（含 ``doubtful``）
    计存活。无参与传导子节点（全影子 / 空）时 ``ratio`` 守为 1.0——不凭空判失效。
    """

    participating = [c for c in children if not c.node_type.is_shadow]
    total = sum(c.argument_weight for c in participating)
    surviving = sum(
        c.argument_weight for c in participating if c.status not in _NON_SURVIVING
    )
    ratio = surviving / total if total > 0 else 1.0
    return ResidualSupport(
        ratio=ratio,
        surviving_weight=surviving,
        total_weight=total,
        participating_children=len(participating),
    )


def verdict_for_ratio(ratio: float) -> ImpactVerdict:
    """据剩余支撑率判 ``invalid`` / ``weaken`` / ``unaffected``（ADR-0013）。

    ``ratio < 0.5`` → ``invalid``；``0.5 ≤ ratio < 0.7`` → ``weaken``；``ratio ≥ 0.7``
    → ``unaffected``。
    """

    if ratio < INVALID_RATIO_THRESHOLD:
        return ImpactVerdict.INVALID
    if ratio < WEAKEN_RATIO_THRESHOLD:
        return ImpactVerdict.WEAKEN
    return ImpactVerdict.UNAFFECTED


# --------------------------------------------------------------------------- #
# 复用既有成立假设激活（塌方时 · ADR-0003）
# --------------------------------------------------------------------------- #


_ACTIVATING_ACTIONS: frozenset[MergeAction] = frozenset(
    {
        MergeAction.REPLACE,
        MergeAction.REWRITE,
        MergeAction.SUPPLEMENT,
        MergeAction.CONFLICT,
    }
)
"""合并算子（#6）已激活假设的裁决动作（doubtful/error 行的 supported 列）。影响传导遇
``invalid`` 时：节点已被判这些动作 → 假设已激活、保留原裁决、仅翻 status；
``KEEP`` / ``FREEZE`` / ``None`` → 复用节点既有 supported 假设重新激活。"""


def _reactivate(node: ArgumentationNode) -> MergeDecision:
    """复用节点**已有**成立假设去激活（仅复用、绝不新建，ADR-0003）。

    无 supported 假设 → ``KEEP``（无候选、原文入 HITL-2）；有 supported → 按最高
    confidence 者的关系取动作（与合并算子 #6 同源映射）、全部 supported 入候选。
    """

    supported = [
        h for h in node.candidate_hypotheses if h.status is HypothesisStatus.SUPPORTED
    ]
    if not supported:
        return MergeDecision(action=MergeAction.KEEP)
    primary = max(supported, key=lambda h: h.confidence)
    action = HYPOTHESIS_RELATION_TO_MERGE_ACTION[primary.relation]
    return MergeDecision(
        action=action,
        activated_hypothesis_ids=[h.hypothesis_id for h in supported],
    )


def _invalidate(node: ArgumentationNode) -> ArgumentationNode:
    """把上层论点置 ``invalid``：status 翻 ``invalid``，按需复用既有假设激活。

    状态迁移合法性经集中状态机子缝 :func:`validate_transition` 校验（``credible`` /
    ``doubtful`` / ``error`` → ``invalid``，ADR-0011）——上层论点在合并后处于这三态之一，
    故迁移恒合法；越权流转由状态机统一拦截。
    """

    decision = node.merge_decision
    if decision is not None and decision.action in _ACTIVATING_ACTIONS:
        new_decision = decision  # 已激活假设（合并算子对 doubtful/error 行所设）——保留。
    else:
        new_decision = _reactivate(node)  # KEEP/FREEZE/None → 复用 supported 重新激活。
    validate_transition(node.status, NodeStatus.INVALID)
    return node.model_copy(
        update={"status": NodeStatus.INVALID, "merge_decision": new_decision}
    )


# --------------------------------------------------------------------------- #
# 影响传导主入口（串行·不产文本·纯函数）
# --------------------------------------------------------------------------- #


def impact(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """对标注完成的树跑失效传导，返回标注后的新树（不修改输入）。

    后序遍历（先子后父）：使 ``invalid`` 子节点在判父论点时已计为不存活，失效逐层上推
    至根。详见模块 docstring。

    - 只判 ``main_claim`` / ``sub_claim``（有子可失支撑者）；叶子与影子节点不参与。
    - 自身已 ``error`` 的上层论点不再改判 ``invalid``（``error`` 是更直接的自证其伪判决）。
    - ``invalid``：status 翻 ``invalid`` + 按需复用既有 supported 假设激活 ``merge_decision``。
    - ``weaken``：仅追加 ``weakening`` 批注、不失效、不改 merge_decision。
    - ``unaffected``：不动。绝不改 ``content``、不新建假设、不置 ``adopted``/``corrected``。
    """

    by_id: dict[str, ArgumentationNode] = {n.node_id: n for n in tree}
    if len(by_id) != len(tree):
        # node_id 重复——树结构不变式违反（应由 validate_tree 兜底）；守势：逐节点浅拷。
        return [n.model_copy() for n in tree]

    # 可变状态视图：后序中子节点先翻 invalid，父论点读其最新 status 计剩余支撑率。
    working: dict[str, ArgumentationNode] = {n.node_id: n for n in tree}
    seen: set[str] = set()

    def judge(node: ArgumentationNode) -> None:
        nid = node.node_id
        if nid in seen:
            return  # 防御：环或重复到达（树应无环，validate_tree 兜底）。
        seen.add(nid)
        # 后序：先判子节点。
        for cid in node.children_ids:
            child = working.get(cid)
            if child is not None:
                judge(child)
        current = working[nid]
        if current.node_type not in _UPPER_CLAIMS:
            return  # 叶子 / 影子不参与传导。
        if current.status is NodeStatus.ERROR:
            return  # 自证其伪：不再改判 invalid。
        children = [working[c] for c in current.children_ids if c in working]
        verdict = verdict_for_ratio(compute_residual_support(children).ratio)
        if verdict is ImpactVerdict.INVALID:
            working[nid] = _invalidate(current)
        elif verdict is ImpactVerdict.WEAKEN:
            tags = list(current.issue_tags)
            if WEAKENING_TAG not in tags:
                tags.append(WEAKENING_TAG)
            working[nid] = current.model_copy(update={"issue_tags": tags})
        # UNAFFECTED：不动，working[nid] 仍为输入节点。

    for n in tree:
        if n.parent_id is None or n.parent_id not in by_id:
            judge(n)  # 森林根（含 dangling parent_id，守势当根处理）。
    for n in tree:  # 兜底：覆盖任何未从根到达的节点（结构异常时）。
        if n.node_id not in seen:
            judge(n)

    # 输出新实例：未变节点浅拷、已变节点取 working 中的新实例。
    return [
        working[n.node_id] if working[n.node_id] is not n else n.model_copy()
        for n in tree
    ]
