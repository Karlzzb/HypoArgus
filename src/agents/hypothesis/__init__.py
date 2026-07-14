"""线路 2 · 开药 Agent：仅投机生成（PRD §5、issue #5、Slice 3 重构）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + proposal 模型，
``agent.py`` 放 propose 纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.hypothesis import propose_hypotheses, HypothesisLlmClient, Hypothesis, ...``
的外部 import 路径不变（拆分硬约束）。

Slice 3 重构：hypothesis 节点重定义为 hypothesis_propose——仅 propose、不取证、产 pending
假说。原 ``hypothesize``（propose + verify）改名为 ``propose_hypotheses``；取证步模型
（verdict / search / conclude / verify-step）移除（推迟到 Slice 5 的 judgment seam）。
"""

from agents.hypothesis.agent import propose_hypotheses
from agents.hypothesis.contract import (
    FakeHypothesisLlmClient,
    Hypothesis,
    HypothesisLlmClient,
    HypothesisProposal,
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
    "propose_hypotheses",
]
