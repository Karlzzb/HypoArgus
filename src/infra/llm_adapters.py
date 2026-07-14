"""真实 LLM adapter：三个 LLM seam 的第二 adapter（DashScope / qwen-max 等 OpenAI-compatible）。

contract 层（``LlmClient`` / ``HypothesisLlmClient`` / ``JudgmentLlmClient`` Protocol）
provider-free；本模块把注入的 :class:`langchain_core.language_models.BaseChatModel` 经
``with_structured_output`` 绑到各 seam 的结构化输出契约（dev-guide §6.3）。

设计要点：

- **provider 无关**：构造器收任意 ``BaseChatModel``（DashScope / OpenAI / 自建网关 / 测试 fake）。
- **懒构建**：结构化链在首次 invoke 时才绑定，不在构造期触碰 provider 特性——故测试用 fake
  不会在构造期炸；真实 provider 在首次调用时绑定。
- 三个 seam 的 contract schema 均为扁平 BaseModel（``ParseResult`` / ``_ProposalsEnvelope``
  / ``JudgmentResult``、无判别联合 ``oneOf``），故直接用 contract schema 经
  ``with_structured_output`` 绑定、无需扁平信封映射。
- 任何结构化失败（LLM 抛、schema 非法、provider 不支持 method）由各 stage 的 ``_guarded``
  兜底（单向向前、就地降级，见 DEVELOPMENT.md §5）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from agents.hypothesis import HypothesisProposal
from agents.judgment import JudgmentResult
from agents.parser import WEIGHT_RUBRIC, ParagraphView, ParseResult
from domain import Argument, Hypothesis, SessionContext, TimeRange
from infra.retrieval import Source

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


def _build_parse_prompt(paragraphs: list[ParagraphView]) -> str:
    return (
        "你是论证结构解析器。下列文本已按段给出（形如 [paragraph_id] 文本）。"
        "识别每段内的论证节点及其父子归属，输出节点列表。\n"
        "规则：一节点不跨段（paragraph_id 取自其所在段）；parent_index 指向你输出列表"
        "中父节点的位置（根节点为 null）；argument_weight 按下方 rubric 赋值。\n\n"
        f"{WEIGHT_RUBRIC}\n\n"
        f"段落：\n{_format_paragraphs(paragraphs)}"
    )


def _build_propose_prompt(argument: Argument, paragraph_summary: str) -> str:
    summary_block = (
        f"段落摘要：\n{paragraph_summary}" if paragraph_summary else "段落摘要：（无）"
    )
    return (
        "你是论证修订假设生成器。对给定节点投机生成 0..N 条**可证伪**的修订假设，"
        "每条恰好一种 relation（oppose=对立替换 / advance=递进改写 / expand=扩展追加）。"
        "confidence 0-1 仅用于排序、不参与裁决。无假设则返回空列表。\n\n"
        f"节点 [type={argument.argument_type.value}] 内容：\n{argument.content}\n\n"
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
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> str:
    """构造裁决 prompt（PRD §7 输入压缩铁律）。

    只喂节点 ``content`` + 假说 ``text`` + citation 片段 + 运行背景（session_context /
    query_time_range）；**不回灌** status/argument_weight/parent_id/children_ids/issue_tags/
    merge_decision——这些由 :func:`agents.judgment.judge_and_adjudicate` 在调用前后管理。
    """

    args_block = "\n".join(
        f"- [节点 {a.argument_id} | type={a.argument_type.value}] {a.content}"
        for a in argument_tree
    )
    return (
        "你是论证裁决器。据检索素材对覆盖范围内（main_claim / sub_claim / evidence）的节点"
        "判 per-argument 终态（credible / doubtful / error），并对各假设判终态"
        "（supported / doubtful / refuted）。未覆盖节点（qualification / 影子）不判。"
        "无据可判者可省略（不列入 verdicts 即视为未裁决）。\n\n"
        f"节点：\n{args_block}\n\n"
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


# --------------------------------------------------------------------------- #
# adapter（懒构建结构化链；构造期不触碰 provider 特性）
# --------------------------------------------------------------------------- #


class QwenParseLlmClient:
    """解析 LLM seam 的真实 adapter（``LlmClient`` Protocol 的第二 adapter）。"""

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._chain: Any = None

    def _get_chain(self) -> Any:
        if self._chain is None:
            self._chain = self._chat.with_structured_output(
                ParseResult, method=self._method
            )
        return self._chain

    def parse(self, paragraphs: list[ParagraphView]) -> ParseResult:
        result = self._get_chain().invoke(_build_parse_prompt(paragraphs))
        assert isinstance(result, ParseResult)
        return result


class QwenHypothesisLlmClient:
    """开药 LLM seam 的真实 adapter（``HypothesisLlmClient`` Protocol 的第二 adapter）。

    Slice 3 重构后仅 ``propose``（不取证）；取证（吃 citations 判终态）属 Slice 5 的
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
        self, argument: Argument, paragraph_summary: str
    ) -> list[HypothesisProposal]:
        envelope = self._get_propose_chain().invoke(
            _build_propose_prompt(argument, paragraph_summary)
        )
        assert isinstance(envelope, _ProposalsEnvelope)
        return envelope.proposals


class QwenJudgmentLlmClient:
    """裁决 LLM seam 的真实 adapter（``JudgmentLlmClient`` Protocol 的第二 adapter）。

    Slice 5 五合一：吃 citations 判 per-argument / per-hypothesis 终态。``JudgmentResult``
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
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> JudgmentResult:
        result = self._get_chain().invoke(
            _build_judgment_prompt(
                argument_tree,
                hypotheses,
                citations,
                session_context,
                query_time_range,
            )
        )
        assert isinstance(result, JudgmentResult)
        return result


# --------------------------------------------------------------------------- #
# 重写提议 seam（Slice 6）
# --------------------------------------------------------------------------- #


class _RewriteEnvelope(BaseModel):
    """重写提议信封：单段提议重写文本（空串 = 选择不提议，hitl2 见无提议 → 逐字节拷回）。"""

    rewritten_text: str = ""


def _build_rewrite_prompt(
    paragraph_id: str,
    paragraph_summary: str,
    arguments: list[Argument],
    citations: list[Source],
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> str:
    """构造重写提议 prompt（PRD §7 输入压缩铁律）。

    只喂段落摘要 + 段内 argument ``content`` / ``argument_type`` + 段内假说 ``text`` /
    ``relation`` + citation 片段 + 运行背景（session_context / query_time_range）；**不回灌**
    ``status`` / ``argument_weight`` / ``parent_id`` / ``children_ids`` / ``issue_tags`` /
    ``merge_decision``——这些不进 prompt（rewrite_loop 与 argument 状态解耦）。
    """

    args_block = "\n".join(
        f"- [节点 {a.argument_id} | type={a.argument_type.value}] {a.content}"
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
    return (
        "你是论证文档修订器。对给定段落（已识别其论证节点与相关假设 / 检索素材）"
        "产出一版重写后的完整段落文本，使其论证更稳健 / 更准确。若该段无需修订，"
        "返回空串。\n\n"
        f"[段落 {paragraph_id}]\n{summary_block}\n\n"
        f"节点：\n{args_block}\n\n假设：\n{hyp_block}\n\n"
        f"检索素材：\n{cit_block}\n\n"
        f"运行背景：current_time={session_context.current_time.isoformat()}"
        f"、user_prompt={session_context.user_prompt or '（无）'}"
        f"、时间窗={query_time_range.start}~{query_time_range.end}"
    )


class QwenRewriteLlmClient:
    """逐段重写提议 LLM seam 的真实 adapter（``RewriteLlmClient`` Protocol 的第二 adapter）。

    Slice 6（ADR-0017）：judgment 之后对被触达段产一版重写文本。输出为自由文本（非判别联合），
    用扁平信封 :class:`_RewriteEnvelope` 经 ``with_structured_output`` 绑定（provider 兼容、
    与其它 adapter 同形）；空串 = 选择不提议（→ hitl2 逐字节拷回原文）。输入压缩铁律见
    :func:`_build_rewrite_prompt`：不回灌内部状态字段（rewrite_loop 与 argument 状态解耦）。
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
        arguments: list[Argument],
        citations: list[Source],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> str | None:
        envelope = self._get_chain().invoke(
            _build_rewrite_prompt(
                paragraph_id,
                paragraph_summary,
                arguments,
                citations,
                session_context,
                query_time_range,
            )
        )
        assert isinstance(envelope, _RewriteEnvelope)
        return envelope.rewritten_text or None
