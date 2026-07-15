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
    ArgumentStatus,
    ArgumentType,
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
# parse+partition 在同一次 LLM 调用里多吐 query_time_range（桩）与 paragraph_summaries；
# 公开函数 parse() 因此返回 ParseOutput（argument_tree + query_time_range + paragraph_summaries），
# 供 build 闭包写回 PipelineState 三 channel。
# --------------------------------------------------------------------------- #


def test_parse_returns_parse_output_with_stub_time_range_and_summaries():
    """parse() 返回 ParseOutput：argument_tree 铸树、query_time_range 桩、
    paragraph_summaries 取自 LLM ParseResult。"""

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
    by_id = {n.argument_id: n for n in out.argument_tree}
    assert by_id["n0000"].paragraph_id == "p0001"
    assert by_id["n0001"].paragraph_id == "p0002"
    # query_time_range 为桩（agent 注入，不真实调 LLM 识别）。
    assert out.query_time_range == DEFAULT_QUERY_TIME_RANGE
    # paragraph_summaries 顺产自同一次 LLM 调用。
    assert out.paragraph_summaries == summaries


def test_parse_output_summaries_empty_when_llm_omits():
    """LLM 未给 paragraph_summaries → ParseOutput.paragraph_summaries 为空（仍产 argument_tree）。"""

    original_paragraphs = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    out = parse(original_paragraphs, llm)
    assert out.paragraph_summaries == {}
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


# --------------------------------------------------------------------------- #
# 字节级保护原文：content 逐字节来自只读表，LLM 无权改写
# --------------------------------------------------------------------------- #


def test_parse_byte_copies_content_from_store_not_llm():
    """节点 content 逐字节从只读表拷回；ParsedNodeProposal 根本没有 content 字段。"""

    para = "# 标题\n\n正文段落。\n"
    original_paragraphs = _store(para)
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    arguments = parse(original_paragraphs, llm).argument_tree
    assert arguments[0].content == _dec(original_paragraphs.get("p0001"))
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
    arguments = parse(original_paragraphs, llm).argument_tree
    ids = {n.paragraph_id for n in arguments}
    assert "invented" not in ids
    assert all(n.paragraph_id == "p0001" for n in arguments)


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
    arguments = parse(original_paragraphs, llm).argument_tree  # 不抛即通过
    validate_tree(arguments)
    by_id = {n.argument_id: n for n in arguments}
    # 幸存两核心节点连续 n0000/n0001（无空位），p0002 节点 parent 落空为根。
    assert {n.argument_id for n in arguments if not n.argument_id.startswith("bg-")} == {
        "n0000",
        "n0001",
    }
    assert by_id["n0001"].paragraph_id == "p0002"
    assert by_id["n0001"].parent_id is None  # 指向被丢弃提议 → 根，非悬空
    # 「invented」凭空段被丢弃、不出现。
    assert all(n.paragraph_id != "invented" for n in arguments)


# --------------------------------------------------------------------------- #
# 结构硬约束：paragraph_id 单数、一节点一段、一段可多节点
# --------------------------------------------------------------------------- #


def test_parse_argument_has_singular_paragraph_id():
    """每个节点只有一个 paragraph_id（不可跨段）。"""

    original_paragraphs = _store("第一段。\n\n第二段。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.EVIDENCE, parent_index=0),
        ]
    )
    for argument in parse(original_paragraphs, llm).argument_tree:
        assert isinstance(argument.paragraph_id, str)
        assert argument.paragraph_id in {"p0001", "p0002"}


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
    arguments = parse(original_paragraphs, llm).argument_tree
    p1_arguments = [n for n in arguments if n.paragraph_id == "p0001"]
    assert len(p1_arguments) == 3


def test_parse_cross_paragraph_argument_via_parent_child():
    """跨段展开的论点由父子指针消化：P2 节点的 parent 指向 P1 节点。"""

    original_paragraphs = _store("主论点。\n\n支撑分论点。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", ArgumentType.MAIN_CLAIM),
            _proposal("p0002", ArgumentType.SUB_CLAIM, parent_index=0),
        ]
    )
    arguments = parse(original_paragraphs, llm).argument_tree
    by_id = {n.argument_id: n for n in arguments}
    # n0000 在 p0001，n0001 在 p0002，后者 parent 指向前者。
    assert by_id["n0000"].paragraph_id == "p0001"
    assert by_id["n0001"].paragraph_id == "p0002"
    assert by_id["n0001"].parent_id == "n0000"
    assert by_id["n0000"].children_ids == ["n0001"]


# --------------------------------------------------------------------------- #
# 软启发式：无提议段落归为只读 background 影子节点，绝不硬造论点
# --------------------------------------------------------------------------- #


def test_parse_unproposed_paragraph_becomes_background_shadow():
    """无提议的实质段落 → 只读 background 影子节点，而非硬造 main_claim。"""

    original_paragraphs = _store("第一段。\n\n第二段（无提议）。\n")
    llm = FakeLlmClient([_proposal("p0001", ArgumentType.MAIN_CLAIM)])
    arguments = parse(original_paragraphs, llm).argument_tree
    p2 = [n for n in arguments if n.paragraph_id == "p0002"]
    assert len(p2) == 1
    assert p2[0].argument_type == ArgumentType.BACKGROUND
    assert p2[0].argument_type.is_shadow


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
    by_para = {n.paragraph_id: n for n in parse(original_paragraphs, llm).argument_tree if not n.argument_id.startswith("bg-")}
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
    by_para = {n.paragraph_id: n for n in parse(original_paragraphs, llm).argument_tree if not n.argument_id.startswith("bg-")}
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
    by_para = {n.paragraph_id: n for n in parse(original_paragraphs, llm).argument_tree if not n.argument_id.startswith("bg-")}
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
    tree = parse(original_paragraphs, llm).argument_tree
    fed_ids = [v.paragraph_id for v in seen]
    assert "p0002" not in fed_ids  # ``---`` 段未被喂 LLM
    # ``---`` 段作为只读 background 影子节点存在、content 逐字节保留。
    bg = [n for n in tree if n.paragraph_id == "p0002"]
    assert len(bg) == 1
    assert bg[0].argument_type == ArgumentType.BACKGROUND
    assert bg[0].argument_type.is_shadow
    assert bg[0].content.strip() == "---"
