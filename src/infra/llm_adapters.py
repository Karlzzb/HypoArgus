"""真实 LLM adapter：四个 LLM seam 的第二 adapter（DashScope / qwen-max 等 OpenAI-compatible）。

contract 层（``LlmClient`` / ``HypothesisLlmClient`` / ``JudgmentLlmClient`` / ``RewriteLlmClient`` Protocol）
provider-free；本模块把注入的 :class:`langchain_core.language_models.BaseChatModel` 经
``with_structured_output`` 绑到各 seam 的结构化输出契约（DEVELOPMENT.md §11）。

设计要点：

- **provider 无关**：构造器收任意 ``BaseChatModel``（DashScope / OpenAI / 自建网关 / 测试 fake）。
- **懒构建**：结构化链在首次 invoke 时才绑定，不在构造期触碰 provider 特性——故测试用 fake
  不会在构造期炸；真实 provider 在首次调用时绑定。
- 四个 seam 的 contract schema 均为扁平 BaseModel（``ParseResult`` / ``_ProposalsEnvelope``
  / ``JudgmentResult`` / ``_RewriteEnvelope``、无判别联合 ``oneOf``）。hypothesis / judgment /
  rewrite 三 seam 直接用 contract schema 经 ``with_structured_output`` 绑定；**parse 拆两阶段**
  ——绑内部信封 ``_ParseTreeEnvelope``（proposals-only）+ ``_SummariesEnvelope``（按 8 段分块的
  ``ParagraphSummary``），再折成 ``ParseResult``（P-01：单绑定下大论文摘要被系统性少填）。
- 任何结构化失败（LLM 抛、schema 非法、provider 不支持 method）由各 stage 的 ``_guarded``
  兜底（单向向前、就地降级，见 DEVELOPMENT.md §5）。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from agents.hypothesis import HypothesisProposal
from agents.judgment import JudgmentResult
from agents.parser import (
    WEIGHT_RUBRIC,
    ParagraphSummary,
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
)
from domain import Argument, ArgumentType, Hypothesis, ParagraphRecord, SessionContext, TimeRange
from infra.retrieval import Source

logger = logging.getLogger(__name__)

__all__ = [
    "QwenParseLlmClient",
    "QwenHypothesisLlmClient",
    "QwenJudgmentLlmClient",
    "QwenRewriteLlmClient",
]


# --------------------------------------------------------------------------- #
# prompt 构造（纯函数）
# --------------------------------------------------------------------------- #


def _format_paragraphs(paragraphs: list[ParagraphView]) -> str:
    return "\n\n".join(f"[{p.paragraph_id}] {p.text}" for p in paragraphs)


def _build_tree_prompt(paragraphs: list[ParagraphView]) -> str:
    """阶段一（树）prompt：识别每段论证节点 + 父子归属 + parent_index + WEIGHT_RUBRIC。

    与旧 ``_build_parse_prompt`` 的唯一区别是**移除摘要生成指令**——
    proposals-only 输出更小，大论文（paper_07 62 段）的树也装得进 8K 输出预算，
    摘要交由阶段二分块产出。
    """

    return (
        "你是论证结构解析器。下列文本已按段给出（形如 [paragraph_id] 文本）。"
        "识别每段内的论证节点及其父子归属，输出节点列表。\n"
        "规则：一节点不跨段（paragraph_id 取自其所在段）；parent_index 指向你输出列表"
        "中父节点的位置（根节点为 null）；argument_weight 按下方 rubric 赋值。\n\n"
        f"{WEIGHT_RUBRIC}\n\n"
        f"段落：\n{_format_paragraphs(paragraphs)}"
    )


def _build_summaries_prompt(batch: list[ParagraphView]) -> str:
    """阶段二（摘要）prompt：为 batch 内每个 [paragraph_id] 各产一句内容摘要。

    强调：batch 内每个 paragraph_id 恰一条摘要，不遗漏、不空——
    逐块产出（每块远低于输出预算）避免大论文顺序产摘要触顶截断。
    """

    return (
        "你是文档内容摘要器。下列文本已按段给出（形如 [paragraph_id] 文本）。"
        "为上述每一个 [paragraph_id] 各产出一句该段的内容摘要，"
        "不得遗漏任何一段、不得返回空摘要（数组逐元素必填）。\n\n"
        f"段落：\n{_format_paragraphs(batch)}"
    )


def _build_propose_prompt(
    argument: Argument, paragraph_summary: str, original_content: str
) -> str:
    """构造假设生成 prompt（PRD §7 输入压缩铁律）。

    T-02：节点所在段原文取自 ``paragraph_list.original_content``（每段一份），不再读
    ``Argument.content``。confidence 0-1 仅排序、不裁决。
    """
    summary_block = (
        f"段落摘要：\n{paragraph_summary}" if paragraph_summary else "段落摘要：（无）"
    )
    original_block = (
        f"节点所在段原文：\n{original_content}" if original_content else "节点所在段原文：（无）"
    )
    return (
        "你是论证修订假设生成器。对给定节点投机生成 0..N 条**可证伪**的修订假设，"
        "每条恰好一种 relation（oppose=对立替换 / advance=递进改写 / expand=扩展追加）。"
        "confidence 0-1 仅用于排序、不参与裁决。无假设则返回空列表。\n\n"
        f"节点 [type={argument.argument_type.value}]：\n{original_block}\n\n"
        f"{summary_block}"
    )


def _format_hypotheses(hypotheses: dict[str, list[Hypothesis]]) -> str:
    if not hypotheses:
        return "（暂无假设）"
    lines = []
    for arg_id, hyps in hypotheses.items():
        for h in hyps:
            lines.append(
                f"- [假设 {h.hypothesis_id}] 归属节点 {arg_id} | "
                f"关系 {h.relation.value}\n  {h.text}"
            )
    return "\n".join(lines)


def _format_citations(citations: dict[str, list[Source]]) -> str:
    if not citations:
        return "（暂无检索素材）"
    lines = []
    for key, sources in citations.items():
        for o in sources:
            lines.append(
                f"- [素材 key={key} | {o.kind.value} | {o.origin}] "
                f"{o.title or ''}\n  {o.snippet}"
            )
    return "\n".join(lines)


def _build_judgment_prompt(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    citations: dict[str, list[Source]],
    paragraph_list: list[ParagraphRecord],
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> str:
    """构造裁决 prompt（PRD §7 输入压缩铁律）。

    T-02：按段聚合节点——遍历 ``paragraph_list``，每段 ``original_content`` 出现一次、
    段内节点只列 ``argument_id`` / ``argument_type``（不再逐节点 ``Argument.content``）。
    只喂段原文 + 假说 ``text`` + citation 片段 + 运行背景（session_context /
    query_time_range）；**不回灌** status/argument_weight/parent_id/children_ids/issue_tags/
    merge_decision——这些由 :func:`agents.judgment.judge_and_adjudicate` 在调用前后管理。
    """

    by_id = {a.argument_id: a for a in argument_tree}
    para_lines: list[str] = []
    for record in paragraph_list:
        node_line = ", ".join(
            f"[节点 {aid} | type={by_id[aid].argument_type.value}]"
            for aid in record.argument_tree_ids
            if aid in by_id
        ) or "（无）"
        original_block = record.original_content or "（无）"
        para_lines.append(
            f"- [段落 {record.paragraph_id}]\n  原文：{original_block}\n  节点：{node_line}"
        )
    args_block = "\n".join(para_lines) or "（无）"
    return (
        "你是论证裁决器。据检索素材对覆盖范围内（main_claim / sub_claim / evidence）的节点"
        "判 per-argument 终态（credible / doubtful / error），并对各假设判终态"
        "（supported / doubtful / refuted）。未覆盖节点（qualification / 影子）不判。"
        "无据可判者可省略（不列入 verdicts 即视为未裁决）。\n\n"
        f"段落：\n{args_block}\n\n"
        f"假设：\n{_format_hypotheses(hypotheses)}\n\n"
        f"检索素材：\n{_format_citations(citations)}\n\n"
        f"运行背景：current_time={session_context.current_time.isoformat()}"
        f"、user_prompt={session_context.user_prompt or '（无）'}"
        f"、时间窗={query_time_range.start}~{query_time_range.end}"
    )


# --------------------------------------------------------------------------- #
# 信封 schema（扁平单 schema，provider 兼容）
# --------------------------------------------------------------------------- #


class _ProposalsEnvelope(BaseModel):
    """假设提案信封：包裹 list 以适配 function-calling 的对象 schema。"""

    proposals: list[HypothesisProposal] = []


class _ParseTreeEnvelope(BaseModel):
    """阶段一解析树信封：仅包裹节点提议（无摘要字段），输出更小以适配大论文。

    与 ``ParseResult`` 的区别：去掉 ``paragraph_summaries`` / ``query_time_range``，
    让阶段一只产 proposals——树装得进 8K 输出预算（paper_07 62 段不再被摘要挤占）。
    """

    proposals: list[ParsedNodeProposal] = Field(default_factory=list)


class _SummariesEnvelope(BaseModel):
    """阶段二摘要信封：按 chunk 包裹段落摘要，逐段独立产出。

    与阶段一解耦——每块远低于输出预算，逐块重试瞬态抖动而不影响已建好的树。
    """

    summaries: list[ParagraphSummary] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# adapter（懒构建结构化链；构造期不触碰 provider 特性）
# --------------------------------------------------------------------------- #


class QwenParseLlmClient:
    """解析 LLM seam 的真实 adapter（``LlmClient`` Protocol 的第二 adapter）——两阶段解析。

    两阶段设计把树（proposals）与摘要（summaries）拆为两个 LLM 关注点：

    - **阶段一·树**：一次调用、所有段落同进——保留跨段父子链接（parent_index 可指向
      他段节点）；proposals-only 输出更小、大论文（paper_07 62 段）也装得进 8K 输出预算。
    - **阶段二·摘要**：按 ``summary_chunk_size`` 分块、逐块产出——每块远低于输出预算，
      逐块重试瞬态抖动（glitch）而不影响已建好的树。

    退化判定要求同时存在 main_claim 与 evidence 节点（真实多段论文二者皆有，否则重试）。
    全部尝试都退化（返回了 envelope 但缺节点）时返回最后一次诚实输出——
    parser / 测试看到诚实结果而非崩溃（contract 1 不抛）。
    但若每次尝试都抛异常（从未返回任何 envelope），则重新抛出最后一个异常——
    真实 provider 故障不被静默吞掉为空树，编排层的 ``_guarded`` 兜底会处理。
    """

    def __init__(
        self,
        chat_model: BaseChatModel,
        *,
        method: str = "function_calling",
        max_attempts: int = 5,
        summary_chunk_size: int = 8,
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._max_attempts = max_attempts
        self._summary_chunk_size = summary_chunk_size
        self._tree_chain: Any = None
        self._summaries_chain: Any = None

    def _get_tree_chain(self) -> Any:
        if self._tree_chain is None:
            self._tree_chain = self._chat.with_structured_output(
                _ParseTreeEnvelope, method=self._method
            )
        return self._tree_chain

    def _get_summaries_chain(self) -> Any:
        if self._summaries_chain is None:
            self._summaries_chain = self._chat.with_structured_output(
                _SummariesEnvelope, method=self._method
            )
        return self._summaries_chain

    # ---- 阶段一：树（全局、重试韧性） ----

    def _invoke_tree(
        self, paragraphs: list[ParagraphView]
    ) -> tuple[_ParseTreeEnvelope | None, str | None, BaseException | None]:
        """单次调用树链；返回 (结果 | None, 退化原因 | None, 异常 | None)。

        只在拿到**非退化**的 ``_ParseTreeEnvelope`` 时返回非 None 结果。
        退化定义：抛异常、或非 ``_ParseTreeEnvelope``、或 proposals 为空、
        或无任何 proposal 的 ``argument_type == MAIN_CLAIM``、
        或无任何 proposal 的 ``argument_type == EVIDENCE``。
        """

        try:
            result = self._get_tree_chain().invoke(_build_tree_prompt(paragraphs))
        except BaseException as exc:  # noqa: BLE001 — 重新抛出由调用方裁决
            return None, f"invoke 抛异常: {type(exc).__name__}: {exc}", exc
        if not isinstance(result, _ParseTreeEnvelope):
            return (
                None,
                f"非 _ParseTreeEnvelope 返回: {type(result).__name__}",
                None,
            )
        if len(result.proposals) == 0:
            return result, "proposals 为空（零提议退化）", None
        if not any(
            p.argument_type == ArgumentType.MAIN_CLAIM for p in result.proposals
        ):
            return result, "无 main_claim 提议（退化）", None
        if not any(
            p.argument_type == ArgumentType.EVIDENCE for p in result.proposals
        ):
            return result, "无 evidence 提议（退化）", None
        return result, None, None

    def _parse_tree(
        self, paragraphs: list[ParagraphView]
    ) -> list[ParsedNodeProposal]:
        last_result: _ParseTreeEnvelope | None = None
        last_exc: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            result, reason, exc = self._invoke_tree(paragraphs)
            if exc is not None:
                last_exc = exc
                logger.warning(
                    "parse[tree] 尝试 %d/%d 失败（异常）: %s",
                    attempt,
                    self._max_attempts,
                    reason,
                )
                continue
            if result is None:
                # isinstance 失败（reason 已含类型信息），无 envelope 可保留。
                logger.warning(
                    "parse[tree] 尝试 %d/%d 退化: %s",
                    attempt,
                    self._max_attempts,
                    reason,
                )
                continue
            # 到此 result 是 _ParseTreeEnvelope（可能退化：缺 main_claim/evidence / 空）。
            last_result = result
            if reason is None:
                if attempt > 1:
                    logger.info(
                        "parse[tree] 在第 %d/%d 次尝试后成功",
                        attempt,
                        self._max_attempts,
                    )
                return result.proposals
            logger.warning(
                "parse[tree] 尝试 %d/%d 退化: %s", attempt, self._max_attempts, reason
            )
        if last_result is not None:
            # 全部尝试退化但至少拿到过 _ParseTreeEnvelope：返回最后一次诚实输出。
            return last_result.proposals
        if last_exc is not None:
            # 从未拿到 envelope 但至少抛过一次异常：重新抛出，交由 _guarded 兜底。
            raise last_exc
        # 从未拿到 envelope 也从未抛异常（纯 isinstance 失败）：空提议（诚实、不抛）。
        return []

    # ---- 阶段二：摘要（分块、逐块重试韧性） ----

    def _invoke_summaries(
        self, batch: list[ParagraphView]
    ) -> tuple[_SummariesEnvelope | None, str | None, BaseException | None]:
        """单次调用摘要链；返回 (结果 | None, 不完备原因 | None, 异常 | None)。

        不完备定义：抛异常、或非 ``_SummariesEnvelope``、或 batch 中任一
        ``paragraph_id`` 缺失于 ``result.summaries``、或任一返回摘要为空。
        """

        batch_ids = {p.paragraph_id for p in batch}
        try:
            result = self._get_summaries_chain().invoke(_build_summaries_prompt(batch))
        except BaseException as exc:  # noqa: BLE001 — 重新抛出由调用方裁决
            return None, f"invoke 抛异常: {type(exc).__name__}: {exc}", exc
        if not isinstance(result, _SummariesEnvelope):
            return (
                None,
                f"非 _SummariesEnvelope 返回: {type(result).__name__}",
                None,
            )
        got_ids = {s.paragraph_id for s in result.summaries}
        missing = batch_ids - got_ids
        if missing:
            return result, f"缺失摘要段落: {sorted(missing)[:5]}", None
        empty = [
            s.paragraph_id for s in result.summaries if not s.summary.strip()
        ]
        if empty:
            return result, f"空摘要段落: {empty[:5]}", None
        return result, None, None

    def _parse_summaries(
        self, paragraphs: list[ParagraphView]
    ) -> list[ParagraphSummary]:
        merged: list[ParagraphSummary] = []
        for chunk_idx, start in enumerate(
            range(0, len(paragraphs), self._summary_chunk_size)
        ):
            batch = paragraphs[start : start + self._summary_chunk_size]
            batch_ids = {p.paragraph_id for p in batch}
            best: list[ParagraphSummary] = []
            best_cov = -1
            for attempt in range(1, self._max_attempts + 1):
                result, reason, exc = self._invoke_summaries(batch)
                if exc is not None:
                    logger.warning(
                        "parse[summaries] 块 %d 尝试 %d/%d 失败（异常）: %s",
                        chunk_idx,
                        attempt,
                        self._max_attempts,
                        reason,
                    )
                    continue
                if result is None:
                    # isinstance 失败（reason 已含类型信息），无 summaries 可保留。
                    logger.warning(
                        "parse[summaries] 块 %d 尝试 %d/%d 不完备: %s",
                        chunk_idx,
                        attempt,
                        self._max_attempts,
                        reason,
                    )
                    continue
                if reason is None:
                    if attempt > 1:
                        logger.info(
                            "parse[summaries] 块 %d 在第 %d/%d 次尝试后成功",
                            chunk_idx,
                            attempt,
                            self._max_attempts,
                        )
                    best = result.summaries
                    break
                # 不完备：保留覆盖最多 batch 段落的尝试作为 best-effort。
                cov = sum(
                    1
                    for s in result.summaries
                    if s.paragraph_id in batch_ids and s.summary.strip()
                )
                if cov > best_cov:
                    best_cov = cov
                    best = result.summaries
                logger.warning(
                    "parse[summaries] 块 %d 尝试 %d/%d 不完备: %s",
                    chunk_idx,
                    attempt,
                    self._max_attempts,
                    reason,
                )
            # 兜底：LLM 漏填 / 返空摘要素落（如纯标题段「# 标题」LLM 拒摘要）→ 用该段
            # 自身文本（去换行、截断 80 字）补非空摘要。摘要素自段落原文、非 LLM 虚构，
            # 使下游 paragraph_summaries 对每个喂入段全覆盖（契约 7 确定性、不弱化）。
            best_by_id: dict[str, str] = {s.paragraph_id: s.summary for s in best}
            for v in batch:
                if (best_by_id.get(v.paragraph_id) or "").strip():
                    continue
                snippet = v.text.strip().replace("\n", " ")[:80] or v.paragraph_id
                best_by_id[v.paragraph_id] = snippet
            merged.extend(
                ParagraphSummary(paragraph_id=v.paragraph_id, summary=best_by_id[v.paragraph_id])
                for v in batch
            )
        return merged

    def parse(self, paragraphs: list[ParagraphView]) -> ParseResult:
        proposals = self._parse_tree(paragraphs)
        summaries = self._parse_summaries(paragraphs)
        return ParseResult(proposals=proposals, paragraph_summaries=summaries)


class QwenHypothesisLlmClient:
    """开药 LLM seam 的真实 adapter（``HypothesisLlmClient`` Protocol 的第二 adapter）。

    重构后仅 ``propose``（不取证）；取证（吃 citations 判终态）属
    judgment adapter，不在此处。
    """

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._propose_chain: Any = None

    def _get_propose_chain(self) -> Any:
        if self._propose_chain is None:
            self._propose_chain = self._chat.with_structured_output(
                _ProposalsEnvelope, method=self._method
            )
        return self._propose_chain

    def propose(
        self, argument: Argument, paragraph_summary: str, original_content: str
    ) -> list[HypothesisProposal]:
        envelope = self._get_propose_chain().invoke(
            _build_propose_prompt(argument, paragraph_summary, original_content)
        )
        assert isinstance(envelope, _ProposalsEnvelope)
        return envelope.proposals


class QwenJudgmentLlmClient:
    """裁决 LLM seam 的真实 adapter（``JudgmentLlmClient`` Protocol 的第二 adapter）。

    五合一：吃 citations 判 per-argument / per-hypothesis 终态。``JudgmentResult``
    本即扁平信封（``argument_verdicts`` + ``hypothesis_verdicts`` 两 list、无判别联合），
    故直接用 contract schema 经 ``with_structured_output`` 绑定、无需额外信封映射（与
    :class:`QwenParseLlmClient` 同形）。输入压缩铁律见 :func:`_build_judgment_prompt`。
    """

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._chain: Any = None

    def _get_chain(self) -> Any:
        if self._chain is None:
            self._chain = self._chat.with_structured_output(
                JudgmentResult, method=self._method
            )
        return self._chain

    def judge(
        self,
        argument_tree: list[Argument],
        hypotheses: dict[str, list[Hypothesis]],
        citations: dict[str, list[Source]],
        paragraph_list: list[ParagraphRecord],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> JudgmentResult:
        result = self._get_chain().invoke(
            _build_judgment_prompt(
                argument_tree,
                hypotheses,
                citations,
                paragraph_list,
                session_context,
                query_time_range,
            )
        )
        assert isinstance(result, JudgmentResult)
        return result


# --------------------------------------------------------------------------- #
# 重写提议 seam
# --------------------------------------------------------------------------- #


class _RewriteEnvelope(BaseModel):
    """重写提议信封：单段提议重写文本（空串 = 选择不提议，hitl2 见无提议 → 逐字节拷回）。"""

    rewritten_text: str = ""


def _build_rewrite_prompt(
    paragraph_id: str,
    paragraph_summary: str,
    original_content: str,
    arguments: list[Argument],
    citations: list[Source],
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> str:
    """构造重写提议 prompt（PRD §7 输入压缩铁律）。

    T-02：段原文改取 ``paragraph_list.original_content``（每段一份），prompt 按段聚合——
    段原文出现一次、节点只列 ``argument_id`` / ``argument_type``（不再逐节点拷 ``content``）。
    只喂段落摘要 + 段原文 + 段内 argument ``argument_type`` + 段内假说 ``text`` /
    ``relation`` + citation 片段 + 运行背景（session_context / query_time_range）；**不回灌**
    ``status`` / ``argument_weight`` / ``parent_id`` / ``children_ids`` / ``issue_tags`` /
    ``merge_decision``——这些不进 prompt（rewrite_loop 与 argument 状态解耦）。
    """

    args_block = "\n".join(
        f"- [节点 {a.argument_id} | type={a.argument_type.value}]"
        for a in arguments
    ) or "（无）"
    hyps = [h for a in arguments for h in a.candidate_hypotheses]
    hyp_block = (
        "\n".join(
            f"- [假设 {h.hypothesis_id} | relation={h.relation.value}] {h.text}"
            for h in hyps
        )
        or "（无）"
    )
    cit_block = (
        "\n".join(
            f"- [{o.kind.value} | {o.origin}] {o.title or ''}\n  {o.snippet}"
            for o in citations
        )
        or "（无）"
    )
    summary_block = (
        f"段落摘要：\n{paragraph_summary}" if paragraph_summary else "段落摘要：（无）"
    )
    original_block = (
        f"段落原文：\n{original_content}" if original_content else "段落原文：（无）"
    )
    return (
        "你是论证文档修订器。对给定段落（已识别其论证节点与相关假设 / 检索素材）"
        "产出一版重写后的完整段落文本，使其论证更稳健 / 更准确。若该段无需修订，"
        "返回空串。\n\n"
        f"[段落 {paragraph_id}]\n{summary_block}\n\n{original_block}\n\n"
        f"节点：\n{args_block}\n\n假设：\n{hyp_block}\n\n"
        f"检索素材：\n{cit_block}\n\n"
        f"运行背景：current_time={session_context.current_time.isoformat()}"
        f"、user_prompt={session_context.user_prompt or '（无）'}"
        f"、时间窗={query_time_range.start}~{query_time_range.end}"
    )


class QwenRewriteLlmClient:
    """逐段重写提议 LLM seam 的真实 adapter（``RewriteLlmClient`` Protocol 的第二 adapter）。

    ADR-0017：judgment 之后对被触达段产一版重写文本。输出为自由文本（非判别联合），
    用扁平信封 :class:`_RewriteEnvelope` 经 ``with_structured_output`` 绑定（provider 兼容、
    与其它 adapter 同形）；空串 = 选择不提议（→ hitl2 逐字节拷回原文）。输入压缩铁律见
    :func:`_build_rewrite_prompt`：T-02 段原文取 ``paragraph_list.original_content``、不回灌
    内部状态字段（rewrite_loop 与 argument 状态解耦）。
    """

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._chain: Any = None

    def _get_chain(self) -> Any:
        if self._chain is None:
            self._chain = self._chat.with_structured_output(
                _RewriteEnvelope, method=self._method
            )
        return self._chain

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
        envelope = self._get_chain().invoke(
            _build_rewrite_prompt(
                paragraph_id,
                paragraph_summary,
                original_content,
                arguments,
                citations,
                session_context,
                query_time_range,
            )
        )
        assert isinstance(envelope, _RewriteEnvelope)
        return envelope.rewritten_text or None
