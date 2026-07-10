"""公共检索能力层：统一契约 + 合规校验 + Mock 桩（PRD §6、issue #3）。

三类检索智能体（网络 / 知识库 / 结构化数据）共用同一请求/响应形状，每条素材携带
稳定 ``source_id`` + ``origin`` + ``kind``（可溯源、可审计）。三条合规约束在接口层
表达并由 :func:`validate_request` 强制执行：

- 网络检索：仅访问预置官方白名单域名（禁止泛网搜索）、请求脱敏。
- 知识库检索：调用前校验用户权限、支持按类型与时间过滤。
- 结构化数据检索：仅执行预定义模板、禁止泛化 SQL（请求形状本身无 raw SQL 字段）。

本切片**不真实实现**三类检索引擎的运行逻辑（HTTP / DB driver / 语义检索索引）——
只产出契约 + Mock 桩，供体检线路（#4）与开药线路（#5）对接开发与单测。Mock 先校验
合规、再返回受控的固定带来源素材，确定且可断言（同一请求 → 同一 ``source_id``，
不依赖计数器）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

__all__ = [
    "RetrievalKind",
    "Source",
    "RetrievalResponse",
    "NetworkRetrievalRequest",
    "KnowledgeBaseRetrievalRequest",
    "StructuredRetrievalRequest",
    "RetrievalRequest",
    "ComplianceError",
    "RetrievalConfig",
    "RetrievalLayer",
    "validate_request",
    "redact_query",
    "create_mock_retrieval_layer",
]


class RetrievalKind(StrEnum):
    """检索通道类型。三类检索智能体统一接收请求、共用同一响应形状。"""

    NETWORK = "network"
    KNOWLEDGE_BASE = "knowledge_base"
    STRUCTURED = "structured"


class Source(BaseModel):
    """一条带来源标识的检索素材。

    三件套保证可溯源、可审计：稳定 ``source_id``（Mock 中由请求内容确定性派生）、
    ``origin``（域名 / 知识库 / 模板 id）、``kind``（产生它的通道）。
    """

    source_id: str
    kind: RetrievalKind
    origin: str
    title: str | None = None
    snippet: str
    locator: str | None = None


class RetrievalResponse(BaseModel):
    """统一检索响应。

    ``redacted_query`` 仅网络检索置值，审计脱敏确已发生；其余通道为 ``None``。
    """

    kind: RetrievalKind
    materials: list[Source] = Field(default_factory=list)
    redacted_query: str | None = None


# --------------------------------------------------------------------------- #
# 统一请求形状（按 kind 判别的联合）
# --------------------------------------------------------------------------- #


class NetworkRetrievalRequest(BaseModel):
    """网络检索请求。

    ``domain`` 必填且须命中白名单——请求形状无「全网」入口，泛网搜索由类型本身禁止。
    ``redact_pii`` 接口层强制为真，禁止发出未脱敏的网络请求。
    """

    kind: Literal[RetrievalKind.NETWORK] = RetrievalKind.NETWORK
    query: str
    domain: str
    redact_pii: bool = True


class KnowledgeBaseRetrievalRequest(BaseModel):
    """知识库检索请求。

    调用前校验 ``user_id`` 权限；``type_filter`` / ``time_filter`` 可选，支持按类型与
    时间过滤（语义由真实引擎解释，本切片不解释）。
    """

    kind: Literal[RetrievalKind.KNOWLEDGE_BASE] = RetrievalKind.KNOWLEDGE_BASE
    query: str
    user_id: str
    type_filter: str | None = None
    time_filter: str | None = None


class StructuredRetrievalRequest(BaseModel):
    """结构化数据检索请求。

    只允许预定义模板（``template_id`` 须注册），调用方仅提供 ``template_id`` 与绑定
    参数 ``params``——**请求形状无 raw SQL 字段**，泛化 SQL 由类型本身禁止。
    """

    kind: Literal[RetrievalKind.STRUCTURED] = RetrievalKind.STRUCTURED
    template_id: str
    params: dict[str, str | int | float] = Field(default_factory=dict)


RetrievalRequest = Annotated[
    NetworkRetrievalRequest | KnowledgeBaseRetrievalRequest | StructuredRetrievalRequest,
    Field(discriminator="kind"),
]
"""统一检索请求：按 ``kind`` 判别的联合，三类共用同一入口与响应形状。"""


# --------------------------------------------------------------------------- #
# 合规约束配置
# --------------------------------------------------------------------------- #


class ComplianceError(Exception):
    """请求违反接口层合规约束（白名单 / 权限 / 模板）时抛出。"""


_DEFAULT_NETWORK_WHITELIST: frozenset[str] = frozenset(
    {"stats.example.com", "pubmed.ncbi.nlm.nih.gov", "who.int"}
)
_DEFAULT_AUTHORIZED_USERS: frozenset[str] = frozenset({"analyst-1", "analyst-2"})
_DEFAULT_STRUCTURED_TEMPLATES: Mapping[str, frozenset[str]] = {
    "revenue_by_quarter": frozenset({"company", "year"}),
    "incident_count_by_region": frozenset({"region", "since"}),
}


@dataclass(frozen=True)
class RetrievalConfig:
    """合规约束配置：白名单域名 / 授权用户 / 预定义模板及其允许参数。"""

    network_whitelist: frozenset[str] = _DEFAULT_NETWORK_WHITELIST
    authorized_users: frozenset[str] = _DEFAULT_AUTHORIZED_USERS
    structured_templates: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: dict(_DEFAULT_STRUCTURED_TEMPLATES)
    )


# --------------------------------------------------------------------------- #
# 合规校验（纯函数 seam）
# --------------------------------------------------------------------------- #


def validate_request(
    request: RetrievalRequest, config: RetrievalConfig | None = None
) -> None:
    """在接口层强制三条合规约束；违反则抛 :class:`ComplianceError`。

    纯函数、可独立单测——体检（#4）/开药（#5）在发出检索前都应先过此闸。
    """

    cfg = config or RetrievalConfig()
    if isinstance(request, NetworkRetrievalRequest):
        if not request.query.strip():
            raise ComplianceError("网络检索：query 不可为空")
        if not request.redact_pii:
            raise ComplianceError("网络检索：必须脱敏（redact_pii 不可为 False）")
        if request.domain not in cfg.network_whitelist:
            raise ComplianceError(
                f"网络检索：domain {request.domain!r} 不在白名单"
            )
    elif isinstance(request, KnowledgeBaseRetrievalRequest):
        if not request.query.strip():
            raise ComplianceError("知识库检索：query 不可为空")
        if request.user_id not in cfg.authorized_users:
            raise ComplianceError(
                f"知识库检索：用户 {request.user_id!r} 未授权"
            )
    elif isinstance(request, StructuredRetrievalRequest):
        allowed = cfg.structured_templates.get(request.template_id)
        if allowed is None:
            raise ComplianceError(
                f"结构化检索：未知模板 {request.template_id!r}"
            )
        extra = set(request.params) - set(allowed)
        if extra:
            raise ComplianceError(
                f"结构化检索：模板 {request.template_id!r} 不接受参数 "
                f"{sorted(extra)}"
            )
    else:  # pragma: no cover - 判别联合保证不达
        raise ComplianceError(f"未知检索请求类型：{type(request).__name__}")


# --------------------------------------------------------------------------- #
# 请求脱敏（纯函数）
# --------------------------------------------------------------------------- #


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b\d{2,4}[-\s]?\d{3,4}[-\s]?\d{3,4}\b")


def redact_query(query: str) -> str:
    """脱敏：把查询中的电话样串与邮箱替换为 ``[REDACTED]``。

    网络检索真实引擎发出请求前应先脱敏；Mock 层调用此函数以兑现接口层承诺。
    """

    redacted = _PHONE_RE.sub("[REDACTED]", query)
    redacted = _EMAIL_RE.sub("[REDACTED]", redacted)
    return redacted


# --------------------------------------------------------------------------- #
# 公共检索层协议 + Mock 桩
# --------------------------------------------------------------------------- #


class RetrievalLayer(Protocol):
    """公共检索层协议：三类检索共用同一入口、同一响应形状。"""

    def retrieve(self, request: RetrievalRequest) -> RetrievalResponse: ...


def _source_id(kind: RetrievalKind, origin: str, seed: str, idx: int) -> str:
    """确定性 source_id：由通道 + origin + 请求种子 + 序号派生，非计数器。"""

    digest = hashlib.blake2b(
        f"{kind.value}|{origin}|{seed}|{idx}".encode(), digest_size=6
    ).hexdigest()
    return f"src-{digest}"


class _MockRetrievalLayer:
    """Mock 桩：先校验合规，再返回受控的固定带来源素材。

    确定性——同一请求恒产生同一 ``source_id`` 与响应，可供 #4/#5 直接断言。
    不调用任何真实 HTTP / DB / 语义检索引擎。
    """

    def __init__(self, config: RetrievalConfig | None = None) -> None:
        self._config = config or RetrievalConfig()

    def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        validate_request(request, self._config)  # 合规先行

        if isinstance(request, NetworkRetrievalRequest):
            query = redact_query(request.query)  # 接口层已强制 redact_pii
            origin = request.domain
            materials = [
                Source(
                    source_id=_source_id(
                        RetrievalKind.NETWORK, origin, query, i
                    ),
                    kind=RetrievalKind.NETWORK,
                    origin=origin,
                    title=f"mock-result-{i}",
                    snippet=f"[mock:{origin}] {query}",
                    locator=f"https://{origin}/mock/{i}",
                )
                for i in range(2)
            ]
            return RetrievalResponse(
                kind=RetrievalKind.NETWORK,
                materials=materials,
                redacted_query=query,
            )

        if isinstance(request, KnowledgeBaseRetrievalRequest):
            origin = "internal-kb"
            flt = request.type_filter or "*"
            materials = [
                Source(
                    source_id=_source_id(
                        RetrievalKind.KNOWLEDGE_BASE, origin, request.query, i
                    ),
                    kind=RetrievalKind.KNOWLEDGE_BASE,
                    origin=origin,
                    title=f"mock-kb-{i}",
                    snippet=f"[mock:kb:{flt}] {request.query}",
                    locator=f"kb://{origin}/doc-{i}",
                )
                for i in range(2)
            ]
            return RetrievalResponse(
                kind=RetrievalKind.KNOWLEDGE_BASE, materials=materials
            )

        # 结构化数据检索
        origin = request.template_id
        seed = repr(sorted(request.params.items()))
        materials = [
            Source(
                source_id=_source_id(RetrievalKind.STRUCTURED, origin, seed, i),
                kind=RetrievalKind.STRUCTURED,
                origin=origin,
                title=f"mock-row-{i}",
                snippet=f"[mock:sql:{origin}] {dict(request.params)}",
                locator=f"db://{origin}/row-{i}",
            )
            for i in range(2)
        ]
        return RetrievalResponse(
            kind=RetrievalKind.STRUCTURED, materials=materials
        )


def create_mock_retrieval_layer(
    config: RetrievalConfig | None = None,
) -> RetrievalLayer:
    """返回 Mock 检索层：受控、确定、合规先行，供 #4/#5 直接对接。"""

    return _MockRetrievalLayer(config)
