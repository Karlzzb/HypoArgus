"""线路 2 · 开药 Agent：投机生成 + 逐条取证（PRD §5、issue #5、ADR-0002/0007/0008/0011）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + 取证步模型，
``agent.py`` 放两阶段纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.hypothesis import hypothesize, HypothesisLlmClient, Hypothesis, ...``
的外部 import 路径不变（拆分硬约束）。
"""

from agents.hypothesis.agent import hypothesize
from agents.hypothesis.contract import (
    FakeHypothesisLlmClient,
    Hypothesis,
    HypothesisConcludeStep,
    HypothesisLlmClient,
    HypothesisProposal,
    HypothesisRelation,
    HypothesisSearchStep,
    HypothesisStatus,
    HypothesisVerdict,
    HypothesisVerifyStep,
)

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
    "hypothesize",
]
