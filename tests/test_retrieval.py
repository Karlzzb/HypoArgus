"""公共检索层契约 + Mock 桩测试（PRD §6、issue #3）。

三条合规约束在接口层表达并由 ``validate_request`` 强制：
- 网络：白名单域名 + 禁泛网搜索 + 请求脱敏。
- 知识库：调用前校验权限 + 按类型/时间过滤。
- 结构化：仅预定义模板 + 禁泛化 SQL（请求形状无 raw SQL 字段）。

Mock 先校验合规、再返回受控的固定带来源素材，确定且可断言
（同一请求 → 同一 source_id，不依赖计数器）。本切片不真实开发三类检索引擎。
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from infra.retrieval import (
    ComplianceError,
    KnowledgeBaseRetrievalRequest,
    NetworkRetrievalRequest,
    RetrievalConfig,
    RetrievalKind,
    RetrievalResponse,
    Source,
    StructuredRetrievalRequest,
    create_mock_retrieval_layer,
    redact_query,
    validate_request,
)

# --------------------------------------------------------------------------- #
# 配置：受控的白名单 / 授权用户 / 预定义模板。
# --------------------------------------------------------------------------- #

CONFIG = RetrievalConfig(
    network_whitelist=frozenset({"stats.example.com", "who.int"}),
    authorized_users=frozenset({"analyst-1"}),
    structured_templates={
        "revenue_by_quarter": frozenset({"company", "year"}),
        "incident_count": frozenset({"region", "since"}),
    },
)


# --------------------------------------------------------------------------- #
# 统一请求 / 响应形状 + 来源标识
# --------------------------------------------------------------------------- #


def test_three_kinds_share_one_response_shape():
    """三类检索共用同一 RetrievalResponse 形状；每条素材带来源标识。"""

    layer = create_mock_retrieval_layer(CONFIG)
    for request, expected_kind in [
        (
            NetworkRetrievalRequest(query="GDP 2024", domain="stats.example.com"),
            RetrievalKind.NETWORK,
        ),
        (
            KnowledgeBaseRetrievalRequest(query=" revený forecast", user_id="analyst-1"),
            RetrievalKind.KNOWLEDGE_BASE,
        ),
        (
            StructuredRetrievalRequest(
                template_id="revenue_by_quarter",
                params={"company": "acme", "year": 2024},
            ),
            RetrievalKind.STRUCTURED,
        ),
    ]:
        resp = layer.retrieve(request)
        assert isinstance(resp, RetrievalResponse)
        assert resp.kind == expected_kind
        assert len(resp.materials) >= 1
        for src in resp.materials:
            assert isinstance(src, Source)
            # 来源标识三件套：稳定 id + origin + kind。
            assert src.source_id
            assert src.origin
            assert src.kind == expected_kind
            assert src.snippet


def test_discriminated_union_parses_by_kind():
    """RetrievalRequest 是按 kind 判别的联合：dict 解析分发到对应子类。"""

    adapter: TypeAdapter = TypeAdapter(
        NetworkRetrievalRequest | KnowledgeBaseRetrievalRequest | StructuredRetrievalRequest
    )
    net = adapter.validate_python(
        {"kind": "network", "query": "x", "domain": "stats.example.com"}
    )
    assert isinstance(net, NetworkRetrievalRequest)
    kb = adapter.validate_python(
        {"kind": "knowledge_base", "query": "x", "user_id": "analyst-1"}
    )
    assert isinstance(kb, KnowledgeBaseRetrievalRequest)
    st = adapter.validate_python(
        {"kind": "structured", "template_id": "revenue_by_quarter", "params": {}}
    )
    assert isinstance(st, StructuredRetrievalRequest)


# --------------------------------------------------------------------------- #
# 网络检索：白名单 + 禁泛网 + 脱敏
# --------------------------------------------------------------------------- #


def test_network_happy_path_whitelisted_and_redacted():
    layer = create_mock_retrieval_layer(CONFIG)
    resp = layer.retrieve(
        NetworkRetrievalRequest(
            query="联系我 me@corp.com 致电 138-0000-0001",
            domain="stats.example.com",
        )
    )
    assert resp.kind == RetrievalKind.NETWORK
    assert resp.redacted_query is not None
    # 脱敏已发生：邮箱与电话样串被替换，不进入「实际发出」的查询。
    assert "me@corp.com" not in resp.redacted_query
    assert "138-0000-0001" not in resp.redacted_query
    # 素材 origin = 命中的白名单域名。
    assert all(s.origin == "stats.example.com" for s in resp.materials)


def test_network_non_whitelisted_domain_rejected():
    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="白名单"):
        layer.retrieve(
            NetworkRetrievalRequest(query="x", domain="evil.example.com")
        )


def test_network_redact_disabled_rejected():
    """接口层强制脱敏：redact_pii=False 直接拒绝。"""

    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="脱敏"):
        layer.retrieve(
            NetworkRetrievalRequest(
                query="x", domain="stats.example.com", redact_pii=False
            )
        )


def test_network_empty_query_rejected():
    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="query"):
        layer.retrieve(
            NetworkRetrievalRequest(query="   ", domain="stats.example.com")
        )


def test_network_has_no_general_web_entry_point():
    """禁泛网搜索：网络请求形状强制要求具体 domain，无「全网」入口。"""

    fields = NetworkRetrievalRequest.model_fields
    assert "domain" in fields
    # domain 无默认值 → 必填。
    assert fields["domain"].is_required()


# --------------------------------------------------------------------------- #
# 知识库检索：权限校验 + 类型/时间过滤
# --------------------------------------------------------------------------- #


def test_knowledge_base_happy_path_authorized_with_filters():
    layer = create_mock_retrieval_layer(CONFIG)
    resp = layer.retrieve(
        KnowledgeBaseRetrievalRequest(
            query="营收预测",
            user_id="analyst-1",
            type_filter="report",
            time_filter="2024-01-01/2024-12-31",
        )
    )
    assert resp.kind == RetrievalKind.KNOWLEDGE_BASE
    assert len(resp.materials) >= 1


def test_knowledge_base_unauthorized_user_rejected():
    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="授权"):
        layer.retrieve(
            KnowledgeBaseRetrievalRequest(query="x", user_id="intruder")
        )


def test_knowledge_base_filters_are_optional():
    """type_filter / time_filter 可选；不带也能通过权限校验。"""

    layer = create_mock_retrieval_layer(CONFIG)
    resp = layer.retrieve(
        KnowledgeBaseRetrievalRequest(query="x", user_id="analyst-1")
    )
    assert resp.kind == RetrievalKind.KNOWLEDGE_BASE


# --------------------------------------------------------------------------- #
# 结构化数据检索：预定义模板 + 禁泛化 SQL
# --------------------------------------------------------------------------- #


def test_structured_happy_path_registered_template():
    layer = create_mock_retrieval_layer(CONFIG)
    resp = layer.retrieve(
        StructuredRetrievalRequest(
            template_id="revenue_by_quarter",
            params={"company": "acme", "year": 2024},
        )
    )
    assert resp.kind == RetrievalKind.STRUCTURED
    assert all(s.origin == "revenue_by_quarter" for s in resp.materials)


def test_structured_unknown_template_rejected():
    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="模板"):
        layer.retrieve(
            StructuredRetrievalRequest(
                template_id="DROP TABLE users; --", params={}
            )
        )


def test_structured_extra_params_rejected():
    """模板只接受预定义参数；多传的未知参数被拒。"""

    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError, match="参数"):
        layer.retrieve(
            StructuredRetrievalRequest(
                template_id="revenue_by_quarter",
                params={"company": "acme", "year": 2024, "extra": "nope"},
            )
        )


def test_structured_request_has_no_raw_sql_field():
    """禁泛化 SQL：请求形状本身没有 raw SQL 字段（by construction）。"""

    fields = StructuredRetrievalRequest.model_fields
    assert "sql" not in fields
    assert "query" not in fields  # 结构化检索不走自由查询
    assert "template_id" in fields
    assert "params" in fields


# --------------------------------------------------------------------------- #
# Mock 确定性 + 合规先行
# --------------------------------------------------------------------------- #


def test_mock_deterministic_same_input_same_output():
    """同一请求 → 同一 source_id（id 由请求内容派生，非计数器）。"""

    layer = create_mock_retrieval_layer(CONFIG)
    req = NetworkRetrievalRequest(query="GDP 2024", domain="stats.example.com")
    r1 = layer.retrieve(req)
    r2 = layer.retrieve(req)
    assert [s.source_id for s in r1.materials] == [s.source_id for s in r2.materials]
    assert r1.redacted_query == r2.redacted_query
    # 不同查询 → 不同 id。
    other = layer.retrieve(
        NetworkRetrievalRequest(query="CPI 2024", domain="stats.example.com")
    )
    assert [s.source_id for s in other.materials] != [s.source_id for s in r1.materials]


def test_mock_validates_before_returning_materials():
    """Mock 先校验合规：违规请求抛 ComplianceError，绝不返回素材。"""

    layer = create_mock_retrieval_layer(CONFIG)
    with pytest.raises(ComplianceError):
        layer.retrieve(
            NetworkRetrievalRequest(query="x", domain="evil.example.com")
        )


def test_validate_request_is_pure_no_layer_needed():
    """validate_request 可独立调用（纯函数 seam），不依赖 Mock。"""

    validate_request(
        NetworkRetrievalRequest(query="x", domain="stats.example.com"), CONFIG
    )
    with pytest.raises(ComplianceError):
        validate_request(
            NetworkRetrievalRequest(query="x", domain="evil.example.com"), CONFIG
        )


# --------------------------------------------------------------------------- #
# 脱敏纯函数
# --------------------------------------------------------------------------- #


def test_redact_query_strips_email_and_phone():
    out = redact_query("联系 me@corp.com 或 139-1234-5678")
    assert "me@corp.com" not in out
    assert "139-1234-5678" not in out
    assert "[REDACTED]" in out


def test_redact_query_leaves_clean_text():
    assert redact_query("GDP growth 2024") == "GDP growth 2024"
