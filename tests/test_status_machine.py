"""集中状态机子缝测试（issue #11 · ADR-0011）。

纯函数子缝 ``status_machine`` 收口节点状态流转的「合法 / 非法」判定，原先散落在
hitl2 / writeback / impact 各处的内联守卫统一复用之。「非法状态变更一律拦截」
（PRD §12）由本缝单一定义点兑现。
"""

from __future__ import annotations

import pytest

from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.status_machine import (
    ALLOWED_TRANSITIONS,
    IllegalStatusTransitionError,
    mark_node_error,
    transition_node,
    validate_transition,
)


def test_illegal_transition_unverified_to_corrected_rejected():
    """unverified → corrected 是非法跳级，状态机一律拦截（PRD §12）。"""

    with pytest.raises(IllegalStatusTransitionError):
        validate_transition(NodeStatus.UNVERIFIED, NodeStatus.CORRECTED)


@pytest.mark.parametrize(
    "old,new",
    [
        (NodeStatus.UNVERIFIED, NodeStatus.PENDING_VERIFICATION),
        (NodeStatus.UNVERIFIED, NodeStatus.CREDIBLE),
        (NodeStatus.UNVERIFIED, NodeStatus.DOUBTFUL),
        (NodeStatus.UNVERIFIED, NodeStatus.ERROR),
        (NodeStatus.UNVERIFIED, NodeStatus.INVALID),
        (NodeStatus.PENDING_VERIFICATION, NodeStatus.CREDIBLE),
        (NodeStatus.PENDING_VERIFICATION, NodeStatus.DOUBTFUL),
        (NodeStatus.PENDING_VERIFICATION, NodeStatus.ERROR),
        (NodeStatus.PENDING_VERIFICATION, NodeStatus.INVALID),
        (NodeStatus.CREDIBLE, NodeStatus.ADOPTED),
        (NodeStatus.DOUBTFUL, NodeStatus.ADOPTED),
        (NodeStatus.ERROR, NodeStatus.ADOPTED),
        (NodeStatus.INVALID, NodeStatus.ADOPTED),
        (NodeStatus.ADOPTED, NodeStatus.CORRECTED),
        # invalid 由影响传导对上层论点判：credible/doubtful/error 均可被拖垮至 invalid。
        (NodeStatus.CREDIBLE, NodeStatus.INVALID),
        (NodeStatus.DOUBTFUL, NodeStatus.INVALID),
        (NodeStatus.ERROR, NodeStatus.INVALID),
        # invalid 幂等：再次传导判 invalid 仍合法（守势，正常 judge 不会重复到达）。
        (NodeStatus.INVALID, NodeStatus.INVALID),
    ],
)
def test_legal_forward_transitions_accepted(old, new):
    """ADR-0011 正向边一律放行（体检判决 / HITL-2 采纳 / 影响传导失效 / 回写翻正）。"""

    validate_transition(old, new)  # 不抛即合法。


@pytest.mark.parametrize(
    "old,new",
    [
        # 重复采纳：adopted/corrected 不可再采纳（hitl2._apply_adopt 原内联守卫）。
        (NodeStatus.ADOPTED, NodeStatus.ADOPTED),
        (NodeStatus.CORRECTED, NodeStatus.ADOPTED),
        # 未经任何判决直接采纳：必须先有 credible/doubtful/error/invalid 判决。
        (NodeStatus.UNVERIFIED, NodeStatus.ADOPTED),
        (NodeStatus.PENDING_VERIFICATION, NodeStatus.ADOPTED),
        # corrected 为终态：不可再流转。
        (NodeStatus.CORRECTED, NodeStatus.CORRECTED),
        (NodeStatus.CORRECTED, NodeStatus.INVALID),
        (NodeStatus.CORRECTED, NodeStatus.CREDIBLE),
        # 不可从 adopted 退回 / 跳级。
        (NodeStatus.ADOPTED, NodeStatus.CREDIBLE),
        (NodeStatus.ADOPTED, NodeStatus.INVALID),
        (NodeStatus.ADOPTED, NodeStatus.DOUBTFUL),
    ],
)
def test_illegal_transitions_rejected(old, new):
    """越权 / 跳级 / 重复 / 终态后流转一律拦截（PRD §12）。"""

    with pytest.raises(IllegalStatusTransitionError):
        validate_transition(old, new)


def test_corrected_is_terminal_no_outgoing_edges():
    """corrected 为终态：合法可达集为空（回写成功后不再流转）。"""

    assert ALLOWED_TRANSITIONS[NodeStatus.CORRECTED] == frozenset()


def _node(status: NodeStatus) -> ArgumentationNode:
    return ArgumentationNode(
        node_id="n-test",
        node_type=NodeType.EVIDENCE,
        paragraph_id="p0",
        content="原文",
        status=status,
    )


def test_transition_node_flips_status_and_returns_copy():
    """transition_node 翻正状态、返回新副本、不修改输入（纯函数契约）。"""

    pending = _node(NodeStatus.PENDING_VERIFICATION)
    credible = transition_node(pending, NodeStatus.CREDIBLE)
    assert credible.status is NodeStatus.CREDIBLE
    assert pending.status is NodeStatus.PENDING_VERIFICATION  # 输入未变


def test_transition_node_rejects_illegal_via_state_machine():
    """transition_node 复用 validate_transition，非法变更同样抛（统一写入点）。"""

    with pytest.raises(IllegalStatusTransitionError):
        transition_node(_node(NodeStatus.UNVERIFIED), NodeStatus.CORRECTED)


@pytest.mark.parametrize(
    "start",
    list(NodeStatus),
)
def test_mark_node_error_always_legal_from_any_state(start):
    """编排兜底紧急通道：从任意态 → error 恒合法（PRD §13「就地置错误状态」）。"""

    out = mark_node_error(_node(start), reason="verify")
    assert out.status is NodeStatus.ERROR
    assert any(
        t == "orchestrator_error:verify" for t in out.issue_tags
    )


def test_mark_node_error_does_not_mutate_input_and_dedups_tag():
    """mark_node_error 不改输入、同 reason 标签去重（重试不累积标签）。"""

    node = _node(NodeStatus.PENDING_VERIFICATION)
    once = mark_node_error(node, reason="merge")
    twice = mark_node_error(once, reason="merge")
    assert node.status is NodeStatus.PENDING_VERIFICATION  # 输入未变
    assert twice.issue_tags.count("orchestrator_error:merge") == 1
