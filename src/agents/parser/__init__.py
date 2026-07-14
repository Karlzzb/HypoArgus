"""论证结构解析 Agent（PRD §4、issue #2）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + 结构化 I/O 模型，
``agent.py`` 放纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.parser import parse, LlmClient, FakeLlmClient, ...`` 的外部 import
路径不变（拆分硬约束）。
"""

from agents.parser.agent import parse
from agents.parser.contract import (
    WEIGHT_RUBRIC,
    FakeLlmClient,
    LlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseOutput,
    ParseResult,
)

__all__ = [
    "WEIGHT_RUBRIC",
    "ParagraphView",
    "ParsedNodeProposal",
    "ParseResult",
    "ParseOutput",
    "LlmClient",
    "FakeLlmClient",
    "parse",
]
