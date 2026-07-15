"""TASK-SA-2 真实检索适配器离线测试（Slice 2 · PRD §Testing Decisions）。

覆盖四类离线（不联网、不调 LLM）契约：

- **映射测**：给定 canned ``SearchAgentOutputState``，``map_citations`` 映射出的 ``Source`` 列表
  形状正确（字段映射、key=item_id、ACCEPTED+DEGRADED 全映射、``snippet=content``）。
- **桥接测**：同步 ``RetrievalFn`` 在「调用方无运行 loop」（threadpool 模拟）下经 worker loop
  正确拿到 async runtime 结果。
- **节点测**：注入真适配器（背 ``FakeSearchAgentRuntime``）到 ``Agents``，跑 retrieval 节点，
  断言 ``citations`` channel 写入正确、``paragraph_list`` 被读用于 ``target_text``。
- **tracer-bullet 测**：无触达段终稿逐字节等于原文（真实后端未触达时既有契约不破）。

seam 数 = 1（``RetrievalFn`` Protocol）；伪 runtime 在该 seam 注入。镜像既有
``tests/test_orchestrator_e2e.py`` 的 ``replace(base, retrieval=...)`` 注入式范式与
``tests/test_orchestrator_fallback.py`` 的异常兜底范式。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agents.assembly import create_real_agents, create_stub_agents
from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.parser import FakeLlmClient
from agents.retrieval import (
    FakeSearchAgentRuntime,
    build_real_retrieval,
    map_citations,
    real_retrieval,
)
from agents.retrieval.contract import build_search_agent_payload
from domain import (
    Argument,
    ArgumentType,
    Hypothesis,
    HypothesisRelation,
    ParagraphRecord,
    SessionContext,
)
from infra.retrieval import RetrievalKind, Source
from runtime.orchestrator import Orchestrator

# --------------------------------------------------------------------------- #
# canned SearchAgentOutputState 构造辅助（绕过 vendored 构造链、直给合法模型）
# --------------------------------------------------------------------------- #


def _judgment(
    *,
    confidence: float = 0.8,
    directness: float = 0.7,
    quote_match_mode: str = "SNIPPET",
) -> dict[str, Any]:
    return {
        "confidence": confidence,
        "directness": directness,
        "supported_claim_ids": [],
        "refuted_claim_ids": [],
        "reason": "deterministic fixture judgment",
        "scope_compatible": True,
        "scope_mismatch_reasons": [],
        "quote_match_mode": quote_match_mode,
    }


def _provenance() -> dict[str, Any]:
    return {
        "query_ids": [],
        "tool_call_id": None,
        "scenario_key": None,
        "dataset_id": None,
        "query_execution_id": None,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "published_at": None,
        "content_fingerprint": "fp-content",
        "source_evidence_fingerprint": "fp-source",
    }


def _citation(
    citation_id: str,
    *,
    task_ids: list[str],
    content: str,
    source_type: str = "WEB",
    source_name: str = "volcano-web",
    url: str | None = "https://example.org/evidence",
    relation: str = "SUPPORT",
    status: str = "ACCEPTED",
    summary: str = "该来源给出了支持\"X\"的直接事实。",
    title: str | None = "Evidence Title",
) -> dict[str, Any]:
    """一条 canned CitationRecord dict（含 judgment/provenance 必填）。"""

    return {
        "citation_id": citation_id,
        "task_ids": task_ids,
        "content": content,
        "summary": summary,
        "title": title,
        "source_type": source_type,
        "source_name": source_name,
        "url": url,
        "document_id": None,
        "knowledge_id": None,
        "file_id": None,
        "chunk_id": None,
        "page": None,
        "relation": relation,
        "status": status,
        "judgment": _judgment(),
        "provenance": _provenance(),
    }


def _task(
    task_id: str,
    *,
    item_id: str,
    line_type: str = "forward",
    verdict: str = "SUPPORTED",
    citation_ids: list[str] | None = None,
    conclusion_summary: str = "证据支持该论点。",
) -> dict[str, Any]:
    """一条 canned TaskDecision dict（conclusion_summary 与 verdict 一致、过校验）。"""

    return {
        "task_id": task_id,
        "item_id": item_id,
        "node_id": item_id,
        "hypothesis_id": None,
        "line_type": line_type,
        "target_text": "fixture target text",
        "run_status": "SUCCESS",
        "verdict": verdict,
        "confidence": 0.8,
        "conclusion_summary": conclusion_summary,
        "citation_ids": citation_ids or [],
        "supporting_citation_ids": [],
        "refuting_citation_ids": [],
        "supplementary_citation_ids": [],
        "atomic_claim_results": [],
        "evidence_gap": None,
        "warnings": [],
    }


def _output(
    *,
    results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    request_id: str = "req-1",
    document_id: str = "doc-fixture",
    paragraph_id: str = "p0",
) -> Any:
    """构造合法 ``SearchAgentOutputState``（过 reference / uniqueness 校验）。"""

    from search_agent.evidence_retrieval.public_contracts import SearchAgentOutputState

    return SearchAgentOutputState.model_validate(
        {
            "request_id": request_id,
            "document_id": document_id,
            "paragraph_id": paragraph_id,
            "run_status": {
                "status": "SUCCESS",
                "completed_task_count": len(results),
                "partial_task_count": 0,
                "error_task_count": 0,
                "message": None,
            },
            "results": results,
            "citations": citations,
            "warnings": [],
            "trace": {},
        }
    )


# --------------------------------------------------------------------------- #
# 映射测（PRD §Q6）
# --------------------------------------------------------------------------- #


def test_map_forward_accepted_citation_to_source() -> None:
    """一条 forward ACCEPTED citation → 一条 Source，落 key=item_id（=argument_id）。

    字段映射（PRD §Q6 表）：``source_id←citation_id``、``kind←source_type``（WEB→network）、
    ``origin←source_name``、``title←title``、``snippet←content``（非 summary）、
    ``locator←url``。
    """

    output = _output(
        results=[
            _task("req-1:task:0", item_id="n0001", citation_ids=["cit-a"]),
        ],
        citations=[
            _citation("cit-a", task_ids=["req-1:task:0"], content="真实证据片段原文"),
        ],
    )

    mapped = map_citations(output)

    assert set(mapped.keys()) == {"n0001"}
    [source] = mapped["n0001"]
    assert isinstance(source, Source)
    assert source.source_id == "cit-a"
    assert source.kind is RetrievalKind.NETWORK
    assert source.origin == "volcano-web"
    assert source.title == "Evidence Title"
    assert source.snippet == "真实证据片段原文"  # content、非 summary
    assert source.locator == "https://example.org/evidence"


def test_map_degraded_citation_also_landed() -> None:
    """DEGRADED citation 同样映射（非拒、是「仅片段」提取但已过 V12 全部质量闸）。

    ``Source`` schema 无 status 字段（Q2 不动），故 ACCEPTED + DEGRADED 全映射让 judgment
    按内容自加权——DEGRADED 不被丢、同样带 ``content``、同样绑 claim。
    """

    output = _output(
        results=[
            _task("req-1:task:0", item_id="n0001", citation_ids=["cit-a"]),
        ],
        citations=[
            _citation(
                "cit-a",
                task_ids=["req-1:task:0"],
                content="仅片段的真实证据原文",
                status="DEGRADED",
            ),
        ],
    )

    mapped = map_citations(output)
    [source] = mapped["n0001"]
    assert source.snippet == "仅片段的真实证据原文"


def test_map_accepted_and_degraded_coexist_under_same_item() -> None:
    """同一 item 下 ACCEPTED + DEGRADED 并存——全映射、两者都落该 key 的 Source 列表。"""

    output = _output(
        results=[
            _task(
                "req-1:task:0",
                item_id="n0001",
                citation_ids=["cit-acc", "cit-deg"],
            ),
        ],
        citations=[
            _citation(
                "cit-acc",
                task_ids=["req-1:task:0"],
                content="完整证据 A",
                status="ACCEPTED",
            ),
            _citation(
                "cit-deg",
                task_ids=["req-1:task:0"],
                content="片段证据 B",
                status="DEGRADED",
            ),
        ],
    )

    mapped = map_citations(output)
    assert sorted(s.source_id for s in mapped["n0001"]) == ["cit-acc", "cit-deg"]


def test_map_shared_citation_lands_under_both_item_ids() -> None:
    """一条 citation 被多 task 共享（output_adapter 合并去重）→ 同时落入两个 item_id key。

    key = item_id（forward→argument_id）；item 维度的 per-argument 证据粒度由此守住。
    """

    output = _output(
        results=[
            _task("req-1:task:0", item_id="n0001", citation_ids=["cit-shared"]),
            _task("req-1:task:1", item_id="n0002", citation_ids=["cit-shared"]),
        ],
        citations=[
            _citation(
                "cit-shared",
                task_ids=["req-1:task:0", "req-1:task:1"],
                content="被两节点共享的证据",
            ),
        ],
    )

    mapped = map_citations(output)
    assert set(mapped.keys()) == {"n0001", "n0002"}
    assert mapped["n0001"][0].source_id == "cit-shared"
    assert mapped["n0002"][0].source_id == "cit-shared"
    # 同一 Source 对象形状一致（snippet=content、origin、locator）。
    assert mapped["n0001"][0].snippet == "被两节点共享的证据"
    assert mapped["n0002"][0].snippet == "被两节点共享的证据"


def test_map_reverse_hypothesis_item_id_and_source_type_kinds() -> None:
    """reverse citation 落 key=hypothesis_id；source_type → kind 三类映射全测（PRD §Q6）。"""

    output = _output(
        results=[
            _task(
                "req-1:task:0",
                item_id="h_n0001",
                line_type="reverse",
                verdict="REFUTED",
                citation_ids=["cit-web", "cit-kb", "cit-sql"],
                conclusion_summary="证据反驳该假说。",
            ),
        ],
        citations=[
            _citation(
                "cit-web",
                task_ids=["req-1:task:0"],
                content="web 证据",
                source_type="WEB",
            ),
            _citation(
                "cit-kb",
                task_ids=["req-1:task:0"],
                content="kb 证据",
                source_type="KNOWLEDGE_BASE",
                url="kb://internal/doc-0",
            ),
            _citation(
                "cit-sql",
                task_ids=["req-1:task:0"],
                content="sql 证据",
                source_type="STRUCTURED_DATA",
                url="db://revenue_by_quarter/row-0",
            ),
        ],
    )

    mapped = map_citations(output)
    assert set(mapped.keys()) == {"h_n0001"}
    by_id = {s.source_id: s for s in mapped["h_n0001"]}
    assert by_id["cit-web"].kind is RetrievalKind.NETWORK
    assert by_id["cit-kb"].kind is RetrievalKind.KNOWLEDGE_BASE
    assert by_id["cit-sql"].kind is RetrievalKind.STRUCTURED


def test_map_empty_output_yields_empty_citations() -> None:
    """无 citations（真实后端未触达 / 未配置）→ 空映射、tracer-bullet 守住。"""

    output = _output(results=[_task("req-1:task:0", item_id="n0001")], citations=[])

    assert map_citations(output) == {}


# --------------------------------------------------------------------------- #
# payload 构造测（PRD §Q2/Q5/Q7/Q8/Q9）
# --------------------------------------------------------------------------- #


def _session(session_id: str = "sess-1", user_id: str = "u-1") -> SessionContext:
    import datetime

    return SessionContext(
        session_id=session_id,
        user_id=user_id,
        current_time=datetime.datetime(2026, 7, 15, 9, 0, 0),
        user_prompt="",
    )


def test_build_payload_forward_item_uses_paragraph_original_content() -> None:
    """forward ``target_text`` = 段 ``original_content``（``Argument`` 无文本字段、ADR-0025 代价）；
    ``item_id=argument_id``、``required_slots=[]``、``argument_context`` 空、per-段一条 payload。"""

    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="本段需要核验的事实陈述。",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)

    payloads = build_search_agent_payload(
        [argument], {}, _session(), [paragraph]
    )

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.request_id == "sess-1"
    assert payload.user_id == "u-1"
    assert payload.paragraph.paragraph_id == "p0"
    assert payload.paragraph.paragraph_text == "本段需要核验的事实陈述。"
    assert payload.paragraph.argument_context.argument_path == []
    [forward] = payload.paragraph.forward_items
    assert forward.item_id == "n0001"
    assert forward.target_text == "本段需要核验的事实陈述。"
    assert forward.required_slots == []
    assert payload.paragraph.reverse_items == []


def test_build_payload_redacts_pii_in_target_text_and_paragraph_text() -> None:
    """合规重承载（§Q5）：forward ``target_text`` 与 ``paragraph_text`` 经框架 ``redact_query``
    脱敏——V12 ``tracing.redact`` 仅 trace、非出网查询；脱敏在 seam 侧承载。"""

    pii_content = "联系人邮箱 john@example.com 可核实该陈述。"
    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content=pii_content,
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.EVIDENCE)

    payloads = build_search_agent_payload(
        [argument], {}, _session(), [paragraph]
    )

    payload = payloads[0]
    assert "[REDACTED]" in payload.paragraph.paragraph_text
    assert "john@example.com" not in payload.paragraph.paragraph_text
    [forward] = payload.paragraph.forward_items
    assert "[REDACTED]" in forward.target_text
    assert "john@example.com" not in forward.target_text


def test_build_payload_reverse_items_from_hypotheses() -> None:
    """reverse_items 来自该段节点的 ``hypotheses``：``item_id=hypothesis_id``、
    ``target_text=redact_query(hypothesis.text)``、``relation_to_original`` ← HypothesisRelation。"""

    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="原段。",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)
    hypotheses = {
        "n0001": [
            Hypothesis(
                hypothesis_id="h_n0001",
                text="对立假说 john@example.com",
                relation=HypothesisRelation.OPPOSE,
            ),
            Hypothesis(
                hypothesis_id="h_n0001b",
                text="递进假说",
                relation=HypothesisRelation.ADVANCE,
            ),
        ]
    }

    payloads = build_search_agent_payload(
        [argument], hypotheses, _session(), [paragraph]
    )

    [payload] = payloads
    reverse_by_id = {r.item_id: r for r in payload.paragraph.reverse_items}
    assert set(reverse_by_id) == {"h_n0001", "h_n0001b"}
    opp = reverse_by_id["h_n0001"]
    assert opp.target_text == "对立假说 [REDACTED]"
    assert opp.relation_to_original == "oppose"
    adv = reverse_by_id["h_n0001b"]
    assert adv.relation_to_original == "advance"


def test_build_payload_document_id_is_stable_content_fingerprint() -> None:
    """``document_id = "doc-" + blake2b(段原文拼接)[:12]``（§Q9）：确定性、跨段 / 跨 resume 稳定、
    只 hash 串不外泄原文。同输入两次构造相同；不同输入不同。"""

    import hashlib

    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="确定性原文 A。",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)

    payloads_a = build_search_agent_payload([argument], {}, _session(), [paragraph])
    payloads_b = build_search_agent_payload([argument], {}, _session(), [paragraph])
    assert payloads_a[0].document_id == payloads_b[0].document_id
    expected = "doc-" + hashlib.blake2b(
        "确定性原文 A。".encode(), digest_size=12
    ).hexdigest()
    assert payloads_a[0].document_id == expected
    assert payloads_a[0].document_id.startswith("doc-")
    # 不外泄原文：document_id 仅 hash 串、不含原文片段。
    assert "确定性原文" not in payloads_a[0].document_id


def test_build_payload_request_id_mints_uuid_when_session_empty() -> None:
    """``request_id ← session_context.session_id``（空则 mint uuid 兜底）；``user_id or None``（§Q9）。"""

    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="x",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)

    # 空 session_id → mint uuid 兜底（非空、确定性合法）。
    payloads = build_search_agent_payload(
        [argument], {}, _session(session_id="", user_id=""), [paragraph]
    )
    [payload] = payloads
    assert payload.request_id  # 非空
    assert payload.user_id is None  # 空 user_id → None（不兜底）
    # 显式 session_id → 直用。
    payloads2 = build_search_agent_payload(
        [argument], {}, _session(session_id="abc", user_id=""), [paragraph]
    )
    assert payloads2[0].request_id == "abc"


def test_build_payload_multiple_paragraphs_yield_one_payload_each() -> None:
    """多段 → 每段一条 payload（per-段 ainvoke）；段内多节点共享同 target_text、靠 item_id 区分。"""

    paragraphs = [
        ParagraphRecord(
            paragraph_id="p0",
            original_content="第一段事实。",
            argument_tree_ids=["n0001", "n0002"],
        ),
        ParagraphRecord(
            paragraph_id="p1",
            original_content="第二段事实。",
            argument_tree_ids=["n0003"],
        ),
    ]
    arguments = [
        Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM),
        Argument(argument_id="n0002", argument_type=ArgumentType.EVIDENCE),
        Argument(argument_id="n0003", argument_type=ArgumentType.SUB_CLAIM),
    ]

    payloads = build_search_agent_payload(
        arguments, {}, _session(), paragraphs
    )

    assert [p.paragraph.paragraph_id for p in payloads] == ["p0", "p1"]
    assert {f.item_id for f in payloads[0].paragraph.forward_items} == {"n0001", "n0002"}
    # 同段两节点共享同 target_text（段原文）。
    texts = {f.target_text for f in payloads[0].paragraph.forward_items}
    assert texts == {"第一段事实。"}
    assert payloads[1].paragraph.forward_items[0].item_id == "n0003"


# --------------------------------------------------------------------------- #
# 桥接测（PRD §Q4 / §Testing Decisions）：同步 RetrievalFn 在无运行 loop 下经 worker loop 拿结果
# --------------------------------------------------------------------------- #


def test_real_retrieval_bridges_async_runtime_via_worker_loop_without_caller_loop() -> None:
    """同步 ``real_retrieval`` 在「调用方无运行 loop」（threadpool 模拟）下经 worker loop
    正确拿到 async runtime 结果、并映射为 ``Source``。

    调用方线程无运行 loop（``asyncio.get_running_loop`` 抛）——证明桥接走
    ``run_coroutine_threadsafe`` + 专用 worker loop、非 ``asyncio.run``（loop-affine client 不跨 loop 炸）。
    """

    import asyncio
    import threading

    from domain import DEFAULT_QUERY_TIME_RANGE

    canned = _output(
        results=[
            _task("sess-1:task:0", item_id="n0001", citation_ids=["cit-1"]),
        ],
        citations=[
            _citation(
                "cit-1",
                task_ids=["sess-1:task:0"],
                content="bridge evidence snippet",
            ),
        ],
        request_id="sess-1",
        paragraph_id="p0",
    )
    runtime = FakeSearchAgentRuntime(responder=lambda payload: canned)

    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="需要核验的事实陈述。",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)

    holder: dict[str, object] = {}

    def caller() -> None:
        # 该线程无运行 loop（threadpool worker 模拟）——get_running_loop 必抛。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            holder["no_loop"] = True
        holder["out"] = real_retrieval(
            [argument],
            {},
            DEFAULT_QUERY_TIME_RANGE,
            _session(),
            [paragraph],
            runtime=runtime,
        )

    thread = threading.Thread(target=caller, name="test-threadpool-sim")
    thread.start()
    thread.join()

    assert holder.get("no_loop") is True  # 调用方确无运行 loop
    out = holder["out"]  # type: ignore[assignment]
    assert set(out.keys()) == {"n0001"}  # type: ignore[union-attr]
    [source] = out["n0001"]  # type: ignore[index]
    assert source.snippet == "bridge evidence snippet"
    assert runtime.invoked_payloads  # runtime.ainvoke 被调用（经 worker loop 桥接）


def test_real_retrieval_maps_across_multiple_paragraphs_and_dedupes_per_item() -> None:
    """多段 payload 逐段 ainvoke、汇总到同一 ``dict[str, list[Source]]``；不同段不同 item_id。"""

    import threading

    from domain import DEFAULT_QUERY_TIME_RANGE

    def responder(payload: object) -> Any:
        para = getattr(payload, "paragraph", None)
        pids: list[str] = [getattr(para, "paragraph_id", "p0")] if para is not None else []
        pid = pids[0] if pids else "p0"
        # 取该段首个 forward item_id 作 item 维度。
        forward_items = list(getattr(para, "forward_items", []) or [])
        item_id = forward_items[0].item_id if forward_items else "n0001"
        task_id = f"sess-1:task:{pid}"
        return _output(
            results=[
                _task(task_id, item_id=item_id, citation_ids=["cit-" + pid]),
            ],
            citations=[
                _citation(
                    "cit-" + pid,
                    task_ids=[task_id],
                    content=f"evidence for {pid}",
                ),
            ],
            request_id="sess-1",
            paragraph_id=pid,
        )

    runtime = FakeSearchAgentRuntime(responder=responder)
    paragraphs = [
        ParagraphRecord(
            paragraph_id="p0",
            original_content="第一段。",
            argument_tree_ids=["n0001"],
        ),
        ParagraphRecord(
            paragraph_id="p1",
            original_content="第二段。",
            argument_tree_ids=["n0002"],
        ),
    ]
    arguments = [
        Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM),
        Argument(argument_id="n0002", argument_type=ArgumentType.SUB_CLAIM),
    ]

    holder: dict[str, object] = {}

    def caller() -> None:
        holder["out"] = real_retrieval(
            arguments,
            {},
            DEFAULT_QUERY_TIME_RANGE,
            _session(),
            paragraphs,
            runtime=runtime,
        )

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join()

    out = holder["out"]  # type: ignore[assignment]
    assert set(out.keys()) == {"n0001", "n0002"}  # type: ignore[union-attr]
    assert out["n0001"][0].snippet == "evidence for p0"  # type: ignore[index]
    assert out["n0002"][0].snippet == "evidence for p1"  # type: ignore[index]


def test_real_retrieval_empty_output_keeps_citations_empty() -> None:
    """runtime 返空输出（真实后端未触达 / 未配置）→ 空 citations、不抛。"""

    import threading

    from domain import DEFAULT_QUERY_TIME_RANGE

    runtime = FakeSearchAgentRuntime()  # 默认返空输出
    paragraph = ParagraphRecord(
        paragraph_id="p0",
        original_content="未触达段。",
        argument_tree_ids=["n0001"],
    )
    argument = Argument(argument_id="n0001", argument_type=ArgumentType.SUB_CLAIM)

    holder: dict[str, object] = {}

    def caller() -> None:
        holder["out"] = real_retrieval(
            [argument],
            {},
            DEFAULT_QUERY_TIME_RANGE,
            _session(),
            [paragraph],
            runtime=runtime,
        )

    thread = threading.Thread(target=caller)
    thread.start()
    thread.join()

    assert holder["out"] == {}  # type: ignore[comparison-overlap]


# --------------------------------------------------------------------------- #
# 节点测（PRD §Testing Decisions）：注入真适配器（背伪 runtime）到 Agents，跑 retrieval 节点
# --------------------------------------------------------------------------- #


_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _per_paragraph_responder() -> Any:
    """返一个 responder：对每段 payload 产一条 citation，item_id 取自该段首个 forward item。"""

    def respond(payload: object) -> Any:
        para = getattr(payload, "paragraph", None)
        pid = getattr(para, "paragraph_id", "p0")
        forward_items = list(getattr(para, "forward_items", []) or [])
        item_id = forward_items[0].item_id if forward_items else f"n-{pid}"
        task_id = f"sess-1:task:{pid}"
        return _output(
            results=[
                _task(task_id, item_id=item_id, citation_ids=[f"cit-{pid}"]),
            ],
            citations=[
                _citation(
                    f"cit-{pid}",
                    task_ids=[task_id],
                    content=f"node-level evidence for {pid}",
                ),
            ],
            request_id="sess-1",
            paragraph_id=pid,
        )

    return respond


def test_retrieval_node_writes_citations_channel_with_real_adapter() -> None:
    """注入真适配器（背伪 runtime）经 ``replace(base, retrieval=...)``，跑 retrieval 节点，
    ``citations`` channel 写入正确（key=item_id、每段一条 Source、snippet=content）。

    ``paragraph_list`` 被读用于 ``target_text``：stub parse 产 3 段影子节点（n-p0001/2/3），
    适配器据 ``argument_tree_ids`` 构 forward_items → responder 据之产 citation。
    """

    base = create_stub_agents()
    runtime = FakeSearchAgentRuntime(responder=_per_paragraph_responder())
    agents = replace(base, retrieval=build_real_retrieval(runtime))
    orch = Orchestrator(agents=agents)

    sc = _session()
    state = orch.graph.invoke({"original_doc": _DOC, "session_context": sc})

    citations = state["citations"]
    # 三段 → 三个 item_id key（stub parse 产 n-p0001/p0002/p0003 影子节点）。
    assert set(citations.keys()) == {"n-p0001", "n-p0002", "n-p0003"}
    assert citations["n-p0001"][0].snippet == "node-level evidence for p0001"
    assert citations["n-p0002"][0].snippet == "node-level evidence for p0002"
    assert citations["n-p0003"][0].snippet == "node-level evidence for p0003"


def test_retrieval_node_reads_paragraph_list_for_target_text() -> None:
    """``paragraph_list`` 被读用于 forward ``target_text``：断言 payload 段原文取自 paragraph_list。

    用记录型 runtime 捕获 payload，断言 ``forward_items[0].target_text`` == 段
    ``original_content``（经 ``redact_query``——此处无 PII 即等价原文）。
    """

    base = create_stub_agents()
    seen_payloads: list[Any] = []

    async def capture_ainvoke(payload: Any) -> dict[str, Any]:
        seen_payloads.append(payload)
        return _empty_output_for_payload(payload)

    class _CaptureRuntime:
        invoked_payloads = seen_payloads

        async def ainvoke(self, payload: Any) -> dict[str, Any]:
            return await capture_ainvoke(payload)

        async def aclose(self) -> None:
            return None

    agents = replace(base, retrieval=build_real_retrieval(_CaptureRuntime()))  # type: ignore[arg-type]
    orch = Orchestrator(agents=agents)
    sc = _session()
    orch.graph.invoke({"original_doc": _DOC, "session_context": sc})

    assert len(seen_payloads) == 3  # 三段各一次 ainvoke
    first = seen_payloads[0]
    assert first.paragraph.paragraph_id == "p0001"
    # forward target_text == 该段 original_content（取自 paragraph_list）。
    [forward] = first.paragraph.forward_items
    assert forward.target_text == "主论点。"


def _empty_output_for_payload(payload: Any) -> dict[str, Any]:
    """对 payload 构造合法空输出 dict（无 citations、tracer-bullet 守住）。"""

    from search_agent.evidence_retrieval.public_contracts import SearchAgentOutputState

    para = getattr(payload, "paragraph", None)
    pid = getattr(para, "paragraph_id", "p0")
    req = getattr(payload, "request_id", "req")
    doc = getattr(payload, "document_id", "doc")
    return SearchAgentOutputState.model_validate(
        {
            "request_id": req,
            "document_id": doc,
            "paragraph_id": pid,
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
    ).model_dump(mode="json")


def test_tracer_bullet_real_adapter_empty_output_keeps_byte_identity() -> None:
    """真实后端未触达 / 未配置（runtime 返空 citations）→ 终稿逐字节等于原文（Story 17 / tracer bullet）。

    真实后端接入了（manifest ``real`` 工厂替换桩），但 runtime 产空 citations → 下游
    judgment / rewrite_loop 见无素材、不触达任何段 → 终稿逐字节还原。既有契约不破。
    """

    base = create_stub_agents()
    runtime = FakeSearchAgentRuntime()  # 默认返空输出
    agents = replace(base, retrieval=build_real_retrieval(runtime))
    orch = Orchestrator(agents=agents)

    sc = _session()
    report = orch.run_with_report(_DOC, session_context=sc)
    assert report.final_document == _DOC  # tracer bullet 字节级承诺
    assert report.errors == []
    state = orch.graph.invoke({"original_doc": _DOC, "session_context": sc})
    assert state["citations"] == {}  # 空 citations 落地


def test_real_adapter_runtime_exception_falls_back_to_empty_citations_and_logs() -> None:
    """真实适配器 runtime 异常 → ``_guarded`` 兜底空 citations + 日志、终稿逐字节还原（PRD §13）。"""

    base = create_stub_agents()

    class _ThrowingRuntime:
        async def ainvoke(self, payload: Any) -> dict[str, Any]:
            raise RuntimeError("retrieval boom")

        async def aclose(self) -> None:
            return None

    agents = replace(base, retrieval=build_real_retrieval(_ThrowingRuntime()))  # type: ignore[arg-type]
    orch = Orchestrator(agents=agents)

    sc = _session()
    report = orch.run_with_report(_DOC, session_context=sc)
    assert report.final_document == _DOC  # 降级空 citations 不触达原文
    assert any("retrieval" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# manifest 接线测（PRD §Solution / §定位线索 · Slice 2）：real= 工厂 + RealDeps.retrieval_runtime
# --------------------------------------------------------------------------- #


def _real_agents_with_retrieval(runtime: object) -> object:
    """``create_real_agents`` + Fake parse LLM + skip HITL-1 + 注入 retrieval runtime。"""

    return create_real_agents(
        llm=FakeLlmClient(),
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        retrieval_runtime=runtime,  # type: ignore[arg-type]
    )


def test_create_real_agents_with_retrieval_runtime_replaces_stub() -> None:
    """``retrieval_runtime`` 给出时 manifest ``real`` 工厂替换桩——retrieval 字段不再等于桩，
    且经 Orchestrator 跑通后 ``citations`` channel 写入真实 runtime 产出的 Source。"""

    fake = FakeSearchAgentRuntime(responder=_per_paragraph_responder())
    stub_retrieval = create_stub_agents().retrieval
    agents = _real_agents_with_retrieval(fake)
    assert agents.retrieval is not stub_retrieval  # 桩被替换

    orch = Orchestrator(agents=agents)  # type: ignore[arg-type]
    sc = _session()
    state = orch.graph.invoke({"original_doc": _DOC, "session_context": sc})
    # real parse（FakeLlmClient 空）产 bg-p0001/2/3 background 影子节点。
    assert set(state["citations"].keys()) == {"bg-p0001", "bg-p0002", "bg-p0003"}
    assert fake.invoked_payloads  # 真实 runtime 经 manifest 工厂被装配并调用


def test_create_real_agents_without_retrieval_runtime_keeps_stub() -> None:
    """``retrieval_runtime`` 缺省（``None``）→ manifest ``real`` 工厂返 ``None``、保留桩。
    retrieval 字段恒等于桩（identity）、产空 citations。"""

    stub_retrieval = create_stub_agents().retrieval
    agents = _real_agents_with_retrieval(runtime=None)
    assert agents.retrieval is stub_retrieval  # 未替换、桩保留

    orch = Orchestrator(agents=agents)  # type: ignore[arg-type]
    sc = _session()
    state = orch.graph.invoke({"original_doc": _DOC, "session_context": sc})
    assert state["citations"] == {}  # 桩产空 citations


def test_create_real_agents_real_retrieval_degrades_to_empty_on_empty_runtime_output() -> None:
    """真实 retrieval 装配 + runtime 返空输出 → 空 citations、终稿逐字节等于原文（tracer-bullet）。"""

    fake = FakeSearchAgentRuntime()  # 默认返空
    agents = _real_agents_with_retrieval(fake)
    orch = Orchestrator(agents=agents)  # type: ignore[arg-type]
    sc = _session()
    report = orch.run_with_report(_DOC, session_context=sc)
    assert report.final_document == _DOC
    assert report.errors == []


# --------------------------------------------------------------------------- #
# daemon worker loop + 进程级单例（PRD §Q4 · Slice 2）
# --------------------------------------------------------------------------- #


def test_lazy_search_agent_runtime_is_process_singleton() -> None:
    """``lazy_search_agent_runtime()`` 多次调用返同一实例——进程级单例、跨请求复用（PRD §4 reuse）。

    无 per-request 资源泄漏；spine / CLI 同源。不调 ainvoke（避免联网），仅验 holder 单例性。
    """

    from agents.retrieval import lazy_search_agent_runtime

    a = lazy_search_agent_runtime()
    b = lazy_search_agent_runtime()
    assert a is b


def test_worker_loop_is_running_daemon_and_singleton() -> None:
    """专用 daemon worker event loop 首调惰性起、后续复用同一 loop 且 ``is_running()``。"""

    from agents.retrieval.agent import _get_worker_loop

    loop = _get_worker_loop()
    assert loop.is_running()  # run_forever 在 daemon 线程
    assert _get_worker_loop() is loop  # 单例


def test_lazy_proxy_builds_runtime_on_worker_loop_on_first_ainvoke() -> None:
    """延迟 proxy 首 ``ainvoke`` 在 worker loop 上建 ``SearchAgentRuntime.from_env(with_llm=False)``
    单例、第二次复用同 runtime（不重建）——loop-affine client 首请求即绑 worker loop。

    离线可构造（Slice 0 守护）；构造不发请求、不需 VOLCANO/BISHENG 凭证。仅验单例复用性、
    不调真实 ainvoke（避免联网）。"""
    pytest.skip("lazy proxy 首调建 runtime 的行为由 Slice 3 real_llm 慢集成测试覆盖（真实 V12 子图触网）")







