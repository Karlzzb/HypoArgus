"""真实 LLM 装配的离线测试（provider 无关部分）。

真实 DashScope 联网冒烟（需 ``DASHSCOPE_API_KEY`` + 网络 + token）见末尾 skip 标注的
``test_real_dashscope_structured_output_smoke``。本文件其余测试**不**触网：
provider 工厂缺 key 报错、CLI 闸门决策解析、以及用 fake chat model 跑 wiring——
fake 模型对 ``with_structured_output`` 的任何失败由各 stage ``_guarded`` 兜底，
故「无人采纳 → 终稿逐字节等于原文」的 tracer bullet 承诺依然成立。
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from agents.assembly import create_real_agents
from agents.hitl1 import (
    FakeHitl1Gate,
    Hitl1Action,
    Hitl1Decision,
    Hitl1Question,
    Hitl1Reply,
)
from agents.hitl2 import (
    ConservativeHitl2Gate,
    Hitl2Action,
    Hitl2Question,
    Hitl2Reply,
    Hitl2Review,
)
from agents.rewrite_loop import FakeRewriteLlmClient, RewriteLoopOutcome
from domain import Argument, ArgumentType, ParagraphRecord
from infra.llm_provider import build_qwen_chat_model
from runtime.cli_gates import CliHitl1Gate, CliHitl2Gate
from runtime.orchestrator import RunResult
from runtime.run_real import run_real_pipeline

_DOC = "主论点。\n\n分论点。\n\n论据。\n".encode()


def _scripted_input(lines: Iterable[str]):
    """返回一个 input 替身：按序吐出 lines，用尽回退到 'skip'。"""

    it = iter(lines)

    def _fn(_prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            return "skip"

    return _fn


def _sample_tree() -> list[Argument]:
    return [
        Argument(
            argument_id="n0",
            argument_type=ArgumentType.MAIN_CLAIM,
            paragraph_id="p0001",
            content="主论点",
        ),
        Argument(
            argument_id="n1",
            argument_type=ArgumentType.EVIDENCE,
            paragraph_id="p0002",
            content="论据",
            parent_id="n0",
        ),
    ]


def _sample_paragraph_list(tree: list[Argument]) -> list[ParagraphRecord]:
    """从树派生 paragraph_list（按 ``paragraph_id`` 分组），供 CLI 闸门渲染反查。"""

    by_para: dict[str, list[str]] = {}
    for a in tree:
        by_para.setdefault(a.paragraph_id, []).append(a.argument_id)
    return [
        ParagraphRecord(paragraph_id=pid, argument_tree_ids=ids)
        for pid, ids in by_para.items()
    ]


# --------------------------------------------------------------------------- #
# provider 工厂
# --------------------------------------------------------------------------- #


def test_build_qwen_chat_model_missing_key_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # 隔离 cwd，不读仓库根 .env
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        build_qwen_chat_model()


def test_build_qwen_chat_model_uses_env_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-real")
    model = build_qwen_chat_model()
    # 不触网：只确认构造成功且 base_url 指向 DashScope。
    assert "dashscope" in str(model.openai_api_base)


# --------------------------------------------------------------------------- #
# CLI 闸门
# --------------------------------------------------------------------------- #


def test_cli_hitl1_gate_skip_via_scripted_input():
    gate = CliHitl1Gate(
        interactive=True, input_fn=_scripted_input(["s"]), out_fn=lambda *_a, **_k: None
    )
    tree = _sample_tree()
    assert gate.review(tree, paragraph_list=_sample_paragraph_list(tree)).action == Hitl1Action.SKIP


def test_cli_hitl1_gate_accept_via_scripted_input():
    gate = CliHitl1Gate(
        interactive=True, input_fn=_scripted_input(["a"]), out_fn=lambda *_a, **_k: None
    )
    tree = _sample_tree()
    assert gate.review(tree, paragraph_list=_sample_paragraph_list(tree)).action == Hitl1Action.ACCEPT


def test_cli_hitl1_gate_edit_empty_ops_via_scripted_input():
    gate = CliHitl1Gate(
        interactive=True,
        input_fn=_scripted_input(["e", "done"]),
        out_fn=lambda *_a, **_k: None,
    )
    tree = _sample_tree()
    decision = gate.review(tree, paragraph_list=_sample_paragraph_list(tree))
    assert decision.action == Hitl1Action.EDIT
    assert decision.ops == []


def test_cli_hitl1_gate_noninteractive_skips():
    gate = CliHitl1Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    tree = _sample_tree()
    assert gate.review(tree, paragraph_list=_sample_paragraph_list(tree)).action == Hitl1Action.SKIP


def test_cli_hitl2_gate_pass_when_no_pending():
    gate = CliHitl2Gate(interactive=True, out_fn=lambda *_a, **_k: None)
    review = Hitl2Review(paragraphs=[], has_pending=False)
    assert gate.review(review).action == Hitl2Action.PASS


def test_cli_hitl2_gate_noninteractive_decides_empty():
    gate = CliHitl2Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    review = Hitl2Review(paragraphs=[], has_pending=True)
    decision = gate.review(review)
    assert decision.action == Hitl2Action.DECIDE
    assert decision.ops == []


# --------------------------------------------------------------------------- #
# CLI 闸门拆分 seam（T-01·ADR-0022 prefactor）：formulate_question + parse_reply
# --------------------------------------------------------------------------- #


def test_cli_hitl1_gate_formulate_question_returns_pure_snapshot_no_input_consumed():
    """formulate_question 产 Hitl1Question（纯数据快照），不渲染依赖、不吃 input。"""

    calls: list[str] = []

    def _input(prompt: str = "") -> str:
        calls.append(prompt)
        return "skip"

    gate = CliHitl1Gate(interactive=True, input_fn=_input, out_fn=lambda *_a, **_k: None)
    tree = _sample_tree()
    paragraph_list = _sample_paragraph_list(tree)
    question = gate.formulate_question(tree, paragraph_list=paragraph_list)
    assert isinstance(question, Hitl1Question)
    assert [n.model_dump() for n in question.argument_tree] == [n.model_dump() for n in tree]
    assert [r.model_dump() for r in question.paragraph_list] == [
        r.model_dump() for r in paragraph_list
    ]
    assert calls == []  # 纯构造、不阻塞取 input


def test_cli_hitl1_gate_parse_reply_is_action_only():
    """CLI parse_reply 一期 action-only（空 ops）；结构化 ops 编辑推后（PRD §7.2）。"""

    gate = CliHitl1Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    decision = gate.parse_reply(Hitl1Reply(action=Hitl1Action.EDIT, text="自由文本"))
    assert decision.action is Hitl1Action.EDIT
    assert decision.ops == []


def test_cli_hitl2_gate_formulate_question_wraps_review():
    """CLI hitl2 formulate_question 产 Hitl2Question（包裹呈现 = interrupt payload）。"""

    gate = CliHitl2Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    review = Hitl2Review(paragraphs=[], has_pending=False)
    question = gate.formulate_question(review)
    assert isinstance(question, Hitl2Question)
    assert question.review is review


def test_cli_hitl2_gate_parse_reply_is_action_only():
    """CLI hitl2 parse_reply 一期 action-only（空 ops，DECIDE 即全驳回）。"""

    gate = CliHitl2Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    decided = gate.parse_reply(Hitl2Reply(action=Hitl2Action.DECIDE, text="自由文本"))
    assert decided.action is Hitl2Action.DECIDE
    assert decided.ops == []


# --------------------------------------------------------------------------- #
# wiring：fake chat model → 兜底 → 终稿逐字节等于原文
# --------------------------------------------------------------------------- #


def test_real_agents_rewrite_llm_is_injected_and_proposes_for_touched_paragraph():
    """``create_real_agents(rewrite_llm=...)`` 注入重写 seam：触达段产提议、未触达省略。

    离线断言 manifest 的 ``rewrite_loop`` real 工厂以 ``partial(propose_rewrites, llm=...)``
    预绑注入的 rewrite_llm——字段存在且可注入（与 parse/hypothesis/judgment 装配 seam 同形）。
    p0001 段内含 supported 假说 → 触达 → 调 ``propose_rewrite``；p0002 无假说 / 无命中
    citations → 不触达、不调 LLM。
    """

    from agents.parser import FakeLlmClient, ParseResult
    from domain import (
        DEFAULT_QUERY_TIME_RANGE,
        DEFAULT_SESSION_CONTEXT,
        Argument,
        ArgumentType,
        Hypothesis,
        HypothesisRelation,
        HypothesisStatus,
        ParagraphRecord,
    )
    from original_paragraphs import OriginalParagraphs

    called: dict = {}

    def factory(paragraph_id, paragraph_summary, original_content, arguments, citations, sc, qtr):
        called["pid"] = paragraph_id
        called["original_content"] = original_content
        return "REWRITTEN"

    agents = create_real_agents(
        llm=FakeLlmClient(result=ParseResult()),
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        rewrite_llm=FakeRewriteLlmClient(propose_factory=factory),
    )

    doc = "主论点。\n\n分论点。\n".encode()
    original_paragraphs = OriginalParagraphs.from_text(doc)
    hyp = Hypothesis(
        hypothesis_id="h1",
        text="x",
        relation=HypothesisRelation.OPPOSE,
        status=HypothesisStatus.SUPPORTED,
    )
    argument_tree = [
        Argument(
            argument_id="n1",
            argument_type=ArgumentType.MAIN_CLAIM,
            paragraph_id="p0001",
            content="主论点",
            candidate_hypotheses=[hyp],
        ),
        Argument(
            argument_id="n2",
            argument_type=ArgumentType.SUB_CLAIM,
            paragraph_id="p0002",
            content="分论点",
        ),
    ]
    by_para: dict[str, list[str]] = {}
    for a in argument_tree:
        by_para.setdefault(a.paragraph_id, []).append(a.argument_id)
    paragraph_list = [
        ParagraphRecord(
            paragraph_id=pid,
            summary="",
            original_content=original_paragraphs.get(pid).decode(
                "utf-8", errors="surrogateescape"
            ),
            argument_tree_ids=by_para.get(pid, []),
        )
        for pid in original_paragraphs.paragraph_ids()
    ]

    outcome = agents.rewrite_loop(
        argument_tree,
        {},
        paragraph_list,
        DEFAULT_SESSION_CONTEXT,
        DEFAULT_QUERY_TIME_RANGE,
    )
    assert isinstance(outcome, RewriteLoopOutcome)
    assert outcome.proposed_rewrites == {"p0001": "REWRITTEN"}  # 仅触达段 p0001 提议
    assert called["pid"] == "p0001"  # 未触达段 p0002 不调 LLM
    assert called["original_content"] == "主论点。\n\n"  # T-02：改写 seam 收到该段原文（含段间分隔字节）
    assert outcome.errors == []


def test_run_real_pipeline_wiring_byte_identical_when_unadopted():
    """fake chat model + Mock 检索 + 保守 HITL-2 → 无人采纳 → 终稿逐字节等于原文。

    fake 模型对 with_structured_output 的任何失败由各 stage _guarded 兜底，
    整条流水线仍推进至终稿（tracer bullet 承诺成立）。
    """

    from langchain_core.language_models import FakeListChatModel

    fake_chat = FakeListChatModel(responses=["{}"])
    report = run_real_pipeline(
        _DOC,
        chat_model=fake_chat,
        hitl1_gate=FakeHitl1Gate(Hitl1Decision(action=Hitl1Action.SKIP)),
        hitl2_gate=ConservativeHitl2Gate(),
    )
    assert isinstance(report, RunResult)
    assert report.final_document == _DOC


# --------------------------------------------------------------------------- #
# 真实 DashScope 联网冒烟（默认 skip：需 key + 网络 + token）
# --------------------------------------------------------------------------- #


@pytest.mark.skip(reason="needs DASHSCOPE_API_KEY + network + tokens")
def test_real_dashscope_structured_output_smoke():
    """验证 build_qwen_chat_model() 端点 + with_structured_output 真跑通。

    手动跑：``DASHSCOPE_API_KEY=... pytest -rsv tests/test_real_llm_wiring.py
    -k dashscope_smoke``——确认 key/端点/schema/判别联合信封在 DashScope 下返回合法对象。
    """

    from agents.parser import ParseResult

    chat = build_qwen_chat_model()
    chain = chat.with_structured_output(ParseResult, method="function_calling")
    out = chain.invoke(
        "解析以下段落的论证节点：\n[p0001] 数据表明方案 X 提升转化率 30%。"
    )
    assert isinstance(out, ParseResult)
