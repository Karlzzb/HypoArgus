"""翻译层 ``EventTranslator`` 单测（T-05·ADR-0023·PRD §6.4）。

验 ``astream_events`` v2 词汇 → §6.4 事件映射、``event_seq`` 单 trace 内从 0 单调自增 +
续跑顺延、``node_instance`` 区分回放环、``visible=False`` 节点过滤、非阻塞写 / 写失败降级、
``human_pause`` / ``stream_finish`` / ``stream_abort`` 终态事件。用合成 raw event dict 喂
翻译层（public ``feed`` / ``emit_*`` / ``flush`` 接口），读 :class:`InMemoryTraceEventStore`
断言产出事件——不触及真图 / 真 LLM（真图集成见 :mod:`tests.test_api_run_translation`）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessageChunk

from agents.assembly import MANIFEST
from api_layer.graph_view import VisibilityConfig, compute_hidden_names
from api_layer.trace_store import EventType, InMemoryTraceEventStore
from api_layer.translator import EventTranslator

_TRACE = "t1"
_SESS = "sess-1"


def _manifest_index() -> dict[str, Any]:
    return {e.name: e for e in MANIFEST}


def _hidden(visibility: VisibilityConfig) -> frozenset[str]:
    return compute_hidden_names(MANIFEST, visibility)


def _raw(
    event: str,
    name: str,
    *,
    run_id: str = "r",
    tags: list[str] | None = None,
    parent_ids: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event": event,
        "name": name,
        "run_id": run_id,
        "tags": tags or [],
        "parent_ids": parent_ids or [],
        "data": data or {},
    }


def _node_raw(event: str, node: str, *, run_id: str, step: int, data: dict[str, Any]) -> dict[str, Any]:
    return _raw(
        event,
        node,
        run_id=run_id,
        tags=[f"graph:step:{step}"],
        data=data,
    )


async def _collect(translator: EventTranslator) -> list[Any]:
    """flush 后取 store 全量事件（按 seq）。"""

    await translator.flush()
    return await translator._store.events_for_trace(_TRACE)


# --------------------------------------------------------------------------- #
# trace_start：仅首段产、resume 段不产
# --------------------------------------------------------------------------- #


async def test_trace_start_emitted_only_on_first_segment() -> None:
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    rows = await _collect(t)
    assert len(rows) == 1
    assert rows[0].event_seq == 0
    assert rows[0].event_type is EventType.TRACE_START


async def test_trace_start_not_emitted_on_resume_segment() -> None:
    """resume 段（start_seq > 0）不产 trace_start——同 trace 已有首推。"""

    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=5,  # 模拟续跑：max_seq=4 → start 5
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    await t.feed(_raw("on_chain_end", "LangGraph", run_id="root"))
    rows = await _collect(t)
    assert rows == []  # 无 trace_start、无 LangGraph 级事件


# --------------------------------------------------------------------------- #
# node_start / node_output / node_end + node_instance 单调
# --------------------------------------------------------------------------- #


async def test_node_lifecycle_events_with_instance_zero() -> None:
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id="rn1", step=1, data={"input": {"x": 1}}))
    await t.feed(_node_raw("on_chain_stream", "parse+partition", run_id="rn1", step=1, data={"chunk": {"y": 2}}))
    await t.feed(_node_raw("on_chain_end", "parse+partition", run_id="rn1", step=1, data={"output": {"y": 2}}))

    rows = await _collect(t)
    node_rows = [r for r in rows if r.event_type in (EventType.NODE_START, EventType.NODE_OUTPUT, EventType.NODE_END)]
    assert [r.event_type for r in node_rows] == [
        EventType.NODE_START,
        EventType.NODE_OUTPUT,
        EventType.NODE_END,
    ]
    assert all(r.payload["node_id"] == "parse+partition" for r in node_rows)
    assert all(r.payload["node_instance"] == 0 for r in node_rows)
    assert node_rows[0].payload["label"] == "解析+切分"
    assert node_rows[0].payload["type"] == "parse"
    assert node_rows[0].payload["color"] == "#4A90D9"
    assert node_rows[0].payload["input"] == {"x": 1}
    assert node_rows[1].payload["output"] == {"y": 2}
    assert node_rows[2].payload["output"] == {"y": 2}
    # event_seq 单调从 0：trace_start(0) + 3 节点事件(1,2,3)
    assert [r.event_seq for r in rows] == [0, 1, 2, 3]
    assert rows[0].event_type is EventType.TRACE_START


async def test_node_instance_distinguishes_replay_loop() -> None:
    """同节点第二次触发（回放环）→ node_instance 从 0 → 1。"""

    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    for i in range(2):
        await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=f"rn{i}", step=1, data={"input": {}}))
        await t.feed(_node_raw("on_chain_end", "parse+partition", run_id=f"rn{i}", step=1, data={"output": {}}))
    rows = await _collect(t)
    starts = [r for r in rows if r.event_type is EventType.NODE_START]
    assert [r.payload["node_instance"] for r in starts] == [0, 1]


# --------------------------------------------------------------------------- #
# llm_thinking：token + full_thought 累积
# --------------------------------------------------------------------------- #


async def test_llm_thinking_accumulates_full_thought() -> None:
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    node_run = "rn-parse"
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=node_run, step=1, data={"input": {}}))
    await t.feed(_raw("on_llm_start", "ChatModel", run_id="llm1", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_llm_stream",
            "ChatModel",
            run_id="llm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="Hello")},
        )
    )
    await t.feed(
        _raw(
            "on_llm_stream",
            "ChatModel",
            run_id="llm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content=" world")},
        )
    )
    await t.feed(_raw("on_llm_end", "ChatModel", run_id="llm1", parent_ids=[node_run]))

    rows = await _collect(t)
    thinks = [r for r in rows if r.event_type is EventType.LLM_THINKING]
    assert len(thinks) == 2
    assert thinks[0].payload["token"] == "Hello"
    assert thinks[0].payload["full_thought"] == "Hello"
    assert thinks[1].payload["token"] == " world"
    assert thinks[1].payload["full_thought"] == "Hello world"
    assert thinks[0].payload["node_id"] == "parse+partition"


async def test_llm_thinking_empty_token_skipped() -> None:
    """空 content chunk（如 tool_call 元数据）不产 llm_thinking。"""

    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    node_run = "rn-parse"
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=node_run, step=1, data={"input": {}}))
    await t.feed(_raw("on_llm_start", "ChatModel", run_id="llm1", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_llm_stream",
            "ChatModel",
            run_id="llm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="")},
        )
    )
    rows = await _collect(t)
    assert [r for r in rows if r.event_type is EventType.LLM_THINKING] == []


# --------------------------------------------------------------------------- #
# on_chat_model_*：langchain chat 模型走 on_chat_model_{start,stream,end}
# （非 on_llm_*）；翻译层须同样 mint llm_thinking + 累积 full_thought
# --------------------------------------------------------------------------- #


async def test_chat_model_stream_mints_llm_thinking() -> None:
    """``on_chat_model_stream``（真 Qwen ChatOpenAI / BaseChatModel 词汇）须与
    ``on_llm_stream`` 同样产 ``LLM_THINKING``，含 token + 累积 full_thought。"""

    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    node_run = "rn-parse"
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=node_run, step=1, data={"input": {}}))
    await t.feed(_raw("on_chat_model_start", "ChatModel", run_id="cm1", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_chat_model_stream",
            "ChatModel",
            run_id="cm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="Hello")},
        )
    )
    await t.feed(
        _raw(
            "on_chat_model_stream",
            "ChatModel",
            run_id="cm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content=" world")},
        )
    )
    await t.feed(_raw("on_chat_model_end", "ChatModel", run_id="cm1", parent_ids=[node_run]))

    rows = await _collect(t)
    thinks = [r for r in rows if r.event_type is EventType.LLM_THINKING]
    assert len(thinks) == 2
    assert thinks[0].payload["token"] == "Hello"
    assert thinks[0].payload["full_thought"] == "Hello"
    assert thinks[1].payload["token"] == " world"
    assert thinks[1].payload["full_thought"] == "Hello world"
    assert all(r.payload["node_id"] == "parse+partition" for r in thinks)


async def test_chat_model_start_end_bookkeeps_full_thought_buffer() -> None:
    """``on_chat_model_start`` 须 seed ``_llm_full[run_id]``；``on_chat_model_end``
    须清空——之后同 run_id 再来 stream 不应串到旧 buffer。"""

    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    node_run = "rn-parse"
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=node_run, step=1, data={"input": {}}))
    await t.feed(_raw("on_chat_model_start", "ChatModel", run_id="cm1", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_chat_model_stream",
            "ChatModel",
            run_id="cm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="first")},
        )
    )
    await t.feed(_raw("on_chat_model_end", "ChatModel", run_id="cm1", parent_ids=[node_run]))
    # end 后 buffer 应清空——同 run_id 再来一个 stream（理论上不会发生，但验证 seed/clear）：
    # on_chat_model_start 重置 buffer，新一段累积从空开始。
    await t.feed(_raw("on_chat_model_start", "ChatModel", run_id="cm1", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_chat_model_stream",
            "ChatModel",
            run_id="cm1",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="second")},
        )
    )

    rows = await _collect(t)
    thinks = [r for r in rows if r.event_type is EventType.LLM_THINKING]
    assert len(thinks) == 2
    assert thinks[0].payload["full_thought"] == "first"
    assert thinks[1].payload["full_thought"] == "second"


# --------------------------------------------------------------------------- #
# tool_call
# --------------------------------------------------------------------------- #


async def test_tool_call_emitted_with_args() -> None:
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    node_run = "rn-judg"
    await t.feed(_node_raw("on_chain_start", "judgment", run_id=node_run, step=4, data={"input": {}}))
    await t.feed(
        _raw(
            "on_tool_start",
            "search_tool",
            run_id="tool1",
            parent_ids=[node_run],
            data={"input": {"q": "foo"}},
        )
    )
    rows = await _collect(t)
    calls = [r for r in rows if r.event_type is EventType.TOOL_CALL]
    assert len(calls) == 1
    assert calls[0].payload["node_id"] == "judgment"
    assert calls[0].payload["name"] == "search_tool"
    assert calls[0].payload["args"] == {"q": "foo"}


# --------------------------------------------------------------------------- #
# visible=False 过滤：节点级丢弃、trace 级保留
# --------------------------------------------------------------------------- #


async def test_hidden_node_events_dropped_trace_level_kept() -> None:
    """隐藏 retrieval 节点：其 node_* / llm_thinking / tool_call 丢弃，
    trace_start / human_pause / stream_finish 保留。"""

    vis = VisibilityConfig(hidden=frozenset({"retrieval"}))
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(vis),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    node_run = "rn-ret"
    await t.feed(_node_raw("on_chain_start", "retrieval", run_id=node_run, step=3, data={"input": {}}))
    # LLM / tool 事件在节点执行期间（on_chain_start 之后、on_chain_end 之前）触发。
    await t.feed(_raw("on_llm_start", "M", run_id="llm9", parent_ids=[node_run]))
    await t.feed(
        _raw(
            "on_llm_stream",
            "M",
            run_id="llm9",
            parent_ids=[node_run],
            data={"chunk": AIMessageChunk(content="x")},
        )
    )
    await t.feed(_raw("on_tool_start", "t", run_id="tool9", parent_ids=[node_run], data={"input": {}}))
    await t.feed(_node_raw("on_chain_stream", "retrieval", run_id=node_run, step=3, data={"chunk": {"z": 9}}))
    await t.feed(_node_raw("on_chain_end", "retrieval", run_id=node_run, step=3, data={"output": {"z": 9}}))
    # trace 级保留：
    await t.emit_human_pause("retrieval", "问", "提示", {"k": 1})
    await t.emit_stream_finish()

    rows = await _collect(t)
    types = [r.event_type for r in rows]
    assert EventType.NODE_START not in types
    assert EventType.NODE_OUTPUT not in types
    assert EventType.NODE_END not in types
    assert EventType.LLM_THINKING not in types
    assert EventType.TOOL_CALL not in types
    assert EventType.TRACE_START in types
    assert EventType.HUMAN_PAUSE in types
    assert EventType.STREAM_FINISH in types
    pause = [r for r in rows if r.event_type is EventType.HUMAN_PAUSE][0]
    assert pause.payload == {"node_id": "retrieval", "question": "问", "hint": "提示", "detail": {"k": 1}}


# --------------------------------------------------------------------------- #
# event_seq 续跑顺延无断层
# --------------------------------------------------------------------------- #


async def test_event_seq_continues_from_start_seq_gap_free() -> None:
    """首段产 0..2；续跑段 start_seq=3 产 3..4，无断层。"""

    store = InMemoryTraceEventStore()
    t1 = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t1.feed(_raw("on_chain_start", "LangGraph", run_id="root1"))
    await t1.feed(_node_raw("on_chain_start", "parse+partition", run_id="rn1", step=1, data={"input": {}}))
    await t1.feed(_node_raw("on_chain_end", "parse+partition", run_id="rn1", step=1, data={"output": {}}))
    await t1.flush()
    assert await store.max_seq(_TRACE) == 2

    t2 = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=await store.max_seq(_TRACE) + 1,  # 3
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t2.feed(_raw("on_chain_start", "LangGraph", run_id="root2"))  # resume 段不产 trace_start
    await t2.feed(_node_raw("on_chain_start", "hitl1", run_id="rn2", step=2, data={"input": {}}))
    await t2.emit_stream_finish()
    await t2.flush()
    rows = await store.events_for_trace(_TRACE)
    assert [r.event_seq for r in rows] == [0, 1, 2, 3, 4]
    assert rows[3].event_type is EventType.NODE_START
    assert rows[4].event_type is EventType.STREAM_FINISH


# --------------------------------------------------------------------------- #
# 非阻塞：慢写不阻 feed 推进；写失败降级不抛
# --------------------------------------------------------------------------- #


class _SlowStore(InMemoryTraceEventStore):
    """append 每次 sleep：测非阻塞——feed 不应等 store。"""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay

    async def append(self, event: Any) -> None:
        await asyncio.sleep(self._delay)
        await super().append(event)


class _FailingStore(InMemoryTraceEventStore):
    async def append(self, event: Any) -> None:
        raise RuntimeError("store-boom")


async def test_slow_store_does_not_block_feed() -> None:
    """慢写（每次 50ms）6 事件：feed 循环应远小于 6*50ms（非阻塞），flush 才等。"""

    store = _SlowStore(delay=0.05)
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    t0 = time.monotonic()
    for i in range(6):
        await t.feed(_node_raw("on_chain_start", "parse+partition", run_id=f"rn{i}", step=1, data={"input": {}}))
    feed_elapsed = time.monotonic() - t0
    # 非阻塞：feed 仅入队，不等慢写 → 6 次 feed 应 << 6*0.05=0.3s。
    assert feed_elapsed < 0.1, f"feed 阻塞了：{feed_elapsed:.3f}s"
    await t.flush()  # flush 等 drainer 排空（≥ 一次延迟）。
    rows = await store.events_for_trace(_TRACE)
    assert len(rows) == 7  # trace_start + 6 node_start


async def test_failing_store_does_not_propagate() -> None:
    """写失败降级记错、不抛——flush / feed 不杀调用方，事件不入库。"""

    store = _FailingStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(_raw("on_chain_start", "LangGraph", run_id="root"))
    await t.feed(_node_raw("on_chain_start", "parse+partition", run_id="rn1", step=1, data={"input": {}}))
    await t.emit_stream_finish()
    await t.flush()  # 不抛
    assert await store.events_for_trace(_TRACE) == []  # 全失败 → 空


# --------------------------------------------------------------------------- #
# stream_abort
# --------------------------------------------------------------------------- #


async def test_stream_abort_carries_reason() -> None:
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.emit_stream_abort("GRAPH_TIMEOUT")
    rows = await _collect(t)
    assert len(rows) == 1
    assert rows[0].event_type is EventType.STREAM_ABORT
    assert rows[0].payload == {"abort_reason": "GRAPH_TIMEOUT"}


# --------------------------------------------------------------------------- #
# bytes / pydantic 入参安全序列化为 JSON
# --------------------------------------------------------------------------- #


async def test_node_input_with_bytes_and_pydantic_is_json_safe() -> None:
    from agents.hitl1 import Hitl1Question
    from domain import Argument, ArgumentStatus, ArgumentType

    arg = Argument(
        argument_id="n0001",
        argument_type=ArgumentType.MAIN_CLAIM,
        parent_id=None,
        children_ids=[],
        paragraph_id="p0001",
        content="主论点",
        argument_weight=50,
        status=ArgumentStatus.UNVERIFIED,
    )
    store = InMemoryTraceEventStore()
    t = EventTranslator(
        store,
        session_id=_SESS,
        trace_id=_TRACE,
        start_seq=0,
        manifest_index=_manifest_index(),
        hidden=_hidden(VisibilityConfig()),
    )
    await t.feed(
        _node_raw(
            "on_chain_start",
            "parse+partition",
            run_id="rn1",
            step=1,
            data={"input": {"original_doc": b"raw bytes \xff", "arg": arg}},
        )
    )
    await t.feed(
        _node_raw(
            "on_chain_end",
            "parse+partition",
            run_id="rn1",
            step=1,
            data={"output": {"q": Hitl1Question(argument_tree=[arg])}},
        )
    )
    rows = await _collect(t)
    start = [r for r in rows if r.event_type is EventType.NODE_START][0]
    assert start.payload["input"]["original_doc"] == "raw bytes \udcff"
    assert start.payload["input"]["arg"]["argument_id"] == "n0001"
    end = [r for r in rows if r.event_type is EventType.NODE_END][0]
    assert end.payload["output"]["q"]["argument_tree"][0]["argument_id"] == "n0001"
    # ts tz-aware
    assert rows[0].ts.tzinfo is not None
    _ = datetime.now(tz=UTC)  # 触碰 UTC import
