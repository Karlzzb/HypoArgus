"""线路 1 · 体检 Agent（PRD §5、issue #4、ADR-0011）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + ReAct 步模型，
``agent.py`` 放纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.verification import verify, VerifyLlmClient, FakeVerifyLlmClient, ...``
的外部 import 路径不变（拆分硬约束）。
"""

from agents.verification.agent import verify
from agents.verification.contract import (
    ConcludeStep,
    FakeVerifyLlmClient,
    SearchStep,
    VerifyLlmClient,
    VerifyStep,
    VerifyVerdict,
)

__all__ = [
    "VerifyVerdict",
    "SearchStep",
    "ConcludeStep",
    "VerifyStep",
    "VerifyLlmClient",
    "FakeVerifyLlmClient",
    "verify",
]
