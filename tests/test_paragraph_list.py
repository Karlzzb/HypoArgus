"""``paragraph_list`` channel reducer 测试（PRD §Solution / T-01）。

段落聚合根 channel 的 reducer：按 ``paragraph_id`` upsert（形如 ``merge_argument_tree``
按 ``argument_id`` upsert），保持首见顺序——同 id 覆盖、新 id 追加。单写者 = parse+partition
（即便无冲突亦沿用同形以策安全，使 hitl1 打回重跑 parse 时整列表重写不重复、不丢序）。
"""

from __future__ import annotations

from domain import DEFAULT_SESSION_CONTEXT, ArgumentType, ParagraphRecord
from runtime.orchestrator import Orchestrator, merge_paragraph_list


def _rec(pid: str, **kw: object) -> ParagraphRecord:
    return ParagraphRecord(paragraph_id=pid, **kw)  # type: ignore[arg-type]


def test_merge_paragraph_list_empty_returns_right():
    """left / right 均空 → 空列表。"""

    assert merge_paragraph_list(None, None) == []
    assert merge_paragraph_list([], []) == []


def test_merge_paragraph_list_appends_new_in_order():
    """新段落按出现顺序追加、保持首见序。"""

    merged = merge_paragraph_list(
        [_rec("p0001"), _rec("p0002")],
        [_rec("p0003")],
    )
    assert [r.paragraph_id for r in merged] == ["p0001", "p0002", "p0003"]


def test_merge_paragraph_list_upserts_by_paragraph_id():
    """同 paragraph_id 覆盖（upsert），不新增条目、保持原位置。"""

    merged = merge_paragraph_list(
        [_rec("p0001", summary="旧"), _rec("p0002")],
        [_rec("p0001", summary="新")],
    )
    assert len(merged) == 2
    assert [r.paragraph_id for r in merged] == ["p0001", "p0002"]
    by_id = {r.paragraph_id: r for r in merged}
    assert by_id["p0001"].summary == "新"


def test_merge_paragraph_list_right_only():
    """left 为空 → 返回 right 副本（顺序不变）。"""

    merged = merge_paragraph_list(None, [_rec("p0001"), _rec("p0002")])
    assert [r.paragraph_id for r in merged] == ["p0001", "p0002"]


# --------------------------------------------------------------------------- #
# parse+partition 节点把 paragraph_list 写入 state（stub 装配路径，PRD §23 / T-01）
#
# 离线 Fake parse 桩产出 paragraph_list（含 original_content 与 argument_tree_ids），
# 使无真实 LLM 时 tracer-bullet 字节一致路径仍成立。这是黑盒：经 Orchestrator 桩图
# invoke 后断言 state["paragraph_list"] 落地、与 OriginalParagraphs / argument_tree 一致。
# --------------------------------------------------------------------------- #


def _dec(b: bytes) -> str:
    return b.decode("utf-8", errors="surrogateescape")


def test_stub_parse_writes_paragraph_list_to_state():
    """parse+partition 节点把 paragraph_list 写入 state：覆盖全部段、按段序、
    original_content 逐字节等于解码 bytes、argument_tree_ids 与 argument_tree 一致。"""

    doc = "主论点段。\n\n论据段。\n\n无提议段。\n".encode()
    orch = Orchestrator()  # 全套桩
    state = orch.graph.invoke({"original_doc": doc, "session_context": DEFAULT_SESSION_CONTEXT})
    paragraph_list = state["paragraph_list"]
    original_paragraphs = state["original_paragraphs"]
    argument_tree = state["argument_tree"]

    # 覆盖全部段、按规范段序。
    assert [r.paragraph_id for r in paragraph_list] == list(
        original_paragraphs.paragraph_ids()
    )
    # 每段原文逐字节等于解码 bytes。
    for rec in paragraph_list:
        assert rec.original_content == _dec(original_paragraphs.get(rec.paragraph_id))
    # argument_tree_ids 与 argument_tree 节点集一致（stub 每段一个 background 影子节点）。
    tree_ids = {n.argument_id for n in argument_tree}
    pl_ids = {aid for rec in paragraph_list for aid in rec.argument_tree_ids}
    assert pl_ids == tree_ids
    # stub 影子节点恒 BACKGROUND（不硬造论点）。
    for n in argument_tree:
        assert n.argument_type == ArgumentType.BACKGROUND


def test_stub_parse_paragraph_list_summary_empty_in_stub():
    """stub parse 桩的 paragraph_list.summary 为空（桩不产摘要）。"""

    doc = "段。\n".encode()
    orch = Orchestrator()
    state = orch.graph.invoke({"original_doc": doc, "session_context": DEFAULT_SESSION_CONTEXT})
    for rec in state["paragraph_list"]:
        assert rec.summary == ""



