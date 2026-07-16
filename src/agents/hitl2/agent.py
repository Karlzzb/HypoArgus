"""HITL-2 ``build_review`` / ``resolve_rewrites`` / ``assemble_final_document`` /
``confirm`` 纯函数（PRD §13、ADR-0010/0017）。

``build_review`` 构建终稿确认呈现（被触达段的原文 × 提议重写文本），``resolve_rewrites``
应用段级三态决策（确认 / 编辑 / 驳回），``assemble_final_document`` 按规范顺序拼接
``final_document``，``confirm`` 串联三者 + 硬闸门校验。纯函数子缝（PRD «Testing Decisions»
呈现缝 / 决策缝 / 拼接缝），可独立单测。

``build_review`` 只呈现 ``proposed_rewrites`` 中的段（rewrite_loop 已判定为触达）；
``original_text`` 按 ``paragraph_id`` 从只读表取该段原文（不整篇加载）。
``has_pending`` = 是否存在任何提议重写，驱动硬闸门的「一键通过」与「禁自动采纳」分支。

``confirm`` 流程：
1. ``build_review(original_paragraphs, proposed_rewrites)``——构建呈现。
2. ``gate.review(review)``——闸门返回决策。
3. 硬闸门校验（ADR-0010）：``PASS`` 仅当无待决内容；有提议重写时绝不可 ``PASS``
   （绝不无人拍板自动采纳）。
4. ``DECIDE``：``resolve_rewrites`` 在新 dict 上应用 ops（确认→提议文本、编辑→编辑文本、
   驳回→省略），每步校验合法性（op 引用必须在 ``proposed_rewrites`` 内）；非法步即抛 →
   调用方 ``proposed_rewrites`` 不动。
5. ``assemble_final_document`` 拼接 ``final_document``（按 ``original_paragraphs`` 规范
   顺序：resolved 段用确认 / 编辑文本、其余段逐字节原文）。
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.hitl2.contract import (
    ConfirmRewriteOp,
    EditRewriteOp,
    Hitl2Action,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Op,
    Hitl2Review,
    ParagraphRewriteReview,
    RejectRewriteOp,
)
from original_paragraphs import OriginalParagraphs

__all__ = [
    "Hitl2Confirmation",
    "build_review",
    "resolve_rewrites",
    "assemble_final_document",
    "confirm",
]


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="surrogateescape")


def _encode(s: str) -> bytes:
    return s.encode("utf-8", errors="surrogateescape")


@dataclass(frozen=True)
class Hitl2Confirmation:
    """HITL-2 确认结果：终稿 bytes + 已确认 / 编辑的段文本（``resolved_rewrites``）。

    ``final_document`` 为按 ``paragraph_id`` 规范顺序缝合的终稿（确认 / 编辑段用其文本、
    驳回 / 未触达段逐字节原文）；``resolved_rewrites`` 为决策应用后的段文本表（仅含被
    确认 / 编辑的段，驳回段省略）——供崩溃恢复 / 续跑入口 :meth:`Orchestrator.resume_rewrite`
    幂等重推导 ``final_document``。
    """

    final_document: bytes
    resolved_rewrites: dict[str, str]


def build_review(
    original_paragraphs: OriginalParagraphs,
    proposed_rewrites: dict[str, str],
) -> Hitl2Review:
    """构建终稿确认呈现：被触达段的原文 × 提议重写文本。

    纯函数子缝（PRD «Testing Decisions» 呈现缝）：``只读原文表 + proposed_rewrites → 呈现``。
    只呈现 ``proposed_rewrites`` 中的段（rewrite_loop 已判定为触达）；``original_text``
    按 ``paragraph_id`` 从只读表取该段原文（不整篇加载）。``has_pending`` = 是否存在任何
    提议重写，驱动硬闸门的「一键通过」与「禁自动采纳」分支。
    """

    paragraphs: list[ParagraphRewriteReview] = []
    for paragraph_id, proposed_text in proposed_rewrites.items():
        original_text = _decode(original_paragraphs.get(paragraph_id))
        paragraphs.append(
            ParagraphRewriteReview(
                paragraph_id=paragraph_id,
                original_text=original_text,
                proposed_text=proposed_text,
            )
        )
    return Hitl2Review(paragraphs=paragraphs, has_pending=bool(paragraphs))


def resolve_rewrites(
    proposed_rewrites: dict[str, str],
    decision_ops: list[Hitl2Op],
) -> dict[str, str]:
    """应用段级三态决策，返回 resolved_rewrites（确认 / 编辑段文本；驳回段省略）。

    在新 dict 上工作、不修改 ``proposed_rewrites``。每步校验：op 引用的 ``paragraph_id``
    必须在 ``proposed_rewrites`` 内（HITL-2 不凭空造段、只从已提议段中勾选）；非法步即抛
    :class:`Hitl2GateError` → 整个决策丢弃、调用方 ``proposed_rewrites`` 不动（原子性）。

    - 确认 → ``resolved[pid] = proposed_rewrites[pid]``。
    - 编辑 → ``resolved[pid] = op.text``（覆盖提议文本）。
    - 驳回 → ``resolved`` 中省略该 pid（终稿回退原文 bytes）。
    """

    resolved: dict[str, str] = {}
    for op in decision_ops:
        if op.paragraph_id not in proposed_rewrites:
            raise Hitl2GateError(
                f"越权操作：段 {op.paragraph_id} 不在 proposed_rewrites "
                f"（{sorted(proposed_rewrites)}）内"
            )
        if isinstance(op, ConfirmRewriteOp):
            resolved[op.paragraph_id] = proposed_rewrites[op.paragraph_id]
        elif isinstance(op, EditRewriteOp):
            resolved[op.paragraph_id] = op.text
        elif isinstance(op, RejectRewriteOp):
            resolved.pop(op.paragraph_id, None)
        else:  # pragma: no cover - 判别联合已穷尽
            raise AssertionError(f"未处理的 Hitl2Op：{op!r}")
    return resolved


def assemble_final_document(
    original_paragraphs: OriginalParagraphs,
    resolved_rewrites: dict[str, str],
) -> bytes:
    """按规范顺序拼接 ``final_document``（纯「拼接」子缝）。

    遍历 :meth:`OriginalParagraphs.paragraph_ids` 规范顺序：pid 在 ``resolved_rewrites`` →
    用 ``resolved_rewrites[pid]`` 编码后的 bytes（确认 / 编辑文本）；否则 →
    ``original_paragraphs.get(pid)`` 逐字节原文。拼接得 ``final_document``。

    规范顺序遍历保证：只要 ``resolved_rewrites`` 为空（无人确认），``final_document`` 与
    原始输入逐字节相等（分区不变式）。幂等：重跑同一 ``resolved_rewrites`` 得同一份 bytes。
    """

    out = bytearray()
    for paragraph_id in original_paragraphs.paragraph_ids():
        if paragraph_id in resolved_rewrites:
            out += _encode(resolved_rewrites[paragraph_id])
        else:
            out += original_paragraphs.get(paragraph_id)
    return bytes(out)


def confirm(
    original_paragraphs: OriginalParagraphs,
    proposed_rewrites: dict[str, str],
    gate: Hitl2Gate,
) -> Hitl2Confirmation:
    """应用 HITL-2 决策，返回终稿 bytes + resolved_rewrites。

    流程：
    1. ``build_review(original_paragraphs, proposed_rewrites)``——构建呈现。
    2. ``gate.review(review)``——闸门返回决策。
    3. 硬闸门校验（ADR-0010）：``PASS`` 仅当无提议重写；有提议重写时绝不可 ``PASS``
       （绝不无人拍板自动采纳）。
    4. ``DECIDE``：``resolve_rewrites`` 应用段级三态 ops（每步校验合法性；非法步即抛、
       调用方 ``proposed_rewrites`` 不动）。
    5. ``assemble_final_document`` 拼接 ``final_document``。
    """

    review = build_review(original_paragraphs, proposed_rewrites)
    decision = gate.review(review)

    if decision.action is Hitl2Action.PASS:
        if review.has_pending:
            raise Hitl2GateError(
                "硬闸门拦截：有待决提议重写时不可 PASS（绝不在无人拍板时自动采纳）"
            )
        resolved: dict[str, str] = {}
    else:  # DECIDE
        resolved = resolve_rewrites(proposed_rewrites, decision.ops)

    final_document = assemble_final_document(original_paragraphs, resolved)
    return Hitl2Confirmation(
        final_document=final_document, resolved_rewrites=resolved
    )
