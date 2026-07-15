"""真实 V12 检索全链慢集成测试（``real_llm`` 标记）。

与 ``tests/test_real_llm_pipeline_e2e.py``（检索桩产空 citations、保守闸门下终稿逐字节
等于原文）互补——本测试驱动 **真实检索后端**（Slice 2 的 ``lazy_search_agent_runtime``：
``with_llm=False``、Volcano 全网检索、daemon worker loop 承载、进程级单例）：

真实解析 → 真实开药 → 真实检索（citations 非空）→ 真实裁决（据真实证据判终态）。

断言聚焦「citations 非空 + 可溯源 + judgment 非空裁决」，不对具体 citation 内容做脆弱
断言（网络结果不定）。需 ``DASHSCOPE_API_KEY``（解析 / 开药 / 裁决 LLM）+
``VOLCANO_SEARCH_API_KEY``（全网检索 → 网络类 citations）+ ``BISHENG_BASE_URL``（知识库后端；
内网地址，缺失或不可达时 V12 降级、Volcano 网络类 citations 仍非空）+ 网络；凭证缺则整模块
跳过，离线质量门默认 deselected（``pytest -m "not real_llm"``）。

tracer bullet（Story 17）由 ``test_real_llm_pipeline_e2e.py``（保守闸门 → 终稿逐字节等于原文；
Slice 2 已把 ``lazy_search_agent_runtime`` 接入 ``run_real_pipeline``，故凭证齐时该测试亦 exercising
真实检索，未触达 / 未配置仍逐字节还原）与 ``tests/test_retrieval_adapter.py`` 的
``test_tracer_bullet_real_adapter_empty_output_keeps_byte_identity`` 共守，此处不重复。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from real_papers import REAL_PAPER_CASES

from agents.assembly import create_real_agents
from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.hitl2 import ConservativeHitl2Gate
from agents.retrieval import lazy_search_agent_runtime
from domain import HypothesisStatus, SessionContext
from infra.llm_adapters import (
    QwenHypothesisLlmClient,
    QwenJudgmentLlmClient,
    QwenParseLlmClient,
    QwenRewriteLlmClient,
)
from infra.llm_provider import build_qwen_chat_model
from runtime.orchestrator import Orchestrator

_HAS_KEYS = bool(
    os.environ.get("DASHSCOPE_API_KEY")
    and os.environ.get("VOLCANO_SEARCH_API_KEY")
    and os.environ.get("BISHENG_BASE_URL")
)
pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not _HAS_KEYS,
        reason="needs DASHSCOPE_API_KEY + VOLCANO_SEARCH_API_KEY + BISHENG_BASE_URL + network",
    ),
]


# 选一篇最小真实论文（集成电路工程技术专业，~6.6KB）跑全链，控制 LLM 调用量与 Volcano/Bisheng
# 网络往返耗时。解析层已对全 9 篇深验（``test_real_llm_parse.py``）；此处只证真实检索后端在真实
# 数据上产非空可溯源 citations、驱动裁决判非空终态。
_E2E_PAPER: list[tuple[str, bytes]] = [
    (name, doc)
    for name, doc in REAL_PAPER_CASES
    if name == "paper_03_集成电路工程技术专业"
]


@pytest.fixture(scope="module")
def real_chat_model() -> Iterator[BaseChatModel]:
    """模块级共享真实 ``ChatOpenAI``（DashScope），避免每用例重建。

    timeout=120 / max_tokens=8192 与既有真实套件一致——大论文结构化输出留余量、缓解截断。
    """

    model = build_qwen_chat_model(timeout=120.0, max_tokens=8192)
    yield model


@pytest.fixture(scope="module")
def real_retrieval_pipeline_state(
    real_chat_model: BaseChatModel,
) -> dict[str, Any]:
    """跑一次真实全链（真实 LLM + 真实检索后端），返回终态 state 供两个断言用例共享。

    模块级复用 = 跨用例不每次重建 runtime / 不每次重跑全链（PRD §Q4 reuse、控成本）。
    HITL-1 保守 SKIP（不重放 parse、开药照跑）、HITL-2 保守全驳回（终稿逐字节等于原文、
    tracer bullet 底线不变）——但 retrieval / judgment 照实跑，citations 与终态裁决落 state。
    """

    name, doc = _E2E_PAPER[0]
    agents = create_real_agents(
        llm=QwenParseLlmClient(real_chat_model),
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        hypothesis_llm=QwenHypothesisLlmClient(real_chat_model),
        judgment_llm=QwenJudgmentLlmClient(real_chat_model),
        rewrite_llm=QwenRewriteLlmClient(real_chat_model),
        hitl2_gate=ConservativeHitl2Gate(),
        retrieval_runtime=lazy_search_agent_runtime(),
    )
    sc = SessionContext(
        session_id="real-retrieval-e2e",
        user_id="real-retrieval-e2e",
        current_time=datetime(2025, 1, 1, 0, 0, 0),
        user_prompt="",
    )
    orch = Orchestrator(agents=agents)
    state: dict[str, Any] = orch.graph.invoke(
        {"original_doc": doc, "session_context": sc}
    )
    return state


def test_real_retrieval_produces_nonempty_traceable_citations(
    real_retrieval_pipeline_state: dict[str, Any],
) -> None:
    """真实检索后端产非空 citations，且至少一条可溯源（``origin`` / ``locator`` 非空）。

    断言聚焦「至少一条」而非「全部」——Bisheng 知识库为内网、缺失或超时时 V12 降级，
    Volcano 全网检索产网络类 citations（带 url → ``locator``），故「至少一条可溯源」即可
    证明真实后端使 citations 非空（PRD §Q6 / Story 1/7/8）。不对具体内容做脆弱断言。
    """

    citations: dict[str, Any] = real_retrieval_pipeline_state.get("citations") or {}
    all_sources: list[Any] = [s for sources in citations.values() for s in sources]
    name = _E2E_PAPER[0][0]
    assert all_sources, f"[{name}] 真实检索应产非空 citations（至少一条 Source）"

    traceable = [
        s
        for s in all_sources
        if getattr(s, "origin", "") and getattr(s, "locator", "")
    ]
    assert traceable, (
        f"[{name}] 真实 citations 应至少一条可溯源（origin + locator 非空），"
        f"got {len(all_sources)} 条但无一兼具 origin 与 locator"
    )


def test_real_retrieval_drives_nontrivial_judgment(
    real_retrieval_pipeline_state: dict[str, Any],
) -> None:
    """真实 citations 驱动下游 judgment 判非空终态（至少一条假设脱离 ``pending``）。

    judgment 节点读 ``citations`` 经 ``QwenJudgmentLlmClient`` 重判；真实证据应使至少一条
    假设落 ``supported / doubtful / refuted``（非 ``pending``）——即「真实后端使 judgment 见
    真实素材」成立（PRD §Solution / Story 1/3）。保守 HITL-2 仍驳回重写、终稿逐字节等于原文，
    但裁决终态已落 ``hypotheses`` channel、非永远 pending。
    """

    hypotheses: dict[str, Any] = (
        real_retrieval_pipeline_state.get("hypotheses") or {}
    )
    name = _E2E_PAPER[0][0]
    all_hypotheses: list[Any] = [h for hypes in hypotheses.values() for h in hypes]
    assert all_hypotheses, f"[{name}] 真实开药应产非空假设"
    judged = [h for h in all_hypotheses if h.status != HypothesisStatus.PENDING]
    assert judged, (
        f"[{name}] 真实 citations 应驱动 judgment 判非空终态（至少一条假设脱离 pending），"
        f"got {len(all_hypotheses)} 条假设但全为 pending"
    )
