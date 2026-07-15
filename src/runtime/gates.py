"""中断驱动 HITL 闸门（T-03·ADR-0022）。

T-01 把 gate seam 拆为 ``formulate_question`` + ``parse_reply``：同步 gate（Fake / Cli /
Conservative）覆写 ``review()`` 做全保真同步决策；本模块落地**异步**驱动闸门
``InterruptHitl1Gate`` / ``InterruptHitl2Gate``——``review()`` 组合两段 seam：
``parse_reply(interrupt(formulate_question(view)))``。

业务纯函数 :func:`agents.hitl1.confirm_partition` / :func:`agents.hitl2.confirm` 仍只调
``gate.review()``（零改动）。中断路径下，``review()`` 经 ``interrupt()`` 把
``formulate_question`` 产出的 ``Hitl*Question``（interrupt payload）落 checkpoint 暂停；
驱动者（CLI resume 循环 / T-04 HTTP）从 ``aget_state().tasks[].interrupts[].value`` 取回该
载荷、渲染、收输入、构造 ``Hitl*Reply``、以 ``Command(resume=reply)`` 续跑。resume 时
``interrupt()`` 返回该回复、喂 ``parse_reply`` 产 **action-only** ``Hitl*Decision``（空 ops，
PRD §7.2 注 / ADR-0022 一期 human_response 仅 action + 自由文本）。

中断路径下终稿对触达段恒为原文：一期 ``parse_reply`` 产 action-only 决策（hitl2 的
``DECIDE`` 空 ops = 全驳回），故无人拍板 → 终稿逐字节还原（ADR-0010）。结构化逐段 ops
编辑推后。

``interrupt()`` 仅在图节点执行上下文（contextvar）内有效——``review()`` 由节点经
``confirm_partition`` / ``confirm`` 同步调用、处于节点执行栈内，故 ``interrupt()`` 生效。
``review()`` 在节点外直接调用（单测）会因无执行上下文而抛——故本模块单测只验
``formulate_question`` / ``parse_reply``（纯数据），``review()`` 组合由集成测试覆盖。
"""

from __future__ import annotations

from langgraph.types import interrupt

from agents.hitl1 import (
    Hitl1Decision,
    Hitl1Question,
    Hitl1Reply,
)
from agents.hitl2 import (
    Hitl2Decision,
    Hitl2Question,
    Hitl2Reply,
    Hitl2Review,
)
from domain import Argument, ParagraphRecord

__all__ = ["InterruptHitl1Gate", "InterruptHitl2Gate"]


class InterruptHitl1Gate:
    """hitl1 的中断驱动闸门：``review()`` = ``parse_reply(interrupt(formulate_question))``。

    ``formulate_question`` 产 ``Hitl1Question``（当前论证树快照、interrupt payload）；
    ``parse_reply`` 产 action-only ``Hitl1Decision``（空 ops）。两段与 ``FakeHitl1Gate``
    同形（snapshot / action-only）；唯 ``review()`` 组合二者并插入 ``interrupt()``——
    纯函数 :func:`confirm_partition` 不改、仍调 ``gate.review()``。
    """

    def formulate_question(
        self, argument_tree: list[Argument], *, paragraph_list: list[ParagraphRecord]
    ) -> Hitl1Question:
        return Hitl1Question(
            argument_tree=[n.model_copy(deep=True) for n in argument_tree],
            paragraph_list=[r.model_copy(deep=True) for r in paragraph_list],
        )

    def parse_reply(self, reply: Hitl1Reply) -> Hitl1Decision:
        # 一期 action-only：reply 的 text 不影响决策，ops 恒空（结构化 ops 推后）。
        return Hitl1Decision(action=reply.action)

    def review(
        self, argument_tree: list[Argument], *, paragraph_list: list[ParagraphRecord]
    ) -> Hitl1Decision:
        """组合拆分 seam：``formulate_question → interrupt → parse_reply``。

        首次执行：``interrupt`` 暂停、载荷 ``Hitl1Question``（含 ``paragraph_list`` 快照）
        落 checkpoint、本方法抛 ``GraphInterrupt``（由图捕获为暂停）。resume：``interrupt``
        返回驱动者下发的 ``Hitl1Reply``、``parse_reply`` 产 action-only 决策、交
        :func:`confirm_partition` 应用。
        """

        question = self.formulate_question(argument_tree, paragraph_list=paragraph_list)
        reply: Hitl1Reply = interrupt(question)
        return self.parse_reply(reply)


class InterruptHitl2Gate:
    """hitl2 的中断驱动闸门：``review()`` = ``parse_reply(interrupt(formulate_question))``。

    ``formulate_question`` 产 ``Hitl2Question``（包裹 ``Hitl2Review`` 呈现：被触达段原文 ×
    提议重写）；``parse_reply`` 产 action-only ``Hitl2Decision``（空 ops，``DECIDE`` 即全驳回）。
    纯函数 :func:`confirm` 不改、仍调 ``gate.review()``——硬闸门校验（``PASS`` 仅当无待决）
    仍由 :func:`confirm` 兜底。
    """

    def formulate_question(self, review: Hitl2Review) -> Hitl2Question:
        return Hitl2Question(review=review)

    def parse_reply(self, reply: Hitl2Reply) -> Hitl2Decision:
        # 一期 action-only：与 review 的「无待决→PASS / 有待决→DECIDE+空 ops」决策面一致，
        # 但 parse_reply 只据 reply.action 落 action-only（has_pending 上下文已在 review 侧消化）。
        return Hitl2Decision(action=reply.action)

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        """组合拆分 seam：``formulate_question → interrupt → parse_reply``。"""

        question = self.formulate_question(review)
        reply: Hitl2Reply = interrupt(question)
        return self.parse_reply(reply)
