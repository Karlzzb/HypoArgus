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
    ParagraphView,
    ParsedNodeProposal,
    ParseResult,
    parse,
)
from domain import NodeStatus, NodeType
from raw_store import RawParagraphStore
from tree_invariants import validate_tree


def _store(*paragraphs: str) -> RawParagraphStore:
    return RawParagraphStore.from_text("".join(paragraphs).encode())


def _proposal(
    paragraph_id: str,
    node_type: NodeType = NodeType.BACKGROUND,
    *,
    parent_index: int | None = None,
    argument_weight: int = 0,
) -> ParsedNodeProposal:
    return ParsedNodeProposal(
        paragraph_id=paragraph_id,
        node_type=node_type,
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
    store = _store(para)
    llm = FakeLlmClient([_proposal("p0001", NodeType.MAIN_CLAIM)])
    nodes = parse(store, llm)
    assert nodes[0].content == _dec(store.get("p0001"))
    # LLM 提议模型无 content 字段——LLM 输出永不成为节点文本。
    assert "content" not in ParsedNodeProposal.model_fields


def test_parse_rejects_invented_paragraph_id():
    """proposal 的 paragraph_id 不在只读表 → 该节点被丢弃，不凭空造段。"""

    store = _store("real paragraph.\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("invented", NodeType.EVIDENCE),  # 不存在 → 丢弃
        ]
    )
    nodes = parse(store, llm)
    ids = {n.paragraph_id for n in nodes}
    assert "invented" not in ids
    assert all(n.paragraph_id == "p0001" for n in nodes)


# --------------------------------------------------------------------------- #
# 结构硬约束：paragraph_id 单数、一节点一段、一段可多节点
# --------------------------------------------------------------------------- #


def test_parse_node_has_singular_paragraph_id():
    """每个节点只有一个 paragraph_id（不可跨段）。"""

    store = _store("第一段。\n\n第二段。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("p0002", NodeType.EVIDENCE, parent_index=0),
        ]
    )
    for node in parse(store, llm):
        assert isinstance(node.paragraph_id, str)
        assert node.paragraph_id in {"p0001", "p0002"}


def test_parse_one_paragraph_can_host_multiple_nodes():
    """一个段落可含多个节点。"""

    store = _store("含多个论据的段落。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.SUB_CLAIM),
            _proposal("p0001", NodeType.EVIDENCE, parent_index=0),
            _proposal("p0001", NodeType.EVIDENCE, parent_index=0),
        ]
    )
    nodes = parse(store, llm)
    p1_nodes = [n for n in nodes if n.paragraph_id == "p0001"]
    assert len(p1_nodes) == 3


def test_parse_cross_paragraph_argument_via_parent_child():
    """跨段展开的论点由父子指针消化：P2 节点的 parent 指向 P1 节点。"""

    store = _store("主论点。\n\n支撑分论点。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("p0002", NodeType.SUB_CLAIM, parent_index=0),
        ]
    )
    nodes = parse(store, llm)
    by_id = {n.node_id: n for n in nodes}
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

    store = _store("第一段。\n\n第二段（无提议）。\n")
    llm = FakeLlmClient([_proposal("p0001", NodeType.MAIN_CLAIM)])
    nodes = parse(store, llm)
    p2 = [n for n in nodes if n.paragraph_id == "p0002"]
    assert len(p2) == 1
    assert p2[0].node_type == NodeType.BACKGROUND
    assert p2[0].node_type.is_shadow


def test_parse_shadow_nodes_are_readonly_weight_zero():
    """影子节点 argument_weight 恒 0（不参与传导）。"""

    store = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", NodeType.BACKGROUND, argument_weight=50)])
    nodes = parse(store, llm)
    bg = [n for n in nodes if n.node_type == NodeType.BACKGROUND][0]
    assert bg.argument_weight == 0


def test_parse_core_vs_shadow_classification():
    """main/sub/evidence/qualification 为核心；background/evaluation 为影子。"""

    store = _store("段1\n\n段2\n\n段3\n\n段4\n\n段5\n\n段6\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("p0002", NodeType.SUB_CLAIM),
            _proposal("p0003", NodeType.EVIDENCE),
            _proposal("p0004", NodeType.QUALIFICATION),
            _proposal("p0005", NodeType.BACKGROUND),
            _proposal("p0006", NodeType.EVALUATION),
        ]
    )
    by_para = {n.paragraph_id: n for n in parse(store, llm) if not n.node_id.startswith("bg-")}
    assert not by_para["p0001"].node_type.is_shadow
    assert not by_para["p0002"].node_type.is_shadow
    assert not by_para["p0003"].node_type.is_shadow
    assert not by_para["p0004"].node_type.is_shadow
    assert by_para["p0005"].node_type.is_shadow
    assert by_para["p0006"].node_type.is_shadow


# --------------------------------------------------------------------------- #
# parent_index 解析 + 环断 + children 回填
# --------------------------------------------------------------------------- #


def test_parse_parent_index_resolves_to_node_id():
    """parent_index 指向 LLM 输出列表中的位置，解析器解析为 node_id。"""

    store = _store("根。\n\n子。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("p0002", NodeType.EVIDENCE, parent_index=0),
        ]
    )
    nodes = parse(store, llm)
    by_id = {n.node_id: n for n in nodes}
    assert by_id["n0001"].parent_id == "n0000"


def test_parse_self_or_oob_parent_index_becomes_root():
    """自指或越界的 parent_index → 根（parent_id=None）。"""

    store = _store("自指。\n\n越界。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM, parent_index=0),  # 自指
            _proposal("p0002", NodeType.EVIDENCE, parent_index=999),  # 越界
        ]
    )
    by_id = {n.node_id: n for n in parse(store, llm)}
    assert by_id["n0000"].parent_id is None
    assert by_id["n0001"].parent_id is None


def test_parse_breaks_llm_cycle():
    """LLM 给出成环的 parent_index → 解析器断环，输出通过 validate_tree。"""

    store = _store("环A。\n\n环B。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM, parent_index=1),  # n0000→n0001
            _proposal("p0002", NodeType.EVIDENCE, parent_index=0),  # n0001→n0000
        ]
    )
    nodes = parse(store, llm)
    validate_tree(nodes)  # 不抛即通过
    # 至少一个被断为根。
    assert any(n.parent_id is None for n in nodes)


def test_parse_backfills_children_bidirectionally():
    """解析后 children_ids 与 parent_id 双向一致（通过 validate_tree）。"""

    store = _store("主。\n\n子1。\n\n子2。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.MAIN_CLAIM),
            _proposal("p0002", NodeType.EVIDENCE, parent_index=0),
            _proposal("p0003", NodeType.EVIDENCE, parent_index=0),
        ]
    )
    nodes = parse(store, llm)
    validate_tree(nodes)
    by_id = {n.node_id: n for n in nodes}
    assert set(by_id["n0000"].children_ids) == {"n0001", "n0002"}


# --------------------------------------------------------------------------- #
# argument_weight rubric + 越界 clamp（bug 修正：越界应 clamp 而非 pydantic 拒绝）
# --------------------------------------------------------------------------- #


def test_parse_weight_rubric_preserved():
    """带引源论据高分（如 85）、泛泛断言低分（如 30）按 LLM 赋值保留。"""

    store = _store("有据。\n\n泛泛。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.EVIDENCE, argument_weight=85),
            _proposal("p0002", NodeType.EVIDENCE, argument_weight=30),
        ]
    )
    by_para = {n.paragraph_id: n for n in parse(store, llm) if not n.node_id.startswith("bg-")}
    assert by_para["p0001"].argument_weight == 85
    assert by_para["p0002"].argument_weight == 30


def test_parse_clamps_out_of_range_weight():
    """越界 weight 被 clamp 到 [0,100]——真实 LLM 偶尔返回 101，应被宽容而非整体崩溃。

    （docstring 称「越界 clamp」；本测试驱动该行为落地——修正 pydantic Field 硬拒绝。）
    """

    store = _store("超上界。\n\n超下界。\n")
    llm = FakeLlmClient(
        [
            _proposal("p0001", NodeType.EVIDENCE, argument_weight=150),
            _proposal("p0002", NodeType.EVIDENCE, argument_weight=-5),
        ]
    )
    by_para = {n.paragraph_id: n for n in parse(store, llm) if not n.node_id.startswith("bg-")}
    assert by_para["p0001"].argument_weight == 100
    assert by_para["p0002"].argument_weight == 0


# --------------------------------------------------------------------------- #
# 初始状态 + 只把实质段落喂给 LLM
# --------------------------------------------------------------------------- #


def test_parse_nodes_start_unverified():
    """解析输出节点初始状态恒 unverified。"""

    store = _store("段。\n")
    llm = FakeLlmClient([_proposal("p0001", NodeType.MAIN_CLAIM)])
    for node in parse(store, llm):
        assert node.status == NodeStatus.UNVERIFIED


def test_parse_only_feeds_substantive_paragraphs_to_llm():
    """空白段落不喂 LLM（自动归影子），只实质段进入 ParagraphView。"""

    store = _store("实质段。\n\n\n\n")  # p0001 实质，p0002 纯空白
    seen: list[ParagraphView] = []

    def factory(views: list[ParagraphView]) -> ParseResult:
        seen.extend(views)
        return ParseResult(nodes=[_proposal(v.paragraph_id, NodeType.MAIN_CLAIM) for v in views])

    llm: LlmClient = FakeLlmClient(factory=factory)
    parse(store, llm)
    assert [v.paragraph_id for v in seen] == ["p0001"]


def test_parse_invalid_proposal_rejected_by_pydantic() -> None:
    """非负非数值的 weight 在提议层被拒绝（clamp 只针对越界数值，不针对非法类型）。"""

    with pytest.raises(ValidationError):
        ParsedNodeProposal(paragraph_id="p0001", node_type=NodeType.EVIDENCE, argument_weight="bad")  # type: ignore[arg-type]
