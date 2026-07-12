"""HypoArgus — 论证驱动型文档修订多智能体系统。

本包提供全局调度中枢与端到端骨架：纯文本 → 确定性段落切分 → 只读原文段落表 →
论证树（桩）→ 双线路（桩）→ 合并/影响/一致性（桩）→ HITL（桩）→ 逐字节回写 → 终稿。

核心承诺：无任何采纳改动时，终稿与原始输入逐字节完全一致。
"""

from hypoargus.agents import Agents, create_real_agents, create_stub_agents
from hypoargus.consistency import (
    DUPLICATE_QUALIFICATION_TAG,
    MIXED_PARAGRAPH_KIND_TAG,
    MULTI_MAIN_CLAIM_TAG,
    MULTI_PRIMARY_PER_PARAGRAPH_TAG,
    consistency,
)
from hypoargus.domain import ArgumentationNode, MergeAction, MergeDecision, NodeStatus, NodeType
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
from hypoargus.hitl2 import (
    AdoptOp,
    CandidateView,
    ConservativeHitl2Gate,
    EditContentOp,
    FakeHitl2Gate,
    Hitl2Action,
    Hitl2Decision,
    Hitl2Gate,
    Hitl2GateError,
    Hitl2Op,
    Hitl2Review,
    NodeReview,
    RejectOp,
)
from hypoargus.hitl2 import build_review as build_hitl2_review
from hypoargus.hitl2 import (
    confirm as hitl2_confirm,
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
from hypoargus.impact import (
    INVALID_RATIO_THRESHOLD,
    WEAKEN_RATIO_THRESHOLD,
    WEAKENING_TAG,
    ImpactVerdict,
    ResidualSupport,
    compute_residual_support,
    impact,
    verdict_for_ratio,
)
from hypoargus.merge import apply_partial_updates, merge
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
from hypoargus.writeback import (
    SUPPLEMENT_AUDIT_MARKER,
    WRITEBACK_ERROR_TAG,
    WritebackResult,
    writeback,
)

__all__ = [
    "ArgumentationNode",
    "NodeStatus",
    "NodeType",
    "Orchestrator",
    "RawParagraphStore",
    "partition",
    "writeback",
    # 修订回写 Agent（PRD §11、issue #10）：段落原子缝合·幂等·纯函数 seam。
    "WritebackResult",
    "SUPPLEMENT_AUDIT_MARKER",
    "WRITEBACK_ERROR_TAG",
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
    # HITL-2 修订确认硬闸门（PRD §10 节点 2、issue #9、ADR-0010/0011）。
    "Hitl2Action",
    "Hitl2Decision",
    "Hitl2Op",
    "AdoptOp",
    "RejectOp",
    "EditContentOp",
    "Hitl2Gate",
    "FakeHitl2Gate",
    "ConservativeHitl2Gate",
    "Hitl2Review",
    "NodeReview",
    "CandidateView",
    "Hitl2GateError",
    "build_hitl2_review",
    "hitl2_confirm",
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
    # 双轨合并算子（PRD §7、issue #6）：确定性 12 格矩阵纯函数。
    "MergeAction",
    "MergeDecision",
    "merge",
    "apply_partial_updates",
    # 影响传导 Agent（PRD §8、issue #7）：串行·不产文本·剩余支撑率失效判定纯函数。
    "ImpactVerdict",
    "ResidualSupport",
    "WEAKENING_TAG",
    "INVALID_RATIO_THRESHOLD",
    "WEAKEN_RATIO_THRESHOLD",
    "compute_residual_support",
    "verdict_for_ratio",
    "impact",
    # 一致性校验 Agent（PRD §9、issue #8）：批注门禁·单次扫描·只贴 issue_tags 纯函数。
    "MIXED_PARAGRAPH_KIND_TAG",
    "MULTI_PRIMARY_PER_PARAGRAPH_TAG",
    "MULTI_MAIN_CLAIM_TAG",
    "DUPLICATE_QUALIFICATION_TAG",
    "consistency",
]
