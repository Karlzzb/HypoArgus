"""HITL-1 结构确认闸门契约（PRD §10 节点 1、issue #2）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 编辑 op 判别联合 + 闸门 Protocol +
Fake 桩，``agent.py`` 放 ``confirm`` 纯函数。``Hitl1Gate`` 为注入 seam（真实
``interrupt`` + ``Command(resume)`` 属 #11；``FakeHitl1Gate`` 供离线单测）。

解析输出初始论证树后、双线路启动前触发。用户可调层级、合并或拆分节点、修正边界、
标记无需处理的段落；**支持跳过**（跳过则直接进入下一环节，不改动原文一个字）。

与解析器对 LLM 的防御性兜底**非对称**：解析器遇环即断、越界即兜底（LLM 不可信）；HITL-1
是「人」的意图性编辑，遇非法编辑一律**拒绝**（抛 :class:`tree_invariants.TreeInvariantError`）、
绝不静默修复。整个决策要么全部应用、要么全部丢弃——在深拷贝上工作、每步 ``validate_tree``，
非法步 #N 即终止、调用方原树不动。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from domain import ArgumentationNode, NodeType

__all__ = [
    "Hitl1Action",
    "MergeOp",
    "SplitOp",
    "ReparentOp",
    "SetTypeOp",
    "MarkNoOpOp",
    "FixBoundaryOp",
    "Hitl1Op",
    "Hitl1Decision",
    "Hitl1Gate",
    "FakeHitl1Gate",
]


class Hitl1Action(StrEnum):
    """HITL-1 会话级决策。"""

    SKIP = "skip"  # 跳过结构确认 → 不改动原文一个字
    ACCEPT = "accept"  # 接受解析树原样
    EDIT = "edit"  # 应用结构编辑序列


# --------------------------------------------------------------------------- #
# 编辑操作（pydantic v2 判别联合，每个 op 只载自身字段）
# --------------------------------------------------------------------------- #


class MergeOp(BaseModel):
    """合并同段若干节点为一个（幸存者保留自身属性，被删节点的子节点改挂幸存者）。

    跨段合并违反 ADR-0001（一节点一段），解析器拒绝。
    """

    action: Literal["merge"] = "merge"
    node_ids: list[str]


class SplitOp(BaseModel):
    """拆分节点 → 同段叶兄弟（唯一 id，继承类型/段/父，无子）。"""

    action: Literal["split"] = "split"
    node_id: str


class ReparentOp(BaseModel):
    """调整层级：改 ``parent_id``（``new_parent_id=None`` 即提为根）。"""

    action: Literal["reparent"] = "reparent"
    node_id: str
    new_parent_id: str | None


class SetTypeOp(BaseModel):
    """改节点类型；权重作为副作用调整（影子→0、影子→核心→50、核心→核心保留）。"""

    action: Literal["set_type"] = "set_type"
    node_id: str
    new_type: NodeType


class MarkNoOpOp(BaseModel):
    """标记段落无需处理：该段所有节点转 ``background`` 影子、权重 0，结构不变。"""

    action: Literal["mark_no_op"] = "mark_no_op"
    paragraph_id: str


class FixBoundaryOp(BaseModel):
    """修正段内边界——延后实现（domain 无 ``text_span``，ADR-0001）。"""

    action: Literal["fix_boundary"] = "fix_boundary"
    node_id: str


Hitl1Op = Annotated[
    MergeOp | SplitOp | ReparentOp | SetTypeOp | MarkNoOpOp | FixBoundaryOp,
    Field(discriminator="action"),
]


class Hitl1Decision(BaseModel):
    """HITL-1 决策：会话级动作 + （edit 时）有序编辑序列。"""

    action: Hitl1Action
    ops: list[Hitl1Op] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 闸门 seam + 离线桩
# --------------------------------------------------------------------------- #


class Hitl1Gate(Protocol):
    """HITL-1 闸门 seam：审阅树 → 返回纯数据决策。

    真实实现用 ``interrupt`` 把树交给用户、用 ``Command(resume)`` 收回决策（#11）；
    本 seam 不绑任何前端/中断机制。``confirm`` 保证：闸门看到的是**原始**树，
    而非中间编辑态——多步编辑在闸门一次返回、由 ``confirm`` 顺序应用。
    """

    def review(self, tree: list[ArgumentationNode]) -> Hitl1Decision: ...


class FakeHitl1Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。"""

    def __init__(self, decision: Hitl1Decision) -> None:
        self._decision = decision

    def review(self, tree: list[ArgumentationNode]) -> Hitl1Decision:
        return self._decision.model_copy(deep=True)
