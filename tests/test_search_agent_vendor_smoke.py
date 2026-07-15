"""TASK-SA-0 smoke：vendored SearchAgent V12 可导入、可构造、无需凭证。

守护 Slice 0 的三条验收：vendored 子智能体（已 carve-out 出 strict/ruff）能作为
顶层 ``search_agent`` 包导入；``SearchAgentRuntime.from_env(with_llm=False)`` 在无
网络、无 LLM 凭证下完成构造（HTTP client 仅构造不发起请求，凭据校验在请求期才触发）。
默认质量门跑此测试（非 ``real_llm``）——不联网、不调 LLM。
"""

from __future__ import annotations


def test_search_agent_imports_as_top_level_package() -> None:
    """V12 内部全用相对导入，故只需 ``search_agent`` 为顶层包，``evidence_retrieval``
    作为 ``search_agent.evidence_retrieval`` 子包随之解析。"""
    import search_agent
    from search_agent.api import SearchAgentRuntime, ainvoke_search_agent
    from search_agent.evidence_retrieval.public_contracts import (
        CitationRecord,
        SearchAgentInputState,
        SearchAgentOutputState,
        TaskDecision,
    )

    assert search_agent.__name__ == "search_agent"
    assert SearchAgentRuntime is not None
    assert callable(ainvoke_search_agent)
    assert {
        CitationRecord,
        SearchAgentInputState,
        SearchAgentOutputState,
        TaskDecision,
    }


def test_runtime_constructs_without_llm_credentials() -> None:
    """``with_llm=False`` 走 ``EvidenceRetrievalDependencies.defaults``：构造四个
    httpx client（loop 在首请求时才绑）+ 确定性 judge，不调 LLM、不发请求、不需
    VOLCANO/BISHENG 凭证。"""
    from search_agent.api import SearchAgentRuntime

    runtime = SearchAgentRuntime.from_env(with_llm=False)
    assert runtime is not None
