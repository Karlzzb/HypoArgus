"""体检 Agent 契约：ReAct 结构化步 + LLM Protocol + 离线 Fake 桩（PRD §5、issue #4）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + discriminated-union 步模型，
``agent.py`` 放 ReAct 纯函数。``VerifyLlmClient`` 为注入 seam：真实适配器用
``with_structured_output(VerifyStep)`` 保证结构合法（dev-guide §6.3）；本切片提供
``FakeVerifyLlmClient`` 供离线单测——provider-free、确定、可断言。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from domain import ArgumentationNode
from infra.retrieval import RetrievalKind, Source

__all__ = [
    "VerifyVerdict",
    "SearchStep",
    "ConcludeStep",
    "VerifyStep",
    "VerifyLlmClient",
    "FakeVerifyLlmClient",
]


# --------------------------------------------------------------------------- #
# 结构化 ReAct 步（discriminated union · dev-guide §6.3）
# --------------------------------------------------------------------------- #


class VerifyVerdict(StrEnum):
    """体检终判（原文侧三态，ADR-0008/0011）。"""

    CREDIBLE = "credible"
    DOUBTFUL = "doubtful"
    ERROR = "error"


class SearchStep(BaseModel):
    """继续检索：调整检索词、选通道、附通道特有参数。

    ``channel`` 为 ``network`` 或 ``knowledge_base``（体检聚焦查清事实的两类正向检索）；
    结构化数据检索（``structured``）不在体检通道内，:class:`infra.retrieval_tool.RetrievalTool`
    拒绝 → 节点 ``error``。
    """

    action: Literal["search"] = "search"
    query: str
    channel: RetrievalKind
    domain: str | None = None  # 网络检索白名单域名
    user_id: str | None = None  # 知识库检索授权用户
    type_filter: str | None = None
    time_filter: str | None = None


class ConcludeStep(BaseModel):
    """就地结论：写回终判 + 简短理由（可复算、可解释，ADR-0013 rationale 同精神）。"""

    action: Literal["conclude"] = "conclude"
    verdict: VerifyVerdict
    reasoning: str = ""


VerifyStep = Annotated[SearchStep | ConcludeStep, Field(discriminator="action")]
"""单步 ReAct 决策：检索或结论（按 ``action`` 判别）。"""


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class VerifyLlmClient(Protocol):
    """体检 LLM seam：节点 + 已累积 observations → 下一步 ReAct 决策。

    真实适配器用 ``with_structured_output(VerifyStep)`` 保证结构合法（dev-guide §6.3），
    并据节点 ``content`` 与 observations 推理。本 seam 不绑任何 provider。
    """

    def next_step(
        self, node: ArgumentationNode, observations: list[Source]
    ) -> SearchStep | ConcludeStep: ...


class FakeVerifyLlmClient:
    """离线 ReAct LLM 桩。provider-free、确定（供单测）。

    三种注入：
    - ``script``：``list[VerifyStep]``，按序返回（忽略 node/observations），用尽即抛
      （模拟 LLM 未给结论 → 由迭代硬上限兜底为 ``error``）。
    - ``factory``：``callable(node, observations) -> VerifyStep``，可据输入动态决策。
    - 二者皆无 → 立即结论 ``credible``（无检索，等价于不校验的最简桩）。
    """

    def __init__(
        self,
        script: list[SearchStep | ConcludeStep] | None = None,
        *,
        factory: Callable[[ArgumentationNode, list[Source]], SearchStep | ConcludeStep]
        | None = None,
    ) -> None:
        self._factory = factory
        self._script = list(script) if script is not None else None
        self._cursor = 0

    def next_step(
        self, node: ArgumentationNode, observations: list[Source]
    ) -> SearchStep | ConcludeStep:
        if self._factory is not None:
            return self._factory(node, observations)
        if self._script is not None:
            if self._cursor >= len(self._script):
                raise RuntimeError("script 用尽未给结论（应由迭代硬上限兜底）")
            step = self._script[self._cursor]
            self._cursor += 1
            return step
        return ConcludeStep(verdict=VerifyVerdict.CREDIBLE)
