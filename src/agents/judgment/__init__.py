"""裁决 Agent：judgment 五合一节点（PRD §5、ADR-0017、Slice 5）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + verdict 模型，
``agent.py`` 放裁决编排纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.judgment import judge_and_adjudicate, JudgmentLlmClient, ...`` 的外部
import 路径不变（拆分硬约束）。

Slice 5 五合一：检索之后的五节点（verification ReAct 取证 / hypothesis 取证 /
merge / impact / consistency）并入单一 ``judgment`` 节点——吃 citations 判 per-argument /
per-hypothesis 终态、再按序调 merge/impact/consistency 纯函数、整树写回。
"""

from agents.judgment.agent import judge_and_adjudicate
from agents.judgment.contract import (
    ArgumentVerdictEntry,
    FakeJudgmentLlmClient,
    HypothesisVerdictEntry,
    JudgmentArgumentVerdict,
    JudgmentHypothesisVerdict,
    JudgmentLlmClient,
    JudgmentOutcome,
    JudgmentResult,
)

__all__ = [
    "JudgmentArgumentVerdict",
    "JudgmentHypothesisVerdict",
    "ArgumentVerdictEntry",
    "HypothesisVerdictEntry",
    "JudgmentResult",
    "JudgmentOutcome",
    "JudgmentLlmClient",
    "FakeJudgmentLlmClient",
    "judge_and_adjudicate",
]
