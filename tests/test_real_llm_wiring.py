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

from agents.hitl1 import FakeHitl1Gate, Hitl1Action, Hitl1Decision
from agents.hitl2 import (
    ConservativeHitl2Gate,
    Hitl2Action,
    Hitl2Review,
)
from domain import Argument, ArgumentType
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
    assert gate.review(_sample_tree()).action == Hitl1Action.SKIP


def test_cli_hitl1_gate_accept_via_scripted_input():
    gate = CliHitl1Gate(
        interactive=True, input_fn=_scripted_input(["a"]), out_fn=lambda *_a, **_k: None
    )
    assert gate.review(_sample_tree()).action == Hitl1Action.ACCEPT


def test_cli_hitl1_gate_edit_empty_ops_via_scripted_input():
    gate = CliHitl1Gate(
        interactive=True,
        input_fn=_scripted_input(["e", "done"]),
        out_fn=lambda *_a, **_k: None,
    )
    decision = gate.review(_sample_tree())
    assert decision.action == Hitl1Action.EDIT
    assert decision.ops == []


def test_cli_hitl1_gate_noninteractive_skips():
    gate = CliHitl1Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    assert gate.review(_sample_tree()).action == Hitl1Action.SKIP


def test_cli_hitl2_gate_pass_when_no_pending():
    gate = CliHitl2Gate(interactive=True, out_fn=lambda *_a, **_k: None)
    review = Hitl2Review(arguments=[], has_pending=False)
    assert gate.review(review).action == Hitl2Action.PASS


def test_cli_hitl2_gate_noninteractive_decides_empty():
    gate = CliHitl2Gate(interactive=False, out_fn=lambda *_a, **_k: None)
    review = Hitl2Review(arguments=[], has_pending=True)
    decision = gate.review(review)
    assert decision.action == Hitl2Action.DECIDE
    assert decision.ops == []


# --------------------------------------------------------------------------- #
# wiring：fake chat model → 兜底 → 终稿逐字节等于原文
# --------------------------------------------------------------------------- #


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
