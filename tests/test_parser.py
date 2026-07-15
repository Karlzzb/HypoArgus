"""论证结构解析 Agent 单测（PRD §4、issue #2 验收准则）。

解析器在只读底座上工作：LLM 只做「按段识别」，解析器强制 LLM 不可信的所有结构硬约束
（控制流落代码）。这些测试逐条锁定 #2 的验收准则——解析器未来重写时，行为契约不变。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.parser import (
    FakeLlmClient,
    LlmClient,
    ParagraphSummary,
    ParagraphView,
    ParsedNodeProposal,
    ParseOutput,
    ParseResult,
    is_substantive,
    parse,
)
from domain import (
    DEFAULT_QUERY_TIME_RANGE,
    DEFAULT_SESSION_CONTEXT,
    Argument,
    ArgumentStatus,
    ArgumentType,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from original_paragraphs import OriginalParagraphs
from tree_invariants import validate_tree

# --------------------------------------------------------------------------- #
# 贯穿 state 域类型（ADR-0021 / PRD §17·Slice 1）
#
# SessionContext / TimeRange 为贯穿全链的运行上下文与时间范围；query_time_range 当前为桩
# （默认 2025–2026，真实 LLM 时间识别待后续切片）。这些类型是 LLM seam 输入背景的载体。
# --------------------------------------------------------------------------- #


def test_time_range_carries_start_end_rationale():
    """TimeRange 承载 start / end / rationale 三字段（date | None）。"""

    tr = TimeRange(start="2025-01-01", end="2026-12-31", rationale="本文数据窗")
    assert tr.start is not None
    assert tr.end is not None
    assert tr.rationale == "本文数据窗"


def test_default_query_time_range_is_2025_2026_stub():
    """DEFAULT_QUERY_TIME_RANGE 为伪代码桩：2025 起始、2026 结束、rationale 标注待真实识别。"""

    assert DEFAULT_QUERY_TIME_RANGE.start == __import__("datetime").date(2025, 1, 1)
    assert DEFAULT_QUERY_TIME_RANGE.end == __import__("datetime").date(2026, 12, 31)
    assert "真实识别" in DEFAULT_QUERY_TIME_RANGE.rationale


def test_session_context_carries_session_user_time_prompt():
    """SessionContext 承载 session_id / user_id / current_time / user_prompt 四字段。"""

    import datetime

    now = datetime.datetime(2026, 7, 13, 12, 0, 0)
    sc = SessionContext(
        session_id="s1",
        user_id="u1",
        current_time=now,
        user_prompt="精简冗余论据",
    )
    assert sc.session_id == "s1"
    assert sc.user_id == "u1"
    assert sc.current_time == now
    assert sc.user_prompt == "精简冗余论据"


def test_default_session_context_is_deterministic():
    """DEFAULT_SESSION_CONTEXT 为确定性桩（current_time 固定、可测可复现）。"""

    assert DEFAULT_SESSION_CONTEXT.current_time is not None
    # 同进程内两次取值稳定（不依赖 datetime.now）。
    assert DEFAULT_SESSION_CONTEXT.current_time == DEFAULT_SESSION_CONTEXT.current_time


# --------------------------------------------------------------------------- #
# ParseResult 扩展（ADR-0021 / PRD §17·Slice 1）
#
# parse+partition 在同一次 LLM 调用里多吐 query_time_range（桩，默认 2025–2026）与
# paragraph_summaries（paragraph_id → 摘要）。当前 query_time_range 由 agent 注桩、
# 不真实调 LLM 识别；paragraph_summaries 真实由 LLM 顺产。
# --------------------------------------------------------------------------- #


def test_parse_result_defaults_carry_stub_time_range_and_empty_summaries():
    """ParseResult() 默认携带桩 query_time_range（2025–2026）与空 paragraph_summaries。"""

    result = ParseResult()
    assert result.query_time_range == DEFAULT_QUERY_TIME_RANGE
    assert result.query_time_range.start is not None
    assert result.query_time_range.start.year == 2025
    assert result.query_time_range.end is not None
    assert result.query_time_range.end.year == 2026
    assert result.paragraph_summaries == []


def test_parse_result_accepts_paragraph_summaries_from_llm():
    """ParseResult 可承载 LLM 顺产的 paragraph_summaries（list[ParagraphSummary]）。"""

    result = ParseResult(
        paragraph_summaries=[
            ParagraphSummary(paragraph_id="p0001", summary="主论点段"),
            ParagraphSummary(paragraph_id="p0002", summary="论据段"),
        ]
    )
    by_id = {ps.paragraph_id: ps.summary for ps in result.paragraph_summaries}
    assert by_id["p0001"] == "主论点段"
    assert by_id["p0002"] == "论据段"


# --------------------------------------------------------------------------- #
# parse() 返回 ParseOutput（ADR-0021 / PRD §17·Slice 1）
#
# parse+partition 在同一次 LLM 调用里多吐 query_time_range（桩）与段落摘要（折叠进
# paragraph_list.summary）；公开函数 parse() 因此返回 ParseOutput（argument_tree +
# query_time_range + paragraph_list），供 build 闭包写回 PipelineState 三 channel。
# --------------------------------------------------------------------------- #


def test_parse_returns_parse_output_with_stub_time_range_and_summaries():
    """parse() 返回 ParseOutput：argument_tree 铸树、query_time_range 桩、
    paragraph_list.summary 取自 LLM ParseResult（摘要单一定义点）。"""

    original_paragraphs = _store("主论点段。\n\n论据段。\n")
    summaries = {"p0001": "主论点段摘要", "p0002": "论据段摘要"}
    llm = FakeLlmClient(
        ParseResult(
            proposals=[
                _proposal("p0001", ArgumentType.MAIN_CLAIM),
                _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
            ],
            paragraph_summaries=[
                ParagraphSummary(paragraph_id="p0001", summary="主论点段摘要"),
                ParagraphSummary(paragraph_id="p0002", summary="论据段摘要"),
            ],
        )
    )
    out = parse(original_paragraphs, llm)
    # argument_tree 仍按既有铸树逻辑产出（两核心节点）。
    assert isinstance(out, ParseOutput)
    assert len(out.argument_tree) == 2
    # query_time_range 为桩（agent 注入，不真实调 LLM 识别）。
    assert out.query_time_range == DEFAULT_QUERY_TIME_RANGE
    # paragraph_list.summary 顺产自同一次 LLM 调用（摘要单一定义点）。
    assert {r.paragraph_id: r.summary for r in out.paragraph_list} == summaries


def test_parse_output_summaries_empty_when_llm_omits():
    """LLM 未给 paragraph_summaries → 每段 ParagraphRecord.summary 为空（仍产 argument_tree）。"""

    original_paragraphs = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    out = parse(original_paragraphs, llm)
    assert all(r.summary == "" for r in out.paragraph_list)
    assert len(out.argument_tree) == 1


def _store(*paragraphs: str) -> OriginalParagraphs:
    return OriginalParagraphs.from_text("".join(paragraphs).encode())


def _proposal(
    paragraph_id: str,
    argument_type: ArgumentType = ArgumentType.BACKGROUND,
    *,
    parent_index: int | None = None,
    argument_weight: int = 0,
) -> ParsedNodeProposal:
    return ParsedNodeProposal(
        paragraph_id=paragraph_id,
        argument_type=argument_type,
        parent_index=parent_index,
        argument_weight=argument_weight,
    )


def _dec(b: bytes) -> str:
    return b.decode("utf-8", errors="surrogateescape")


def _core_by_paragraph(out: ParseOutput) -> dict[str, Argument]:
    """``paragraph_id → 该段首个核心节点``（跳过 ``bg-`` 影子）。

    T-04：``Argument`` 不存 ``paragraph_id``，节点→段归属经 ``paragraph_list.argument_tree_ids``
    反查。本测试助手把「该段的核心节点」按段取出来，供按段断言节点属性的测试复用。
    """

    by_id = {n.argument_id: n for n in out.argument_tree}
    result: dict[str, Argument] = {}
    for rec in out.paragraph_list:
        for aid in rec.argument_tree_ids:
            if not aid.startswith("bg-"):
                result[rec.paragraph_id] = by_id[aid]
                break
    return result


# --------------------------------------------------------------------------- #
# ParagraphRecord：段落聚合根（PRD §Solution / T-04 翻转后段落侧单一定义点）
#
# 段落做成聚合根，正向拥有其论证节点引用；段落原文每段一份。T-04 翻转后
# Argument 不存 paragraph_id / content，paragraph_summaries channel 退役，
# paragraph_list 为原文与摘要的单一定义点。
# --------------------------------------------------------------------------- #


def test_paragraph_record_defaults():
    """ParagraphRecord 仅 paragraph_id 必填；summary / original_content / argument_tree_ids
    有空默认（每段原文每段一份、节点引用列表可空）。"""

    rec = ParagraphRecord(paragraph_id="p0001")
    assert rec.paragraph_id == "p0001"
    assert rec.summary == ""
    assert rec.original_content == ""
    assert rec.argument_tree_ids == []


def test_paragraph_record_carries_aggregate_fields():
    """ParagraphRecord 承载 summary + original_content + argument_tree_ids 三字段。"""

    rec = ParagraphRecord(
        paragraph_id="p0002",
        summary="论据段摘要",
        original_content="论据段原文。",
        argument_tree_ids=["n0001", "bg-p0002"],
    )
    assert rec.summary == "论据段摘要"
    assert rec.original_content == "论据段原文。"
    assert rec.argument_tree_ids == ["n0001", "bg-p0002"]


def test_parse_output_has_paragraph_list_channel_defaulting_empty():
    """ParseOutput 新增 paragraph_list（list[ParagraphRecord]），默认空。"""

    out = ParseOutput()
    assert out.paragraph_list == []
    assert isinstance(out.paragraph_list, list)


def test_parse_produces_paragraph_list_covering_all_paragraphs_in_order():
    """parse 同点产出 paragraph_list：覆盖 OriginalParagraphs 全部段、按规范段序、每段一条。"""

    original_paragraphs = _store("主论点段。\n\n论据段。\n\n第三段。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    # 覆盖全部段、按 paragraph_ids() 规范段序。
    assert [r.paragraph_id for r in out.paragraph_list] == list(
        original_paragraphs.paragraph_ids()
    )


def test_parse_paragraph_list_original_content_byte_equals_decoded_store():
    """每条 ParagraphRecord.original_content 逐字节等于该段解码 bytes（每段唯一一份）。"""

    para = "# 标题\n\n正文段落。\n\n---\n"
    original_paragraphs = _store(para)
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    out = parse(original_paragraphs, llm)
    for rec in out.paragraph_list:
        assert rec.original_content == _dec(original_paragraphs.get(rec.paragraph_id))
    # 每段原文唯一一份：与节点 content 等价（双写过渡，T-04 后节点不再各拷一份）。
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    assert by_pid["p0001"].original_content == _dec(original_paragraphs.get("p0001"))


def test_parse_paragraph_list_argument_tree_ids_match_tree_per_paragraph():
    """每段 argument_tree_ids 恰为 argument_tree 中该段全部节点 id（核心 + background 影子）。"""

    # p0001 含三节点、p0002 无提议 → 降级为 background 影子。
    original_paragraphs = _store("含多节点段。\n\n无提议段。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.SUB_CLAIM),
            _proposal("p0001", ArgumentType.EVIDENCE, parent_index=0),
            _proposal("p0001", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    tree_ids = {n.argument_id for n in out.argument_tree}
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    # p0001 含三节点（核心）
    assert len(by_pid["p0001"].argument_tree_ids) == 3
    assert set(by_pid["p0001"].argument_tree_ids) <= tree_ids
    # p0002 无提议 → 降级为 background 影子节点，其 id 进入该段 argument_tree_ids。
    assert by_pid["p0002"].argument_tree_ids == ["bg-p0002"]
    # 并集恰为 argument_tree 全部节点集、无重复归属。
    all_listed = [aid for r in out.paragraph_list for aid in r.argument_tree_ids]
    assert set(all_listed) == tree_ids
    assert len(all_listed) == len(set(all_listed))


def test_parse_paragraph_list_ids_partition_argument_tree():
    """paragraph_list 全部 argument_tree_ids 的并集恰为 argument_tree 全部节点集（不漂移）。"""

    original_paragraphs = _store("主。\n\n子1。\n\n子2。\n\n无提议。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
            _proposal("p0003", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    listed_ids = {aid for rec in out.paragraph_list for aid in rec.argument_tree_ids}
    tree_ids = {n.argument_id for n in out.argument_tree}
    assert listed_ids == tree_ids
    # 每个 id 恰出现于一个段落（正向一对多，不重复归属）。
    all_listed = [aid for rec in out.paragraph_list for aid in rec.argument_tree_ids]
    assert len(all_listed) == len(set(all_listed))


def test_parse_paragraph_list_summary_carries_parse_summaries():
    """每条 ParagraphRecord.summary 取自 parse 摘要阶段；无摘要的段为空串。"""

    original_paragraphs = _store("主论点段。\n\n论据段。\n")
    llm = FakeLlmClient(
        ParseResult(
            proposals=[
                _proposal("p0001", ArgumentType.MAIN_CLAIM),
                _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
            ],
            paragraph_summaries=[
                ParagraphSummary(paragraph_id="p0001", summary="主论点段摘要"),
                ParagraphSummary(paragraph_id="p0002", summary="论据段摘要"),
            ],
        )
    )
    out = parse(original_paragraphs, llm)
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    assert by_pid["p0001"].summary == "主论点段摘要"
    assert by_pid["p0002"].summary == "论据段摘要"


def test_parse_paragraph_list_summary_empty_when_llm_omits():
    """LLM 未给某段摘要 → 该段 ParagraphRecord.summary 为空串。"""

    original_paragraphs = _store("段1。\n\n段2。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    assert by_pid["p0001"].summary == ""
    assert by_pid["p0002"].summary == ""


# --------------------------------------------------------------------------- #
# 字节级保护原文：content 逐字节来自只读表，LLM 无权改写
# --------------------------------------------------------------------------- #


def test_parse_byte_copies_content_from_store_not_llm():
    """段落 original_content 逐字节从只读表拷回；ParsedNodeProposal 根本没有 content 字段。"""

    para = "# 标题\n\n正文段落。\n"
    original_paragraphs = _store(para)
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    out = parse(original_paragraphs, llm)
    # T-04：原文不再存于节点，而是每段一份存于 paragraph_list.original_content（逐字节来自只读表）。
    rec = out.paragraph_list[0]
    assert rec.original_content == _dec(original_paragraphs.get("p0001"))
    # LLM 提议模型无 content 字段——LLM 输出永不成为节点文本。
    assert "content" not in ParsedNodeProposal.model_fields


def test_parse_rejects_invented_paragraph_id():
    """proposal 的 paragraph_id 不在只读表 → 该节点被丢弃，不凭空造段。"""

    original_paragraphs = _store("real paragraph.\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("invented", ArgumentType.EVIDENCE),  # 不存在 → 丢弃
        ]
    )
    out = parse(original_paragraphs, llm)
    paragraph_ids = {r.paragraph_id for r in out.paragraph_list}
    assert "invented" not in paragraph_ids  # 凭空段被丢弃、不出现
    assert paragraph_ids == {"p0001"}  # 仅保留只读表内的真实段


def test_parse_dangling_parent_to_dropped_proposal_becomes_root() -> None:
    """parent_index 指向被丢弃提议（凭空 paragraph_id）→ 落空为根，不悬空 parent_id。

    回归真实 LLM 数据发现的 bug：旧实现按枚举索引赋 n-id，丢弃凭空提议后留空位，
    后续提议 parent_index 指向该空位即解析出不存在的 parent_id，validate_tree 崩。
    n-id 现按幸存顺序连续赋值，指向被丢弃提议的 parent_index 落空为根（与越界同语义）。
    """

    original_paragraphs = _store("主论点。\n\n论据段。\n")
    # p0001(idx0) / invented(idx1, 凭空→丢弃) / p0002(idx2, parent_index=1 指向被丢弃的 idx1)。
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("invented", ArgumentType.EVIDENCE),  # 凭空 → 丢弃，n-id 空位
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=1),  # 指向被丢弃的 idx1
        ]
    )
    out = parse(original_paragraphs, llm)  # 不抛即通过
    arguments = out.argument_tree
    validate_tree(arguments)
    by_id = {n.argument_id: n for n in arguments}
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    # 幸存两核心节点连续 n0000/n0001（无空位），p0002 节点 parent 落空为根。
    assert {n.argument_id for n in arguments if not n.argument_id.startswith("bg-")} == {
        "n0000",
        "n0001",
    }
    assert "n0001" in by_pid["p0002"].argument_tree_ids  # n0001 归属 p0002
    assert by_id["n0001"].parent_id is None  # 指向被丢弃提议 → 根，非悬空
    # 「invented」凭空段被丢弃、不出现。
    assert "invented" not in by_pid


# --------------------------------------------------------------------------- #
# 结构硬约束：一节点一段、一段可多节点
# --------------------------------------------------------------------------- #


def test_parse_argument_singular_paragraph_membership():
    """每个节点恰归属一个段落（不可跨段）——经 paragraph_list.argument_tree_ids 判定。"""

    original_paragraphs = _store("第一段。\n\n第二段。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    # 段落集合覆盖原文两段、无凭空造段。
    assert {r.paragraph_id for r in out.paragraph_list} == {"p0001", "p0002"}
    # 每个节点恰出现于一个段落的 argument_tree_ids（一节点一段）。
    all_listed = [aid for rec in out.paragraph_list for aid in rec.argument_tree_ids]
    tree_ids = {n.argument_id for n in out.argument_tree}
    assert set(all_listed) == tree_ids
    assert len(all_listed) == len(set(all_listed))


def test_parse_one_paragraph_can_host_multiple_arguments():
    """一个段落可含多个节点。"""

    original_paragraphs = _store("含多个论据的段落。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.SUB_CLAIM),
            _proposal("p0001", ArgumentType.EVIDENCE, parent_index=0),
            _proposal("p0001", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    # 一段三节点（核心），均归属 p0001。
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    assert len(by_pid["p0001"].argument_tree_ids) == 3


def test_parse_cross_paragraph_argument_via_parent_child():
    """跨段展开的论点由父子指针消化：P2 节点的 parent 指向 P1 节点。"""

    original_paragraphs = _store("主论点。\n\n支撑分论点。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.SUB_CLAIM, parent_index=0),
        ]
    )
    out = parse(original_paragraphs, llm)
    arguments = out.argument_tree
    by_id = {n.argument_id: n for n in arguments}
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    # n0000 在 p0001，n0001 在 p0002，后者 parent 指向前者。
    assert "n0000" in by_pid["p0001"].argument_tree_ids
    assert "n0001" in by_pid["p0002"].argument_tree_ids
    assert by_id["n0001"].parent_id == "n0000"
    assert by_id["n0000"].children_ids == ["n0001"]


# --------------------------------------------------------------------------- #
# 软启发式：无提议段落归为只读 background 影子节点，绝不硬造论点
# --------------------------------------------------------------------------- #


def test_parse_unproposed_paragraph_becomes_background_shadow():
    """无提议的实质段落 → 只读 background 影子节点，而非硬造 main_claim。"""

    original_paragraphs = _store("第一段。\n\n第二段（无提议）。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    out = parse(original_paragraphs, llm)
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    by_id = {n.argument_id: n for n in out.argument_tree}
    p2_ids = by_pid["p0002"].argument_tree_ids
    assert len(p2_ids) == 1
    p2_node = by_id[p2_ids[0]]
    assert p2_node.argument_type == ArgumentType.BACKGROUND
    assert p2_node.argument_type.is_shadow


def test_parse_shadow_arguments_are_readonly_weight_zero():
    """影子节点 argument_weight 恒 0（不参与传导）。"""

    original_paragraphs = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.BACKGROUND, argument_weight=50)])
    arguments = parse(original_paragraphs, llm).argument_tree
    bg = [n for n in arguments if n.argument_type == ArgumentType.BACKGROUND][0]
    assert bg.argument_weight == 0


def test_parse_core_vs_shadow_classification():
    """main/sub/evidence/qualification 为核心；background/evaluation 为影子。"""

    original_paragraphs = _store("段1\n\n段2\n\n段3\n\n段4\n\n段5\n\n段6\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.SUB_CLAIM),
            _proposal("p0003", ArgumentType.EVIDENCE),
            _proposal("p0004", ArgumentType.QUALIFICATION),
            _proposal("p0005", ArgumentType.BACKGROUND),
            _proposal("p0006", ArgumentType.EVALUATION),
        ]
    )
    out = parse(original_paragraphs, llm)
    by_para = _core_by_paragraph(out)
    assert not by_para["p0001"].argument_type.is_shadow
    assert not by_para["p0002"].argument_type.is_shadow
    assert not by_para["p0003"].argument_type.is_shadow
    assert not by_para["p0004"].argument_type.is_shadow
    assert by_para["p0005"].argument_type.is_shadow
    assert by_para["p0006"].argument_type.is_shadow


# --------------------------------------------------------------------------- #
# parent_index 解析 + 环断 + children 回填
# --------------------------------------------------------------------------- #


def test_parse_parent_index_resolves_to_argument_id():
    """parent_index 指向 LLM 输出列表中的位置，解析器解析为 argument_id。"""

    original_paragraphs = _store("根。\n\n子。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    arguments = parse(original_paragraphs, llm).argument_tree
    by_id = {n.argument_id: n for n in arguments}
    assert by_id["n0001"].parent_id == "n0000"


def test_parse_self_or_oob_parent_index_becomes_root():
    """自指或越界的 parent_index → 根（parent_id=None）。"""

    original_paragraphs = _store("自指。\n\n越界。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM, parent_index=0),  # 自指
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=999),  # 越界
        ]
    )
    by_id = {n.argument_id: n for n in parse(original_paragraphs, llm).argument_tree}
    assert by_id["n0000"].parent_id is None
    assert by_id["n0001"].parent_id is None


def test_parse_breaks_llm_cycle():
    """LLM 给出成环的 parent_index → 解析器断环，输出通过 validate_tree。"""

    original_paragraphs = _store("环A。\n\n环B。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM, parent_index=1),  # n0000→n0001
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),  # n0001→n0000
        ]
    )
    arguments = parse(original_paragraphs, llm).argument_tree
    validate_tree(arguments)  # 不抛即通过
    # 至少一个被断为根。
    assert any(n.parent_id is None for n in arguments)


def test_parse_backfills_children_bidirectionally():
    """解析后 children_ids 与 parent_id 双向一致（通过 validate_tree）。"""

    original_paragraphs = _store("主。\n\n子1。\n\n子2。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
            _proposal("p0003", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    arguments = parse(original_paragraphs, llm).argument_tree
    validate_tree(arguments)
    by_id = {n.argument_id: n for n in arguments}
    assert set(by_id["n0000"].children_ids) == {"n0001", "n0002"}


# --------------------------------------------------------------------------- #
# argument_weight rubric + 越界 clamp（bug 修正：越界应 clamp 而非 pydantic 拒绝）
# --------------------------------------------------------------------------- #


def test_parse_weight_rubric_preserved():
    """带引源论据高分（如 85）、泛泛断言低分（如 30）按 LLM 赋值保留。"""

    original_paragraphs = _store("有据。\n\n泛泛。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.EVIDENCE, argument_weight=85),
            _proposal("p0002", ArgumentType.EVIDENCE, argument_weight=30),
        ]
    )
    out = parse(original_paragraphs, llm)
    by_para = _core_by_paragraph(out)
    assert by_para["p0001"].argument_weight == 85
    assert by_para["p0002"].argument_weight == 30


def test_parse_clamps_out_of_range_weight():
    """越界 weight 被 clamp 到 [0,100]——真实 LLM 偶尔返回 101，应被宽容而非整体崩溃。

    （docstring 称「越界 clamp」；本测试驱动该行为落地——修正 pydantic Field 硬拒绝。）
    """

    original_paragraphs = _store("超上界。\n\n超下界。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.EVIDENCE, argument_weight=150),
            _proposal("p0002", ArgumentType.EVIDENCE, argument_weight=-5),
        ]
    )
    out = parse(original_paragraphs, llm)
    by_para = _core_by_paragraph(out)
    assert by_para["p0001"].argument_weight == 100
    assert by_para["p0002"].argument_weight == 0


# --------------------------------------------------------------------------- #
# 初始状态 + 只把实质段落喂给 LLM
# --------------------------------------------------------------------------- #


def test_parse_arguments_start_unverified():
    """解析输出节点初始状态恒 unverified。"""

    original_paragraphs = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    for argument in parse(original_paragraphs, llm).argument_tree:
        assert argument.status == ArgumentStatus.UNVERIFIED


def test_parse_only_feeds_substantive_paragraphs_to_llm():
    """空白段落不喂 LLM（自动归影子），只实质段进入 ParagraphView。"""

    original_paragraphs = _store("实质段。\n\n\n\n")  # p0001 实质，p0002 纯空白
    seen: list[ParagraphView] = []

    def factory(views: list[ParagraphView]) -> ParseResult:
        seen.extend(views)
        return ParseResult(proposals=[_proposal(v.paragraph_id, ArgumentType.MAIN_CLAIM) for v in views])

    llm: LlmClient = FakeLlmClient(factory=factory)
    parse(original_paragraphs, llm)
    assert [v.paragraph_id for v in seen] == ["p0001"]


def test_parse_invalid_proposal_rejected_by_pydantic() -> None:
    """非负非数值的 weight 在提议层被拒绝（clamp 只针对越界数值，不针对非法类型）。"""

    with pytest.raises(ValidationError):
        ParsedNodeProposal(paragraph_id="p0001", argument_type=ArgumentType.EVIDENCE, argument_weight="bad")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# is_substantive：纯主题分隔线（---）不喂 LLM、归影子节点
# --------------------------------------------------------------------------- #


def test_is_substantive_classifies_breaks_and_tables() -> None:
    """``is_substantive`` 排除空白与纯主题分隔线，保留正文 / 标题 / 表格分隔行。

    表格分隔行 ``| --- |`` 含 ``|``、非纯分隔线——应视为实质（保留喂 LLM），
    避免 PRD 测试误把表格结构当无内容段落丢弃。
    """

    assert is_substantive(b"") is False
    assert is_substantive(b"   \n  ") is False
    assert is_substantive(b"---") is False
    assert is_substantive(b"***") is False
    assert is_substantive(b"___") is False
    assert is_substantive(b"- - -") is False
    assert is_substantive(b"----") is False
    assert is_substantive(b"--") is True  # 仅两个，非主题分隔线
    assert is_substantive(b"| --- | --- |") is True  # 表格分隔行，非纯分隔线
    assert is_substantive("# 标题\n".encode()) is True
    assert is_substantive("正文段落。\n".encode()) is True


def test_parse_does_not_feed_thematic_break_to_llm() -> None:
    """纯 ``---`` 分隔线段不喂 LLM、归只读 background 影子节点（不硬造论点）。"""

    # p0002 = ``---``：文档分隔线。解析器不应把它喂给 LLM。
    original_paragraphs = _store("主论点段。\n\n---\n\n论据段。\n")
    seen: list[ParagraphView] = []

    def factory(views: list[ParagraphView]) -> ParseResult:
        seen.extend(views)
        return ParseResult(proposals=[_proposal(v.paragraph_id, ArgumentType.MAIN_CLAIM) for v in views])

    llm: LlmClient = FakeLlmClient(factory=factory)
    out = parse(original_paragraphs, llm)
    tree = out.argument_tree
    fed_ids = [v.paragraph_id for v in seen]
    assert "p0002" not in fed_ids  # ``---`` 段未被喂 LLM
    # ``---`` 段作为只读 background 影子节点存在、段原文逐字节保留于 paragraph_list。
    by_id = {n.argument_id: n for n in tree}
    by_pid = {r.paragraph_id: r for r in out.paragraph_list}
    p2_ids = by_pid["p0002"].argument_tree_ids
    assert len(p2_ids) == 1
    bg = by_id[p2_ids[0]]
    assert bg.argument_type == ArgumentType.BACKGROUND
    assert bg.argument_type.is_shadow
    assert by_pid["p0002"].original_content.strip() == "---"
