"""裁决 Agent：judgment 五合一节点（PRD §5、ADR-0019、Slice 5）。

检索（retrieval 节点产 citations）之后的五节点（verification ReAct 取证 / hypothesis 取证 /
merge / impact / consistency）并入单一 ``judgment`` 节点。本模块吃 ``citations`` 判
per-argument / per-hypothesis 终态，再按序调用**不动**的 ``merge`` / ``impact`` /
``consistency`` 纯函数、整树写回 ``argument_tree``（单写者，故裁撤
``argument_credibility`` partial channel）。

编排顺序（控制流落代码而非 prompt 散文，dev-guide §0 铁律）：

1. ``llm.judge(...)`` 取 per-argument / per-hypothesis 终态（:class:`JudgmentResult`）。
2. 构造局部 ``argument_credibility`` dict（verdict↔:class:`ArgumentStatus`）+
   ``updated_hypotheses``（把各 :class:`Hypothesis.status` 由 pending 换为终态）。
3. ``merge_with_partials`` 字段级合流两 partial + 跑 12 格矩阵裁决（**复用**
   :func:`agents.merge.merge_with_partials`，逻辑不动）。
4. ``impact`` 影响传导（**复用** :func:`agents.impact.impact`，纯函数不动）。
5. ``consistency`` 一致性校验贴批注（**复用** :func:`agents.consistency.consistency`，纯函数不动）。
6. 返回 :class:`JudgmentOutcome`（整树 + 终态化假说）。

输入压缩铁律（PRD §7）：喂给 LLM seam 的只含段 ``original_content``（每段一份，取自
段落聚合根 ``ParagraphRecord``）+ 假说 ``text`` + citation 片段 + ``session_context`` /
``query_time_range`` 背景；**不回灌**
``status`` / ``argument_weight`` / ``parent_id`` / ``children_ids`` / ``issue_tags`` /
``merge_decision``——这些由本模块在调用前后管理、不进 prompt。真实 prompt 构造属
:mod:`infra.llm_adapters` 的 ``QwenJudgmentLlmClient``（Slice 5 cycle 7）；本模块的
:func:`judge_and_adjudicate` 与 provider 无关、可独立单测。

状态语义（ADR-0008 对称）：原文侧 ``credible / doubtful / error`` ↔ 假说侧
``supported / doubtful / refuted``。judgment 据 citations 判终态后，merge 矩阵读
``原文.status × 假说.status`` 裁决 KEEP/REPLACE/REWRITE/SUPPLEMENT/CONFLICT/FREEZE。
"""

from __future__ import annotations

from agents.consistency import consistency
from agents.impact import impact
from agents.judgment.contract import (
    HypothesisVerdictEntry,
    JudgmentArgumentVerdict,
    JudgmentHypothesisVerdict,
    JudgmentLlmClient,
    JudgmentOutcome,
    JudgmentResult,
)
from agents.merge import merge, merge_with_partials
from domain import (
    Argument,
    ArgumentStatus,
    Hypothesis,
    HypothesisStatus,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source

__all__ = ["judge_and_adjudicate"]


# --------------------------------------------------------------------------- #
# verdict → 域状态映射（ADR-0008 对称：原文侧 ↔ 假说侧三态）
# --------------------------------------------------------------------------- #


_ARG_VERDICT_TO_STATUS: dict[JudgmentArgumentVerdict, ArgumentStatus] = {
    JudgmentArgumentVerdict.CREDIBLE: ArgumentStatus.CREDIBLE,
    JudgmentArgumentVerdict.DOUBTFUL: ArgumentStatus.DOUBTFUL,
    JudgmentArgumentVerdict.ERROR: ArgumentStatus.ERROR,
}
"""per-argument 裁决 → 节点 ``status``（原文侧三态）。"""


_HYP_VERDICT_TO_STATUS: dict[JudgmentHypothesisVerdict, HypothesisStatus] = {
    JudgmentHypothesisVerdict.SUPPORTED: HypothesisStatus.SUPPORTED,
    JudgmentHypothesisVerdict.DOUBTFUL: HypothesisStatus.DOUBTFUL,
    JudgmentHypothesisVerdict.REFUTED: HypothesisStatus.REFUTED,
}
"""per-hypothesis 裁决 → 假说 ``status``（假说侧三态）。"""


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数 seam，可独立单测
# --------------------------------------------------------------------------- #


def _apply_hypothesis_verdicts(
    hypotheses: dict[str, list[Hypothesis]],
    verdicts: list[HypothesisVerdictEntry],
) -> dict[str, list[Hypothesis]]:
    """把 per-hypothesis 终态裁决写回各假说 ``status``（pending→终态）。

    未裁决假说保持 ``pending``（保守、不激活）。返回新 dict（不修改输入）：每个假说
    经 ``model_copy`` 深拷，避免与 hypothesis_propose 写入的 partial 共享可变对象。
    """

    verdict_by_id = {
        entry.hypothesis_id: _HYP_VERDICT_TO_STATUS[entry.verdict]
        for entry in verdicts
    }
    out: dict[str, list[Hypothesis]] = {}
    for arg_id, hyps in hypotheses.items():
        updated: list[Hypothesis] = []
        for hyp in hyps:
            new_status = verdict_by_id.get(hyp.hypothesis_id)
            if new_status is not None:
                updated.append(hyp.model_copy(update={"status": new_status}))
            else:
                updated.append(hyp.model_copy())
        out[arg_id] = updated
    return out


def judge_and_adjudicate(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    citations: dict[str, list[Source]],
    paragraph_list: list[ParagraphRecord],
    session_context: SessionContext,
    query_time_range: TimeRange,
    llm: JudgmentLlmClient,
) -> JudgmentOutcome:
    """吃 citations 判终态、再按序调 merge/impact/consistency 纯函数、整树写回。

    - ``llm.judge(...)`` 取 per-argument / per-hypothesis 终态（T-02：按段聚合节点 + 段原文一次，
      输入压缩铁律见模块 docstring）。
    - 构造局部 ``argument_credibility``（verdict→:class:`ArgumentStatus`）+
      ``updated_hypotheses``（pending→终态写回假说）。
    - ``merge_with_partials`` 字段级合流两 partial + 矩阵裁决；``impact`` 影响传导；
      ``consistency`` 一致性批注（T-02：按 ``argument_tree_ids`` 分组、``original_content`` 去重）——
      三者均为**不动**的既有纯函数，本函数仅按序串联。
    - 返回 :class:`JudgmentOutcome`：裁决后的整树（写回 ``argument_tree`` channel）+
      终态化假说（写回 ``hypotheses`` channel，供 HITL-2 与矩阵回看）。

    异常不在此处兜底：由 ``_judgment_node`` 的 :func:`agents.assembly._guarded` 统一降级
    （覆盖范围内未判决节点就地置 ``error``、贴 ``orchestrator_error:judgment``、向前推进）。
    """

    result: JudgmentResult = llm.judge(
        argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range
    )
    argument_credibility: dict[str, ArgumentStatus] = {
        entry.argument_id: _ARG_VERDICT_TO_STATUS[entry.verdict]
        for entry in result.argument_verdicts
    }
    updated_hypotheses = _apply_hypothesis_verdicts(
        hypotheses, result.hypothesis_verdicts
    )
    merged = merge_with_partials(
        argument_tree, argument_credibility, updated_hypotheses, merge
    )
    impacted = impact(merged)
    final = consistency(impacted, paragraph_list)
    return JudgmentOutcome(argument_tree=final, hypotheses=updated_hypotheses)
