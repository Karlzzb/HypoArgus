"""逐段重写提议 Agent：rewrite_loop 节点（PRD §12、ADR-0017、Slice 6）。

ADR-0014 子包拆分：``contract.py`` 放 Protocol + Fake 桩 + outcome 模型，
``agent.py`` 放逐段提议纯函数。本 ``__init__`` re-export 两者的公开符号，保持
``from agents.rewrite_loop import propose_rewrites, RewriteLlmClient, ...`` 的外部
import 路径不变（拆分硬约束）。

Slice 6（ADR-0017）：judgment 之后、hitl2 之前新增 ``rewrite_loop`` 节点——对被触达段
（supported 假说 / 命中 citations）由 LLM 提议重写文本，产 ``proposed_rewrites``
（仅触达段），供 hitl2 逐段确认 / 编辑 / 驳回后拼装 ``final_document``。
"""

from agents.rewrite_loop.agent import propose_rewrites
from agents.rewrite_loop.contract import (
    FakeRewriteLlmClient,
    RewriteLlmClient,
    RewriteLoopOutcome,
)

__all__ = [
    "RewriteLlmClient",
    "FakeRewriteLlmClient",
    "RewriteLoopOutcome",
    "propose_rewrites",
]
