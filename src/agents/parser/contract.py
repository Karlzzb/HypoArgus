"""解析 Agent 契约：seam 数据形状 + LLM Protocol + 离线 Fake 桩。

ADR-0014：有 seam 的 Agent 拆为子包——``contract.py`` 放 Protocol + Fake 桩 + 结构化 I/O
模型，``agent.py`` 放纯函数。本模块承载与具体 provider / LLM 无关的「契约面」，可被
``agent.py`` 与外部测试独立 import。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from domain import DEFAULT_QUERY_TIME_RANGE, Argument, ArgumentType, TimeRange

__all__ = [
    "WEIGHT_RUBRIC",
    "ParagraphView",
    "ParsedNodeProposal",
    "ParseResult",
    "ParseOutput",
    "LlmClient",
    "FakeLlmClient",
]


# 明文权重 rubric（ADR-0013）：解析 Agent 建树时按此为每节点赋 argument_weight (0-100)。
# 真实 LlmClient 适配器应把此 rubric 写进解析 prompt；解析器只校验值域。
WEIGHT_RUBRIC = """\
argument_weight (0-100) 赋值 rubric：
- evidence：带数据/引源的直接论据 80-100；泛泛断言 30-50。
- sub_claim：50-70（视支撑论据强度）。
- main_claim：70-90（高阶论点）。
- qualification：40-60（限定条件，调节力度）。
- background / evaluation（影子）：0（不参与传导）。
"""


class ParagraphView(BaseModel):
    """喂给 LLM 的一段原文视图：``paragraph_id`` + 该段文本（非整篇）。

    LLM 据此识别该段内的论证节点及其父子归属；解析器随后用 ``paragraph_id`` 从
    只读表逐字节拷回 ``content``——LLM 输出永不成为节点文本。
    """

    paragraph_id: str
    text: str


class ParsedNodeProposal(BaseModel):
    """LLM 提出的单个节点（结构化输出，dev-guide §6.3）。

    不含 ``content``：节点文本由解析器从只读表逐字节拷回，LLM 无权改写。
    ``parent_index`` 指向 LLM 输出列表中的父节点位置（稳定、由 LLM 控制排序），
    解析器解析为 ``argument_id``。``argument_weight`` 不加 pydantic 边界——真实 LLM
    偶尔返回 101，越界由解析器 :func:`agents.parser.agent._clamp_weight` 宽容 clamp（不整体崩溃）。
    """

    paragraph_id: str
    argument_type: ArgumentType
    parent_index: int | None = None
    argument_weight: int = 0


class ParseResult(BaseModel):
    """LLM 解析输出：一组节点提议 + 时间范围（桩）+ 段落摘要。

    ``query_time_range`` 当前为桩（``DEFAULT_QUERY_TIME_RANGE``，默认 2025–2026）——agent 端
    不真实依赖 LLM 识别时间，真实 LLM 时间识别属后续切片（PRD Out of Scope）。
    ``paragraph_summaries`` 为 ``paragraph_id → 摘要``，由同一次 LLM 调用顺产，供
    hypothesis_propose / rewrite_loop 读取（避免逐点喂入时上下文爆炸，ADR-0021）。
    """

    proposals: list[ParsedNodeProposal] = Field(default_factory=list)
    query_time_range: TimeRange = Field(default_factory=lambda: DEFAULT_QUERY_TIME_RANGE.model_copy(deep=True))
    paragraph_summaries: dict[str, str] = Field(default_factory=dict)


class ParseOutput(BaseModel):
    """``parse+partition`` 节点的产出（agent 铸造，写回 ``PipelineState`` 三 channel）。

    - ``argument_tree``：解析器铸造成的初始论证树（逐字节拷回 ``content``、LLM 无权改写）。
    - ``query_time_range``：桩值（agent 注入 ``DEFAULT_QUERY_TIME_RANGE``，不真实调 LLM 识别）。
    - ``paragraph_summaries``：从 LLM ``ParseResult`` 顺产的 ``paragraph_id → 摘要``。
    """

    argument_tree: list[Argument] = Field(default_factory=list)
    query_time_range: TimeRange = Field(default_factory=lambda: DEFAULT_QUERY_TIME_RANGE.model_copy(deep=True))
    paragraph_summaries: dict[str, str] = Field(default_factory=dict)


class LlmClient(Protocol):
    """解析 LLM seam：按段输入 → 结构化节点提议。

    真实适配器用 ``with_structured_output(ParseResult)`` 保证结构合法（dev-guide §6.3），
    并把 :data:`WEIGHT_RUBRIC` 写进解析 prompt。本 seam 不绑任何 provider。
    """

    def parse(self, paragraphs: list[ParagraphView]) -> ParseResult: ...


class FakeLlmClient:
    """离线 LLM 桩：按注入的提议或工厂返回 :class:`ParseResult`。

    provider-free、确定——解析器逻辑可完全离线单测。两种注入方式：

    - ``factory``：``callable(paragraphs) -> ParseResult``，可据输入动态生成提议；
    - ``result``：固定 :class:`ParseResult`（或 :class:`ParsedNodeProposal` 列表），
      忽略输入恒返回之。

    二者皆无则返回空提议（解析器将把每段归为影子节点，等价于解析桩）。
    """

    def __init__(
        self,
        result: ParseResult | list[ParsedNodeProposal] | None = None,
        *,
        factory: Callable[[list[ParagraphView]], ParseResult] | None = None,
    ) -> None:
        self._factory = factory
        if result is None:
            self._result: ParseResult = ParseResult()
        elif isinstance(result, ParseResult):
            self._result = result
        else:
            self._result = ParseResult(proposals=list(result))

    def parse(self, paragraphs: list[ParagraphView]) -> ParseResult:
        if self._factory is not None:
            return self._factory(paragraphs)
        return self._result.model_copy(deep=True)
