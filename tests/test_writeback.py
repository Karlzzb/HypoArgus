"""终稿拼装纯函数子缝测试（ADR-0017、PRD §11/§12、«Testing Decisions»）。

本文件原为 ``writeback`` 纯函数子缝测试；Slice 6 裁撤 writeback 节点、终稿改由
``rewrite_loop``（逐段提议重写）+ ``hitl2``（确认 / 编辑 / 驳回后拼接）落地后，本文件
改/扩为「提议→确认→拼接」纯函数子缝：

- ``propose_rewrites``（rewrite_loop）：被触达段产 LLM 提议重写、未触达段省略、LLM 抛错段
  省略 + 记日志（不碰 ``argument_tree``，仅产 ``proposed_rewrites`` + per-段 errors）。
- ``assemble_final_document``（hitl2）：按确认 / 驳回 / 未触达三态拼接 ``final_document``
  （确认→提议文本、驳回 / 未触达→逐字节原文）。

行为级黑盒测试（PRD «Testing Decisions»）。rewrite_loop 的 LLM seam 以
``FakeRewriteLlmClient`` 离线桩注入——provider-free、确定、可断言。
"""

from __future__ import annotations

from agents.hitl2 import assemble_final_document
from agents.rewrite_loop import (
    FakeRewriteLlmClient,
    RewriteLoopOutcome,
    propose_rewrites,
)
from domain import (
    DEFAULT_QUERY_TIME_RANGE,
    DEFAULT_SESSION_CONTEXT,
    Argument,
    ArgumentStatus,
    ArgumentType,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    ParagraphRecord,
)
from infra.retrieval import Source
from original_paragraphs import OriginalParagraphs

# --------------------------------------------------------------------------- #
# 构造小工具
# --------------------------------------------------------------------------- #


def _store(*paragraphs: tuple[str, str]) -> OriginalParagraphs:
    """从 (paragraph_id, text) 序列构造只读原文表（文本以 utf-8 编码固化）。"""

    from partition import Paragraph

    return OriginalParagraphs(
        [Paragraph(pid, text.encode("utf-8")) for pid, text in paragraphs]
    )


def _paragraph_list(
    store: OriginalParagraphs,
    argument_tree: list[Argument],
    summaries: dict[str, str] | None = None,
) -> list[ParagraphRecord]:
    """从原文表 + 树构造 paragraph_list（original_content 取自段 bytes 解码，与 parse 同源）。"""

    summaries = summaries or {}
    by_para: dict[str, list[str]] = {}
    for a in argument_tree:
        by_para.setdefault(a.paragraph_id, []).append(a.argument_id)
    return [
        ParagraphRecord(
            paragraph_id=pid,
            summary=summaries.get(pid, ""),
            original_content=store.get(pid).decode("utf-8", errors="surrogateescape"),
            argument_tree_ids=by_para.get(pid, []),
        )
        for pid in store.paragraph_ids()
    ]


def _hyp(
    hid: str,
    *,
    relation: HypothesisRelation = HypothesisRelation.OPPOSE,
    status: HypothesisStatus = HypothesisStatus.SUPPORTED,
    text: str | None = None,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hid,
        text=text or f"假设-{hid}",
        relation=relation,
        status=status,
    )


def _argument(
    argument_id: str,
    *,
    paragraph_id: str,
    argument_type: ArgumentType = ArgumentType.EVIDENCE,
    status: ArgumentStatus = ArgumentStatus.UNVERIFIED,
    content: str = "",
    candidates: list[Hypothesis] | None = None,
) -> Argument:
    return Argument(
        argument_id=argument_id,
        argument_type=argument_type,
        paragraph_id=paragraph_id,
        content=content,
        status=status,
        candidate_hypotheses=list(candidates or []),
    )


# --------------------------------------------------------------------------- #
# propose_rewrites —— 触达判定 + LLM 提议 + 失败回退
# --------------------------------------------------------------------------- #


def test_propose_rewrites_touched_by_supported_hypothesis_gets_proposal():
    """段内有 supported 假说 → 触达 → LLM 提议文本落入 proposed_rewrites。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    argument_tree = [
        _argument("n0", paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM, content="主论点。"),
        _argument(
            "n1", paragraph_id="p0002", content="分论点。",
            candidates=[_hyp("h1", status=HypothesisStatus.SUPPORTED)],
        ),
    ]

    def factory(pid, summary, original_content, arguments, citations, sc, qtr):
        assert pid == "p0002"
        assert original_content == "分论点。"  # T-02：改写 seam 收到该段原文
        return "重写后的分论点"

    outcome = propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree, {"p0002": "分论点摘要"}),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    assert isinstance(outcome, RewriteLoopOutcome)
    assert outcome.proposed_rewrites == {"p0002": "重写后的分论点"}
    assert outcome.errors == []


def test_propose_rewrites_untouched_paragraph_omitted_and_never_calls_llm():
    """段无 supported 假说、无 citations → 未触达 → 不进 proposed_rewrites、不调 LLM。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    argument_tree = [
        _argument("n0", paragraph_id="p0001", content="主论点。"),
        # p0002 仅 pending 假说、无 citations → 未触达。
        _argument(
            "n1", paragraph_id="p0002", content="分论点。",
            candidates=[_hyp("h1", status=HypothesisStatus.PENDING)],
        ),
    ]
    seen: list[str] = []

    def factory(pid, summary, original_content, arguments, citations, sc, qtr):  # type: ignore[no-untyped-def]
        seen.append(pid)
        return "不应被调"

    outcome = propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    assert outcome.proposed_rewrites == {}
    assert outcome.errors == []
    assert seen == []  # 未触达段绝不调 LLM


def test_propose_rewrites_touched_by_citations_gets_proposal():
    """段无 supported 假说但有命中 citations → 触达 → 提议。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    argument_tree = [
        _argument("n0", paragraph_id="p0001", content="主论点。"),
        _argument("n1", paragraph_id="p0002", content="分论点。"),  # 无假说
    ]
    citations = {"n1": [Source(source_id="s1", kind="network", origin="url", snippet="证据")]}

    def factory(pid, summary, original_content, arguments, c, sc, qtr):  # type: ignore[no-untyped-def]
        return "据证据重写"

    outcome = propose_rewrites(
        argument_tree,
        citations=citations,
        paragraph_list=_paragraph_list(store, argument_tree, {"p0002": "摘要"}),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    assert outcome.proposed_rewrites == {"p0002": "据证据重写"}


def test_propose_rewrites_llm_returns_none_omits_silently():
    """LLM 返回 None（选择不提议）→ 省略、不记 error（非失败）。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    argument_tree = [
        _argument(
            "n1", paragraph_id="p0002", content="分论点。",
            candidates=[_hyp("h1", status=HypothesisStatus.SUPPORTED)],
        ),
    ]

    def factory(pid, summary, original_content, arguments, c, sc, qtr):  # type: ignore[no-untyped-def]
        return None

    outcome = propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree, {"p0002": "摘要"}),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )
    assert outcome.proposed_rewrites == {}
    assert outcome.errors == []


def test_propose_rewrites_llm_raises_omits_and_logs_per_paragraph():
    """某触达段 LLM 抛错 → 该段省略 + 记 [rewrite_loop] 日志、其余段照常提议（不杀全树）。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"), ("p0003", "论据。"))
    argument_tree = [
        _argument(
            "n1", paragraph_id="p0002", content="分论点。",
            candidates=[_hyp("h1", status=HypothesisStatus.SUPPORTED)],
        ),
        _argument(
            "n2", paragraph_id="p0003", content="论据。",
            candidates=[_hyp("h2", status=HypothesisStatus.SUPPORTED)],
        ),
    ]

    def factory(pid, summary, original_content, arguments, citations, sc, qtr):
        if pid == "p0002":
            raise RuntimeError("LLM boom")
        return "重写论据"

    outcome = propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree, {"p0002": "摘要", "p0003": "摘要"}),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    # 抛错段省略、其余段照常提议（per-段捕获、不杀全树）。
    assert outcome.proposed_rewrites == {"p0003": "重写论据"}
    assert len(outcome.errors) == 1
    assert outcome.errors[0].startswith("[rewrite_loop] p0002")
    assert "RuntimeError" in outcome.errors[0]
    assert "LLM boom" in outcome.errors[0]


def test_propose_rewrites_does_not_mutate_argument_tree():
    """rewrite_loop 不碰 argument_tree：输入树对象 / 字段不变。"""

    store = _store(("p0001", "主论点。"), ("p0002", "分论点。"))
    argument_tree = [
        _argument(
            "n1", paragraph_id="p0002", content="分论点。",
            candidates=[_hyp("h1", status=HypothesisStatus.SUPPORTED)],
        ),
    ]
    snapshot = [n.model_dump() for n in argument_tree]

    def factory(pid, summary, original_content, arguments, c, sc, qtr):  # type: ignore[no-untyped-def]
        return "重写"

    propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree, {"p0002": "摘要"}),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    assert [n.model_dump() for n in argument_tree] == snapshot


# --------------------------------------------------------------------------- #
# T-02 回归锁：改写 seam 收到该段 original_content（引发本重构的原始问题）
# --------------------------------------------------------------------------- #


def test_rewrite_seam_receives_paragraph_original_content():
    """Fake-LLM spy：触达段调用 propose_rewrite 时拿到该段 ``original_content``（T-02 回归锁）。

    引发本重构的原始问题（「分段后是否只有段落 id 与总结、段落原文是否传到改写节点」）
    在本方案下被结构性回答：原文以 ``paragraph_list.original_content`` 单份存储、经
    ``argument_tree_ids`` 正向解析节点后传入改写 seam。本测试把「原文到达改写节点」锁为回归。
    """

    store = _store(("p0001", "主论点。"), ("p0002", "分论点原文XYZ。"))
    argument_tree = [
        _argument("n0", paragraph_id="p0001", argument_type=ArgumentType.MAIN_CLAIM, content="主论点。"),
        _argument(
            "n1", paragraph_id="p0002", content="分论点原文XYZ。",
            candidates=[_hyp("h1", status=HypothesisStatus.SUPPORTED)],
        ),
    ]
    captured: dict[str, str] = {}

    def factory(pid, summary, original_content, arguments, citations, sc, qtr):
        captured[pid] = original_content
        return "重写" if pid == "p0002" else None

    outcome = propose_rewrites(
        argument_tree,
        citations={},
        paragraph_list=_paragraph_list(store, argument_tree),
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    # 仅触达段 p0002 被调；其 original_content == 段落原文（非摘要、非空）。
    assert captured == {"p0002": "分论点原文XYZ。"}
    assert outcome.proposed_rewrites == {"p0002": "重写"}


def test_build_rewrite_prompt_uses_paragraph_original_not_node_content():
    """渲染后的 prompt 含段 ``original_content``、不含逐节点 ``Argument.content``（T-02）。

    真实 adapter ``QwenRewriteLlmClient`` 的 prompt 由 :func:`_build_rewrite_prompt` 构造——
    按段聚合：段原文一次、节点只列 id/type。节点 content 与段原文不同时，prompt 取段原文、
    不取节点 content，把「不再读 ``Argument.content``」锁为回归。
    """

    from infra.llm_adapters import _build_rewrite_prompt

    # 节点 content 与段落 original_content 不同：证明 prompt 取段原文、不再读节点 content。
    arguments = [_argument("n1", paragraph_id="p0002", content="节点旧文本")]
    prompt = _build_rewrite_prompt(
        paragraph_id="p0002",
        paragraph_summary="摘要",
        original_content="段落原文XYZ",
        arguments=arguments,
        citations=[],
        session_context=DEFAULT_SESSION_CONTEXT,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
    )
    assert "段落原文XYZ" in prompt
    assert "节点旧文本" not in prompt


# --------------------------------------------------------------------------- #
# assemble_final_document —— 确认/驳回/未触达三态拼接（纯「拼接」子缝）
# --------------------------------------------------------------------------- #


def test_assemble_no_resolved_rewrites_byte_identical():
    """无确认段（resolved_rewrites 空）→ 终稿逐字节等于原始输入（分区不变式）。"""

    doc = "主论点。\n\n分论点。\n\n论据。\n".encode()
    original_paragraphs = OriginalParagraphs.from_text(doc)
    assert assemble_final_document(original_paragraphs, {}) == doc


def test_assemble_confirmed_paragraph_uses_resolved_text_others_byte_identical():
    """确认段用 resolved 文本、其余段逐字节原文。"""

    doc = b"keep\n\nchange\n\nkeep2\n"
    original_paragraphs = OriginalParagraphs.from_text(doc)
    resolved = {"p0002": "重写后的分论点"}
    out = assemble_final_document(original_paragraphs, resolved)
    # 确认段用 resolved 文本。
    assert "重写后的分论点".encode() in out
    assert b"change" not in out
    # 其余段逐字节还原。
    assert original_paragraphs.get("p0001") in out
    assert original_paragraphs.get("p0003") in out


def test_assemble_uses_store_canonical_order():
    """拼接按 original_paragraphs 规范顺序，与 resolved_rewrites 的 dict 顺序无关。"""

    doc = b"aaa\n\nbbb\n\nccc\n"
    original_paragraphs = OriginalParagraphs.from_text(doc)
    p1 = original_paragraphs.get("p0001")
    p2 = original_paragraphs.get("p0002")
    p3 = original_paragraphs.get("p0003")
    resolved = {"p0002": "BBB"}
    out = assemble_final_document(original_paragraphs, resolved)
    # 规范顺序：p0001 → BBB → p0003（与 resolved 的 dict 顺序无关）。
    assert out.startswith(p1)
    assert out.endswith(p3)
    assert out.index(b"BBB") < out.index(p3)
    # p0002 原文被替换。
    assert p2 not in out
    assert b"BBB" in out


def test_assemble_untouched_paragraph_byte_identical(sample_doc):
    """未触达段（不在 resolved_rewrites）逐字节等于原文（含空行/缩进/末尾空格）。"""

    _name, doc = sample_doc
    original_paragraphs = OriginalParagraphs.from_text(doc)
    pids = original_paragraphs.paragraph_ids()
    if len(pids) < 2:
        import pytest

        pytest.skip("样例不足两段")
    target_pid = pids[0]
    resolved = {target_pid: "替换文本"}
    out = assemble_final_document(original_paragraphs, resolved)
    # 非确认段逐字节还原。
    for pid in pids:
        if pid == target_pid:
            continue
        assert original_paragraphs.get(pid) in out
    # 确认段被替换。
    assert "替换文本".encode() in out
