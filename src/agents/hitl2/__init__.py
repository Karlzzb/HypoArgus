"""HITL-2 修订确认硬闸门（PRD §10 节点 2、issue #9、ADR-0010/0011）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 操作 op 判别联合 + 呈现视图 +
闸门 Protocol + Fake/默认闸门实现 + ``Hitl2GateError``，``agent.py`` 放
``build_review`` 与 ``confirm`` 纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.hitl2 import confirm, build_review, Hitl2Gate, ConservativeHitl2Gate, ...``
的外部 import 路径不变（拆分硬约束）。
"""

from agents.hitl2.agent import build_review, confirm
from agents.hitl2.contract import (
    AdoptOp,
    ArgumentReview,
    CandidateView,
    ConservativeHitl2Gate,
    EditContentOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Op,
    Hitl2Review,
    RejectOp,
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
    "ArgumentReview",
    "Hitl2Review",
    "Hitl2Gate",
    "FakeHitl2Gate",
    "ConservativeHitl2Gate",
    "build_review",
    "confirm",
]
