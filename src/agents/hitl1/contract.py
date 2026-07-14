"""HITL-1 partition 确认闸门契约（PRD §10 节点 1、ADR-0018）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 编辑 op 判别联合 + 闸门 Protocol +
Fake 桩 + partition 闸门产出，``agent.py`` 放 ``confirm`` / ``confirm_partition`` 纯函数。
``Hitl1Gate`` 为注入 seam（真实 ``interrupt`` + ``Command(resume)`` 属后续切片；
``FakeHitl1Gate`` 供离线单测）。

解析输出初始论证树后触发。人确认段落切分是否合理：
- **确认继续**（``SKIP`` / ``ACCEPT`` / ``EDIT``）——既有结构编辑语义收编于此：跳过 / 接受 /
  应用结构编辑序列后向下游推进（编辑改树形不改文本）。
- **打回重跑**（``REPLAY``）——按用户 prompt 重跑 ``parse+partition``（当前伪代码桩，
  ADR-0020）；打回**打破「绝不打回」**（ADR-0018），须**有界**（max retries 默认 3），
  超限向前推进 + 贴 ``partition_retry_exhausted``（受控分支、非异常降级）。

与解析器对 LLM 的防御性兜底**非对称**：解析器遇环即断、越界即兜底（LLM 不可信）；HITL-1
是「人」的意图性编辑，遇非法编辑一律**拒绝**（抛 :class:`tree_invariants.TreeInvariantError`）、
绝不静默修复。整个决策要么全部应用、要么全部丢弃——在深拷贝上工作、每步 ``validate_tree``，
非法步 #N 即终止、调用方原树不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from domain import Argument, ArgumentType

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
    "Hitl1Route",
    "Hitl1Outcome",
    "DEFAULT_MAX_PARTITION_RETRIES",
    "Hitl1Gate",
    "FakeHitl1Gate",
]


class Hitl1Action(StrEnum):
    """HITL-1 会话级决策（ADR-0018：两类语义收编）。

    ``SKIP`` / ``ACCEPT`` / ``EDIT`` = **确认继续**（跳过 / 接受 / 应用结构编辑后向下游）；
    ``REPLAY`` = **打回重跑** ``parse+partition``（有界，超限贴标签向前）。
    """

    SKIP = "skip"  # 跳过 → 确认继续，不改动原文一个字
    ACCEPT = "accept"  # 接受解析树原样 → 确认继续
    EDIT = "edit"  # 应用结构编辑序列 → 确认继续
    REPLAY = "replay"  # 打回重跑 parse+partition（ADR-0018·有界）


# --------------------------------------------------------------------------- #
# 编辑操作（pydantic v2 判别联合，每个 op 只载自身字段）
# --------------------------------------------------------------------------- #


class MergeOp(BaseModel):
    """合并同段若干节点为一个（幸存者保留自身属性，被删节点的子节点改挂幸存者）。

    跨段合并违反 ADR-0001（一节点一段），解析器拒绝。
    """

    action: Literal["merge"] = "merge"
    argument_ids: list[str]


class SplitOp(BaseModel):
    """拆分节点 → 同段叶兄弟（唯一 id，继承类型/段/父，无子）。"""

    action: Literal["split"] = "split"
    argument_id: str


class ReparentOp(BaseModel):
    """调整层级：改 ``parent_id``（``new_parent_id=None`` 即提为根）。"""

    action: Literal["reparent"] = "reparent"
    argument_id: str
    new_parent_id: str | None


class SetTypeOp(BaseModel):
    """改节点类型；权重作为副作用调整（影子→0、影子→核心→50、核心→核心保留）。"""

    action: Literal["set_type"] = "set_type"
    argument_id: str
    new_type: ArgumentType


class MarkNoOpOp(BaseModel):
    """标记段落无需处理：该段所有节点转 ``background`` 影子、权重 0，结构不变。"""

    action: Literal["mark_no_op"] = "mark_no_op"
    paragraph_id: str


class FixBoundaryOp(BaseModel):
    """修正段内边界——延后实现（domain 无 ``text_span``，ADR-0001）。"""

    action: Literal["fix_boundary"] = "fix_boundary"
    argument_id: str


Hitl1Op = Annotated[
    MergeOp | SplitOp | ReparentOp | SetTypeOp | MarkNoOpOp | FixBoundaryOp,
    Field(discriminator="action"),
]


class Hitl1Decision(BaseModel):
    """HITL-1 决策：会话级动作 + （edit 时）有序编辑序列。"""

    action: Hitl1Action
    ops: list[Hitl1Op] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# partition 确认闸门产出（ADR-0018）
# --------------------------------------------------------------------------- #


class Hitl1Route(StrEnum):
    """hitl1 节点的路由裁决：确认继续向下游 / 打回重跑上游。

    ``confirm_partition`` 据闸门决策产出，图层级条件边据 ``hitl1_route`` channel 读之
    路由（ADR-0018 受控打回边 ``hitl1 → parse+partition``）。
    """

    CONTINUE = "continue"  # 确认继续 → 下游（verification ∥ hypothesis）
    REPLAY = "replay"  # 打回重跑 → parse+partition（有界；超限改 CONTINUE + 贴标签）


#: 打回重跑的最大次数（ADR-0018，默认 3）。超限即向前推进 + 贴 ``partition_retry_exhausted``。
DEFAULT_MAX_PARTITION_RETRIES = 3


@dataclass(frozen=True)
class Hitl1Outcome:
    """``confirm_partition`` 的产出：确认后的树 + 路由 + 计数 + 是否打回耗尽。

    - ``argument_tree``：确认后的树（SKIP/ACCEPT/EDIT 时可能改树形、不改文本；REPLAY /
      耗尽时原样深拷贝——partition 重切为伪代码桩，不在本节点改树）。
    - ``route``：图层级路由裁决（CONTINUE / REPLAY）。
    - ``retry_count``：打回计数器（REPLAY 时 +1；耗尽时不 +1、保留达上限值）。
    - ``exhausted``：是否因打回超 ``max_retries`` 被迫向前推进（受控分支、非异常降级）。
    """

    argument_tree: list[Argument]
    route: Hitl1Route
    retry_count: int
    exhausted: bool = False


# --------------------------------------------------------------------------- #
# 闸门 seam + 离线桩
# --------------------------------------------------------------------------- #


class Hitl1Gate(Protocol):
    """HITL-1 闸门 seam：审阅树 → 返回纯数据决策。

    真实实现用 ``interrupt`` 把树交给用户、用 ``Command(resume)`` 收回决策（#11）；
    本 seam 不绑任何前端/中断机制。``confirm`` 保证：闸门看到的是**原始**树，
    而非中间编辑态——多步编辑在闸门一次返回、由 ``confirm`` 顺序应用。
    """

    def review(self, argument_tree: list[Argument]) -> Hitl1Decision: ...


class FakeHitl1Gate:
    """离线闸门桩：固定决策，provider-free、确定（供单测）。"""

    def __init__(self, decision: Hitl1Decision) -> None:
        self._decision = decision

    def review(self, argument_tree: list[Argument]) -> Hitl1Decision:
        return self._decision.model_copy(deep=True)
