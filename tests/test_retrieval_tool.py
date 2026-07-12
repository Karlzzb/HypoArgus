"""``RetrievalTool`` seam 单测（ADR-0015）：``SearchStep → RetrievalRequest`` 翻译收口。"""

from __future__ import annotations

import pytest

from agents.hypothesis import HypothesisSearchStep
from agents.verification import SearchStep
from infra.retrieval import RetrievalKind, create_mock_retrieval_layer
from infra.retrieval_tool import RetrievalTool
from infra.tool_protocol import ToolRegistry

_TOOL = RetrievalTool(create_mock_retrieval_layer())


def test_network_search_returns_sources_with_metadata() -> None:
    step = SearchStep(query="incidence", channel=RetrievalKind.NETWORK, domain="who.int")
    result = _TOOL.execute(step=step)
    assert result.success is True
    assert len(result.sources) == 2
    assert result.metadata["kind"] == "network"
    assert "redacted_query" in result.metadata


def test_kb_search_returns_sources_without_redacted_query() -> None:
    step = SearchStep(
        query="policy", channel=RetrievalKind.KNOWLEDGE_BASE, user_id="analyst-1"
    )
    result = _TOOL.execute(step=step)
    assert result.success is True
    assert len(result.sources) == 2
    assert result.metadata["kind"] == "knowledge_base"
    assert "redacted_query" not in result.metadata


def test_structured_channel_rejected() -> None:
    step = SearchStep(query="x", channel=RetrievalKind.STRUCTURED)
    with pytest.raises(ValueError):
        _TOOL.execute(step=step)


def test_empty_query_raises() -> None:
    step = SearchStep(query="   ", channel=RetrievalKind.NETWORK, domain="who.int")
    with pytest.raises(ValueError):
        _TOOL.execute(step=step)


def test_network_missing_domain_raises() -> None:
    step = SearchStep(query="x", channel=RetrievalKind.NETWORK)
    with pytest.raises(ValueError):
        _TOOL.execute(step=step)


def test_kb_missing_user_id_raises() -> None:
    step = SearchStep(query="x", channel=RetrievalKind.KNOWLEDGE_BASE)
    with pytest.raises(ValueError):
        _TOOL.execute(step=step)


def test_registry_dispatch_routes_to_retrieval_tool() -> None:
    reg = ToolRegistry()
    reg.register(RetrievalTool(create_mock_retrieval_layer()))
    step = SearchStep(query="incidence", channel=RetrievalKind.NETWORK, domain="who.int")
    result = reg.dispatch("retrieve", step=step)
    assert result.success is True
    assert len(result.sources) == 2
    with pytest.raises(KeyError):
        reg.dispatch("nope", step=step)


def test_hypothesis_search_step_also_satisfies_retrieval_step() -> None:
    """体检 ``SearchStep`` 与开药 ``HypothesisSearchStep`` 字段同构、均满足 ``RetrievalStep``。"""

    step = HypothesisSearchStep(
        query="counterfactual", channel=RetrievalKind.KNOWLEDGE_BASE, user_id="analyst-1"
    )
    result = _TOOL.execute(step=step)
    assert result.success is True
    assert len(result.sources) == 2
