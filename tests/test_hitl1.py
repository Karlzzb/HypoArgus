"""HITL-1 结构确认闸门单测（PRD §10 节点 1、issue #2）。

解析输出初始论证树后、双线路启动前触发。用户可调层级、合并或拆分节点、修正边界、
标记无需处理的段落；**支持跳过**（跳过则直接进入下一环节，不改动原文一个字）。

HITL-1 是「人」的意图性编辑，与解析器对 LLM 的防御性兜底**非对称**：解析器遇环即断、
越界即兜底；HITL-1 遇非法编辑一律**拒绝**（抛 ``TreeInvariantError``）、绝不静默修复，
且整个决策要么全部应用、要么全部丢弃（copy-first + 每步 revalidate）。

本切片 HITL-1 为同步注入闸门（``Hitl1Gate`` seam，``FakeHitl1Gate`` 供离线单测）；
真实 interrupt + checkpointer 属 #11。
"""

from __future__ import annotations

import pytest

from agents.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Gate,
    Hitl1Question,
    Hitl1Reply,
    confirm,
    confirm_partition,
)
from agents.hitl1.contract import Hitl1Route
from domain import Argument, ArgumentType
from tree_invariants import TreeInvariantError


def _argument(
    argument_id: str,
    *,
    parent_id: str | None = None,
    children_ids: list[str] | None = None,
    paragraph_id: str = "p0001",
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    argument_weight: int = 50,
) -> Argument:
    return Argument(
        argument_id=argument_id,
        argument_type=argument_type,
        parent_id=parent_id,
        children_ids=list(children_ids or []),
        paragraph_id=paragraph_id,
        argument_weight=argument_weight,
    )


def _abc_tree() -> list[Argument]:
    """A 根，B、C 均为 A 的子（同段 p0001）。"""

    return [
        _argument("a", argument_type=ArgumentType.MAIN_CLAIM, children_ids=["b", "c"]),
        _argument("b", parent_id="a"),
        _argument("c", parent_id="a"),
    ]


def _gate(decision: Hitl1Decision) -> Hitl1Gate:
    return FakeHitl1Gate(decision)


# --------------------------------------------------------------------------- #
# slice 1：骨架 + skip/accept + 输入校验 + 空编辑
# --------------------------------------------------------------------------- #


def test_confirm_skip_returns_tree_unchanged():
    """skip → 树原样返回（不改动原文一个字）。"""

    argument_tree = _abc_tree()
    out = confirm(argument_tree, _gate(Hitl1Decision(action=Hitl1Action.SKIP)))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in argument_tree]


def test_confirm_accept_returns_tree_unchanged():
    """accept → 树原样返回。"""

    argument_tree = _abc_tree()
    out = confirm(argument_tree, _gate(Hitl1Decision(action=Hitl1Action.ACCEPT)))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in argument_tree]


def test_confirm_validates_input_tree():
    """输入树本身非法 → 抛 TreeInvariantError，且 gate.review 从未被调用。"""

    bad_tree = [_argument("a", parent_id="ghost")]
    reviewed = []

    class _Spy:
        def review(self, argument_tree):
            reviewed.append(True)
            return Hitl1Decision(action=Hitl1Action.SKIP)

    with pytest.raises(TreeInvariantError):
        confirm(bad_tree, _Spy())  # type: ignore[arg-type]
    assert reviewed == []  # gate 未被调用


def test_confirm_edit_with_empty_ops_unchanged():
    """edit + 空 ops → 树不变（但返回深拷贝，不与输入同对象）。"""

    argument_tree = _abc_tree()
    out = confirm(argument_tree, _gate(Hitl1Decision(action=Hitl1Action.EDIT, ops=[])))
    assert [n.model_dump() for n in out] == [n.model_dump() for n in argument_tree]


def test_confirm_does_not_mutate_caller_tree():
    """confirm 在深拷贝上工作，调用方的树对象永不被改动。"""

    argument_tree = _abc_tree()
    snapshot = [n.model_copy(deep=True) for n in argument_tree]
    confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[],  # 即使有 ops，调用方也不应被改
            )
        ),
    )
    assert [n.model_dump() for n in argument_tree] == [n.model_dump() for n in snapshot]


# --------------------------------------------------------------------------- #
# slice 2：reparent（调层级）
# --------------------------------------------------------------------------- #


def _reparent(argument_id: str, new_parent_id: str | None) -> Hitl1Decision:
    from agents.hitl1 import ReparentOp

    return Hitl1Decision(
        action=Hitl1Action.EDIT,
        ops=[ReparentOp(argument_id=argument_id, new_parent_id=new_parent_id)],
    )


def test_confirm_reparent_updates_parent_and_children():
    """reparent C 到 B 下：C.parent=B，B.children 含 C，A.children 释放 C。"""

    argument_tree = _abc_tree()
    out = confirm(argument_tree, _gate(_reparent("c", "b")))
    by_id = {n.argument_id: n for n in out}
    assert by_id["c"].parent_id == "b"
    assert "c" in by_id["b"].children_ids
    assert "c" not in by_id["a"].children_ids


def test_confirm_reparent_to_none_makes_root():
    """reparent C 到 None → C 成为根。"""

    argument_tree = _abc_tree()
    out = confirm(argument_tree, _gate(_reparent("c", None)))
    by_id = {n.argument_id: n for n in out}
    assert by_id["c"].parent_id is None
    assert "c" not in by_id["a"].children_ids


def test_confirm_reparent_creating_cycle_raises_and_leaves_caller_untouched():
    """reparent A 到其后代 C → 成环 → 抛错；调用方原树不变。"""

    argument_tree = _abc_tree()
    snapshot = [n.model_dump() for n in argument_tree]
    with pytest.raises(TreeInvariantError):
        confirm(argument_tree, _gate(_reparent("a", "c")))
    assert [n.model_dump() for n in argument_tree] == snapshot


def test_confirm_reparent_to_missing_parent_raises():
    """reparent 到不存在的节点 → 抛错。"""

    argument_tree = _abc_tree()
    with pytest.raises(TreeInvariantError):
        confirm(argument_tree, _gate(_reparent("c", "ghost")))


# --------------------------------------------------------------------------- #
# slice 3：merge（同段合并）/ split（同段拆分）
# --------------------------------------------------------------------------- #


def test_confirm_merge_same_paragraph_unions_children():
    """合并同段两节点：幸存者保留自身属性，被删节点的子节点改挂幸存者。"""

    from agents.hitl1 import MergeOp

    # a 根 → b（p0001）、c（p0001），c 有子 d。
    argument_tree = [
        _argument("a", argument_type=ArgumentType.MAIN_CLAIM, children_ids=["b", "c"]),
        _argument("b", parent_id="a", paragraph_id="p0001"),
        _argument("c", parent_id="a", paragraph_id="p0001", children_ids=["d"]),
        _argument("d", parent_id="c", paragraph_id="p0001"),
    ]
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[MergeOp(argument_ids=["b", "c"])],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out}
    assert "c" not in by_id  # 被合并删除
    assert "d" in by_id
    assert by_id["d"].parent_id == "b"  # d 改挂幸存者 b
    assert "d" in by_id["b"].children_ids
    assert "b" in by_id["a"].children_ids


def test_confirm_merge_cross_paragraph_rejected():
    """跨段合并违反 ADR-0001（一节点一段）→ 抛错，调用方不变。"""

    from agents.hitl1 import MergeOp

    argument_tree = [
        _argument("a", argument_type=ArgumentType.MAIN_CLAIM, children_ids=["b", "c"]),
        _argument("b", parent_id="a", paragraph_id="p0001"),
        _argument("c", parent_id="a", paragraph_id="p0002"),
    ]
    snapshot = [n.model_dump() for n in argument_tree]
    with pytest.raises(TreeInvariantError, match="跨段|paragraph"):
        confirm(
            argument_tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[MergeOp(argument_ids=["b", "c"])],
                )
            ),
        )
    assert [n.model_dump() for n in argument_tree] == snapshot


def test_confirm_split_creates_sibling_same_paragraph():
    """拆分节点 N → 新节点为同段叶兄弟，唯一 id，继承类型/父。"""

    from agents.hitl1 import SplitOp

    argument_tree = _abc_tree()
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SplitOp(argument_id="b")],
            )
        ),
    )
    new_arguments = [n for n in out if n.argument_id not in {"a", "b", "c"}]
    assert len(new_arguments) == 1
    new = new_arguments[0]
    by_id = {n.argument_id: n for n in out}
    # 新节点与源节点同段、同类型、同父（叶兄弟），唯一 id
    assert new.paragraph_id == by_id["b"].paragraph_id
    assert new.argument_type == by_id["b"].argument_type
    assert new.parent_id == by_id["b"].parent_id  # 同父兄弟
    assert new.children_ids == []  # 叶
    assert new.argument_id in by_id["a"].children_ids  # 父认子


def test_confirm_split_twice_yields_distinct_ids():
    """连续拆分两次 → 两个不同 id，均不与既有冲突。"""

    from agents.hitl1 import SplitOp

    argument_tree = _abc_tree()
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SplitOp(argument_id="b"), SplitOp(argument_id="b")],
            )
        ),
    )
    new_ids = [n.argument_id for n in out if n.argument_id not in {"a", "b", "c"}]
    assert len(new_ids) == 2
    assert len(set(new_ids)) == 2  # 互不相同


# --------------------------------------------------------------------------- #
# slice 4：set_type / mark_no_op
# --------------------------------------------------------------------------- #


def test_confirm_set_type_demote_to_shadow_zeros_weight():
    """set_type → BACKGROUND：影子节点，权重归零。"""

    from agents.hitl1 import SetTypeOp

    argument_tree = [
        _argument("a", argument_type=ArgumentType.MAIN_CLAIM, argument_weight=80, children_ids=["b"]),
        _argument("b", parent_id="a", argument_type=ArgumentType.EVIDENCE, argument_weight=85),
    ]
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SetTypeOp(argument_id="b", new_type=ArgumentType.BACKGROUND)],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out}
    assert by_id["b"].argument_type == ArgumentType.BACKGROUND
    assert by_id["b"].argument_type.is_shadow
    assert by_id["b"].argument_weight == 0


def test_confirm_set_type_promote_shadow_to_core_sets_default_weight():
    """set_type 影子→核心：权重设保守默认 50（原 0 不适合核心）。"""

    from agents.hitl1 import SetTypeOp

    argument_tree = [
        _argument("a", argument_type=ArgumentType.MAIN_CLAIM, argument_weight=80, children_ids=["b"]),
        _argument("b", parent_id="a", argument_type=ArgumentType.BACKGROUND, argument_weight=0),
    ]
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[SetTypeOp(argument_id="b", new_type=ArgumentType.SUB_CLAIM)],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out}
    assert by_id["b"].argument_type == ArgumentType.SUB_CLAIM
    assert not by_id["b"].argument_type.is_shadow
    assert by_id["b"].argument_weight == 50


def test_confirm_mark_no_op_converts_paragraph_to_shadow():
    """mark_no_op(pid)：该段所有节点转 BACKGROUND、权重 0，结构不变。"""

    from agents.hitl1 import MarkNoOpOp

    argument_tree = [
        _argument(
            "a",
            argument_type=ArgumentType.MAIN_CLAIM,
            argument_weight=80,
            children_ids=["b", "c"],
        ),
        _argument("b", parent_id="a", paragraph_id="p0001", argument_type=ArgumentType.EVIDENCE, argument_weight=70),
        _argument("c", parent_id="a", paragraph_id="p0002", argument_type=ArgumentType.EVIDENCE, argument_weight=60),
    ]
    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[MarkNoOpOp(paragraph_id="p0001")],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out}
    # p0001 的节点（a、b）转影子、权重 0
    assert by_id["a"].argument_type == ArgumentType.BACKGROUND
    assert by_id["a"].argument_weight == 0
    assert by_id["b"].argument_type == ArgumentType.BACKGROUND
    assert by_id["b"].argument_weight == 0
    # p0002 的 c 不受影响
    assert by_id["c"].argument_type == ArgumentType.EVIDENCE
    assert by_id["c"].argument_weight == 60
    # 结构不变
    assert by_id["a"].children_ids == ["b", "c"]
    assert by_id["b"].parent_id == "a"


# --------------------------------------------------------------------------- #
# slice 5：fix_boundary（延后）+ 多步序列
# --------------------------------------------------------------------------- #


def test_confirm_fix_boundary_raises_deferred():
    """fix_boundary 延后实现（domain 无 text_span，ADR-0001）→ NotImplementedError。"""

    from agents.hitl1 import FixBoundaryOp

    argument_tree = _abc_tree()
    snapshot = [n.model_dump() for n in argument_tree]
    with pytest.raises(NotImplementedError, match="text_span"):
        confirm(
            argument_tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[FixBoundaryOp(argument_id="b")],
                )
            ),
        )
    assert [n.model_dump() for n in argument_tree] == snapshot


def test_confirm_edit_sequence_applied_in_order_and_validated():
    """多步编辑按序应用、每步 revalidate：先 split b，再 reparent 新节点到 c 下。"""

    from agents.hitl1 import ReparentOp

    argument_tree = _abc_tree()
    # 第一步 split b → 新节点 new；第二步 reparent new 到 c（需先知道 new 的 id）。
    # 由于 id 由实现决定，第二步用 split 产出的节点——这里改用更简单的序列：
    # reparent c 到 b，再 set b 为 sub_claim（无关结构变更，验证多步不冲突）。
    from agents.hitl1 import SetTypeOp

    out = confirm(
        argument_tree,
        _gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[
                    ReparentOp(argument_id="c", new_parent_id="b"),
                    SetTypeOp(argument_id="b", new_type=ArgumentType.SUB_CLAIM),
                ],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out}
    assert by_id["c"].parent_id == "b"
    assert "c" in by_id["b"].children_ids
    assert by_id["b"].argument_type == ArgumentType.SUB_CLAIM
    assert "c" not in by_id["a"].children_ids


def test_confirm_edit_sequence_invalid_step_rejects_wholesale():
    """序列中某步非法 → 整个决策丢弃，调用方原树不变。"""

    from agents.hitl1 import ReparentOp, SetTypeOp

    argument_tree = _abc_tree()
    snapshot = [n.model_dump() for n in argument_tree]
    # 第一步合法（set b 类型），第二步非法（reparent a 到后代 c 成环）
    with pytest.raises(TreeInvariantError):
        confirm(
            argument_tree,
            _gate(
                Hitl1Decision(
                    action=Hitl1Action.EDIT,
                    ops=[
                        SetTypeOp(argument_id="b", new_type=ArgumentType.SUB_CLAIM),
                        ReparentOp(argument_id="a", new_parent_id="c"),
                    ],
                )
            ),
        )
    assert [n.model_dump() for n in argument_tree] == snapshot


# --------------------------------------------------------------------------- #
# slice 6（重构·ADR-0018）：confirm_partition — partition 确认闸门 + 有界打回
#
# hitl1 重定义为 partition 确认闸门：人确认段落切分是否合理；确认继续（skip/accept/edit）
# → 下游；打回重跑（replay）→ 重跑 parse+partition（按 user prompt，当前伪代码桩）。
# 打回有界（max retries 默认 3）；超限向前推进 + 贴 partition_retry_exhausted（受控分支、
# 非异常降级）。既有 skip/accept/edit 与新 REPLAY 收编为「确认继续 / 打回重跑」两类语义。
# --------------------------------------------------------------------------- #


def test_confirm_partition_skip_continues_route_unchanged_tree():
    """SKIP 决策 → route=CONTINUE、树原样深拷贝、计数不变。"""

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree, retry_count=0, gate=_gate(Hitl1Decision(action=Hitl1Action.SKIP))
    )
    assert out.route is Hitl1Route.CONTINUE
    assert out.retry_count == 0
    assert out.exhausted is False
    assert [n.model_dump() for n in out.argument_tree] == [n.model_dump() for n in argument_tree]


def test_confirm_partition_replay_under_budget_routes_replay_and_increments():
    """REPLAY 决策、预算内 → route=REPLAY、计数 +1、树原样深拷贝（partition 重切为桩）。"""

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree, retry_count=0, gate=_gate(Hitl1Decision(action=Hitl1Action.REPLAY))
    )
    assert out.route is Hitl1Route.REPLAY
    assert out.retry_count == 1
    assert out.exhausted is False
    assert [n.model_dump() for n in out.argument_tree] == [n.model_dump() for n in argument_tree]


def test_confirm_partition_replay_at_limit_exhausts_and_continues():
    """REPLAY 决策、已达 max_retries → 受控向前 + exhausted=True、计数不递增、贴标签由 build 落。"""

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree,
        retry_count=3,
        gate=_gate(Hitl1Decision(action=Hitl1Action.REPLAY)),
        max_retries=3,
    )
    assert out.route is Hitl1Route.CONTINUE  # 超限向前推进
    assert out.retry_count == 3  # 不递增
    assert out.exhausted is True
    assert [n.model_dump() for n in out.argument_tree] == [n.model_dump() for n in argument_tree]


def test_confirm_partition_replay_just_under_limit_still_replays():
    """retry_count = max_retries - 1 仍可再打回一次（边界：>= 才耗尽）。"""

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree,
        retry_count=2,
        gate=_gate(Hitl1Decision(action=Hitl1Action.REPLAY)),
        max_retries=3,
    )
    assert out.route is Hitl1Route.REPLAY
    assert out.retry_count == 3
    assert out.exhausted is False


def test_confirm_partition_edit_applied_and_continues():
    """EDIT 决策 → 结构编辑应用、route=CONTINUE、计数不变。"""

    from agents.hitl1 import ReparentOp

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree,
        retry_count=1,
        gate=_gate(
            Hitl1Decision(
                action=Hitl1Action.EDIT,
                ops=[ReparentOp(argument_id="c", new_parent_id="b")],
            )
        ),
    )
    by_id = {n.argument_id: n for n in out.argument_tree}
    assert out.route is Hitl1Route.CONTINUE
    assert out.retry_count == 1  # 确认继续不递增打回计数
    assert by_id["c"].parent_id == "b"  # 结构编辑已应用
    assert "c" in by_id["b"].children_ids


def test_confirm_partition_validates_input_tree_before_review():
    """输入树非法 → 抛 TreeInvariantError，且 gate.review 从未被调用（与 confirm 同防御）。"""

    bad_tree = [_argument("a", parent_id="ghost")]
    reviewed = []

    class _Spy:
        def review(self, argument_tree):
            reviewed.append(True)
            return Hitl1Decision(action=Hitl1Action.SKIP)

    with pytest.raises(TreeInvariantError):
        confirm_partition(bad_tree, retry_count=0, gate=_Spy())  # type: ignore[arg-type]
    assert reviewed == []


def test_confirm_partition_replay_max_retries_configurable_default_three():
    """max_retries 默认 3：retry_count=3 + REPLAY → 耗尽（验证默认值与可配置）。"""

    argument_tree = _abc_tree()
    out = confirm_partition(
        argument_tree,
        retry_count=3,
        gate=_gate(Hitl1Decision(action=Hitl1Action.REPLAY)),
    )
    assert out.exhausted is True
    assert out.route is Hitl1Route.CONTINUE


def test_confirm_partition_replay_once_then_continue_simulates_loop():
    """打回一次后继续（loop 模拟）：第 1 次 REPLAY(retry 0→1)，第 2 次 SKIP(retry 1, continue)。

    单元级模拟图层级循环：调用方据 outcome.route 决定是否重跑上游、据 outcome.retry_count
    续传计数器。confirm_partition 自身无状态，loop 由图条件边驱动（见 test_orchestrator_fallback）。
    """

    decisions = [
        Hitl1Decision(action=Hitl1Action.REPLAY),
        Hitl1Decision(action=Hitl1Action.SKIP),
    ]

    class _SequentialGate:
        def review(self, argument_tree):
            return decisions.pop(0)

    gate: Hitl1Gate = _SequentialGate()  # type: ignore[assignment]
    argument_tree = _abc_tree()

    out1 = confirm_partition(argument_tree, retry_count=0, gate=gate)
    assert out1.route is Hitl1Route.REPLAY
    assert out1.retry_count == 1
    # 图层级重跑上游后，第二次 hitl1：计数续传 out1.retry_count。
    out2 = confirm_partition(argument_tree, retry_count=out1.retry_count, gate=gate)
    assert out2.route is Hitl1Route.CONTINUE
    assert out2.retry_count == 1  # 确认继续不递增打回计数
    assert out2.exhausted is False


# --------------------------------------------------------------------------- #
# slice 7（T-01·ADR-0022 prefactor）：拆分 gate seam — formulate_question + parse_reply
#
# 把单一 review() 拆为两段语义，为 T-03 的 InterruptDrivenGate（interrupt 暂停、resume 喂回）
# 与 TerminalGate（CLI 同步阻塞）提供共同契约。interrupt payload = formulate_question 产出；
# resume value = parse_reply 输入（ADR-0022）。一期 parse_reply 产 action-only
# Hitl1Decision（空 ops），结构化 ops 编辑推后（PRD §7.2 注）。业务纯函数 confirm /
# confirm_partition 仍只调 gate.review()（同步便捷包装、全保真含 ops），行为等价。
# --------------------------------------------------------------------------- #


def test_formulate_question_returns_tree_snapshot_as_interrupt_payload():
    """formulate_question 产 Hitl1Question：承载当前论证树快照（interrupt payload）。

    快照与原树按值相等、但解耦（不别名），gate 不持有调用方可变引用。
    """

    argument_tree = _abc_tree()
    gate = FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))
    question = gate.formulate_question(argument_tree)
    assert isinstance(question, Hitl1Question)
    assert [n.model_dump() for n in question.argument_tree] == [
        n.model_dump() for n in argument_tree
    ]
    # 快照与原树解耦（不别名）——后续改原树不污染已构造的问题。
    assert question.argument_tree is not argument_tree
    argument_tree.append(_argument("z"))
    assert "z" not in {n.argument_id for n in question.argument_tree}


def test_parse_reply_produces_action_only_decision_with_empty_ops():
    """一期 parse_reply 产 action-only Hitl1Decision（空 ops）；reply 的 text 不影响决策。

    结构化 ops 编辑经 interrupt 路径推后（PRD §7.2 注）——即使 reply.action=EDIT，
    parse_reply 亦只产 action、ops 恒空。
    """

    gate = FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))
    decision = gate.parse_reply(Hitl1Reply(action=Hitl1Action.EDIT, text="自由文本"))
    assert isinstance(decision, Hitl1Decision)
    assert decision.action is Hitl1Action.EDIT
    assert decision.ops == []  # action-only：ops 恒空


@pytest.mark.parametrize(
    "action",
    [Hitl1Action.SKIP, Hitl1Action.ACCEPT, Hitl1Action.EDIT, Hitl1Action.REPLAY],
)
def test_parse_reply_action_round_trips_for_all_actions(action: Hitl1Action) -> None:
    """parse_reply 对四类 action 均原样落到 Hitl1Decision.action（action-only）。"""

    gate = FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP))
    assert gate.parse_reply(Hitl1Reply(action=action)).action is action


def test_seam_does_not_alter_sync_review_full_fidelity():
    """拆分后 review() 仍为同步便捷包装、全保真（含 ops），行为与现状等价。

    FakeHitl1Gate.review 返回构造时注入的完整决策（含 ops），不被 action-only parse_reply
    旁路污染——纯函数 confirm / confirm_partition 仍走 review()。
    """

    from agents.hitl1 import ReparentOp

    decision = Hitl1Decision(
        action=Hitl1Action.EDIT,
        ops=[ReparentOp(argument_id="c", new_parent_id="b")],
    )
    gate = FakeHitl1Gate(decision)
    reviewed = gate.review(_abc_tree())
    assert reviewed.action is Hitl1Action.EDIT
    assert reviewed.ops == decision.ops  # ops 全保真（不被 action-only 语义削空）

