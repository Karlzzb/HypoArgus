"""逐段重写提议 Agent 契约：rewrite_loop seam + LLM Protocol + 离线 Fake 桩
（PRD §12、ADR-0017、Slice 6）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + outcome 模型，``agent.py``
放逐段提议纯函数。``RewriteLlmClient`` 为注入 seam：真实适配器用
``with_structured_output`` 或直 ``str`` 输出（dev-guide §6.3）；本切片提供
``FakeRewriteLoopLlmClient`` 供离线单测——provider-free、确定、可断言。

Slice 6（ADR-0017）：judgment 之后、hitl2 之前新增 ``rewrite_loop`` 节点。对**被触达段**
（段内有 ``supported`` 假说 / 命中 citations）由 LLM 提议一版重写文本；未触达段不进
``proposed_rewrites``（→ hitl2 逐字节拷回）。本 seam 逐段调 ``propose_rewrite``——吃
``paragraph_summary`` + 该段 ``original_content``（段落聚合根 ``ParagraphRecord`` 单份原文，
T-02 取代原先每个节点各拷一份的 ``Argument.content``）+ 段内 ``Argument``（含
``candidate_hypotheses``）+ 段聚合 ``citations`` + 背景；产提议重写文本（``str | None``，
``None`` = 选择不提议）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from domain import Argument, SessionContext, TimeRange
from infra.retrieval import Source

__all__ = [
    "RewriteLlmClient",
    "FakeRewriteLlmClient",
    "RewriteLoopOutcome",
]


# --------------------------------------------------------------------------- #
# outcome 模型（rewrite_loop 节点的产出）
# --------------------------------------------------------------------------- #


class RewriteLoopOutcome(BaseModel):
    """rewrite_loop 节点写回 state 的产出：提议重写表 + per-段失败日志。

    ``proposed_rewrites`` 仅含**被触达且 LLM 成功提议**的段（``paragraph_id → 提议文本``），
    写回 ``proposed_rewrites`` channel（单写者=rewrite_loop、读者=hitl2）。``errors`` 为
    per-段 LLM 抛错日志（``[rewrite_loop] {pid}: ExcType: msg``），由 build 闭包并入
    ``errors`` channel——rewrite_loop **不碰 ``argument_tree``**（新流程按段/文本工作，
    与 argument 的 ``status`` / ``merge_decision`` 解耦；``argument_tree`` 在 judgment 之后
    仍是 judgment 唯一写者）。
    """

    proposed_rewrites: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class RewriteLlmClient(Protocol):
    """逐段重写提议 LLM seam（Slice 6）。

    - :meth:`propose_rewrite`：吃单段输入（``paragraph_id`` + ``paragraph_summary`` +
      该段 ``original_content``（段落聚合根单份原文，T-02 取代逐节点 ``Argument.content``）+
      段内 ``Argument``（含 ``candidate_hypotheses``）+ 段聚合 ``citations`` + 贯穿背景
      ``session_context`` / ``query_time_range``），产该段提议重写文本。返回 ``None`` /
      空串 = 该段不提议（hitl2 见无提议 → 逐字节拷回原文）。

    真实适配器只把 ``paragraph_summary`` + 段 ``original_content`` + argument
    ``argument_type`` + 假说 ``text`` / ``relation`` + citation 片段 + 背景喂 LLM（输入压缩
    铁律，PRD §7）；**不回灌** ``status`` / ``argument_weight`` / ``parent_id`` /
    ``children_ids`` / ``issue_tags`` / ``merge_decision``——这些不进 prompt。本 seam 不绑任何
    provider。T-04：``Argument`` 不存原文字段，段原文取自 ``ParagraphRecord.original_content``。
    """

    def propose_rewrite(
        self,
        paragraph_id: str,
        paragraph_summary: str,
        original_content: str,
        arguments: list[Argument],
        citations: list[Source],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> str | None: ...


class FakeRewriteLlmClient:
    """离线逐段重写 LLM 桩。provider-free、确定（供单测）。

    - ``propose_factory``：``callable(paragraph_id, paragraph_summary, original_content,
      arguments, citations, session_context, query_time_range) -> str | None``，可据段输入
      动态决策（亦供 T-02 spy 断言「改写 seam 收到该段 original_content」回归锁）。
    - 无 → 返回 ``None``（不提议；桩路径无触达段 → 永不被调 → ``proposed_rewrites={}``
      → 字节一致）。
    """

    def __init__(
        self,
        *,
        propose_factory: Callable[
            [str, str, str, list[Argument], list[Source], SessionContext, TimeRange],
            str | None,
        ]
        | None = None,
    ) -> None:
        self._propose_factory = propose_factory

    def propose_rewrite(
        self,
        paragraph_id: str,
        paragraph_summary: str,
        original_content: str,
        arguments: list[Argument],
        citations: list[Source],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> str | None:
        if self._propose_factory is not None:
            return self._propose_factory(
                paragraph_id,
                paragraph_summary,
                original_content,
                arguments,
                citations,
                session_context,
                query_time_range,
            )
        return None
