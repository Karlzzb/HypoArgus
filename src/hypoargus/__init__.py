"""HypoArgus — 论证驱动型文档修订多智能体系统。

本包提供全局调度中枢与端到端骨架：纯文本 → 确定性段落切分 → 只读原文段落表 →
论证树（桩）→ 双线路（桩）→ 合并/影响/一致性（桩）→ HITL（桩）→ 逐字节回写 → 终稿。

核心承诺：无任何采纳改动时，终稿与原始输入逐字节完全一致。
"""

from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.orchestrator import Orchestrator
from hypoargus.partition import partition
from hypoargus.raw_store import RawParagraphStore
from hypoargus.writeback import writeback

__all__ = [
    "ArgumentationNode",
    "NodeStatus",
    "NodeType",
    "Orchestrator",
    "RawParagraphStore",
    "partition",
    "writeback",
]
