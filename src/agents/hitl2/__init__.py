"""HITL-2 终稿文本确认硬闸门（PRD §13、ADR-0010/0017、Slice 6）。

ADR-0014 子包拆分：``contract.py`` 放会话级决策 + 段落级操作 op 判别联合 + 呈现视图 +
闸门 Protocol + Fake/默认闸门实现 + ``Hitl2GateError``，``agent.py`` 放
``build_review`` / ``resolve_rewrites`` / ``assemble_final_document`` / ``confirm`` 纯函数。
本 ``__init__`` re-export 两者的公开符号，保持
``from agents.hitl2 import confirm, build_review, assemble_final_document, Hitl2Gate, ...``
的外部 import 路径不变（拆分硬约束）。

Slice 6（ADR-0017）重定位 hitl2 为终稿文本确认闸门：在 rewrite_loop 逐段提议重写之后、
``final_document`` 落地之前触发，逐段确认 / 编辑 / 驳回 ``proposed_rewrites``，拼装
``final_document``（确认→提议文本、编辑→编辑文本、驳回 / 未触达→逐字节原文）。
"""

from agents.hitl2.agent import (
    Hitl2Confirmation,
    assemble_final_document,
    build_review,
    confirm,
    resolve_rewrites,
)
from agents.hitl2.contract import (
    ConfirmRewriteOp,
    ConservativeHitl2Gate,
    EditRewriteOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Op,
    Hitl2Review,
    ParagraphRewriteReview,
    RejectRewriteOp,
)

__all__ = [
    "Hitl2Action",
    "Hitl2GateError",
    "ConfirmRewriteOp",
    "EditRewriteOp",
    "RejectRewriteOp",
    "Hitl2Op",
    "Hitl2Decision",
    "ParagraphRewriteReview",
    "Hitl2Review",
    "Hitl2Gate",
    "FakeHitl2Gate",
    "ConservativeHitl2Gate",
    "Hitl2Confirmation",
    "build_review",
    "resolve_rewrites",
    "assemble_final_document",
    "confirm",
]
