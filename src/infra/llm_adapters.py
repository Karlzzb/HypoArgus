"""真实 LLM adapter：三个 LLM seam 的第二 adapter（DashScope / qwen-max 等 OpenAI-compatible）。

contract 层（``LlmClient`` / ``VerifyLlmClient`` / ``HypothesisLlmClient`` Protocol）provider-free；
本模块把注入的 :class:`langchain_core.language_models.BaseChatModel` 经
``with_structured_output`` 绑到各 seam 的结构化输出契约（dev-guide §6.3）。

设计要点：

- **provider 无关**：构造器收任意 ``BaseChatModel``（DashScope / OpenAI / 自建网关 / 测试 fake）。
- **懒构建**：结构化链在首次 invoke 时才绑定，不在构造期触碰 provider 特性——故测试用 fake
  不会在构造期炸；真实 provider 在首次调用时绑定。
- **判别联合走单 schema 信封**：``VerifyStep`` / ``HypothesisVerifyStep`` 虽是 contract 导出的
  判别联合（``Annotated[... | ..., Field(discriminator="action")]``），但部分 OpenAI-compatible
  网关对 ``oneOf`` / 判别联合的 function-calling schema 支持不稳。为最大化兼容性，本 adapter
  用一个扁平信封 schema（``action`` + 各分支可选字段）喂 LLM、再 ``to_step()`` 映射回 contract
  的判别类——逻辑等价、provider 更稳。如需切回 contract 原始判别联合，把信封换成
  ``with_structured_output(VerifyStep, ...)`` 即可（schema 形状不变）。
- 任何结构化失败（LLM 抛、schema 非法、provider 不支持 method）由各 stage 的 ``_guarded``
  兜底（单向向前、就地降级，见 DEVELOPMENT.md §5）。
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from agents.hypothesis import (
    HypothesisConcludeStep,
    HypothesisProposal,
    HypothesisSearchStep,
    HypothesisVerdict,
)
from agents.parser import WEIGHT_RUBRIC, ParagraphView, ParseResult
from agents.verification import ConcludeStep, SearchStep, VerifyVerdict
from domain import Argument
from infra.retrieval import RetrievalKind, Source

__all__ = [
    "QwenParseLlmClient",
    "QwenVerifyLlmClient",
    "QwenHypothesisLlmClient",
]


# --------------------------------------------------------------------------- #
# prompt 构造（纯函数）
# --------------------------------------------------------------------------- #


def _format_paragraphs(paragraphs: list[ParagraphView]) -> str:
    return "\n\n".join(f"[{p.paragraph_id}] {p.text}" for p in paragraphs)


def _format_observations(observations: list[Source]) -> str:
    if not observations:
        return "（暂无已累积的检索素材）"
    return "\n".join(
        f"- [{o.kind.value} | {o.origin}] {o.title or ''}\n  {o.snippet}"
        for o in observations
    )


def _build_parse_prompt(paragraphs: list[ParagraphView]) -> str:
    return (
        "你是论证结构解析器。下列文本已按段给出（形如 [paragraph_id] 文本）。"
        "识别每段内的论证节点及其父子归属，输出节点列表。\n"
        "规则：一节点不跨段（paragraph_id 取自其所在段）；parent_index 指向你输出列表"
        "中父节点的位置（根节点为 null）；argument_weight 按下方 rubric 赋值。\n\n"
        f"{WEIGHT_RUBRIC}\n\n"
        f"段落：\n{_format_paragraphs(paragraphs)}"
    )


def _build_verify_prompt(argument: Argument, observations: list[Source]) -> str:
    return (
        "你是事实验证 ReAct 决策器。对给定节点，每步做一个极窄结构化决策：\n"
        "- action=search：继续检索，给 query + channel（network 或 knowledge_base；"
        "structured 不在体检通道）。network 须给 domain 白名单域名。\n"
        "- action=conclude：就地结论，给 verdict（credible/doubtful/error）+ reasoning。"
        "查到明确比对素材即可结论。\n\n"
        f"节点 [type={argument.argument_type.value}] 内容：\n{argument.content}\n\n"
        f"已累积检索素材：\n{_format_observations(observations)}"
    )


def _build_propose_prompt(argument: Argument) -> str:
    return (
        "你是论证修订假设生成器。对给定节点投机生成 0..N 条**可证伪**的修订假设，"
        "每条恰好一种 relation（oppose=对立替换 / advance=递进改写 / expand=扩展追加）。"
        "confidence 0-1 仅用于排序、不参与裁决。无假设则返回空列表。\n\n"
        f"节点 [type={argument.argument_type.value}] 内容：\n{argument.content}"
    )


def _build_hypothesis_verify_prompt(
    hypothesis_text: str, observations: list[Source]
) -> str:
    return (
        "你是假设取证 ReAct 决策器。对给定假设文本，每步做一个极窄结构化决策：\n"
        "- action=search：继续检索取证，给 query + channel（network 或 knowledge_base）。\n"
        "- action=conclude：就地取证结论，给 verdict（supported/doubted/refuted）+ reasoning。\n\n"
        f"假设文本：\n{hypothesis_text}\n\n"
        f"已累积检索素材：\n{_format_observations(observations)}"
    )


# --------------------------------------------------------------------------- #
# 信封 schema（扁平单 schema，provider 兼容；to_step 映射回 contract 判别类）
# --------------------------------------------------------------------------- #


class _VerifyEnvelope(BaseModel):
    """体检单步信封：action + 各分支可选字段。"""

    action: Literal["search", "conclude"]
    query: str | None = None
    channel: str | None = None
    domain: str | None = None
    user_id: str | None = None
    type_filter: str | None = None
    time_filter: str | None = None
    verdict: str | None = None
    reasoning: str = ""

    def to_step(self) -> SearchStep | ConcludeStep:
        if self.action == "search":
            if self.query is None or self.channel is None:
                raise ValueError("SearchStep 缺 query/channel")
            return SearchStep(
                query=self.query,
                channel=RetrievalKind(self.channel),
                domain=self.domain,
                user_id=self.user_id,
                type_filter=self.type_filter,
                time_filter=self.time_filter,
            )
        verdict = (
            VerifyVerdict(self.verdict) if self.verdict else VerifyVerdict.ERROR
        )
        return ConcludeStep(verdict=verdict, reasoning=self.reasoning)


class _HypothesisVerifyEnvelope(BaseModel):
    """取证单步信封：action + 各分支可选字段。"""

    action: Literal["search", "conclude"]
    query: str | None = None
    channel: str | None = None
    domain: str | None = None
    user_id: str | None = None
    type_filter: str | None = None
    time_filter: str | None = None
    verdict: str | None = None
    reasoning: str = ""

    def to_step(self) -> HypothesisSearchStep | HypothesisConcludeStep:
        if self.action == "search":
            if self.query is None or self.channel is None:
                raise ValueError("HypothesisSearchStep 缺 query/channel")
            return HypothesisSearchStep(
                query=self.query,
                channel=RetrievalKind(self.channel),
                domain=self.domain,
                user_id=self.user_id,
                type_filter=self.type_filter,
                time_filter=self.time_filter,
            )
        verdict = (
            HypothesisVerdict(self.verdict)
            if self.verdict
            else HypothesisVerdict.DOUBTFUL
        )
        return HypothesisConcludeStep(verdict=verdict, reasoning=self.reasoning)


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


class QwenVerifyLlmClient:
    """体检 LLM seam 的真实 adapter（``VerifyLlmClient`` Protocol 的第二 adapter）。"""

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._chain: Any = None

    def _get_chain(self) -> Any:
        if self._chain is None:
            self._chain = self._chat.with_structured_output(
                _VerifyEnvelope, method=self._method
            )
        return self._chain

    def next_step(
        self, argument: Argument, observations: list[Source]
    ) -> SearchStep | ConcludeStep:
        envelope = self._get_chain().invoke(_build_verify_prompt(argument, observations))
        assert isinstance(envelope, _VerifyEnvelope)
        return envelope.to_step()


class QwenHypothesisLlmClient:
    """开药 LLM seam 的真实 adapter（``HypothesisLlmClient`` Protocol 的第二 adapter）。"""

    def __init__(
        self, chat_model: BaseChatModel, *, method: str = "function_calling"
    ) -> None:
        self._chat = chat_model
        self._method = method
        self._propose_chain: Any = None
        self._verify_chain: Any = None

    def _get_propose_chain(self) -> Any:
        if self._propose_chain is None:
            self._propose_chain = self._chat.with_structured_output(
                _ProposalsEnvelope, method=self._method
            )
        return self._propose_chain

    def _get_verify_chain(self) -> Any:
        if self._verify_chain is None:
            self._verify_chain = self._chat.with_structured_output(
                _HypothesisVerifyEnvelope, method=self._method
            )
        return self._verify_chain

    def propose(self, argument: Argument) -> list[HypothesisProposal]:
        envelope = self._get_propose_chain().invoke(_build_propose_prompt(argument))
        assert isinstance(envelope, _ProposalsEnvelope)
        return envelope.proposals

    def next_verify_step(
        self, hypothesis_text: str, observations: list[Source]
    ) -> HypothesisSearchStep | HypothesisConcludeStep:
        envelope = self._get_verify_chain().invoke(
            _build_hypothesis_verify_prompt(hypothesis_text, observations)
        )
        assert isinstance(envelope, _HypothesisVerifyEnvelope)
        return envelope.to_step()
