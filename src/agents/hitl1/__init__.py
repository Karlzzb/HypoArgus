"""HITL-1 结构确认闸门（PRD §10 节点 1、issue #2）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 编辑 op 判别联合 + 闸门 Protocol +
Fake 桩，``agent.py`` 放 ``confirm`` 纯函数。本 ``__init__`` re-export 两者的公开符号，
保持 ``from agents.hitl1 import confirm, Hitl1Gate, FakeHitl1Gate, MergeOp, ...`` 的
外部 import 路径不变（拆分硬约束）。
"""

from agents.hitl1.agent import confirm, confirm_partition
from agents.hitl1.contract import (
    DEFAULT_MAX_PARTITION_RETRIES,
    FakeHitl1Gate,
    FixBoundaryOp,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Gate,
    Hitl1Op,
    Hitl1Outcome,
    Hitl1Route,
    MarkNoOpOp,
    MergeOp,
    ReparentOp,
    SetTypeOp,
    SplitOp,
)

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
    "confirm",
    "confirm_partition",
]
