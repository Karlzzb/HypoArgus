"""裁决 Agent 契约：judgment seam + LLM Protocol + 离线 Fake 桩（PRD §5、ADR-0019、Slice 5）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + verdict 模型，``agent.py``
放裁决编排纯函数。``JudgmentLlmClient`` 为注入 seam：真实适配器用
``with_structured_output(_JudgmentEnvelope)``（dev-guide §6.3）；本切片提供
``FakeJudgmentLlmClient`` 供离线单测——provider-free、确定、可断言。

Slice 5 五合一（ADR-0019）：检索（retrieval 节点产 citations）之后的五节点（verification
ReAct 取证 / hypothesis 取证 / merge / impact / consistency）并入单一 ``judgment`` 节点。
本 seam 吃 ``citations`` 判 per-argument / per-hypothesis 终态，再由 ``agent.py`` 按序调用
**不动**的 ``merge`` / ``impact`` / ``consistency`` 纯函数、整树写回 ``argument_tree``
（单写者，故裁撤 ``argument_credibility`` partial channel）。

信封形态：扁平 ``JudgmentResult``（``argument_verdicts`` + ``hypothesis_verdicts`` 两 list），
**不**用判别联合 ``oneOf``——延续 ``infra/llm_adapters.py`` 既有风格，规避结构化输出在
判别联合下的不稳。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from domain import Argument, Hypothesis, ParagraphRecord, SessionContext, TimeRange
from infra.retrieval import Source

__all__ = [
    "JudgmentArgumentVerdict",
    "JudgmentHypothesisVerdict",
    "ArgumentVerdictEntry",
    "HypothesisVerdictEntry",
    "JudgmentResult",
    "JudgmentOutcome",
    "JudgmentLlmClient",
    "FakeJudgmentLlmClient",
]


# --------------------------------------------------------------------------- #
# verdict 模型（裁决 seam 的产出）
# --------------------------------------------------------------------------- #


class JudgmentArgumentVerdict(StrEnum):
    """原文侧 per-argument 终态裁决（↔ :class:`domain.ArgumentStatus` 三态，ADR-0008/0011）。

    覆盖范围内的 claim / evidence 节点由 judgment 据 citations 判 ``credible / doubtful /
    error``；未覆盖节点（qualification / 影子）不裁决、保持 ``unverified``。
    """

    CREDIBLE = "credible"
    DOUBTFUL = "doubtful"
    ERROR = "error"


class JudgmentHypothesisVerdict(StrEnum):
    """假设侧 per-hypothesis 终态裁决（↔ :class:`domain.HypothesisStatus` 三态，ADR-0008）。

    hypothesis_propose 产 ``pending`` 假说；judgment 据 citations 取证后落终态
    ``supported / doubtful / refuted``。未裁决假说保持 ``pending``（保守、不激活）。
    """

    SUPPORTED = "supported"
    DOUBTFUL = "doubtful"
    REFUTED = "refuted"


class ArgumentVerdictEntry(BaseModel):
    """单条 per-argument 裁决：节点 id + 终态。"""

    argument_id: str
    verdict: JudgmentArgumentVerdict


class HypothesisVerdictEntry(BaseModel):
    """单条 per-hypothesis 裁决：假设 id + 终态。"""

    hypothesis_id: str
    verdict: JudgmentHypothesisVerdict


class JudgmentResult(BaseModel):
    """裁决 seam 的扁平信封产出。

    两个 list 各承载一类终态裁决；缺失 id 即「未裁决」（保守保留原态）。扁平结构而非
    判别联合 ``oneOf``——延续 ``infra/llm_adapters.py`` 风格，规避结构化输出在判别联合下
    的不稳。默认空 → 全 KEEP → 未触达 → 逐字节忠实（tracer bullet 承诺）。
    """

    argument_verdicts: list[ArgumentVerdictEntry] = Field(default_factory=list)
    hypothesis_verdicts: list[HypothesisVerdictEntry] = Field(default_factory=list)


class JudgmentOutcome(BaseModel):
    """judgment 节点写回 state 的两 channel：裁决后的整树 + 终态化后的假说 partial。

    ``argument_tree`` 为经 merge/impact/consistency 串联后的整树（单写者=judgment、整树
    写回 ``argument_tree`` channel）；``hypotheses`` 为终态化后的假说（status pending→终态），
    供 HITL-2 读 ``candidate_hypotheses`` 与 merge 矩阵回看。
    """

    argument_tree: list[Argument]
    hypotheses: dict[str, list[Hypothesis]] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class JudgmentLlmClient(Protocol):
    """裁决 LLM seam（Slice 5 五合一）。

    - :meth:`judge`：吃 ``argument_tree`` + ``hypotheses`` + ``citations`` + ``paragraph_list``
      + 贯穿背景（``session_context`` / ``query_time_range``），产 per-argument / per-hypothesis
      终态裁决（:class:`JudgmentResult`）。

    真实适配器用 ``with_structured_output(_JudgmentEnvelope)`` 保证结构合法（dev-guide §6.3），
    并据段 ``original_content`` + 节点 ``argument_type`` + 假说 ``text`` + citation 片段推理。本 seam
    不绑任何 provider。输入压缩铁律（PRD §7）：T-02 prompt 按段聚合节点——每段 ``original_content``
    出现一次、节点只列 ``argument_id`` / ``argument_type``（不再逐节点 ``Argument.content``）+ 假说
    ``text`` + citation 片段 + 背景；**不回灌** ``status`` / ``argument_weight`` / ``parent_id`` /
    ``children_ids`` / ``issue_tags`` / ``merge_decision``——这些由 ``agent.py`` 在调用前后管理、不进 LLM。
    """

    def judge(
        self,
        argument_tree: list[Argument],
        hypotheses: dict[str, list[Hypothesis]],
        citations: dict[str, list[Source]],
        paragraph_list: list[ParagraphRecord],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> JudgmentResult: ...


class FakeJudgmentLlmClient:
    """离线裁决 LLM 桩。provider-free、确定（供单测）。

    - ``judge_factory``：``callable(argument_tree, hypotheses, citations, paragraph_list,
      session_context, query_time_range) -> JudgmentResult``，可据输入动态决策。
    - 无 → 返回**空** :class:`JudgmentResult`（无裁决 → 全 KEEP → 未触达 → 逐字节忠实，
      tracer bullet 承诺）。
    """

    def __init__(
        self,
        *,
        judge_factory: Callable[
            [list[Argument], dict[str, list[Hypothesis]], dict[str, list[Source]],
             list[ParagraphRecord], SessionContext, TimeRange],
            JudgmentResult,
        ]
        | None = None,
    ) -> None:
        self._judge_factory = judge_factory

    def judge(
        self,
        argument_tree: list[Argument],
        hypotheses: dict[str, list[Hypothesis]],
        citations: dict[str, list[Source]],
        paragraph_list: list[ParagraphRecord],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> JudgmentResult:
        if self._judge_factory is not None:
            return self._judge_factory(
                argument_tree,
                hypotheses,
                citations,
                paragraph_list,
                session_context,
                query_time_range,
            )
        return JudgmentResult()
