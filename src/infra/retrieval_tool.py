"""检索工具：``RetrievalTool`` 包 ``RetrievalLayer``，独占 ``SearchStep → RetrievalRequest``
翻译（ADR-0015）。

两 ReAct Agent（体检 #4 / 开药 #5）原先各复制一份 ``_build_request``（``verification.py``
与 ``hypothesis.py``），逻辑近乎同构——按 ``channel`` 构造 ``NetworkRetrievalRequest`` /
``KnowledgeBaseRetrievalRequest``、校验 query 非空与 domain/user_id 必填、拒绝 ``structured``
通道。现收口于此一处（locality：通道/参数校验集中，未来调整只改此模块）。

未检索通道（``structured``）由翻译处抛 ``ValueError``——保留 ReAct 循环的异常→兜底路径
（节点落 ``error`` / 假设落 ``doubtful``，与 #4/#5 既有行为一致）。合规（白名单域名 /
授权用户 / 模板）仍由检索层 ``validate_request`` 在接口层强制，本工具只构造请求形状。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from infra.retrieval import (
    KnowledgeBaseRetrievalRequest,
    NetworkRetrievalRequest,
    RetrievalKind,
    RetrievalLayer,
    RetrievalRequest,
)
from infra.tool_protocol import BaseTool, ToolResult

__all__ = ["RetrievalStep", "RetrievalTool"]


@runtime_checkable
class RetrievalStep(Protocol):
    """检索步 duck-type 契约。

    体检的 :class:`agents.verification.SearchStep` 与开药的
    :class:`agents.hypothesis.HypothesisSearchStep` 字段同构、均满足本协议——
    使 :class:`RetrievalTool` 不依赖任一具体 Agent 的 step 类型。
    """

    query: str
    channel: RetrievalKind
    domain: str | None
    user_id: str | None
    type_filter: str | None
    time_filter: str | None


def _build_request(step: RetrievalStep) -> RetrievalRequest:
    """把检索步翻译为 :class:`RetrievalRequest`；通道/参数不全 → 抛 :class:`ValueError`。

    合规（白名单域名 / 授权用户 / 模板）由检索层 ``validate_request`` 在接口层强制；
    此处只构造请求形状。结构化数据通道（``structured``）不在 ReAct 范围 → 抛错
    （→ 节点 ``error`` / 假设 ``doubtful``）。
    """

    if not step.query.strip():
        raise ValueError("检索词不可为空")
    if step.channel is RetrievalKind.NETWORK:
        if not step.domain:
            raise ValueError("网络检索须指定 domain")
        return NetworkRetrievalRequest(query=step.query, domain=step.domain)
    if step.channel is RetrievalKind.KNOWLEDGE_BASE:
        if not step.user_id:
            raise ValueError("知识库检索须指定 user_id")
        return KnowledgeBaseRetrievalRequest(
            query=step.query,
            user_id=step.user_id,
            type_filter=step.type_filter,
            time_filter=step.time_filter,
        )
    raise ValueError(
        f"ReAct 不支持通道 {step.channel!r}（仅 network / knowledge_base）"
    )


class RetrievalTool(BaseTool):
    """检索工具：包 :class:`RetrievalLayer`，独占 ``SearchStep → RetrievalRequest`` 翻译。

    Agent 经 ``ToolRegistry.dispatch("retrieve", step=<SearchStep>)`` 调用；返回
    :class:`ToolResult` 携带 ``sources``（流入 :class:`infra.history.HistoryStore`）。
    入参 ``step`` 须满足 :class:`RetrievalStep`（体检/开药的 ``SearchStep`` 均满足）。
    """

    def __init__(self, retrieval: RetrievalLayer) -> None:
        self._retrieval = retrieval

    @property
    def name(self) -> str:
        return "retrieve"

    def execute(self, **kwargs: Any) -> ToolResult:
        step: RetrievalStep = kwargs["step"]  # 体检/开药均传 step=<SearchStep>
        request = _build_request(step)
        response = self._retrieval.retrieve(request)
        metadata: dict[str, str] = {"kind": response.kind.value}
        if response.redacted_query:
            metadata["redacted_query"] = response.redacted_query
        return ToolResult(sources=list(response.materials), metadata=metadata)
