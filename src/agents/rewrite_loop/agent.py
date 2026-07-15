"""逐段重写提议 Agent：rewrite_loop 节点（PRD §12、ADR-0017、Slice 6）。

对 judgment 之后的 ``argument_tree`` 逐段（按 ``paragraph_list`` 规范顺序）判定
**触达**：段内任一 ``candidate_hypotheses`` 含 ``supported`` 假说，或 ``citations`` 命中
该段任一 ``argument_id`` / 段内任一 ``hypothesis_id`` → 触达。触达段调
:meth:`RewriteLlmClient.propose_rewrite` 产提议重写文本；未触达段不进 ``proposed_rewrites``
（→ hitl2 逐字节拷回原文，字节级忠实）。LLM 返回 ``None`` / 空串 = 选择不提议（省略、
非失败）；LLM 抛错 = per-段失败（该段省略 + 记 ``[rewrite_loop]`` 日志、不杀全树）。

T-02：直接遍历 ``paragraph_list``（段落聚合根）、按 ``argument_tree_ids`` 正向解析段内节点
取该段 ``original_content``——不再反向 join ``Argument.paragraph_id``、不再读
``Argument.content``。``rewrite_loop`` **不碰 ``argument_tree``**（新流程按段/文本工作，与
argument 的 ``status`` / ``merge_decision`` 解耦；``argument_tree`` 在 judgment 之后仍是
judgment 唯一写者）。失败信号落在 ``errors`` channel + 段回退原文，不写 ``argument_tree``。

输入压缩铁律（PRD §7）：seam 只喂 ``paragraph_summary`` + 段 ``original_content`` +
argument ``argument_type`` + 假说 ``text`` / ``relation`` + citation 片段 + 背景；**不回灌**
``status`` / ``argument_weight`` / ``parent_id`` / ``children_ids`` / ``issue_tags`` /
``merge_decision``——真实 prompt 构造属 :mod:`infra.llm_adapters` 的 ``QwenRewriteLlmClient``；
本模块 :func:`propose_rewrites` 与 provider 无关、可独立单测。
"""

from __future__ import annotations

from agents.rewrite_loop.contract import RewriteLlmClient, RewriteLoopOutcome
from domain import (
    Argument,
    Hypothesis,
    HypothesisStatus,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source

__all__ = ["propose_rewrites"]


def _hypotheses_for(argument: Argument) -> list[Hypothesis]:
    """节点取假说候选（``candidate_hypotheses``——judgment 已落终态）。"""

    return argument.candidate_hypotheses


def _is_touched(
    arguments: list[Argument], citations: dict[str, list[Source]]
) -> bool:
    """段是否被触达：有 supported 假说，或 citations 命中该段 argument / hypothesis id。"""

    for argument in arguments:
        if any(
            h.status is HypothesisStatus.SUPPORTED
            for h in _hypotheses_for(argument)
        ):
            return True
    arg_ids = {a.argument_id for a in arguments}
    hyp_ids = {
        h.hypothesis_id for a in arguments for h in _hypotheses_for(a)
    }
    for key in citations:
        if key in arg_ids or key in hyp_ids:
            return True
    return False


def _citations_for_paragraph(
    arguments: list[Argument], citations: dict[str, list[Source]]
) -> list[Source]:
    """聚合命中该段的 citations（key 为段内 argument_id 或 hypothesis_id）。"""

    arg_ids = {a.argument_id for a in arguments}
    hyp_ids = {
        h.hypothesis_id for a in arguments for h in _hypotheses_for(a)
    }
    out: list[Source] = []
    for key, sources in citations.items():
        if key in arg_ids or key in hyp_ids:
            out.extend(sources)
    return out


def propose_rewrites(
    argument_tree: list[Argument],
    citations: dict[str, list[Source]],
    paragraph_list: list[ParagraphRecord],
    session_context: SessionContext,
    query_time_range: TimeRange,
    llm: RewriteLlmClient,
) -> RewriteLoopOutcome:
    """逐段提议重写：触达段产 LLM 提议、未触达段省略、抛错段省略 + 记日志。

    T-02：遍历 ``paragraph_list``（规范段序，与终稿拼装同序）、按 ``argument_tree_ids``
    正向解析段内节点（不再反向 join ``Argument.paragraph_id``）。每段：未触达 → 跳过
    （不调 LLM）；触达 → 调 ``llm.propose_rewrite``（喂该段 ``original_content`` + ``summary``）：
    返回非空串 → 入 ``proposed_rewrites``；返回 ``None`` / 空串 → 省略（非失败）；抛错 → 省略 +
    追加 ``[rewrite_loop] {pid}: ExcType: msg`` 到 ``errors``（per-段捕获、不杀全树）。

    不修改 ``argument_tree``（只读用于触达判定 + LLM 输入）。
    """

    # 节点按 argument_id 索引一次，供 argument_tree_ids 正向解析（O(N) 而非逐段扫全树）。
    arguments_by_id: dict[str, Argument] = {
        a.argument_id: a for a in argument_tree
    }

    proposed_rewrites: dict[str, str] = {}
    errors: list[str] = []
    for record in paragraph_list:
        paragraph_id = record.paragraph_id
        arguments = [
            arguments_by_id[aid]
            for aid in record.argument_tree_ids
            if aid in arguments_by_id
        ]
        if not _is_touched(arguments, citations):
            continue
        paragraph_citations = _citations_for_paragraph(arguments, citations)
        try:
            text = llm.propose_rewrite(
                paragraph_id,
                record.summary,
                record.original_content,
                [a.model_copy() for a in arguments],
                list(paragraph_citations),
                session_context,
                query_time_range,
            )
        except Exception as exc:  # noqa: BLE001 - per-段捕获、记日志、不杀全树
            errors.append(
                f"[rewrite_loop] {paragraph_id}: {type(exc).__name__}: {exc}"
            )
            continue
        if text:
            proposed_rewrites[paragraph_id] = text

    return RewriteLoopOutcome(proposed_rewrites=proposed_rewrites, errors=errors)
