"""检索 Agent 契约：retrieval 真实适配器 seam + runtime Protocol + 映射纯函数 + 离线 Fake 桩。

PRD §Q1/Q3/B1 · Slice 2：把 vendored SearchAgent V12 作为真实检索 provider 接入 retrieval
seam（填 manifest 的 ``real=None`` 空位，与 judgment 同形管理）。本子包拆三模块（ADR-0014）：

- ``contract.py``（本文件）：注入 seam ``RetrievalRuntime`` Protocol + 映射纯函数
  :func:`map_citations` + payload 构造纯函数 :func:`build_search_agent_payload` + 离线
  :class:`FakeSearchAgentRuntime` + source_type→kind 映射表。纯函数 provider-free、确定、
  可独立单测（镜像 :mod:`agents.judgment.contract` 的拆分约定）。
- ``agent.py``：真实适配器编排 :func:`real_retrieval`（实现 :class:`agents.assembly.RetrievalFn`）+
  daemon worker loop + 延迟单例 proxy + :func:`build_real_retrieval`。

映射与 payload 纯函数刻意与 runtime 解耦——给定 ``SearchAgentOutputState`` 直接算
``dict[str, list[Source]]``、给定框架 state 输入直接算 ``SearchAgentInputState``，不触网、
不调 LLM，离线单测即覆盖（PRD §Testing「映射测」「不新增测试 seam」）。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Protocol, cast

from search_agent.evidence_retrieval.public_contracts import (
    SearchAgentInputState,
    SearchAgentOutputState,
)
from search_agent.evidence_retrieval.schemas import (
    ArgumentContext,
    ForwardItem,
    ReverseItem,
)

from domain import (
    Argument,
    Hypothesis,
    HypothesisRelation,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import RetrievalKind, Source, redact_query

__all__ = [
    "RetrievalRuntime",
    "FakeSearchAgentRuntime",
    "map_citations",
    "build_search_agent_payload",
    "SOURCE_TYPE_TO_KIND",
]


# --------------------------------------------------------------------------- #
# 注入 seam：runtime Protocol（真实 = vendored SearchAgentRuntime；测试 = Fake）
# --------------------------------------------------------------------------- #


class RetrievalRuntime(Protocol):
    """真实检索后端的注入 seam（PRD §Q4）。

    生产实现：vendored ``SearchAgentRuntime``（``with_llm=False``、确定性 judge、loop-affine
    httpx client 由 daemon worker loop 承载）。``ainvoke`` 为 **async**——同步 ``RetrievalFn``
    经 :func:`asyncio.run_coroutine_threadsafe` 桥接、``NodeFn`` 同步签名不动。

    测试实现：:class:`FakeSearchAgentRuntime` 返回 canned 输出、不联网。seam 数 = 1
    （``RetrievalFn`` Protocol）；runtime 在该 seam 注入（PRD §Testing「不新增测试 seam」）。
    """

    async def ainvoke(self, payload: Any) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# CitationRecord.source_type → Source.kind 映射（PRD §Q6 表）
# --------------------------------------------------------------------------- #

SOURCE_TYPE_TO_KIND: dict[str, RetrievalKind] = {
    "WEB": RetrievalKind.NETWORK,
    "KNOWLEDGE_BASE": RetrievalKind.KNOWLEDGE_BASE,
    "STRUCTURED_DATA": RetrievalKind.STRUCTURED,
}


# --------------------------------------------------------------------------- #
# 映射纯函数：SearchAgentOutputState → dict[str, list[Source]]
# --------------------------------------------------------------------------- #


def map_citations(output: SearchAgentOutputState) -> dict[str, list[Source]]:
    """把 V12 ``SearchAgentOutputState.citations`` 映射为框架 ``Source`` 列表（PRD §Q6）。

    key = ``item_id``（forward→``argument_id`` / reverse→``hypothesis_id``），经
    ``citation.task_ids`` → ``result.task_id`` → ``result.item_id`` 反查（``CitationRecord``
    不直载 ``item_id``；:meth:`SearchAgentOutputState.validate_public_references` 保证
    ``result.task_id in citation.task_ids`` 的双向绑定）。一条 citation 被多 task 共享
    （output_adapter 合并去重）则同时落入多个 item_id key——每 item 各得一份证据。

    字段映射：``source_id←citation_id``、``kind←source_type``（WEB→network /
    KNOWLEDGE_BASE→knowledge_base / STRUCTURED_DATA→structured）、``origin←source_name``、
    ``title←title``、``snippet←content``（**非 summary**——``content`` 是 judge 从原文抽的
    真实证据片段 ``" ".join(quoted_spans)[:600]``、本就是 snippet 语义；``summary`` 是关系
    模板句、零证据原文，喂给 judgment 等于没给证据可读）、``locator←url``。

    全映射（ACCEPTED + DEGRADED）：``status`` 非拒（DEGRADED 是「仅片段」提取但已过 V12 全部
    质量闸）的 citation 全落 ``citations``；``Source`` schema 无 status 字段（Q2 不动），
    故全映射让 judgment 按内容自加权。verdict 丢弃（Q1：judgment 重判、无双倍 LLM 成本）。
    """

    task_id_to_item_id: dict[str, str] = {
        result.task_id: result.item_id for result in output.results
    }
    out: dict[str, list[Source]] = {}
    for citation in output.citations:
        kind = SOURCE_TYPE_TO_KIND[citation.source_type]
        source = Source(
            source_id=citation.citation_id,
            kind=kind,
            origin=citation.source_name,
            title=citation.title,
            snippet=citation.content,
            locator=citation.url,
        )
        for task_id in citation.task_ids:
            item_id = task_id_to_item_id.get(task_id)
            if item_id is None:
                # citation 绑定的 task_id 不在 results（理论不可能——output 校验保证；
                # 防御性跳过，不抛、不卡流水线）。
                continue
            out.setdefault(item_id, []).append(source)
    return out


# --------------------------------------------------------------------------- #
# payload 构造纯函数（PRD §Q2/Q5/Q7/Q8/Q9）
# --------------------------------------------------------------------------- #

_HYPOTHESIS_RELATION_TO_REVERSE: dict[HypothesisRelation, str] = {
    HypothesisRelation.OPPOSE: "oppose",
    HypothesisRelation.ADVANCE: "advance",
    HypothesisRelation.EXPAND: "expand",
}


def _document_fingerprint(paragraph_list: list[ParagraphRecord]) -> str:
    """内容指纹：``"doc-" + blake2b(段原文拼接, digest_size=12)``（PRD §Q9）。

    确定性、跨段 / 跨 resume 稳定、只 hash 串不外泄原文。``original_doc`` bytes 不在
    ``RetrievalFn`` 5 输入内（Slice 1 锁定 5 输入、未含 ``original_doc``），故从
    ``paragraph_list.original_content`` 拼接派生——paragraph_list 是 original_doc 的分区，
    拼接是其等价内容指纹，满足 §Q9 全部意图（确定性 / 稳定 / 不泄原文）。
    """

    joined = "\n\n".join(record.original_content for record in paragraph_list)
    digest = hashlib.blake2b(joined.encode("utf-8", errors="surrogateescape"), digest_size=12).hexdigest()
    return f"doc-{digest}"


def build_search_agent_payload(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    session_context: SessionContext,
    paragraph_list: list[ParagraphRecord],
) -> list[SearchAgentInputState]:
    """构造 per-段 V12 ``SearchAgentInputState`` 列表（PRD §Q2/Q5/Q7/Q8/Q9）。

    V12 ``ainvoke`` 单段输入（``paragraph: ParagraphInput``），故适配器按段展开为多条
    payload、逐段 ainvoke、汇总 citations。每段 payload：

    - ``forward_items``：该段 ``argument_tree_ids`` 对应的 ``Argument`` 节点，每节点一条
      :class:`ForwardItem`（``item_id=argument_id``、``target_text=redact_query(段原文)``、
      ``required_slots=[]``）。forward ``target_text`` = 段 ``original_content``（``Argument``
      无文本字段、ADR-0025 代价；同段多节点共享同 target_text、靠 ``item_id`` + 空
      ``required_slots`` 区分、保留 per-argument 粒度）。
    - ``reverse_items``：该段节点的 ``hypotheses``，每条假说一个 :class:`ReverseItem`
      （``item_id=hypothesis_id``、``target_text=redact_query(hypothesis.text)``、
      ``relation_to_original`` ← ``HypothesisRelation``、``required_slots=[]``）。
    - ``paragraph_text`` = ``redact_query(段原文)``；``argument_context`` = 空
      :class:`ArgumentContext`（§Q8：``Argument`` 无祖先文本字段）。
    - id 映射（§Q9）：``request_id ← session_context.session_id``（空则 mint uuid 兜底）、
      ``document_id ← _document_fingerprint``、``user_id ← session_context.user_id or None``。

    合规重承载（§Q5）：构造 payload 前对 forward/reverse 的 ``target_text`` 与
    ``paragraph_text`` 跑框架 :func:`redact_query`（V12 ``tracing.redact`` 仅 trace、非
    出网查询）；domain whitelist 作废（开放式全网检索、PRD §6 已记录偏差）。
    """

    document_id = _document_fingerprint(paragraph_list)
    request_id = session_context.session_id or str(uuid.uuid4())
    user_id = session_context.user_id or None
    # argument_id → paragraph 反查：每节点恰属一段（ADR-0001），正向存于 paragraph.argument_tree_ids。
    argument_ids_by_para = {
        argument.argument_id: argument for argument in argument_tree
    }

    payloads: list[SearchAgentInputState] = []
    for record in paragraph_list:
        redacted_para = redact_query(record.original_content)
        forward_items: list[ForwardItem] = []
        reverse_items: list[ReverseItem] = []
        for argument_id in record.argument_tree_ids:
            argument = argument_ids_by_para.get(argument_id)
            if argument is None:
                continue
            forward_items.append(
                ForwardItem(
                    item_id=argument.argument_id,
                    target_text=redacted_para,
                    required_slots=[],
                )
            )
            for hypothesis in hypotheses.get(argument.argument_id, []):
                reverse_items.append(
                    ReverseItem(
                        item_id=hypothesis.hypothesis_id,
                        target_text=redact_query(hypothesis.text),
                        relation_to_original=_HYPOTHESIS_RELATION_TO_REVERSE[
                            hypothesis.relation
                        ],
                        required_slots=[],
                    )
                )
        payloads.append(
            SearchAgentInputState(
                request_id=request_id,
                document_id=document_id,
                user_id=user_id,
                paragraph={
                    "paragraph_id": record.paragraph_id,
                    "paragraph_text": redacted_para,
                    "forward_items": forward_items,
                    "reverse_items": reverse_items,
                    "argument_context": ArgumentContext(),
                },
            )
        )
    return payloads


# --------------------------------------------------------------------------- #
# 离线 Fake runtime（测试 seam 注入用；不联网、不调 LLM）
# --------------------------------------------------------------------------- #


class FakeSearchAgentRuntime:
    """伪 :class:`RetrievalRuntime`：返回 canned 输出、不联网。

    供离线映射 / 桥接 / 节点测试在该 seam 注入（PRD §Testing「伪 runtime 在该 seam 注入」）。
    ``responder`` 收 payload（``SearchAgentInputState`` 或 dict）、返合法
    ``SearchAgentOutputState``（或其 dict）；默认返空输出（无触达、tracer-bullet 守住）。
    """

    def __init__(
        self,
        responder: Any | None = None,
    ) -> None:
        self._responder = responder
        self.invoked_payloads: list[Any] = []
        self.aclose_calls = 0

    async def ainvoke(self, payload: Any) -> dict[str, Any]:
        self.invoked_payloads.append(payload)
        if self._responder is None:
            return cast(dict[str, Any], _empty_output(payload).model_dump(mode="json"))
        result = self._responder(payload)
        if isinstance(result, dict):
            return result
        return cast(dict[str, Any], result.model_dump(mode="json"))

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _empty_output(payload: Any) -> SearchAgentOutputState:
    """给定 payload 构造合法空输出（无 citations、无触达）——tracer-bullet 兜底。"""

    paragraph_id = _payload_paragraph_id(payload)
    request_id = _payload_request_id(payload)
    document_id = _payload_document_id(payload)
    return SearchAgentOutputState.model_validate(
        {
            "request_id": request_id,
            "document_id": document_id,
            "paragraph_id": paragraph_id,
            "run_status": {
                "status": "SUCCESS",
                "completed_task_count": 0,
                "partial_task_count": 0,
                "error_task_count": 0,
                "message": None,
            },
            "results": [],
            "citations": [],
            "warnings": [],
            "trace": {},
        }
    )


def _payload_paragraph_id(payload: Any) -> str:
    para = _payload_paragraph(payload)
    if isinstance(para, dict):
        return cast(str, para.get("paragraph_id") or "p0")
    return cast(str, getattr(para, "paragraph_id", "p0"))


def _payload_request_id(payload: Any) -> str:
    if isinstance(payload, dict):
        return cast(str, payload.get("request_id") or "req")
    return cast(str, getattr(payload, "request_id", "req"))


def _payload_document_id(payload: Any) -> str:
    if isinstance(payload, dict):
        return cast(str, payload.get("document_id") or "doc")
    return cast(str, getattr(payload, "document_id", "doc"))


def _payload_paragraph(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("paragraph")
    return getattr(payload, "paragraph", None)


# 静默未使用 import（TimeRange 仅作类型文档出现于 RetrievalFn 签名，本模块不直接调）
_ = TimeRange
