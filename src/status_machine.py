"""节点状态机集中子缝（issue #11 · ADR-0011 · PRD §12/§13）。

原先散落在 hitl2（``_apply_adopt``）、writeback（``adopted → corrected`` 翻正）、
impact（``→ invalid``）各处的内联状态守卫，统一收口到本缝。「非法状态变更一律拦截」
（PRD §12）由本缝单一定义点兑现，各调用方复用 :func:`validate_transition` /
:func:`transition_node`，杜绝规则漂移。

合法正向边（ADR-0011 状态机全貌）：

::

    unverified → pending_verification | credible | doubtful | error | invalid
    pending_verification → credible | doubtful | error | invalid
    credible / doubtful / error → adopted | invalid
    invalid → adopted | invalid
    adopted → corrected
    corrected：（终态）

``pending_verification`` 为**可选中间态**：体检（#4）可直达 ``credible/doubtful/error``
（实现现状），亦可先置 ``pending`` 再判决——二者皆合法。``invalid`` 由影响传导对**上层
论点**单独判定（``error`` 是体检对叶子论据的判决，二者分工见 ADR-0011）；上层论点纵未
经体检判决（``unverified``），亦可因子节点支撑塌方而被传导判 ``invalid``。``adopted →
corrected`` 由回写成功触发；``credible/doubtful/error/invalid → adopted`` 由 HITL-2 采纳
触发（``credible`` 仅贴 ``conflict`` 时方可采纳，该约束由 HITL-2 层 ``_is_pending`` 守，
不在状态机层）。

紧急通道 :func:`mark_node_error`：PRD §13 授权编排中枢在下游异常 / 超时时「就地置
目标节点错误状态」，故 ``→ error`` 从任意态恒合法——与上表的正向 ``error`` 判决分流，
不走 :func:`validate_transition`，避免把「紧急兜底」误判为「非法跳级」。
"""

from __future__ import annotations

from collections.abc import Mapping

from hypoargus.domain import ArgumentationNode, NodeStatus

__all__ = [
    "ALLOWED_TRANSITIONS",
    "IllegalStatusTransitionError",
    "mark_node_error",
    "validate_transition",
    "transition_node",
]


class IllegalStatusTransitionError(Exception):
    """非法状态变更：尝试的状态迁移不在 :data:`ALLOWED_TRANSITIONS` 内。

    PRD §12「非法状态变更一律拦截」的统一异常。各状态写入点（hitl2 / writeback /
    impact）据此拦截越权流转，整条决策丢弃。
    """


# 合法正向迁移表（ADR-0011）。键 = 起始态，值 = 可达终态集合。
ALLOWED_TRANSITIONS: Mapping[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.UNVERIFIED: frozenset(
        {
            NodeStatus.PENDING_VERIFICATION,
            NodeStatus.CREDIBLE,
            NodeStatus.DOUBTFUL,
            NodeStatus.ERROR,
            NodeStatus.INVALID,
        }
    ),
    NodeStatus.PENDING_VERIFICATION: frozenset(
        {
            NodeStatus.CREDIBLE,
            NodeStatus.DOUBTFUL,
            NodeStatus.ERROR,
            NodeStatus.INVALID,
        }
    ),
    NodeStatus.CREDIBLE: frozenset({NodeStatus.ADOPTED, NodeStatus.INVALID}),
    NodeStatus.DOUBTFUL: frozenset({NodeStatus.ADOPTED, NodeStatus.INVALID}),
    NodeStatus.ERROR: frozenset({NodeStatus.ADOPTED, NodeStatus.INVALID}),
    NodeStatus.INVALID: frozenset({NodeStatus.ADOPTED, NodeStatus.INVALID}),
    NodeStatus.ADOPTED: frozenset({NodeStatus.CORRECTED}),
    NodeStatus.CORRECTED: frozenset(),
}


def validate_transition(old: NodeStatus, new: NodeStatus) -> None:
    """校验 ``old → new`` 是否合法；非法即抛 :class:`IllegalStatusTransitionError`。

    本函数只覆盖**正向**状态机（体检判决 / HITL-2 采纳 / 影响传导失效 / 回写翻正）。
    编排中枢异常兜底的「就地置 error」走 :func:`mark_node_error` 紧急通道，不经本函数
    （PRD §13 授权从任意态 → error）。
    """

    allowed = ALLOWED_TRANSITIONS.get(old, frozenset())
    if new not in allowed:
        raise IllegalStatusTransitionError(
            f"非法状态变更：{old.value} → {new.value}（合法可达："
            f"{sorted(s.value for s in allowed) or '终态·无可达'}）"
        )


def transition_node(
    node: ArgumentationNode, new_status: NodeStatus
) -> ArgumentationNode:
    """校验后返回翻正后的节点副本（不修改输入）。

    状态机合法迁移的统一写入点：先 :func:`validate_transition` 拦截越权，再
    ``model_copy`` 翻正。调用方不再各自内联守卫，杜绝规则漂移。
    """

    validate_transition(node.status, new_status)
    return node.model_copy(update={"status": new_status})


# 异常兜底错误标签（与 writeback_error 同一 ``issue_tags`` 通道，便于审计一眼识别）。
ORCHESTRATOR_ERROR_TAG = "orchestrator_error"


def mark_node_error(
    node: ArgumentationNode, reason: str | None = None
) -> ArgumentationNode:
    """编排中枢异常兜底：就地置节点 ``error`` + 贴 ``orchestrator_error`` 标签（PRD §13）。

    紧急通道，**从任意态恒合法**——下游异常 / 超时时编排中枢「就地置目标节点错误状态」
    的统一写入点。不走 :func:`validate_transition`，避免把紧急兜底误判为非法跳级。
    ``reason`` 写入标签便于审计回溯（形如 ``orchestrator_error:merge:RuntimeError``）。
    """

    tag = ORCHESTRATOR_ERROR_TAG
    if reason:
        tag = f"{ORCHESTRATOR_ERROR_TAG}:{reason}"
    tags = list(node.issue_tags)
    if tag not in tags:
        tags.append(tag)
    return node.model_copy(
        update={"status": NodeStatus.ERROR, "issue_tags": tags}
    )
