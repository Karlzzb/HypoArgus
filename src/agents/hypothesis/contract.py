"""开药 Agent 契约：投机生成 + 取证结构化步 + LLM Protocol + 离线 Fake 桩（PRD §5、issue #5）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + discriminated-union 步模型，
``agent.py`` 放两阶段纯函数。``HypothesisLlmClient`` 为注入 seam：真实适配器生成用
``with_structured_output(list[HypothesisProposal])``、取证用
``with_structured_output(HypothesisVerifyStep)``（dev-guide §6.3）；本切片提供
``FakeHypothesisLlmClient`` 供离线单测——provider-free、确定、可断言。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from domain import (
    ArgumentationNode,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
)
from infra.retrieval import RetrievalKind, Source

__all__ = [
    "HypothesisRelation",
    "HypothesisStatus",
    "Hypothesis",
    "HypothesisVerdict",
    "HypothesisProposal",
    "HypothesisSearchStep",
    "HypothesisConcludeStep",
    "HypothesisVerifyStep",
    "HypothesisLlmClient",
    "FakeHypothesisLlmClient",
]


# --------------------------------------------------------------------------- #
# 结构化 ReAct 步（取证 · discriminated union · dev-guide §6.3）
# --------------------------------------------------------------------------- #


class HypothesisVerdict(StrEnum):
    """假设取证终判（假设侧三态，ADR-0008/0011）。"""

    SUPPORTED = "supported"
    DOUBTFUL = "doubtful"
    REFUTED = "refuted"


class HypothesisProposal(BaseModel):
    """投机生成 seam 的产出：一条待取证的假设（尚无 status，status 由取证赋予）。

    ``relation`` 单一（ADR-0007）；``confidence`` 0-1，仅排序、不裁决（ADR-0008）。
    """

    text: str
    relation: HypothesisRelation
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class HypothesisSearchStep(BaseModel):
    """取证继续检索：调整检索词、选通道、附通道特有参数。

    通道为 ``network`` 或 ``knowledge_base``（镜像体检 #4 的正向检索范围）；结构化数据检索
    （``structured``）不在取证通道内，:class:`infra.retrieval_tool.RetrievalTool` 拒绝 →
    假设落 ``doubtful``。
    """

    action: Literal["search"] = "search"
    query: str
    channel: RetrievalKind
    domain: str | None = None  # 网络检索白名单域名
    user_id: str | None = None  # 知识库检索授权用户
    type_filter: str | None = None
    time_filter: str | None = None


class HypothesisConcludeStep(BaseModel):
    """就地结论：写回取证终判 + 简短理由（可复算、可解释）。"""

    action: Literal["conclude"] = "conclude"
    verdict: HypothesisVerdict
    reasoning: str = ""


HypothesisVerifyStep = Annotated[
    HypothesisSearchStep | HypothesisConcludeStep, Field(discriminator="action")
]
"""单步取证决策：检索或结论（按 ``action`` 判别）。"""


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class HypothesisLlmClient(Protocol):
    """开药 LLM seam。

    - :meth:`propose`：节点 → 0..N 条假设提案（投机生成，不读体检结论/检索）。
    - :meth:`next_verify_step`：假设文本 + 已累积 observations → 下一步取证决策。

    真实适配器生成用 ``with_structured_output(list[HypothesisProposal])``、取证用
    ``with_structured_output(HypothesisVerifyStep)`` 保证结构合法（dev-guide §6.3）。
    本 seam 不绑任何 provider。
    """

    def propose(self, node: ArgumentationNode) -> list[HypothesisProposal]: ...

    def next_verify_step(
        self, hypothesis_text: str, observations: list[Source]
    ) -> HypothesisSearchStep | HypothesisConcludeStep: ...


class FakeHypothesisLlmClient:
    """离线开药 LLM 桩。provider-free、确定（供单测）。

    生成（``propose``）：
    - ``propose_factory``：``callable(node) -> list[HypothesisProposal]``，可据节点决策。
    - 二者皆无 → 返回 ``[]``（无假设，等价于不生成的最简桩）。

    取证（``next_verify_step``）：
    - ``verify_factory``：``callable(hypothesis_text, observations) -> HypothesisVerifyStep``，
      可据假设文本与累积 observations 动态决策（多假设断言用此）。
    - ``verify_script``：``list[HypothesisVerifyStep]``，按序、跨所有取证调用全局消费
      （用尽即抛，模拟 LLM 未给结论 → 由迭代硬上限兜底为 ``doubtful``）。
    - 二者皆无 → 立即结论 ``supported``（无检索，等价于取证通过的最简桩）。
    """

    def __init__(
        self,
        *,
        propose_factory: Callable[[ArgumentationNode], list[HypothesisProposal]]
        | None = None,
        verify_factory: Callable[[str, list[Source]], HypothesisSearchStep | HypothesisConcludeStep]
        | None = None,
        verify_script: list[HypothesisSearchStep | HypothesisConcludeStep] | None = None,
    ) -> None:
        self._propose_factory = propose_factory
        self._verify_factory = verify_factory
        self._verify_script = list(verify_script) if verify_script is not None else None
        self._verify_cursor = 0

    def propose(self, node: ArgumentationNode) -> list[HypothesisProposal]:
        if self._propose_factory is not None:
            return self._propose_factory(node)
        return []

    def next_verify_step(
        self, hypothesis_text: str, observations: list[Source]
    ) -> HypothesisSearchStep | HypothesisConcludeStep:
        if self._verify_factory is not None:
            return self._verify_factory(hypothesis_text, observations)
        if self._verify_script is not None:
            if self._verify_cursor >= len(self._verify_script):
                raise RuntimeError("verify script 用尽未给结论（应由迭代硬上限兜底）")
            step = self._verify_script[self._verify_cursor]
            self._verify_cursor += 1
            return step
        return HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED)
