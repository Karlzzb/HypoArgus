"""开药 Agent 契约：投机生成 seam + LLM Protocol + 离线 Fake 桩（PRD §5、issue #5、Slice 3）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + proposal 模型，``agent.py``
放 propose 纯函数。``HypothesisLlmClient`` 为注入 seam：真实适配器用
``with_structured_output(_ProposalsEnvelope)``（dev-guide §6.3）；本切片提供
``FakeHypothesisLlmClient`` 供离线单测——provider-free、确定、可断言。

Slice 3（重构）：hypothesis 节点重定义为 **hypothesis_propose**——仅 ``propose``、不取证，
产 ``list[Hypothesis]``（status=pending）。既有 ``next_verify_step`` 取证职责移出（推迟到
Slice 5 的 judgment 节点重接），故取证步模型（verdict / search / conclude / verify-step
判别联合）从本 seam 删除。``propose`` 输入从单 ``Argument`` 改为
``(argument, paragraph_summary)``——逐 argument 调用、读段落摘要而非整段 content（PRD §7
输入压缩铁律）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from domain import (
    Argument,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
)

__all__ = [
    "HypothesisRelation",
    "HypothesisStatus",
    "Hypothesis",
    "HypothesisProposal",
    "HypothesisLlmClient",
    "FakeHypothesisLlmClient",
]


# --------------------------------------------------------------------------- #
# proposal 模型（投机生成 seam 的产出）
# --------------------------------------------------------------------------- #


class HypothesisProposal(BaseModel):
    """投机生成 seam 的产出：一条待取证假设（status 由 propose 节点置 pending、judgment 落终态）。

    ``relation`` 单一（ADR-0007）；``confidence`` 0-1，仅排序、不裁决（ADR-0008）。
    """

    text: str
    relation: HypothesisRelation
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class HypothesisLlmClient(Protocol):
    """开药 LLM seam（Slice 3 重构后仅 propose）。

    - :meth:`propose`：``(argument, paragraph_summary)`` → 0..N 条假设提案
      （投机生成，不读体检结论/检索；读段落摘要而非整段 content）。

    真实适配器用 ``with_structured_output(_ProposalsEnvelope)`` 保证结构合法（dev-guide §6.3）。
    本 seam 不绑任何 provider。取证（吃 citations 判终态）属 Slice 5 的 judgment seam，
    不在此处。
    """

    def propose(
        self, argument: Argument, paragraph_summary: str
    ) -> list[HypothesisProposal]: ...


class FakeHypothesisLlmClient:
    """离线开药 LLM 桩。provider-free、确定（供单测）。

    生成（``propose``）：
    - ``propose_factory``：``callable(argument, paragraph_summary) -> list[HypothesisProposal]``，
      可据节点与段落摘要决策。
    - 无 → 返回 ``[]``（无假设，等价于不生成的最简桩）。
    """

    def __init__(
        self,
        *,
        propose_factory: Callable[[Argument, str], list[HypothesisProposal]]
        | None = None,
    ) -> None:
        self._propose_factory = propose_factory

    def propose(self, argument: Argument, paragraph_summary: str) -> list[HypothesisProposal]:
        if self._propose_factory is not None:
            return self._propose_factory(argument, paragraph_summary)
        return []
