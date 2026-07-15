"""PostgresSaver 序列化编解码器单测（T-03·ADR-0022）。

``OriginalParagraphs``（slots + ``MappingProxyType`` + ``bytes`` value）不被
langgraph 默认 ``JsonPlusSerializer`` 的 msgpack 编码器支持（``_msgpack_default``
只认 pydantic / dataclass / namedtuple / Enum / Item 等已知族，末尾抛
``TypeError``）。本切片为它注册自定义编解码器 ``HypoArgusSerializer``：
顶层把 ``OriginalParagraphs`` 摊成带哨兵键的纯数据信封（order + entries），
其余值原样委托 ``JsonPlusSerializer``。读回时据哨兵键还原。

纯函数子缝单测（不触 Postgres）：``dumps_typed → loads_typed`` 写读等价。
PG 落库往返见 ``test_interrupt_resume.py`` 的 checkpointer 集成测试。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from domain import (
    Argument,
    ArgumentType,
    Hypothesis,
    HypothesisRelation,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source
from original_paragraphs import OriginalParagraphs
from runtime.checkpoint import _DECODE_TAG, HypoArgusSerializer


def _op() -> OriginalParagraphs:
    return OriginalParagraphs.from_text(
        "主论点。\n\n分论点。\n\n论据。\n".encode()
    )


# ---- 最小图（仅 original_paragraphs channel）用于 PG 序列化往返集成测试 ---- #


def _merge_seen(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class _CkptOpState(TypedDict, total=False):
    op: OriginalParagraphs
    seen: Annotated[dict, _merge_seen]


def _ckpt_op_init(state: _CkptOpState) -> dict:
    return {"op": _op(), "seen": {"init": True}}


def _ckpt_op_graph() -> StateGraph:
    g = StateGraph(_CkptOpState)
    g.add_node("init", _ckpt_op_init)
    g.add_edge(START, "init")
    g.add_edge("init", END)
    return g


def test_serializer_round_trips_original_paragraphs_byte_exact() -> None:
    """OriginalParagraphs 经 dumps/loads 写读等价：段落序 + 每段 bytes 逐字节相同。"""

    serde = HypoArgusSerializer()
    op = _op()
    typ, blob = serde.dumps_typed(op)
    assert typ == "msgpack"  # 委托 JsonPlusSerializer 的 msgpack 通道

    back = serde.loads_typed((typ, blob))
    assert isinstance(back, OriginalParagraphs)
    assert back.paragraph_ids() == op.paragraph_ids()  # 段序保持
    for pid in op.paragraph_ids():
        assert back.get(pid) == op.get(pid)  # 每段 bytes 逐字节相同


def test_serializer_round_trips_empty_paragraphs_doc() -> None:
    """仅空白文档（partition 仍产段）写读等价。"""

    serde = HypoArgusSerializer()
    op = OriginalParagraphs.from_text(b"\n\n\n")
    typ, blob = serde.dumps_typed(op)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, OriginalParagraphs)
    assert back.paragraph_ids() == op.paragraph_ids()
    for pid in op.paragraph_ids():
        assert back.get(pid) == op.get(pid)


def test_serializer_delegates_other_state_values_unchanged() -> None:
    """非 OriginalParagraphs 的 state 值（pydantic / bytes / dict / 原生）原样委托，
    不被信封化、读回等价。"""

    serde = HypoArgusSerializer()

    # bytes（original_doc / final_document channel）
    for raw in (b"\xe5\x8e\x9f\xe6\x96\x87", b""):
        typ, blob = serde.dumps_typed(raw)
        assert serde.loads_typed((typ, blob)) == raw

    # 原生 dict[str, str]（proposed_rewrites / paragraph_summaries channel）
    pr = {"p0002": "论据[已修订]"}
    typ, blob = serde.dumps_typed(pr)
    assert serde.loads_typed((typ, blob)) == pr

    # pydantic 模型（Argument / Hypothesis / SessionContext / TimeRange）
    arg = Argument(
        argument_id="n0001",
        argument_type=ArgumentType.EVIDENCE,
        paragraph_id="p0002",
        content="论据。",
    )
    typ, blob = serde.dumps_typed(arg)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, Argument)
    assert back.argument_id == arg.argument_id
    assert back.content == arg.content

    hyp = Hypothesis(
        hypothesis_id="h-abc",
        text="对立证据",
        relation=HypothesisRelation.OPPOSE,
    )
    typ, blob = serde.dumps_typed(hyp)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, Hypothesis)
    assert back.hypothesis_id == hyp.hypothesis_id

    sc = SessionContext(
        session_id="s1",
        user_id="u1",
        current_time=datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC),
        user_prompt="精简",
    )
    typ, blob = serde.dumps_typed(sc)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, SessionContext)
    assert back.session_id == sc.session_id

    qtr = TimeRange(start=date(2025, 1, 1), end=date(2026, 1, 1), rationale="默认")
    typ, blob = serde.dumps_typed(qtr)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, TimeRange)
    assert back.start == qtr.start

    # Source（citations channel 的元素，pydantic）
    src = Source(
        source_id="src-1",
        kind="network",
        origin="example.com",
        snippet="s",
    )
    typ, blob = serde.dumps_typed(src)
    back = serde.loads_typed((typ, blob))
    assert isinstance(back, Source)


def test_serializer_does_not_misdecode_plain_dict_as_original_paragraphs() -> None:
    """含哨兵键但带额外键 / 形状不符的普通 dict 不被误还原为 OriginalParagraphs。"""

    serde = HypoArgusSerializer()
    tricky = {_DECODE_TAG: {"order": ["p0001"], "entries": {"p0001": b"x"}}, "extra": 1}
    typ, blob = serde.dumps_typed(tricky)
    back = serde.loads_typed((typ, blob))
    assert back == tricky  # 多键 dict 原样返回，不触发还原
    assert not isinstance(back, OriginalParagraphs)


# ---- msgpack 类型 allowlist（消除 LangGraph「unregistered type」告警）---- #


_JSONPLUS_LOGGER = "langgraph.checkpoint.serde.jsonplus"


def test_serializer_round_trips_registered_types_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """allowlist 内的领域类型经 dumps/loads 静默还原为强类型对象，不触发 LangGraph
    的「unregistered type」/「Blocked deserialization」日志告警。"""

    serde = HypoArgusSerializer()
    sample: list[object] = [
        Argument(
            argument_id="n0001",
            argument_type=ArgumentType.EVIDENCE,
            paragraph_id="p0001",
            content="论据。",
        ),
        Hypothesis(
            hypothesis_id="h-abc",
            text="对立证据",
            relation=HypothesisRelation.OPPOSE,
        ),
        SessionContext(
            session_id="s1",
            user_id="u1",
            current_time=datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC),
            user_prompt="精简",
        ),
        TimeRange(start=date(2025, 1, 1), end=date(2026, 1, 1), rationale="默认"),
        Source(source_id="src-1", kind="network", origin="example.com", snippet="s"),
    ]

    with caplog.at_level(logging.WARNING, logger=_JSONPLUS_LOGGER):
        for obj in sample:
            typ, blob = serde.dumps_typed(obj)
            back = serde.loads_typed((typ, blob))
            assert type(back) is type(obj), (type(back), type(obj))

    noise = [
        r.message
        for r in caplog.records
        if "unregistered type" in r.message or "Blocked deserialization" in r.message
    ]
    assert not noise, noise


def test_serializer_round_trips_paragraph_list_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``paragraph_list`` channel（``list[ParagraphRecord]``）经 dumps/loads 静默还原为
    强类型 ``ParagraphRecord`` 列表，不触发 LangGraph「unregistered type」告警。

    ``ParagraphRecord`` 属 ``domain`` 模块、已登记进 ``_MSGPACK_TYPE_MODULES``（与
    ``Argument`` 同模块，:func:`_allowed_msgpack_types` 自动发现）——故 T-03 无需新增
    allowlist 条目；本测试锁此回归（若 ``ParagraphRecord`` 漂移到未登记模块，此测试会打
    「Blocked deserialization」告警并退化为裸 dict）。
    """

    serde = HypoArgusSerializer()
    paragraph_list = [
        ParagraphRecord(
            paragraph_id="p0001",
            summary="主论点段",
            original_content="主论点。",
            argument_tree_ids=["n0001"],
        ),
        ParagraphRecord(
            paragraph_id="p0002",
            summary="论据段",
            original_content="论据。",
            argument_tree_ids=["n0002", "n0002-s1"],
        ),
    ]

    with caplog.at_level(logging.WARNING, logger=_JSONPLUS_LOGGER):
        typ, blob = serde.dumps_typed(paragraph_list)
        back = serde.loads_typed((typ, blob))

    assert typ == "msgpack"  # 委托 JsonPlusSerializer 的 msgpack 通道
    assert isinstance(back, list)
    assert len(back) == len(paragraph_list)
    assert all(isinstance(r, ParagraphRecord) for r in back)
    assert [r.model_dump() for r in back] == [r.model_dump() for r in paragraph_list]
    noise = [
        r.message
        for r in caplog.records
        if "unregistered type" in r.message or "Blocked deserialization" in r.message
    ]
    assert not noise, noise


def test_serializer_round_trips_hitl1_question_payload_with_paragraph_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``Hitl1Question`` interrupt 载荷（嵌套 ``paragraph_list``）经 dumps/loads 往返，
    ``paragraph_list`` 仍为强类型 ``ParagraphRecord`` 列表——resume 渲染反查所据不破。
    """

    from agents.hitl1 import Hitl1Question

    serde = HypoArgusSerializer()
    question = Hitl1Question(
        argument_tree=[
            Argument(
                argument_id="n0001",
                argument_type=ArgumentType.MAIN_CLAIM,
                paragraph_id="p0001",
                content="主论点。",
            )
        ],
        paragraph_list=[
            ParagraphRecord(
                paragraph_id="p0001",
                original_content="主论点。",
                argument_tree_ids=["n0001"],
            )
        ],
    )

    with caplog.at_level(logging.WARNING, logger=_JSONPLUS_LOGGER):
        typ, blob = serde.dumps_typed(question)
        back = serde.loads_typed((typ, blob))

    assert isinstance(back, Hitl1Question)
    assert all(isinstance(r, ParagraphRecord) for r in back.paragraph_list)
    assert [r.model_dump() for r in back.paragraph_list] == [
        r.model_dump() for r in question.paragraph_list
    ]
    noise = [
        r.message
        for r in caplog.records
        if "unregistered type" in r.message or "Blocked deserialization" in r.message
    ]
    assert not noise, noise


def test_serializer_blocks_unregistered_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未登记进 allowlist 的类型解码被阻断：不还原为强类型对象、打「Blocked
    deserialization」日志，迫使开发者把它登记进 ``_MSGPACK_TYPE_MODULES``。"""

    class _Foreign(BaseModel):
        x: int = 1

    serde = HypoArgusSerializer()
    typ, blob = serde.dumps_typed(_Foreign())
    with caplog.at_level(logging.WARNING, logger=_JSONPLUS_LOGGER):
        back = serde.loads_typed((typ, blob))

    # 阻断后返回的是裸 kwargs dict，而非 _Foreign 实例。
    assert not isinstance(back, _Foreign)
    assert any("Blocked deserialization" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# PostgresSaver 落库往返（ADR-0022：OriginalParagraphs 经 checkpointer 写读等价）
#
# 默认 JsonPlusSerializer 不认 OriginalParagraphs（msgpack TypeError）；HypoArgusSerializer
# 顶层信封化后须经真实 PostgresSaver 的 put→读回路径验证（不只是 dumps/loads 内存往返）。
# --------------------------------------------------------------------------- #


async def test_original_paragraphs_round_trips_through_postgres_saver(pg_checkpointer) -> None:
    """OriginalParagraphs 经真实 PostgresSaver 写入→读回：段落序 + 每段 bytes 等价。"""

    compiled = _ckpt_op_graph().compile(checkpointer=pg_checkpointer)
    cfg = {"configurable": {"thread_id": "ckpt-op-rt"}}
    await compiled.ainvoke({"seen": {"start": True}}, config=cfg)
    state = await compiled.aget_state(cfg)
    op_back = state.values.get("op")
    assert isinstance(op_back, OriginalParagraphs)
    op = _op()
    assert op_back.paragraph_ids() == op.paragraph_ids()
    for pid in op.paragraph_ids():
        assert op_back.get(pid) == op.get(pid)
