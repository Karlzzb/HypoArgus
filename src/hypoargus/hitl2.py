"""HITL-2 修订确认硬闸门（PRD §10 节点 2、issue #9、ADR-0010/0011）。

在合并、影响传导、一致性校验全部完成后、回写（#10）前触发。界面并列呈现被标为
``doubtful``/``error``/``invalid`` 的段落原文、系统贴的 ``issue_tags``（含
``conflict``）、以及候选修订假设。用户逐条采纳或驳回、可手动修改节点内容。

**此节点为不可跳过的硬闸门**，系统绝不在无人拍板时自动采纳假设（ADR-0010）。任务
配置「是否启用人工确认」开关只作用于 HITL-1，对 HITL-2 无效——HITL-2 恒定开启。
仅当全篇所有节点均可信、无任何待决内容时，本节点呈现「无需修订」一键通过（属闸门内
无待办，非跳过闸门）。

用户采纳某条假设时，节点进入 ``adopted`` 状态并**立即持久化** ``adopted_hypothesis_id``
（ADR-0011 采纳链），使回写（#10）失败重试再失败时用户决定不丢失。候选假设数量按
实际生成动态呈现，不固定为某个数字。

与 HITL-1 的非对称：HITL-1 是可跳过的结构确认、遇非法编辑即拒（结构变更）；HITL-2
是不可跳过的内容确认、遇非法状态变更即拒（状态机），绝不替人拍板。整个决策要么全部
应用、要么全部丢弃——在深拷贝上工作，非法步即终止、调用方原树不动。

本切片为同步注入闸门（``Hitl2Gate`` seam，``FakeHitl2Gate`` 供离线单测）；真实
``interrupt`` + ``Command(resume)`` + checkpointer 属后续切片（#11 落地的是状态机拦截
+ 异常兜底 + 回写幂等续跑，未含 HITL 打断/持久化）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from hypoargus.domain import (
    ArgumentationNode,
    HypothesisRelation,
    HypothesisStatus,
    NodeStatus,
    NodeType,
)
from hypoargus.raw_store import RawParagraphStore
from hypoargus.status_machine import (
    IllegalStatusTransitionError,
    validate_transition,
)

__all__ = [
    "Hitl2Action",
    "Hitl2GateError",
    "AdoptOp",
    "RejectOp",
    "EditContentOp",
    "Hitl2Op",
    "Hitl2Decision",
    "CandidateView",
    "NodeReview",
    "Hitl2Review",
    "Hitl2Gate",
    "FakeHitl2Gate",
    "ConservativeHitl2Gate",
    "build_review",
    "confirm",
]


class Hitl2GateError(Exception):
    """HITL-2 闸门非法决策（硬闸门拦截 / 状态机非法变更 / 越权操作）。

    闸门越权或操作非法时抛出——绝不静默修复、绝不替人拍板，整个决策丢弃、
    调用方原树不动（与 HITL-1 对非法编辑的非对称一致）。
    """


class Hitl2Action(StrEnum):
    """HITL-2 会话级决策。

    ``PASS``：闸门内无待办的一键通过（仅当全篇无待决内容时合法，ADR-0010 空过口径）；
    ``DECIDE``：逐条采纳 / 驳回 / 手改，承载有序操作序列。
    """

    PASS = "pass"
    DECIDE = "decide"


# --------------------------------------------------------------------------- #
# 决策操作（pydantic v2 判别联合，每个 op 只载自身字段）
# --------------------------------------------------------------------------- #


class AdoptOp(BaseModel):
    """采纳某条候选假设：节点置 ``adopted``、持久化 ``adopted_hypothesis_id``。

    ``hypothesis_id`` 必须在该节点被合并算子激活的候选集
    （``merge_decision.activated_hypothesis_ids``）内——HITL-2 不替人凭空造药，
    只能从系统已激活的候选中勾选。``edited_text`` 非空时覆盖该假设的呈现文本
    （用户手动修改待采纳内容），落回 ``candidate_hypotheses`` 供回写（#10）幂等重取。
    """

    action: Literal["adopt"] = "adopt"
    node_id: str
    hypothesis_id: str
    edited_text: str | None = None


class RejectOp(BaseModel):
    """驳回某条候选假设：从 ``candidate_hypotheses`` 移除该假设（持久化驳回决策）。

    节点 ``status`` 不变（仍 ``doubtful``/``error``/``invalid``），原文逐字节保留——
    驳回即「人看过、决定不修订」。被驳回的假设不再参与回写。
    """

    action: Literal["reject"] = "reject"
    node_id: str
    hypothesis_id: str


class EditContentOp(BaseModel):
    """手动修改节点内容：直接覆写 ``node.content``。

    仅作用于待决节点（``doubtful``/``error``/``invalid`` 或贴 ``conflict``）；可信非冲突
    节点不呈现、不可手改（守住「保护原文」底线）。本 op 不置 ``adopted``——是否进入
    回写重写通道由 #10 据 ``adopted_hypothesis_id`` 与 ``content`` 共同决定。
    """

    action: Literal["edit_content"] = "edit_content"
    node_id: str
    content: str


Hitl2Op = Annotated[
    AdoptOp | RejectOp | EditContentOp,
    Field(discriminator="action"),
]


class Hitl2Decision(BaseModel):
    """HITL-2 决策：会话级动作 + （decide 时）有序操作序列。"""

    action: Hitl2Action
    ops: list[Hitl2Op] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 呈现视图（build_review 产出，喂给 gate.seam 与未来前端）
# --------------------------------------------------------------------------- #


class CandidateView(BaseModel):
    """候选假设的呈现视图（Hypothesis 的只读投影）。"""

    hypothesis_id: str
    text: str
    relation: HypothesisRelation
    status: HypothesisStatus
    confidence: float


class NodeReview(BaseModel):
    """单节点的修订确认呈现：段落原文 + 批注 + 候选 + 激活集。

    ``original_text`` 按 ``paragraph_id`` 从只读原文表取该段原文（ADR-0005 HITL-2
    对比左栏的数据源），**不整篇加载原文**。``activated_hypothesis_ids`` 为合并算子
    激活、可被采纳的假设集；``candidates`` 含弱呈现（doubtful）假设供参考。
    """

    node_id: str
    paragraph_id: str
    original_text: str
    node_type: NodeType
    status: NodeStatus
    issue_tags: list[str]
    activated_hypothesis_ids: list[str]
    candidates: list[CandidateView]


class Hitl2Review(BaseModel):
    """HITL-2 闸门看到的呈现：待决节点列表 + 是否有待决内容。"""

    nodes: list[NodeReview]
    has_pending: bool


# --------------------------------------------------------------------------- #
# 闸门 seam + 桩
# --------------------------------------------------------------------------- #


class Hitl2Gate(Protocol):
    """HITL-2 闸门 seam：审阅呈现 → 返回纯数据决策。

    真实实现用 ``interrupt`` 把呈现交给用户、用 ``Command(resume)`` 收回决策（后续切片）；
    本 seam 不绑任何前端 / 中断机制。``confirm`` 保证：闸门看到的是 ``build_review``
    产出的**呈现**，决策的合法性由 ``confirm`` 校验——闸门不可越权（如对无待决内容
    返回 PASS 之外的动作、或采纳未激活的假设）。
    """

    def review(self, review: Hitl2Review) -> Hitl2Decision: ...


class FakeHitl2Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。"""

    def __init__(self, decision: Hitl2Decision) -> None:
        self._decision = decision

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        return self._decision.model_copy(deep=True)


class ConservativeHitl2Gate:
    """保守默认闸门：无待决时一键通过，否则 DECIDE 且不采纳任何假设。

    作为 :func:`create_real_agents` 未注入闸门时的默认——守住「绝不自动采纳」底线：
    无待决内容 → ``PASS``（闸门内无待办的一键通过，ADR-0010 空过口径）；有待决内容 →
    ``DECIDE`` + 空 ops（人看过、全驳回、原文保留）。这是一次性同步桩；真实人判
    ``interrupt`` 属后续切片。本默认使既有 #4–#8 端到端集成测试（无人采纳）仍逐字节等于原文。
    """

    def review(self, review: Hitl2Review) -> Hitl2Decision:
        if not review.has_pending:
            return Hitl2Decision(action=Hitl2Action.PASS)
        return Hitl2Decision(action=Hitl2Action.DECIDE, ops=[])


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数，可独立单测
# --------------------------------------------------------------------------- #

_PENDING_STATUSES: frozenset[NodeStatus] = frozenset(
    {NodeStatus.DOUBTFUL, NodeStatus.ERROR, NodeStatus.INVALID}
)
"""体检 / 影响传导已下待决终判的状态（矩阵原文侧 + 影响传导上层判决）。"""

_CONFLICT_TAG = "conflict"


def _activated_ids(node: ArgumentationNode) -> list[str]:
    if node.merge_decision is None:
        return []
    return list(node.merge_decision.activated_hypothesis_ids)


def _is_pending(node: ArgumentationNode) -> bool:
    """节点是否需 HITL-2 人判：状态待决、或贴 conflict、或有被激活的候选。

    合并矩阵保证：非 conflict 的 ``credible`` 节点激活集为空、状态非待决 → 不呈现
    （以静制动，原文不动）。``doubtful``/``error``/``invalid`` 节点一律呈现；
    conflict 节点虽 ``credible`` 但需人判对立假设 → 呈现。
    """

    if node.status in _PENDING_STATUSES:
        return True
    if _CONFLICT_TAG in node.issue_tags:
        return True
    return bool(_activated_ids(node))


def build_review(
    tree: list[ArgumentationNode], store: RawParagraphStore
) -> Hitl2Review:
    """构建修订确认呈现：待决节点的段落原文 + 批注 + 候选 + 激活集。

    纯函数子缝（PRD «Testing Decisions» 呈现缝）：``标注后的树 + 只读原文表 → 呈现``。
    只呈现待决节点（:func:`_is_pending`）；``original_text`` 按 ``paragraph_id`` 从
    只读表取该段原文（不整篇加载）。``has_pending`` = 是否存在任何待决节点，驱动
    硬闸门的「一键通过」与「禁自动采纳」分支。
    """

    nodes: list[NodeReview] = []
    for node in tree:
        if not _is_pending(node):
            continue
        original = store.get(node.paragraph_id).decode("utf-8", errors="surrogateescape")
        nodes.append(
            NodeReview(
                node_id=node.node_id,
                paragraph_id=node.paragraph_id,
                original_text=original,
                node_type=node.node_type,
                status=node.status,
                issue_tags=list(node.issue_tags),
                activated_hypothesis_ids=_activated_ids(node),
                candidates=[
                    CandidateView(
                        hypothesis_id=h.hypothesis_id,
                        text=h.text,
                        relation=h.relation,
                        status=h.status,
                        confidence=h.confidence,
                    )
                    for h in node.candidate_hypotheses
                ],
            )
        )
    return Hitl2Review(nodes=nodes, has_pending=bool(nodes))


def confirm(
    tree: list[ArgumentationNode],
    store: RawParagraphStore,
    gate: Hitl2Gate,
) -> list[ArgumentationNode]:
    """应用 HITL-2 决策，返回确认后的树。

    流程：
    1. ``build_review(tree, store)``——构建呈现。
    2. ``gate.review(review)``——闸门返回决策。
    3. 硬闸门校验（ADR-0010）：``PASS`` 仅当无待决内容；有待决时绝不可 ``PASS``
       （绝不无人拍板自动采纳）。
    4. ``DECIDE``：在深拷贝上**逐步**应用 ops，每步校验合法性（采纳必须命中已激活
       候选、手改与驳回只作用于待决节点、状态机非法变更一律拦截）；非法步即抛 →
       调用方原树不动。
    """

    review = build_review(tree, store)
    decision = gate.review(review)

    # 硬闸门（ADR-0010）：PASS 仅当闸门内无待办；有待决内容时绝不可一键通过。
    if decision.action is Hitl2Action.PASS:
        if review.has_pending:
            raise Hitl2GateError(
                "硬闸门拦截：有待决内容时不可 PASS（绝不在无人拍板时自动采纳）"
            )
        return [n.model_copy(deep=True) for n in tree]

    # DECIDE：深拷贝上逐步应用 + 每步校验。
    working = [n.model_copy(deep=True) for n in tree]
    for op in decision.ops:
        _apply_op(working, op)
    return working


def _apply_op(nodes: list[ArgumentationNode], op: Hitl2Op) -> None:
    """应用单个 HITL-2 操作；非法即抛 :class:`Hitl2GateError`。"""

    if isinstance(op, AdoptOp):
        _apply_adopt(nodes, op)
    elif isinstance(op, RejectOp):
        _apply_reject(nodes, op)
    elif isinstance(op, EditContentOp):
        _apply_edit_content(nodes, op)
    else:  # pragma: no cover - 判别联合已穷尽
        raise AssertionError(f"未处理的 Hitl2Op：{op!r}")


def _require_node(nodes: list[ArgumentationNode], node_id: str) -> ArgumentationNode:
    """按 id 取节点；不存在则抛 :class:`Hitl2GateError`。"""

    for node in nodes:
        if node.node_id == node_id:
            return node
    raise Hitl2GateError(f"HITL-2 操作引用不存在的节点：{node_id}")


def _apply_adopt(nodes: list[ArgumentationNode], op: AdoptOp) -> None:
    """采纳假设：校验激活集 + 状态机，置 ``adopted`` + 持久化 ``adopted_hypothesis_id``。

    校验（任一失败即抛 :class:`Hitl2GateError`、整个决策丢弃）：
    1. 节点存在；
    2. 节点为待决态（``doubtful``/``error``/``invalid`` 或贴 ``conflict``）——可信非冲突
       节点不呈现、不可采纳（保护原文底线）；
    3. ``hypothesis_id`` 在节点 ``merge_decision.activated_hypothesis_ids`` 内——HITL-2
       不凭空造药，只从系统已激活的候选中勾选；
    4. 状态机迁移合法（``adopted``/``corrected`` 不可重复采纳，ADR-0011）——由集中状态机
       子缝 :func:`validate_transition` 统一拦截，杜绝规则漂移。

    成功则置 ``status = adopted``、``adopted_hypothesis_id = op.hypothesis_id``；
    ``edited_text`` 非空时覆写该假设文本（落回 ``candidate_hypotheses``，供回写 #10 幂等重取）。
    """

    node = _require_node(nodes, op.node_id)
    if not _is_pending(node):
        raise Hitl2GateError(
            f"采纳非法：节点 {op.node_id} 非待决态（{node.status.value}），不可采纳"
        )
    try:
        validate_transition(node.status, NodeStatus.ADOPTED)
    except IllegalStatusTransitionError as exc:
        raise Hitl2GateError(
            f"状态机非法变更：节点 {op.node_id} 已为 {node.status.value}，不可重复采纳"
        ) from exc
    activated = _activated_ids(node)
    if op.hypothesis_id not in activated:
        raise Hitl2GateError(
            f"越权采纳：假设 {op.hypothesis_id} 不在节点 {op.node_id} 的激活候选集 "
            f"{activated} 内"
        )
    if op.edited_text is not None:
        for h in node.candidate_hypotheses:
            if h.hypothesis_id == op.hypothesis_id:
                h.text = op.edited_text
                break
    node.status = NodeStatus.ADOPTED
    node.adopted_hypothesis_id = op.hypothesis_id


def _apply_reject(nodes: list[ArgumentationNode], op: RejectOp) -> None:
    """驳回假设：从 ``candidate_hypotheses`` 移除该假设（持久化驳回决策）。

    节点 ``status`` 不变（仍待决、原文逐字节保留）；被驳回假设不再参与回写。
    ``hypothesis_id`` 必须在节点当前 ``candidate_hypotheses`` 内，否则抛
    :class:`Hitl2GateError`（越权 / 不存在）。
    """

    node = _require_node(nodes, op.node_id)
    idx = next(
        (i for i, h in enumerate(node.candidate_hypotheses) if h.hypothesis_id == op.hypothesis_id),
        None,
    )
    if idx is None:
        raise Hitl2GateError(
            f"驳回越权：假设 {op.hypothesis_id} 不在节点 {op.node_id} 的候选集内"
        )
    node.candidate_hypotheses.pop(idx)


def _apply_edit_content(nodes: list[ArgumentationNode], op: EditContentOp) -> None:
    """手动修改节点内容：覆写 ``content``（仅待决节点）。

    仅作用于待决节点（``doubtful``/``error``/``invalid`` 或贴 ``conflict``）；可信非冲突
    节点不呈现、不可手改（守住「保护原文」底线）。不置 ``adopted``——是否进入回写重写
    通道由 #10 据 ``adopted_hypothesis_id`` 与 ``content`` 共同决定。
    """

    node = _require_node(nodes, op.node_id)
    if not _is_pending(node):
        raise Hitl2GateError(
            f"手改非法：节点 {op.node_id} 非待决态（{node.status.value}），不可手改内容"
        )
    node.content = op.content
