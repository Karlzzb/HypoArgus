"""真实 LLM 端到端流水线测试：整条 ``run_real_pipeline`` 跑真实论文。

与 ``test_real_llm_parse.py``（只验解析层）互补——本测试驱动**整条真实装配**：
真实解析（两阶段）→ 真实开药 → 检索桩（空 citations）→ 真实裁决 → 真实重写提议
→ 保守 HITL-2。保守闸门全驳回 + 空 citations → 无触达段 → 终稿逐字节等于原文。

这是 tracer bullet 在真实数据规模上的兑现：即使真实 LLM 调用偶发抖动，各 stage
``_guarded`` 兜底单向向前，终稿仍逐字节还原原文（保护原文底线）。断言确定性——
不依赖 LLM 非确定输出（保守闸门驳回一切变更）。需 ``DASHSCOPE_API_KEY`` + 网络，缺则跳过。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from langchain_core.language_models import BaseChatModel
from real_papers import REAL_PAPER_CASES

from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.hitl2 import ConservativeHitl2Gate
from infra.llm_provider import build_qwen_chat_model
from runtime.run_real import run_real_pipeline

_HAS_KEY = bool(os.environ.get("DASHSCOPE_API_KEY"))
pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(not _HAS_KEY, reason="needs DASHSCOPE_API_KEY + network"),
]


# 选两篇真实论文（最小 + 中等含 ``---`` 分隔线）跑端到端，控制 LLM 调用量与耗时。
# 解析层已对全 9 篇深验（``test_real_llm_parse.py``）；此处只证整条装配在真实数据上不崩。
_E2E_PAPERS: list[tuple[str, bytes]] = [
    (name, doc)
    for name, doc in REAL_PAPER_CASES
    if name in {"paper_03_集成电路工程技术专业", "paper_08_智能制造工程技术"}
]


@pytest.fixture(scope="module")
def real_chat_model() -> Iterator[BaseChatModel]:
    """模块级共享真实 ``ChatOpenAI``（DashScope），避免每用例重建。

    timeout=120 / max_tokens=8192 与解析层一致——大论文结构化输出留余量、缓解截断。
    """

    model = build_qwen_chat_model(timeout=120.0, max_tokens=8192)
    yield model


@pytest.mark.parametrize(
    "paper",
    _E2E_PAPERS,
    ids=[name for name, _ in _E2E_PAPERS],
)
def test_run_real_pipeline_byte_identical_on_real_paper(
    paper: tuple[str, bytes],
    real_chat_model: BaseChatModel,
) -> None:
    """整条真实 LLM 流水线跑真实论文：保守闸门 → 终稿逐字节等于原文。

    真实解析（两阶段）+ 真实开药 + 真实裁决 + 真实重写提议，但检索桩产空 citations、
    HITL-1 保守 SKIP、HITL-2 保守全驳回——故无触达段、终稿逐字节还原原文。
    这是 tracer bullet 在真实数据规模上的兑现：任一 LLM 调用抖动由 ``_guarded``
    兜底（单向向前、回退原文 bytes），终稿底线不变。
    """

    name, doc = paper
    report = run_real_pipeline(
        doc,
        chat_model=real_chat_model,
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        hitl2_gate=ConservativeHitl2Gate(),
    )
    assert report.final_document == doc, (
        f"[{name}] 保守闸门下终稿应逐字节等于原文（len final="
        f"{len(report.final_document)} != len input={len(doc)}）"
    )
