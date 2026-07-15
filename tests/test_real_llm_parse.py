"""真实 LLM 解析集成测试：把 ``markdown/`` 真实论文喂过真实 DashScope 解析器。

既有测试用 ``FakeLlmClient`` 配玩具级单行段落、且自陈「无法真实验证本智能体」——
本文件补这块缺口：真实 ``QwenParseLlmClient`` 经 ``with_structured_output(ParseResult)``
绑定，真实 LLM 产出节点提议，解析器铸造成树。断言八条行为契约（解析器对 LLM 不可信
输出的代码兜底承诺），任一失则暴露真实解析链路（schema 绑定 / 树构建 / LLM 退化输出）的缺陷。

慢（每论文一次 LLM 调用，约 10–60s）；需 ``DASHSCOPE_API_KEY`` + 网络，缺则整模块跳过。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from langchain_core.language_models import BaseChatModel

from agents.parser import LlmClient, ParseOutput, is_substantive, parse
from domain import ArgumentType
from infra.llm_adapters import QwenParseLlmClient
from infra.llm_provider import build_qwen_chat_model
from original_paragraphs import OriginalParagraphs
from tree_invariants import validate_tree

_HAS_KEY = bool(os.environ.get("DASHSCOPE_API_KEY"))
pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(not _HAS_KEY, reason="needs DASHSCOPE_API_KEY + network"),
]


@pytest.fixture(scope="module")
def real_chat_model() -> Iterator[BaseChatModel]:
    """模块级共享 ``ChatOpenAI``（指向 DashScope），避免每用例重建。

    timeout=120：默认 60s 在结构化输出（function-calling 多步）下偶发读超时；
    拉长至 120s 给 DashScope 留余量，单论文调用 ~10–60s 仍属正常。

    max_tokens=8192：qwen-max 文档最大输出 token；缓解最大论文（paper_07，62 实质段）
    顺序产摘要触顶截断——给最大论文在截断前最完整的输出预算。
    """

    # max_tokens=8192 缓解大论文输出截断（paper_07 62 段顺序产摘要易触顶）。
    model = build_qwen_chat_model(timeout=120.0, max_tokens=8192)
    yield model


@pytest.fixture(scope="module")
def real_parse_llm(real_chat_model: BaseChatModel) -> LlmClient:
    """模块级共享解析 LLM 适配器（懒构建结构化链，首调才绑 provider）。"""

    return QwenParseLlmClient(real_chat_model)


def _substantive_paragraph_ids(op: OriginalParagraphs) -> list[str]:
    """实质段落（喂给 LLM）的 paragraph_id 列表——与解析器共用 :func:`is_substantive`。

    纯 ``---`` 主题分隔线无论证内容、不喂 LLM、不要求摘要（归只读 background 影子节点）。
    用解析器同一判定，保证「测试要求的摘要段集合」与「解析器实际喂 LLM 的段集合」一致。
    """

    return [pid for pid in op.paragraph_ids() if is_substantive(op.get(pid))]


# 真实 provider 瞬态断电（DashScope APITimeoutError 跨窗口）在 agent 控制之外：
# adapter 内 5× 重试治同窗口抖动，但 ~10min 断电窗口会耗尽后抛出。直接调 parse() 不经
# 编排层 ``_guarded``，故此处窗口后重续跑兜底瞬态断电——不掩盖真实失败（重试耗尽仍抛）。
_PROVIDER_RETRY_ATTEMPTS = 2
_PROVIDER_RETRY_DELAY = 30.0


def _parse_resilient(op: OriginalParagraphs, llm: LlmClient) -> ParseOutput:
    """跑 :func:`parse`，对瞬态 provider 断电跨窗口重试。"""

    last_exc: BaseException | None = None
    for _ in range(_PROVIDER_RETRY_ATTEMPTS):
        try:
            return parse(op, llm)
        except Exception as exc:  # noqa: BLE001 — 瞬态断电重试，非吞错；耗尽后抛
            last_exc = exc
            time.sleep(_PROVIDER_RETRY_DELAY)
    assert last_exc is not None
    raise last_exc


def test_real_parse_produces_valid_meaningful_tree(
    real_paper: tuple[str, bytes],
    real_parse_llm: LlmClient,
) -> None:
    """真实论文经真实 DashScope 解析器，断言八条行为契约。

    契约：
    1. ``parse`` 不抛（结构化绑定 + 树构建 + ``validate_tree`` 全成功）。
    2. 字节级原文保护：每段 ``paragraph_list.original_content`` 逐字节来自只读表（LLM 不改写原文）。
    3. 无凭空 paragraph_id：``paragraph_list`` 覆盖只读表全部段、且每个 ``argument_tree_ids`` 的 id 都在树内。
    4. 至少一个核心 ``main_claim`` 节点（核心论点存在）。
    5. 核心节点数 ≥ 2（多段论文有多论证；全背景退化树 = 解析失败）。
    6. 至少一条核心→核心父子链（全根 = LLM 未连证据到论点）。
    7. 每个实质段落在 ``paragraph_list.summary`` 有非空摘要。
    8. 至少一个 EVIDENCE 节点 ``argument_weight > 0``；影子节点权重恒 0。
    另显式断 ``validate_tree``（解析器内部已调，此处 belt-and-suspenders）。
    """

    name, doc = real_paper
    op = OriginalParagraphs.from_text(doc)

    # 契约 1：parse 不抛（瞬态 provider 断电经 _parse_resilient 跨窗口重试兜底）。
    out = _parse_resilient(op, real_parse_llm)
    assert isinstance(out, ParseOutput)

    nodes = out.argument_tree
    core_nodes = [n for n in nodes if not n.argument_type.is_shadow]
    shadow_nodes = [n for n in nodes if n.argument_type.is_shadow]
    valid_ids = set(op.paragraph_ids())
    tree_ids = {n.argument_id for n in nodes}

    # 契约 2：字节级原文保护（surrogateescape 解码后逐字节相等）——段级 original_content 对照。
    for rec in out.paragraph_list:
        expected = op.get(rec.paragraph_id)
        got = rec.original_content.encode("utf-8", errors="surrogateescape")
        assert got == expected, (
            f"[{name}] 字节保护破坏：段落 {rec.paragraph_id} 的 original_content "
            f"不逐字节来自只读表（len got={len(got)} != len expected={len(expected)}）"
        )

    # 契约 3：paragraph_list 全覆盖只读表、无凭空造段；argument_tree_ids 的 id 全在树内。
    assert {r.paragraph_id for r in out.paragraph_list} == valid_ids, (
        f"[{name}] paragraph_list 未全覆盖只读表段集合："
        f"{sorted({r.paragraph_id for r in out.paragraph_list})} vs {sorted(valid_ids)}"
    )
    listed_ids = {aid for r in out.paragraph_list for aid in r.argument_tree_ids}
    assert listed_ids <= tree_ids, (
        f"[{name}] argument_tree_ids 含 argument_tree 不存在的节点："
        f"{sorted(listed_ids - tree_ids)}"
    )

    # 契约 4：至少一个核心 main_claim。
    main_claims = [n for n in core_nodes if n.argument_type == ArgumentType.MAIN_CLAIM]
    assert len(main_claims) >= 1, (
        f"[{name}] 无核心 main_claim 节点（core={len(core_nodes)}, "
        f"types={[n.argument_type.value for n in core_nodes]}）——LLM 退化输出？"
    )

    # 契约 5：核心节点数 ≥ 2。
    assert len(core_nodes) >= 2, (
        f"[{name}] 核心节点仅 {len(core_nodes)} < 2（全背景退化树 = 解析失败）"
    )

    # 契约 6：至少一条核心→核心父子链。
    core_arg_ids = {n.argument_id for n in core_nodes}
    core_links = sum(1 for n in core_nodes if n.parent_id in core_arg_ids)
    assert core_links >= 1, (
        f"[{name}] 无核心→核心父子链（core={len(core_nodes)}, "
        f"全根 = LLM 未把证据连到论点）"
    )

    # 契约 7：每个实质段落有非空摘要（paragraph_list.summary）。
    substantive = _substantive_paragraph_ids(op)
    summary_by_pid = {r.paragraph_id: r.summary for r in out.paragraph_list}
    missing_summaries = [
        pid for pid in substantive if not summary_by_pid.get(pid, "").strip()
    ]
    assert not missing_summaries, (
        f"[{name}] {len(missing_summaries)}/{len(substantive)} 个实质段落缺摘要："
        f"{missing_summaries[:5]}"
    )

    # 契约 8：至少一个 EVIDENCE 权重 > 0；影子权重恒 0。
    evidence_nodes = [
        n for n in core_nodes if n.argument_type == ArgumentType.EVIDENCE
    ]
    assert any(n.argument_weight > 0 for n in evidence_nodes), (
        f"[{name}] 无 EVIDENCE 节点 argument_weight > 0"
        f"（evidence={len(evidence_nodes)}, weights={[n.argument_weight for n in evidence_nodes]}）"
    )
    assert all(n.argument_weight == 0 for n in shadow_nodes), (
        f"[{name}] 影子节点权重非 0："
        f"{[(n.argument_id, n.argument_weight) for n in shadow_nodes if n.argument_weight != 0]}"
    )

    # belt-and-suspenders：显式复检树结构不变式（解析器内部已调）。
    validate_tree(nodes)
