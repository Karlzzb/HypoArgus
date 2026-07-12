"""集中状态机子缝测试（issue #11 · ADR-0011）。

纯函数子缝 ``status_machine`` 收口节点状态流转的「合法 / 非法」判定，原先散落在
hitl2 / writeback / impact 各处的内联守卫统一复用之。「非法状态变更一律拦截」
（PRD §12）由本缝单一定义点兑现。
"""

from __future__ import annotations

import pytest

from domain import Argument, ArgumentStatus, ArgumentType
from status_machine import (
    ALLOWED_TRANSITIONS,
    IllegalStatusTransitionError,
    mark_argument_error,
    transition_argument,
    validate_transition,
)


def test_illegal_transition_unverified_to_corrected_rejected():
    """unverified → corrected 是非法跳级，状态机一律拦截（PRD §12）。"""

    with pytest.raises(IllegalStatusTransitionError):
        validate_transition(ArgumentStatus.UNVERIFIED, ArgumentStatus.CORRECTED)


@pytest.mark.parametrize(
    "old,new",
    [
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.PENDING_VERIFICATION),
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.CREDIBLE),
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.DOUBTFUL),
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.ERROR),
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.INVALID),
        (ArgumentStatus.PENDING_VERIFICATION, ArgumentStatus.CREDIBLE),
        (ArgumentStatus.PENDING_VERIFICATION, ArgumentStatus.DOUBTFUL),
        (ArgumentStatus.PENDING_VERIFICATION, ArgumentStatus.ERROR),
        (ArgumentStatus.PENDING_VERIFICATION, ArgumentStatus.INVALID),
        (ArgumentStatus.CREDIBLE, ArgumentStatus.ADOPTED),
        (ArgumentStatus.DOUBTFUL, ArgumentStatus.ADOPTED),
        (ArgumentStatus.ERROR, ArgumentStatus.ADOPTED),
        (ArgumentStatus.INVALID, ArgumentStatus.ADOPTED),
        (ArgumentStatus.ADOPTED, ArgumentStatus.CORRECTED),
        # invalid 由影响传导对上层论点判：credible/doubtful/error 均可被拖垮至 invalid。
        (ArgumentStatus.CREDIBLE, ArgumentStatus.INVALID),
        (ArgumentStatus.DOUBTFUL, ArgumentStatus.INVALID),
        (ArgumentStatus.ERROR, ArgumentStatus.INVALID),
        # invalid 幂等：再次传导判 invalid 仍合法（守势，正常 judge 不会重复到达）。
        (ArgumentStatus.INVALID, ArgumentStatus.INVALID),
    ],
)
def test_legal_forward_transitions_accepted(old, new):
    """ADR-0011 正向边一律放行（体检判决 / HITL-2 采纳 / 影响传导失效 / 回写翻正）。"""

    validate_transition(old, new)  # 不抛即合法。


@pytest.mark.parametrize(
    "old,new",
    [
        # 重复采纳：adopted/corrected 不可再采纳（hitl2._apply_adopt 原内联守卫）。
        (ArgumentStatus.ADOPTED, ArgumentStatus.ADOPTED),
        (ArgumentStatus.CORRECTED, ArgumentStatus.ADOPTED),
        # 未经任何判决直接采纳：必须先有 credible/doubtful/error/invalid 判决。
        (ArgumentStatus.UNVERIFIED, ArgumentStatus.ADOPTED),
        (ArgumentStatus.PENDING_VERIFICATION, ArgumentStatus.ADOPTED),
        # corrected 为终态：不可再流转。
        (ArgumentStatus.CORRECTED, ArgumentStatus.CORRECTED),
        (ArgumentStatus.CORRECTED, ArgumentStatus.INVALID),
        (ArgumentStatus.CORRECTED, ArgumentStatus.CREDIBLE),
        # 不可从 adopted 退回 / 跳级。
        (ArgumentStatus.ADOPTED, ArgumentStatus.CREDIBLE),
        (ArgumentStatus.ADOPTED, ArgumentStatus.INVALID),
        (ArgumentStatus.ADOPTED, ArgumentStatus.DOUBTFUL),
    ],
)
def test_illegal_transitions_rejected(old, new):
    """越权 / 跳级 / 重复 / 终态后流转一律拦截（PRD §12）。"""

    with pytest.raises(IllegalStatusTransitionError):
        validate_transition(old, new)


def test_corrected_is_terminal_no_outgoing_edges():
    """corrected 为终态：合法可达集为空（回写成功后不再流转）。"""

    assert ALLOWED_TRANSITIONS[ArgumentStatus.CORRECTED] == frozenset()


def _argument(status: ArgumentStatus) -> Argument:
    return Argument(
        argument_id="n-test",
        argument_type=ArgumentType.EVIDENCE,
        paragraph_id="p0",
        content="原文",
        status=status,
    )


def test_transition_argument_flips_status_and_returns_copy():
    """transition_argument 翻正状态、返回新副本、不修改输入（纯函数契约）。"""

    pending = _argument(ArgumentStatus.PENDING_VERIFICATION)
    credible = transition_argument(pending, ArgumentStatus.CREDIBLE)
    assert credible.status is ArgumentStatus.CREDIBLE
    assert pending.status is ArgumentStatus.PENDING_VERIFICATION  # 输入未变


def test_transition_argument_rejects_illegal_via_state_machine():
    """transition_argument 复用 validate_transition，非法变更同样抛（统一写入点）。"""

    with pytest.raises(IllegalStatusTransitionError):
        transition_argument(_argument(ArgumentStatus.UNVERIFIED), ArgumentStatus.CORRECTED)


@pytest.mark.parametrize(
    "start",
    list(ArgumentStatus),
)
def test_mark_argument_error_always_legal_from_any_state(start):
    """编排兜底紧急通道：从任意态 → error 恒合法（PRD §13「就地置错误状态」）。"""

    out = mark_argument_error(_argument(start), reason="verify")
    assert out.status is ArgumentStatus.ERROR
    assert any(
        t == "orchestrator_error:verify" for t in out.issue_tags
    )


def test_mark_argument_error_does_not_mutate_input_and_dedups_tag():
    """mark_argument_error 不改输入、同 reason 标签去重（重试不累积标签）。"""

    argument = _argument(ArgumentStatus.PENDING_VERIFICATION)
    once = mark_argument_error(argument, reason="merge")
    twice = mark_argument_error(once, reason="merge")
    assert argument.status is ArgumentStatus.PENDING_VERIFICATION  # 输入未变
    assert twice.issue_tags.count("orchestrator_error:merge") == 1
