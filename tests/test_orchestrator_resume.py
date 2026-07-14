"""编排中枢终稿拼装幂等续跑 seam 测试（issue #11 · 衔接 rewrite_loop/hitl2 · ADR-0017）。

终稿拼装中断（崩溃 / 进程退出 / hitl2 stage 兜底回退原文）后，对持久化的
``resolved_rewrites``（HITL-2 已确认 / 编辑的段文本表）续跑：复用纯函数
:func:`agents.hitl2.assemble_final_document` 幂等再推导 ``final_document``——
按 ``original_paragraphs`` 规范顺序缝合：确认 / 编辑段用其文本、驳回 / 未触达段逐字节原文，
重跑得同一份 bytes。本 seam 是编排中枢暴露的崩溃恢复入口，调用方持有落盘的
``resolved_rewrites`` + 只读原文表即可续跑，无需重跑整条流水线、亦无需再调 LLM / 闸门。
"""

from __future__ import annotations

from original_paragraphs import OriginalParagraphs
from runtime.orchestrator import Orchestrator

_DOC = "分论点。\n\n论据。\n".encode()


def test_resume_rewrite_confirmed_segment_uses_resolved_text():
    """确认段用 ``resolved_rewrites`` 文本、省略段逐字节原文（Slice 6）。"""

    original_paragraphs = OriginalParagraphs.from_text(_DOC)
    resolved = {"p0002": "论据[已修订]"}  # p0002 经 HITL-2 确认

    out = Orchestrator().resume_rewrite(resolved, original_paragraphs)

    # p0001 省略（驳回 / 未触达）→ 逐字节原文（含段间空行尾随字节）；p0002 确认 → resolved 文本。
    assert out == "分论点。\n\n论据[已修订]".encode()
    assert out.startswith("分论点。\n\n".encode())  # 未触达段逐字节忠实
    assert out.endswith("论据[已修订]".encode())  # 确认文本落地


def test_resume_rewrite_empty_resolved_is_byte_identical():
    """空 ``resolved_rewrites``（无人确认）→ 终稿逐字节等于原文。"""

    original_paragraphs = OriginalParagraphs.from_text(_DOC)
    out = Orchestrator().resume_rewrite({}, original_paragraphs)
    assert out == _DOC  # 分区不变式


def test_resume_rewrite_idempotent_same_bytes():
    """幂等：重跑同一 ``resolved_rewrites`` 得同一份 bytes。"""

    original_paragraphs = OriginalParagraphs.from_text(_DOC)
    resolved = {"p0002": "论据[已修订]"}
    orch = Orchestrator()
    first = orch.resume_rewrite(resolved, original_paragraphs)
    second = orch.resume_rewrite(resolved, original_paragraphs)
    assert first == second  # 幂等重推导


def test_resume_rewrite_edit_segment_uses_edit_text():
    """编辑段用其编辑文本（覆盖原文），其余段逐字节原文。"""

    original_paragraphs = OriginalParagraphs.from_text(_DOC)
    resolved = {"p0001": "分论点[编辑后]"}  # p0001 经 HITL-2 编辑

    out = Orchestrator().resume_rewrite(resolved, original_paragraphs)

    # p0001 编辑文本（无原尾随空行）+ p0002 省略 → 逐字节原文。
    assert out == "分论点[编辑后]论据。\n".encode()
