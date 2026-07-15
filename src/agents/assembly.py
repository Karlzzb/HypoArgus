"""智能体装配与 stage 装配（ADR-0014 · manifest 驱动）。

本模块是「agent → pipeline stage」的 wiring 模块：把每个 Agent 的桩/真实工厂与其
stage 拓扑（deps + 落图闭包含降级兜底）收口为**单一 manifest**（:data:`MANIFEST`），
同时驱动 typed :class:`Agents` 构造（:func:`create_stub_agents` /
:func:`create_real_agents`）与 :func:`runtime.orchestrator.default_pipeline`。

加一个 Agent 的触点因此从 7 降至 3（新子包 + :class:`Agents` 字段 + manifest 条目）。
保 typed :class:`Agents` dataclass（字段访问 ``agents.parse: ParseFn`` 全 typed），
不取动态 ``dict[str, AgentEntry]`` registry——后者虽能把触点降到 2，但令
``agents.parse`` 失去 typed access，在 ``mypy --strict`` 项目中得不偿失（ADR-0014）。

桩的行为：不生产任何真实变更、不读写原文全文、绝不打回或重调度——
确保「无采纳改动 → 终稿逐字节等于原文」这一 tracer bullet 承诺。

**stage 降级兜底**（issue #11 · PRD §13）随 build 闭包落于此：每个下游 stage 经
:func:`_guarded` 统一兜底，任一智能体异常 / 超时即「就地置目标节点错误状态 + 附日志 +
单向向前推进」，绝不因单点波动卡死整篇。无复杂分布式重试降级与跨模块挂起——异常即记
日志、就地降级、继续向前。:class:`agents.hitl2.Hitl2GateError` 为硬闸门正确性硬停
（绝不无人拍板自动采纳，ADR-0010），非单点波动，**不兜底、原样上抛**。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from langgraph.errors import GraphBubbleUp

from agents.hitl1 import Hitl1Gate
from agents.hitl1 import confirm_partition as hitl1_confirm_partition
from agents.hitl1.contract import (
    DEFAULT_MAX_PARTITION_RETRIES,
    Hitl1Outcome,
    Hitl1Route,
)
from agents.hitl2 import ConservativeHitl2Gate, Hitl2Confirmation, Hitl2Gate, Hitl2GateError
from agents.hitl2 import confirm as hitl2_confirm
from agents.hypothesis import HypothesisLlmClient
from agents.hypothesis import propose_hypotheses as propose_hypotheses_fn
from agents.judgment import (
    FakeJudgmentLlmClient,
    JudgmentLlmClient,
    JudgmentOutcome,
    judge_and_adjudicate,
)
from agents.parser import LlmClient
from agents.parser import parse as parse_fn
from agents.parser.contract import ParseOutput
from agents.rewrite_loop import (
    FakeRewriteLlmClient,
    RewriteLlmClient,
    RewriteLoopOutcome,
)
from agents.rewrite_loop import (
    propose_rewrites as propose_rewrites_fn,
)
from domain import (
    DEFAULT_QUERY_TIME_RANGE,
    Argument,
    ArgumentStatus,
    ArgumentType,
    Hypothesis,
    ParagraphRecord,
    SessionContext,
    TimeRange,
)
from infra.retrieval import Source
from original_paragraphs import OriginalParagraphs
from status_machine import mark_argument_error

if TYPE_CHECKING:
    # 仅类型：build 闭包返回 ``NodeFn``、``state: PipelineState`` 注解在
    # ``from __future__ import annotations`` 下为字符串、运行时不求值。运行时无
    # agents→runtime 依赖（依赖方向保持 runtime→agents），故用 TYPE_CHECKING 破环。
    # RetrievalRuntime 仅类型——真实检索后端注入 seam（Slice 2）；避免桩路径 eager import
    # vendored search_agent 栈（``create_real_agents(retrieval_runtime=...)`` 时才 lazy import）。
    from agents.retrieval import RetrievalRuntime
    from runtime.orchestrator import NodeFn, PipelineState

__all__ = [
    "ParseFn",
    "Hitl1Fn",
    "HypothesisProposeFn",
    "RetrievalFn",
    "JudgmentFn",
    "RewriteLoopFn",
    "Hitl2Fn",
    "Agents",
    "AgentEntry",
    "RealDeps",
    "MANIFEST",
    "create_stub_agents",
    "create_real_agents",
]


class ParseFn(Protocol):
    """论证结构解析（#2 接入真实 LLM 解析）。返回初始论证树 + 时间范围（桩）+ 段落摘要。

    partition + parse 合并为单一 ``parse+partition`` 节点（PRD §1 / ADR-0017 / Slice 1）：
    partition 的纯代码切分 + 字节级自检由 build 闭包承担，本 Protocol 承载 parse 语义——
    产 ``ParseOutput``（argument_tree + query_time_range 桩 + paragraph_list（含 summary））。
    """

    def __call__(self, original_paragraphs: OriginalParagraphs) -> ParseOutput: ...


class Hitl1Fn(Protocol):
    """HITL-1 partition 确认闸门（ADR-0017·重定义）。

    读 ``argument_tree`` + ``paragraph_list`` + 打回计数 ``retry_count``，经
    :func:`agents.hitl1.confirm_partition` 产 :class:`agents.hitl1.Hitl1Outcome`（确认后的树 +
    同步后的 paragraph_list + 路由 CONTINUE/REPLAY + 计数 + 耗尽标签）。T-02：EDIT 时随
    merge/split/mark_no_op 同步 ``argument_tree_ids``。确认继续（SKIP/ACCEPT/EDIT）→ 下游；
    打回重跑（REPLAY）→ 重跑 parse+partition（按 user prompt，当前伪代码桩）；打回有界，超限
    向前 + 贴 partition_retry_exhausted（受控分支、非异常降级）。既有结构编辑
    （reparent/merge/split/...）收编于「确认继续」语义。
    """

    def __call__(
        self,
        argument_tree: list[Argument],
        paragraph_list: list[ParagraphRecord],
        retry_count: int,
    ) -> Hitl1Outcome: ...


class JudgmentFn(Protocol):
    """裁决五合一节点（#4 取证 + #5 取证 + #6 merge + #7 impact + #8 consistency·Slice 5）。

    吃 ``citations`` 判 per-argument / per-hypothesis 终态、再按序调 merge/impact/consistency
    纯函数、整树写回 ``argument_tree``（单写者，故裁撤 ``argument_credibility`` partial channel）。
    T-02：裁决 prompt 按段聚合节点 + 段原文一次、consistency 按 ``argument_tree_ids`` 分组去重，
    故本 Protocol 载 ``paragraph_list``。输入压缩铁律见 :mod:`agents.judgment`：只喂段
    ``original_content`` + 假说 ``text`` + citation 片段 + 背景；不回灌
    status/weight/parent_id/children_ids/issue_tags/merge_decision。
    """

    def __call__(
        self,
        argument_tree: list[Argument],
        hypotheses: dict[str, list[Hypothesis]],
        citations: dict[str, list[Source]],
        paragraph_list: list[ParagraphRecord],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> JudgmentOutcome: ...


class HypothesisProposeFn(Protocol):
    """线路 2 · 开药（#5 · Slice 3 重定义为仅 propose）。

    逐 argument 经 ``paragraph_id`` 从 ``paragraph_list`` 反查该段 ``original_content`` +
    ``summary`` 调 ``propose``（不读 ``Argument`` 原文字段；原文 / 摘要取自 ``paragraph_list``），
    产 pending 假设 partial（by ``argument_id``）。取证落终态属 Slice 5 的 judgment 节点。
    """

    def __call__(
        self,
        argument_tree: list[Argument],
        paragraph_list: list[ParagraphRecord],
    ) -> dict[str, list[Hypothesis]]: ...


class RetrievalFn(Protocol):
    """批量检索（PRD §8 / ADR-0017 · Slice 4 · 当前伪代码桩）。

    紧随 hypothesis_propose：批量接收 ``argument_tree`` + ``hypotheses``（含假说文本，作
    查询输入）+ ``query_time_range`` + ``session_context`` + ``paragraph_list``，统一返回 citations
    （key 为 ``argument_id`` 如 ``n0001`` / ``bg-...`` 或 ``hypothesis_id`` 如 ``h-...``，
    两套 id 不冲突）。当前桩不真实检索、产空 citations（``infra.retrieval`` 接口层
    的白名单 / 权限 / 模板契约不动，真实后端后续切片接入）。读 ``session_context`` /
    ``query_time_range`` / ``paragraph_list`` 不触发联网——仅穿背景供真实后端就位。
    ``paragraph_list`` 是 PRD §Q2 最小放宽：补回段原文通道（``Argument`` 无文本字段，
    ADR-0025 代价），forward ``target_text`` 取段 ``original_content`` 属 Slice 2 适配器侧构造，
    本 Protocol 只穿下去；与 judgment / hypothesis_propose / rewrite_loop 同 family。
    """

    def __call__(
        self,
        argument_tree: list[Argument],
        hypotheses: dict[str, list[Hypothesis]],
        query_time_range: TimeRange,
        session_context: SessionContext,
        paragraph_list: list[ParagraphRecord],
    ) -> dict[str, list[Source]]: ...


class RewriteLoopFn(Protocol):
    """逐段重写提议（#10·Slice 6·ADR-0017）。返回提议重写表 + per-段失败日志。

    对被触达段（supported 假说 / 命中 citations）由 LLM 提议重写文本、未触达段省略；
    产 ``proposed_rewrites``（仅触达段）+ per-段 LLM 失败日志（``errors``）。rewrite_loop
    **不碰 ``argument_tree``**（新流程按段/文本工作，与 argument 状态解耦）；失败段记日志 +
    回退原文（信号在 ``errors`` channel、不写 ``argument_tree``）。T-02：直接遍历
    ``paragraph_list``、按 ``argument_tree_ids`` 正向解析节点、取该段 ``original_content``，
    不再反向 join ``Argument.paragraph_id`` / 读 ``Argument.content``。输入压缩铁律见
    :mod:`agents.rewrite_loop`：只喂 ``paragraph_summary`` + 段 ``original_content`` +
    argument ``argument_type`` + 假说 ``text`` + citation 片段 + 背景；不回灌内部状态字段。

    LLM seam 经 :func:`create_real_agents` 的 ``rewrite_llm`` 注入（``real`` 工厂以
    ``partial(propose_rewrites, llm=...)`` 预绑定）；桩（``_stub_rewrite_loop``）内部构造
    :class:`FakeRewriteLlmClient`。故本 Protocol 不显式载 ``llm`` 参数。
    """

    def __call__(
        self,
        argument_tree: list[Argument],
        citations: dict[str, list[Source]],
        paragraph_list: list[ParagraphRecord],
        session_context: SessionContext,
        query_time_range: TimeRange,
    ) -> RewriteLoopOutcome: ...


class Hitl2Fn(Protocol):
    """HITL-2 终稿文本确认闸门（#9 接入，不可跳过硬闸门·Slice 6 重定位）。

    接收只读原文表 + rewrite_loop 的 ``proposed_rewrites``，逐段确认 / 编辑 / 驳回，
    拼装 ``final_document``（确认→提议文本、编辑→编辑文本、驳回 / 未触达→逐字节原文），
    返回 :class:`Hitl2Confirmation`（终稿 bytes + resolved_rewrites）。绝不替人拍板
    （ADR-0010）；``Hitl2GateError`` 原样上抛。
    """

    def __call__(
        self,
        original_paragraphs: OriginalParagraphs,
        proposed_rewrites: dict[str, str],
    ) -> Hitl2Confirmation: ...


@dataclass
class Agents:
    """一组可注入的子智能体契约。

    中枢按固定顺序调用这些契约；每个契约在本切片都可用桩占位，后续切片逐个替换。
    typed dataclass 保字段访问类型安全（ADR-0014：不取 dict registry）。
    """

    parse: ParseFn
    hitl1: Hitl1Fn
    hypothesis_propose: HypothesisProposeFn
    retrieval: RetrievalFn
    judgment: JudgmentFn
    rewrite_loop: RewriteLoopFn
    hitl2: Hitl2Fn


# --------------------------------------------------------------------------- #
# 桩实现
# --------------------------------------------------------------------------- #


def _stub_parse(original_paragraphs: OriginalParagraphs) -> ParseOutput:
    """解析桩：每段一个只读 background 影子节点 + 桩 query_time_range +
    paragraph_list（每段一条聚合根，含 original_content 与 argument_tree_ids）。

    影子节点不参与校验与传导、状态恒 ``unverified``、永不进入 ``adopted``，
    故回写对每段都走逐字节拷回通道——tracer bullet 的字节级承诺由此成立。
    真实解析（#2）将在此识别 main_claim/sub_claim/evidence/qualification 并建父子树、
    并顺产段落摘要（折叠进 paragraph_list.summary）。query_time_range 恒为桩（真实 LLM 时间识别属后续切片）。
    paragraph_list 与真实 parse 同形：按规范段序每段一条、original_content 取自该段 bytes
    解码、argument_tree_ids 为该段影子节点 id（PRD §22）。T-04：Argument 不存 paragraph_id/content，
    桩亦不产 paragraph_summaries（摘要单一定义点为 paragraph_list.summary）。
    """

    pids = list(original_paragraphs.paragraph_ids())
    argument_tree = [
        Argument(
            argument_id=f"n-{pid}",
            argument_type=ArgumentType.BACKGROUND,
        )
        for pid in pids
    ]
    paragraph_list = [
        ParagraphRecord(
            paragraph_id=pid,
            summary="",
            original_content=original_paragraphs.get(pid).decode("utf-8", errors="surrogateescape"),
            argument_tree_ids=[f"n-{pid}"],
        )
        for pid in pids
    ]
    return ParseOutput(
        argument_tree=argument_tree,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        paragraph_list=paragraph_list,
    )


def _stub_hitl1(
    argument_tree: list[Argument],
    paragraph_list: list[ParagraphRecord],
    retry_count: int,
) -> Hitl1Outcome:
    """HITL-1 桩：partition 确认闸门保守继续（不打回）。

    恒返回 CONTINUE、计数不变、不 exhausted——桩路径下不触发打回循环、parse+partition
    仅运行一次，「无触达段终稿逐字节等于原文」承诺由此成立。真实闸门（真实 ``interrupt`` +
    ``Command(resume)``）属后续切片；partition prompt 驱动重切（ADR-0017）亦为后续切片。
    """

    return Hitl1Outcome(
        argument_tree=[n.model_copy(deep=True) for n in argument_tree],
        paragraph_list=[r.model_copy(deep=True) for r in paragraph_list],
        route=Hitl1Route.CONTINUE,
        retry_count=retry_count,
        exhausted=False,
    )


def _stub_hypothesis_propose(
    argument_tree: list[Argument], paragraph_list: list[ParagraphRecord]
) -> dict[str, list[Hypothesis]]:
    """开药桩：不生成假设。"""

    return {}


def _stub_retrieval(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    query_time_range: TimeRange,
    session_context: SessionContext,
    paragraph_list: list[ParagraphRecord],
) -> dict[str, list[Source]]:
    """检索桩（PRD §8 / Slice 4）：不真实检索、只穿 state、产空 citations。

    ``session_context`` / ``query_time_range`` / ``paragraph_list`` 被读取（穿至 seam、供真实
    后端就位）但不触发联网；``argument_tree`` / ``hypotheses`` 接过但不发起查询。返回空 citations
    → 下游 judgment / rewrite_loop 见无素材、不触达任何段，「无触达段终稿逐字节等于原文」
    承诺继续成立。真实后端（批量循环 ``RetrievalLayer.retrieve``）后续切片接入，
    ``infra.retrieval`` 接口层不变。
    """

    return {}


def _stub_judgment(
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    citations: dict[str, list[Source]],
    paragraph_list: list[ParagraphRecord],
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> JudgmentOutcome:
    """裁决桩（ADR-0017·Slice 5）：不判终态、调纯函数向前（空裁决 → 全 KEEP → 未触达）。

    委托 :func:`judge_and_adjudicate` 用 :class:`agents.judgment.FakeJudgmentLlmClient`
    （默认空 :class:`JudgmentResult`）——无裁决 → ``argument_credibility`` 空、假说保持
    ``pending``，merge 矩阵全 KEEP、impact 不动、consistency 不贴标，故「无触达段终稿逐字节
    等于原文」承诺成立。真实裁决（吃 citations 判终态）由 :func:`create_real_agents` 注入
    ``judgment_llm`` 时启用。merge/impact/consistency 为**不动**的既有纯函数、本桩与真实
    装配共用同一串联（judgment 节点 = 这三纯函数的串联编排，逻辑不在此处重写）。
    """

    return judge_and_adjudicate(
        argument_tree,
        hypotheses,
        citations,
        paragraph_list,
        session_context,
        query_time_range,
        llm=FakeJudgmentLlmClient(),
    )


def _stub_hitl2(
    original_paragraphs: OriginalParagraphs, proposed_rewrites: dict[str, str]
) -> Hitl2Confirmation:
    """HITL-2 桩（委托保守默认闸门 :class:`ConservativeHitl2Gate`）。

    无提议重写时一键通过（PASS）；桩路径下 rewrite_loop 产空 ``proposed_rewrites``
    （无触达段）→ 一律 PASS、无人确认 → 终稿逐字节等于原文。真实人判
    ``interrupt`` + checkpointer 属后续切片。
    """

    return hitl2_confirm(original_paragraphs, proposed_rewrites, ConservativeHitl2Gate())


def _stub_rewrite_loop(
    argument_tree: list[Argument],
    citations: dict[str, list[Source]],
    paragraph_list: list[ParagraphRecord],
    session_context: SessionContext,
    query_time_range: TimeRange,
) -> RewriteLoopOutcome:
    """逐段重写提议桩（委托 :func:`agents.rewrite_loop.propose_rewrites` + :class:`FakeRewriteLlmClient`）。

    桩路径下解析产出每段一个 ``background`` 影子节点、无 supported 假说、无 citations →
    无触达段 → ``propose_rewrites`` 永不调 LLM、产空 ``proposed_rewrites`` → hitl2 PASS →
    终稿逐字节等于原文。真实重写（触达段 LLM 提议）由 :func:`create_real_agents` 注入
    ``rewrite_llm`` 时启用。
    """

    return propose_rewrites_fn(
        argument_tree,
        citations,
        paragraph_list,
        session_context,
        query_time_range,
        llm=FakeRewriteLlmClient(),
    )


# --------------------------------------------------------------------------- #
# stage 降级兜底（issue #11 · PRD §13；随 build 闭包落于本 wiring 模块）
# --------------------------------------------------------------------------- #

# 体检覆盖范围（PRD §5）：claim & evidence。整体异常时把范围内未判决节点就地置 error。
_VERIFY_SCOPE: frozenset[ArgumentType] = frozenset(
    {ArgumentType.MAIN_CLAIM, ArgumentType.SUB_CLAIM, ArgumentType.EVIDENCE}
)


def _log_error_patch(stage: str, exc: BaseException) -> dict[str, list[str]]:
    """构造异常兜底日志 patch：``{"errors": ["[stage] ExcType: msg"]}``。"""

    return {"errors": [f"[{stage}] {type(exc).__name__}: {exc}"]}


def _mark_verify_scope_error(
    argument_tree: list[Argument], reason: str
) -> list[Argument]:
    """体检整体异常时把覆盖范围内、仍处未判决态的节点就地置 error（PRD §13）。

    ``claim`` / ``evidence`` 节点若仍 ``unverified`` / ``pending_verification``（体检本应
    判决却整体失败），就地置 ``error`` + 贴 ``orchestrator_error`` 标签——既兑现「目标节点
    置错误状态」，又使这些节点以 ``error``（待决）态流入 HITL-2 被驳回 → 原文逐字节还原。
    已判决（``credible`` / ``doubtful`` / ``error``）或非覆盖节点不动。
    """

    out: list[Argument] = []
    for argument in argument_tree:
        if argument.argument_type in _VERIFY_SCOPE and argument.status in (
            ArgumentStatus.UNVERIFIED,
            ArgumentStatus.PENDING_VERIFICATION,
        ):
            out.append(mark_argument_error(argument, reason=reason))
        else:
            out.append(argument.model_copy())
    return out


def _guarded(
    stage: str,
    body: Callable[[], dict[str, object]],
    fallback: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """stage 异常兜底：``body()`` 正常返回 patch；异常（非 :class:`Hitl2GateError`、非
    :class:`GraphBubbleUp`）→ ``fallback()`` + 日志、单向向前推进（PRD §13）。

    各下游 stage 的兜底形状此前各自重复 ``try / except Hitl2GateError: raise /
    except Exception: log + fallback``——收口于此：各 stage 只声明「正常返回」与
    「降级 patch」两件本质之事，样板集中一处（locality）。``Hitl2GateError`` 为硬闸门
    正确性硬停，**原样上抛、不兜底**（绝不无人拍板自动采纳，ADR-0010）。

    :class:`GraphBubbleUp`（``GraphInterrupt`` 的基类）是 langgraph 控制流异常族——
    T-03 后 hitl1/hitl2 节点经 ``_guarded`` 包裹 ``gate.review()``（其内 ``interrupt()`` 抛
    ``GraphInterrupt`` 暂停）。若 ``except Exception`` 吞之，interrupt 被静默兜底、图不暂停
    ——破坏异步 HITL spine。故 ``GraphBubbleUp`` 与 ``Hitl2GateError`` 同级：原样放行、不走
    fallback。普通异常仍兜底（既有降级语义不动）。
    """

    try:
        return body()
    except (Hitl2GateError, GraphBubbleUp):
        raise
    except Exception as exc:
        return {**_log_error_patch(stage, exc), **fallback()}


# --------------------------------------------------------------------------- #
# build 闭包：Agents 字段 → NodeFn（含 _guarded 兜底；拓扑 seam 的实现层）
# --------------------------------------------------------------------------- #


def _parse_partition_node(agents: Agents) -> NodeFn:
    def parse_partition_node(state: PipelineState) -> dict[str, object]:
        """parse+partition 合并节点（PRD §1 / ADR-0017 / Slice 1）。

        partition 纯代码切分 + 字节级自检（硬停，不兜底），parse 建树 + 顺产
        query_time_range（桩）/ paragraph_list（异常 → 记日志 + 空树向前，PRD §13）。
        partition 不变式自检失败即正确性 bug、应硬停（与原 ``_partition_node`` 一致）；
        parse 部分经 ``_guarded`` 兜底，异常时仍写回 ``original_paragraphs``、
        产空 argument_tree + 桩 query_time_range + 空 summaries 向前推进。
        """

        original_doc: bytes = state["original_doc"]
        original_paragraphs = OriginalParagraphs.from_text(original_doc)
        # original_paragraphs 自检：分区不变式（字节级还原是代码级确定的，不依赖任何模型）。
        rebuilt = b"".join(original_paragraphs.get(pid) for pid in original_paragraphs.paragraph_ids())
        assert rebuilt == original_doc, "分区不变式自检失败：拼接 ≠ 原始输入"

        out = _guarded(
            "parse+partition",
            lambda: _parse_output_patch(agents.parse(original_paragraphs)),
            lambda: _parse_output_patch(ParseOutput()),
        )
        return {**out, "original_paragraphs": original_paragraphs}

    return parse_partition_node


def _parse_output_patch(out: ParseOutput) -> dict[str, object]:
    """把 ParseOutput 摊成写回 PipelineState 的 patch（三 channel，含 paragraph_list）。"""

    return {
        "argument_tree": out.argument_tree,
        "query_time_range": out.query_time_range,
        "paragraph_list": out.paragraph_list,
    }


def _hitl1_outcome_patch(outcome: Hitl1Outcome) -> dict[str, object]:
    """把 :class:`Hitl1Outcome` 摊成写回 PipelineState 的 patch（route + 计数 + 树 +
    paragraph_list + 耗尽标签）。"""

    patch: dict[str, object] = {
        "argument_tree": outcome.argument_tree,
        "paragraph_list": outcome.paragraph_list,
        "hitl1_route": outcome.route.value,
        "partition_retry_count": outcome.retry_count,
    }
    if outcome.exhausted:
        # 受控分支（非异常降级）：贴 partition_retry_exhausted、向前推进。
        patch["errors"] = ["[hitl1] partition_retry_exhausted: 打回超 max retries，向前推进"]
    return patch


def _hitl1_node(agents: Agents) -> NodeFn:
    def hitl1_node(state: PipelineState) -> dict[str, object]:
        """HITL-1 partition 确认闸门（ADR-0017）。

        人确认段落切分是否合理：确认继续（skip/accept/edit）→ 条件边走默认下游；
        打回重跑（replay）→ 条件边路由回 ``parse+partition``（按 user prompt，当前伪代码桩、
        重切原样不触达原文）。打回有界（``max_retries`` 默认 3，ADR-0017）；超限向前推进 +
        贴 ``partition_retry_exhausted``（受控分支、**不**经 ``_guarded`` 异常降级）。
        ``agents.hitl1`` 异常仍经 ``_guarded``：记日志 + 保留 stale 树 + route=continue 向前。
        """

        argument_tree = state["argument_tree"]
        paragraph_list = state.get("paragraph_list", [])
        retry_count = int(state.get("partition_retry_count", 0))
        return _guarded(
            "hitl1",
            lambda: _hitl1_outcome_patch(
                agents.hitl1(argument_tree, paragraph_list, retry_count)
            ),
            lambda: {
                "argument_tree": argument_tree,
                "hitl1_route": Hitl1Route.CONTINUE.value,
                "partition_retry_count": retry_count,
            },
        )

    return hitl1_node


def _judgment_node(agents: Agents) -> NodeFn:
    def judgment_node(state: PipelineState) -> dict[str, object]:
        """裁决五合一节点（ADR-0017·Slice 5）。异常 → 覆盖范围内未判决节点置 error + 日志。

        吃 ``citations`` 判 per-argument / per-hypothesis 终态、再按序调 merge/impact/
        consistency 纯函数、整树写回 ``argument_tree``（单写者，故裁撤
        ``argument_credibility`` partial channel）；终态化后的假说写回 ``hypotheses``
        channel。整体异常时把 ``claim`` / ``evidence`` 范围内仍 ``unverified`` / ``pending``
        的节点就地置 ``error``（PRD §13「目标节点置错误状态」），整树写入；``hypotheses``
        保持 ``pending`` 不动（裁决失败不伪造终态）——下游 HITL-2 见 ``error`` 待决 → 原文
        逐字节还原。
        """

        argument_tree = state["argument_tree"]
        hypotheses = state.get("hypotheses", {})
        citations = state.get("citations", {})
        paragraph_list = state.get("paragraph_list", [])
        session_context = state["session_context"]
        query_time_range = state.get("query_time_range", DEFAULT_QUERY_TIME_RANGE)

        def body() -> dict[str, object]:
            outcome = agents.judgment(
                argument_tree,
                hypotheses,
                citations,
                paragraph_list,
                session_context,
                query_time_range,
            )
            return {
                "argument_tree": outcome.argument_tree,
                "hypotheses": outcome.hypotheses,
            }

        return _guarded(
            "judgment",
            body,
            lambda: {
                "argument_tree": _mark_verify_scope_error(argument_tree, reason="judgment")
            },
        )

    return judgment_node


def _hypothesis_propose_node(agents: Agents) -> NodeFn:
    def hypothesis_propose_node(state: PipelineState) -> dict[str, object]:
        """线路 2 · 开药（#5 · Slice 3 重定义为仅 propose）。异常 → 记日志 + 无假设向前。

        T-02：逐 argument 经 ``paragraph_id`` 从 ``paragraph_list`` 反查该段 ``original_content``
        + ``summary`` 调 ``propose``（原文 / 摘要取自 ``paragraph_list``，不读 ``Argument`` 原文字段），
        产 pending 假设 partial。开药不持有节点 ``status``（只产 ``candidate_hypotheses``），整体
        异常即「本轮无假设」——不置节点 ``error``（避免覆盖体检判决），记日志、空 partial 向前。
        """

        argument_tree = state["argument_tree"]
        paragraph_list = state.get("paragraph_list", [])
        return _guarded(
            "hypothesis_propose",
            lambda: {
                "hypotheses": agents.hypothesis_propose(
                    argument_tree, paragraph_list
                )
            },
            lambda: {},
        )

    return hypothesis_propose_node


def _retrieval_node(agents: Agents) -> NodeFn:
    def retrieval_node(state: PipelineState) -> dict[str, object]:
        """批量检索节点（PRD §8 / ADR-0017 · Slice 4 · 当前伪代码桩）。异常 → 降级空
        citations + 日志、单向向前（PRD §13）。

        读 ``argument_tree`` + ``hypotheses`` + ``query_time_range`` + ``session_context`` +
        ``paragraph_list``，调 ``agents.retrieval`` 批量检索、统一写回 ``citations`` channel
        （单写者=retrieval、reducer=_merge_dict）。当前桩不真实检索、返回空 citations（真实后端
        后续切片接入，``infra.retrieval`` 接口层不变）。``session_context`` / ``query_time_range`` /
        ``paragraph_list`` 被读取但不触发联网——仅穿背景供真实后端就位。检索 fn 异常即「本轮
        无 citations」——不卡死，记日志、空 citations 向前，下游见无素材、不触达任何段，
        终稿逐字节等于原文。
        """

        argument_tree = state["argument_tree"]
        hypotheses = state.get("hypotheses", {})
        query_time_range = state.get("query_time_range", DEFAULT_QUERY_TIME_RANGE)
        session_context = state["session_context"]
        paragraph_list = state.get("paragraph_list", [])
        return _guarded(
            "retrieval",
            lambda: {
                "citations": agents.retrieval(
                    argument_tree,
                    hypotheses,
                    query_time_range,
                    session_context,
                    paragraph_list,
                )
            },
            lambda: {"citations": {}},
        )

    return retrieval_node


def _hitl2_node(agents: Agents) -> NodeFn:
    def hitl2_node(state: PipelineState) -> dict[str, object]:
        """HITL-2 终稿文本确认闸门（#9·Slice 6 重定位·不可跳过硬闸门）。

        接收只读原文表 + rewrite_loop 的 ``proposed_rewrites``，逐段确认 / 编辑 / 驳回，
        拼装 ``final_document``（确认→提议文本、编辑→编辑文本、驳回 / 未触达→逐字节原文）。
        :class:`Hitl2GateError`（含硬闸门拒绝越权 PASS）为正确性硬停，**原样上抛、不兜底**
        （绝不无人拍板自动采纳，ADR-0010）；其余异常兜底：记日志 + 回退原文 bytes 拼接
        （保护原文底线）向前。
        """

        original_paragraphs = state["original_paragraphs"]
        proposed_rewrites = state.get("proposed_rewrites", {})

        def body() -> dict[str, object]:
            confirmation = agents.hitl2(original_paragraphs, proposed_rewrites)
            return {"final_document": confirmation.final_document}

        return _guarded(
            "hitl2",
            body,
            # 回退原文 bytes（保护原文底线）；无确认 → 逐字节等于原文。
            lambda: {
                "final_document": b"".join(
                    original_paragraphs.get(pid) for pid in original_paragraphs.paragraph_ids()
                )
            },
        )

    return hitl2_node


def _rewrite_loop_node(agents: Agents) -> NodeFn:
    def rewrite_loop_node(state: PipelineState) -> dict[str, object]:
        """逐段重写提议（#10·Slice 6·ADR-0017）。异常 → 降级空 proposed_rewrites + 日志、
        单向向前（PRD §13）。

        T-02：读 ``paragraph_list``（段落聚合根），逐段按 ``argument_tree_ids`` 正向解析节点、
        取该段 ``original_content`` 调 ``agents.rewrite_loop``——不再反向 join
        ``Argument.paragraph_id`` / 读 ``Argument.content``。读 ``argument_tree``（触达判定：
        段内有 supported 假说 / 命中 citations）+ ``citations`` + ``paragraph_list`` + 贯穿背景
        （``session_context`` / ``query_time_range``），写回 ``proposed_rewrites`` channel
        （单写者=rewrite_loop、读者=hitl2、reducer=_merge_dict）。per-段 LLM 失败由
        :func:`agents.rewrite_loop.propose_rewrites` 捕获、记入 ``outcome.errors``（该段省略、
        回退原文）；整体异常 → ``_guarded`` 降级空 proposed_rewrites + 日志向前——
        rewrite_loop **不碰 ``argument_tree``**（信号在 errors channel、不写树）。
        """

        argument_tree = state["argument_tree"]
        citations = state.get("citations", {})
        paragraph_list = state.get("paragraph_list", [])
        session_context = state["session_context"]
        query_time_range = state.get("query_time_range", DEFAULT_QUERY_TIME_RANGE)

        def body() -> dict[str, object]:
            outcome = agents.rewrite_loop(
                argument_tree,
                citations,
                paragraph_list,
                session_context,
                query_time_range,
            )
            patch: dict[str, object] = {"proposed_rewrites": outcome.proposed_rewrites}
            if outcome.errors:
                patch["errors"] = outcome.errors
            return patch

        return _guarded(
            "rewrite_loop",
            body,
            lambda: {"proposed_rewrites": {}},
        )

    return rewrite_loop_node


# --------------------------------------------------------------------------- #
# manifest：单一数据源驱动 Agents 构造 + default_pipeline（ADR-0014）
# --------------------------------------------------------------------------- #


def _hitl1_route(state: PipelineState) -> str | list[str] | None:
    """hitl1 条件路由（ADR-0017 受控打回边 ``hitl1 → parse+partition``）。

    读 ``hitl1_route`` channel：``"replay"`` → 返回 ``"parse+partition"``（重跑上游、
    有界）；其余（``"continue"`` / 缺省）→ 返回 ``None`` 走默认下游（依赖 hitl1 的节点们）。
    超限打回由 :func:`_hitl1_outcome_patch` 改写为 ``"continue"`` + 贴标签，故此处不会
    再路由到上游。
    """

    if state.get("hitl1_route") == Hitl1Route.REPLAY.value:
        return "parse+partition"
    return None


@dataclass(frozen=True)
class RealDeps:
    """``create_real_agents`` 的注入参数包，供 manifest 条目的 ``real`` 工厂按需取用。"""

    llm: LlmClient
    hitl1_gate: Hitl1Gate
    judgment_llm: JudgmentLlmClient | None = None
    hypothesis_llm: HypothesisLlmClient | None = None
    rewrite_llm: RewriteLlmClient | None = None
    hitl2_gate: Hitl2Gate | None = None
    retrieval_runtime: RetrievalRuntime | None = None


def _real_retrieval_factory(deps: RealDeps) -> Any:
    """manifest retrieval ``real`` 工厂（PRD §Solution / §定位线索 · Slice 2，与 judgment 同形）。

    ``retrieval_runtime`` 给出时返回绑定 runtime 的 :class:`RetrievalFn`（
    ``partial(real_retrieval, runtime=...)``，镜像 judgment ``partial(judge_and_adjudicate,
    llm=d.judgment_llm)``）替换桩；为 ``None`` 时返 ``None`` 保留桩（真实后端未配置 → 空 citations）。
    延迟 :func:`import agents.retrieval` 以免桩路径 eager 拉起 vendored search_agent 栈
    （langgraph / langchain）；仅真实装配路径触发。
    """

    if deps.retrieval_runtime is None:
        return None
    from agents.retrieval import build_real_retrieval

    return build_real_retrieval(deps.retrieval_runtime)


@dataclass(frozen=True)
class AgentEntry:
    """manifest 条目：一个 stage / Agent 的装配描述（ADR-0014）。

    :attr:`name` 为图节点名 / stage 名；:attr:`field` 为 :class:`Agents` dataclass 字段名
    （``partition`` 无 Agent 字段 → ``None``）；:attr:`stub` 为桩 fn（``partition`` → ``None``），
    异质 callable 故标 ``Any``——**字段访问**仍经 typed :class:`Agents` 保类型安全；
    :attr:`real` 为条件替换工厂（``RealDeps → fn | None``，返回 ``None`` 即保留桩；纯函数
    Agent 与 ``partition`` 为 ``None``）；:attr:`deps` 为上游 stage 名（``()`` 接 START）；
    :attr:`build` 据 :class:`Agents` 产出 :data:`runtime.orchestrator.NodeFn`（含
    :func:`_guarded` 兜底）；:attr:`route` / :attr:`max_replays` 见 :class:`runtime.orchestrator.StageSpec`
    （条件路由 seam + 循环预算，ADR-0017；多数 stage 为 ``None`` / ``0``）。

    展示元数据（PRD §10.1 / §7.3 · T-02）：:attr:`label` / :attr:`node_type` /
    :attr:`color` / :attr:`desc` / :attr:`visible` / :attr:`interrupt` 供
    :func:`api_layer.graph_view.build_graph_view` 单一源摊成 ``GraphView``——
    供后续 ``GET /api/agent/graph``（T-04）与 WS ``graph_static``（T-06）共享，避免漂移。
    :attr:`label` 缺省从 :attr:`name` 推导；:attr:`visible` 为单一可见性旋钮（缺省 ``True``）；
    :attr:`interrupt` 标 HITL 硬闸门节点（``hitl1`` / ``hitl2``），由
    :func:`api_layer.graph_view.build_graph_view` 强制 ``visible=True``——配置 override
    隐藏 interrupt 节点会被忽略并告警（HITL 不可对前端隐身）。
    """

    name: str
    field: str | None
    stub: Any
    real: Callable[[RealDeps], Any] | None
    deps: tuple[str, ...]
    build: Callable[[Agents], NodeFn]
    route: Callable[[PipelineState], str | list[str] | None] | None = None
    max_replays: int = 0
    label: str | None = None
    node_type: str | None = None
    color: str | None = None
    desc: str | None = None
    visible: bool = True
    interrupt: bool = False


MANIFEST: tuple[AgentEntry, ...] = (
    AgentEntry(
        name="parse+partition",
        field="parse",
        stub=_stub_parse,
        real=lambda d: partial(parse_fn, llm=d.llm),
        deps=(),
        build=_parse_partition_node,
        label="解析+切分",
        node_type="parse",
        color="#4A90D9",
        desc="原文切分为只读段落表 + LLM 建初始论证树（顺产 query_time_range / paragraph_list）",
    ),
    AgentEntry(
        name="hitl1",
        field="hitl1",
        stub=_stub_hitl1,
        real=lambda d: partial(hitl1_confirm_partition, gate=d.hitl1_gate),
        deps=("parse+partition",),
        build=_hitl1_node,
        route=_hitl1_route,
        max_replays=DEFAULT_MAX_PARTITION_RETRIES,
        label="HITL-1 切分确认",
        node_type="hitl1",
        color="#D97706",
        desc="人确认段落切分；不合理则按 prompt 有界打回重跑 parse+partition（max 3）",
        interrupt=True,
    ),
    AgentEntry(
        name="hypothesis_propose",
        field="hypothesis_propose",
        stub=_stub_hypothesis_propose,
        real=lambda d: (
            partial(propose_hypotheses_fn, llm=d.hypothesis_llm)
            if d.hypothesis_llm is not None
            else None
        ),
        deps=("hitl1",),
        build=_hypothesis_propose_node,
        label="假设生成",
        node_type="hypothesis",
        color="#9B59B6",
        desc="逐节点在原文边界内仅 propose pending 候选修订假说（取证移至 judgment）",
    ),
    AgentEntry(
        name="retrieval",
        field="retrieval",
        stub=_stub_retrieval,
        real=_real_retrieval_factory,  # Slice 2：填 real=None 空位、与 judgment 同形（real= 工厂 + RealDeps.retrieval_runtime 注入）。
        deps=("hypothesis_propose",),
        build=_retrieval_node,
        label="检索",
        node_type="retrieval",
        color="#16A085",
        desc="批量检索 citations（vendored SearchAgent V12 真实后端、with_llm=False、verdict 丢弃、judgment 重判）",
    ),
    AgentEntry(
        name="judgment",
        field="judgment",
        stub=_stub_judgment,
        real=lambda d: (
            partial(judge_and_adjudicate, llm=d.judgment_llm)
            if d.judgment_llm is not None
            else None
        ),
        deps=("retrieval",),
        build=_judgment_node,
        label="裁决",
        node_type="judgment",
        color="#C0392B",
        desc="五合一：吃 citations 判 per-argument / per-hypothesis 终态 + 串联 merge/impact/consistency",
    ),
    AgentEntry(
        name="rewrite_loop",
        field="rewrite_loop",
        stub=_stub_rewrite_loop,
        real=lambda d: (
            partial(propose_rewrites_fn, llm=d.rewrite_llm)
            if d.rewrite_llm is not None
            else None
        ),
        deps=("judgment",),
        build=_rewrite_loop_node,
        label="重写循环",
        node_type="rewrite",
        color="#8E44AD",
        desc="对被触达段逐段提议重写文本、产 proposed_rewrites；未触达段省略",
    ),
    AgentEntry(
        name="hitl2",
        field="hitl2",
        stub=_stub_hitl2,
        real=lambda d: partial(
            hitl2_confirm, gate=d.hitl2_gate or ConservativeHitl2Gate()
        ),
        deps=("rewrite_loop",),
        build=_hitl2_node,
        label="HITL-2 终稿确认",
        node_type="hitl2",
        color="#D97706",
        desc="逐段确认 / 编辑 / 驳回 proposed_rewrites 后拼装终稿 final_document（不可跳过）",
        interrupt=True,
    ),
)


def create_stub_agents() -> Agents:
    """返回全套桩智能体，用于 tracer bullet 端到端回路。

    manifest 驱动：遍历 :data:`MANIFEST`，按 ``field`` 名把 ``stub`` 装入 typed
    :class:`Agents` dataclass（``partition`` 无 field、跳过）。异质 callable 以 ``Any``
    splat 装入——**字段访问** ``agents.parse: ParseFn`` 仍 typed（ADR-0014：保 typed
    Agents，不取 dict registry；Any-splat 在 ``mypy --strict`` 下允许，被否决的
    dict registry 问题是访问无类型，本方案保访问 typed）。
    """

    return Agents(**{e.field: e.stub for e in MANIFEST if e.field is not None})


def create_real_agents(
    *,
    llm: LlmClient,
    hitl1_gate: Hitl1Gate,
    judgment_llm: JudgmentLlmClient | None = None,
    hypothesis_llm: HypothesisLlmClient | None = None,
    rewrite_llm: RewriteLlmClient | None = None,
    hitl2_gate: Hitl2Gate | None = None,
    retrieval_runtime: RetrievalRuntime | None = None,
) -> Agents:
    """返回「真实解析 + 真实 HITL-1 +（可选）真实开药 + 真实裁决 +（可选）真实重写提议 +
    真实 HITL-2」的智能体组。

    在 :func:`create_stub_agents` 基础上替换桩为真实实现。``llm`` 为解析 seam（具体 provider
    适配器属生产装配）；``hitl1_gate`` 为 HITL-1 注入闸门（真实 interrupt+checkpointer
    属后续切片）。``hypothesis_llm`` 给出时开药桩（#5 · Slice 3 重定义为仅 propose）替换为真实
    「投机生成」实现——逐 argument 经 ``paragraph_list`` 反查段原文 / 摘要调 ``propose``、产 pending
    假说、只写回 ``candidate_hypotheses``、不改 ``status``，字节级承诺依然
    成立。``judgment_llm`` 给出时裁决桩（#4 取证 + #5 取证 + #6 merge + #7 impact + #8
    consistency 五合一·Slice 5）替换为真实裁判实现——吃 citations 判 per-argument /
    per-hypothesis 终态、再按序调 merge/impact/consistency 纯函数、整树写回
    ``argument_tree``（单写者，裁撤 ``argument_credibility`` partial channel）；merge/impact/
    consistency 为确定性纯函数、无 LLM/检索依赖（桩路径与真实装配共用同一串联、逻辑不动），
    故无独立 ``real`` 工厂；裁决只写回 ``status`` / ``merge_decision`` / ``issue_tags`` /
    裁剪假设、不改 ``content``、不置 ``adopted``，字节级承诺依然成立。``rewrite_llm`` 给出时
    重写桩（#10·Slice 6·ADR-0017）替换为真实逐段提议重写实现——对被触达段（supported 假说 /
    命中 citations）调 ``propose_rewrite`` 产 ``proposed_rewrites``、未触达段省略，**不碰
    ``argument_tree``**（按段 / 文本工作、与 argument 状态解耦）。HITL-2（#9·Slice 6 重定位
    为终稿文本确认闸门）为不可跳过的硬闸门：``hitl2_gate`` 缺省时用
    :class:`ConservativeHitl2Gate`（无提议重写→一键通过、有提议重写→全驳回、原文逐字节保留、
    绝不自动采纳，ADR-0010）；逐段确认 / 编辑 / 驳回 ``proposed_rewrites`` 后由
    ``assemble_final_document`` 拼装 ``final_document``（确认→提议文本、编辑→编辑文本、驳回 /
    未触达→逐字节原文）——故注入会确认的闸门时终稿不再逐字节等于原文（变更段用确认文本），
    未变更段仍逐字节还原。原独立 ``writeback`` 节点裁撤、终稿在 hitl2 落地；``adopted`` /
    ``corrected`` / ``adopted_hypothesis_id`` 在新流程不再被写（domain 字段保留不删）。真实
    人判 ``interrupt`` + ``Command(resume)`` + checkpointer 属后续切片；终稿拼装幂等续跑入口见
    :meth:`runtime.orchestrator.Orchestrator.resume_rewrite`（#11）。

    manifest 驱动：遍历 :data:`MANIFEST`，对有 ``real`` 工厂的条目调 ``real(deps)``，返回非
    ``None`` 者替换对应 ``field``；纯函数 Agent（``real=None``）与 ``partition`` 不替换。
    """

    deps = RealDeps(
        llm=llm,
        hitl1_gate=hitl1_gate,
        judgment_llm=judgment_llm,
        hypothesis_llm=hypothesis_llm,
        rewrite_llm=rewrite_llm,
        hitl2_gate=hitl2_gate,
        retrieval_runtime=retrieval_runtime,
    )
    agents = create_stub_agents()
    patches: dict[str, Any] = {}
    for entry in MANIFEST:
        if entry.real is None or entry.field is None:
            continue
        fn = entry.real(deps)
        if fn is not None:
            patches[entry.field] = fn
    return replace(agents, **patches)
