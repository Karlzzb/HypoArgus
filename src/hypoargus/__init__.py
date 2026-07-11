"""HypoArgus — 论证驱动型文档修订多智能体系统。

本包提供全局调度中枢与端到端骨架：纯文本 → 确定性段落切分 → 只读原文段落表 →
论证树（桩）→ 双线路（桩）→ 合并/影响/一致性（桩）→ HITL（桩）→ 逐字节回写 → 终稿。

核心承诺：无任何采纳改动时，终稿与原始输入逐字节完全一致。
"""

from hypoargus.agents import Agents, create_real_agents, create_stub_agents
from hypoargus.domain import ArgumentationNode, NodeStatus, NodeType
from hypoargus.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Gate,
    Hitl1Op,
)
from hypoargus.hitl1 import (
    confirm as hitl1_confirm,
)
from hypoargus.hypothesis import (
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
from hypoargus.hypothesis import hypothesize as hypothesize
from hypoargus.orchestrator import Orchestrator
from hypoargus.parser import (
    WEIGHT_RUBRIC,
    FakeLlmClient,
    LlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
    parse,
)
from hypoargus.partition import partition
from hypoargus.raw_store import RawParagraphStore
from hypoargus.retrieval import (
    ComplianceError,
    RetrievalConfig,
    RetrievalKind,
    RetrievalLayer,
    RetrievalResponse,
    Source,
    create_mock_retrieval_layer,
    redact_query,
    validate_request,
)
from hypoargus.tree_invariants import TreeInvariantError, validate_tree
from hypoargus.verification import (
    ConcludeStep,
    FakeVerifyLlmClient,
    SearchStep,
    VerifyLlmClient,
    VerifyStep,
    VerifyVerdict,
)
from hypoargus.verification import verify as verify
from hypoargus.writeback import writeback

__all__ = [
    "ArgumentationNode",
    "NodeStatus",
    "NodeType",
    "Orchestrator",
    "RawParagraphStore",
    "partition",
    "writeback",
    # 智能体契约与装配（issue #1/#2）。
    "Agents",
    "create_stub_agents",
    "create_real_agents",
    # 论证结构解析 + HITL-1 结构确认（issue #2）。
    "LlmClient",
    "FakeLlmClient",
    "ParagraphView",
    "ParsedNodeProposal",
    "ParseResult",
    "WEIGHT_RUBRIC",
    "parse",
    "Hitl1Action",
    "Hitl1Decision",
    "Hitl1Gate",
    "Hitl1Op",
    "FakeHitl1Gate",
    "hitl1_confirm",
    "TreeInvariantError",
    "validate_tree",
    # 公共检索层（PRD §6、issue #3）：契约 + Mock 桩。
    "ComplianceError",
    "RetrievalConfig",
    "RetrievalKind",
    "RetrievalLayer",
    "RetrievalResponse",
    "Source",
    "create_mock_retrieval_layer",
    "redact_query",
    "validate_request",
    # 线路 1 · 体检 Agent（PRD §5、issue #4）：ReAct 循环 + LLM seam。
    "VerifyVerdict",
    "SearchStep",
    "ConcludeStep",
    "VerifyStep",
    "VerifyLlmClient",
    "FakeVerifyLlmClient",
    "verify",
    # 线路 2 · 开药 Agent（PRD §5、issue #5）：投机生成 + 逐条取证 + LLM seam。
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
