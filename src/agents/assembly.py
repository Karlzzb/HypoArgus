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

from agents.consistency import consistency as consistency_fn
from agents.hitl1 import Hitl1Gate
from agents.hitl1 import confirm as hitl1_confirm
from agents.hitl2 import ConservativeHitl2Gate, Hitl2Gate, Hitl2GateError
from agents.hitl2 import confirm as hitl2_confirm
from agents.hypothesis import HypothesisLlmClient
from agents.hypothesis import hypothesize as hypothesize_fn
from agents.impact import impact as impact_fn
from agents.merge import apply_partial_updates, merge_with_partials
from agents.merge import merge as merge_fn
from agents.parser import LlmClient
from agents.parser import parse as parse_fn
from agents.verification import VerifyLlmClient
from agents.verification import verify as verify_fn
from agents.writeback import WritebackResult, writeback
from domain import ArgumentationNode, NodeStatus, NodeType
from infra.retrieval import RetrievalLayer
from raw_store import RawParagraphStore
from status_machine import mark_node_error

if TYPE_CHECKING:
    # 仅类型：build 闭包返回 ``NodeFn``、``state: PipelineState`` 注解在
    # ``from __future__ import annotations`` 下为字符串、运行时不求值。运行时无
    # agents→runtime 依赖（依赖方向保持 runtime→agents），故用 TYPE_CHECKING 破环。
    from runtime.orchestrator import NodeFn, PipelineState

__all__ = [
    "ParseFn",
    "Hitl1Fn",
    "VerifyFn",
    "HypothesisFn",
    "MergeFn",
    "ImpactFn",
    "ConsistencyFn",
    "Hitl2Fn",
    "WritebackFn",
    "WritebackResult",
    "Agents",
    "AgentEntry",
    "RealDeps",
    "MANIFEST",
    "create_stub_agents",
    "create_real_agents",
]


class ParseFn(Protocol):
    """论证结构解析（#2 接入真实 LLM 解析）。返回初始论证树。"""

    def __call__(self, store: RawParagraphStore) -> list[ArgumentationNode]: ...


class Hitl1Fn(Protocol):
    """HITL-1 结构确认（#2 接入，可跳过）。返回确认后的树。"""

    def __call__(self, tree: list[ArgumentationNode]) -> list[ArgumentationNode]: ...


class VerifyFn(Protocol):
    """线路 1 · 体检（#4 接入 ReAct）。返回对部分节点的状态更新（by node_id）。"""

    def __call__(
        self, tree: list[ArgumentationNode]
    ) -> dict[str, ArgumentationNode]: ...


class HypothesisFn(Protocol):
    """线路 2 · 开药（#5 接入）。返回对部分节点的假设更新（by node_id）。"""

    def __call__(
        self, tree: list[ArgumentationNode]
    ) -> dict[str, ArgumentationNode]: ...


class MergeFn(Protocol):
    """双轨合并算子（#6 接入确定性 12 格矩阵）。返回标注后的同一棵树。"""

    def __call__(self, tree: list[ArgumentationNode]) -> list[ArgumentationNode]: ...


class ImpactFn(Protocol):
    """影响传导（#7 接入，串行·不产文本）。返回标注后的同一棵树。"""

    def __call__(self, tree: list[ArgumentationNode]) -> list[ArgumentationNode]: ...


class ConsistencyFn(Protocol):
    """一致性校验（#8 接入，批注门禁·只贴 issue_tags·不打回）。"""

    def __call__(self, tree: list[ArgumentationNode]) -> list[ArgumentationNode]: ...


class Hitl2Fn(Protocol):
    """HITL-2 修订确认（#9 接入，不可跳过硬闸门）。

    接收标注完成的树 + 只读原文表（HITL-2 对比左栏数据源，ADR-0005），返回采纳后的树。
    """

    def __call__(
        self, tree: list[ArgumentationNode], store: RawParagraphStore
    ) -> list[ArgumentationNode]: ...


class WritebackFn(Protocol):
    """修订回写（#10 真实分流·幂等）。返回终稿 bytes + 状态翻正后的树。

    接收修订确认后的终版树 + 只读原文表，按段落原子缝合（ADR-0001/0005/0011）：
    未变更段逐字节拷回、变更段按被采纳假设的关系分流（对立→替换、递进→改写、
    扩展→段尾追加带审计标识）；成功置 ``corrected``、失败停留 ``adopted`` 并贴
    ``writeback_error``，重跑幂等不重复注入。
    """

    def __call__(
        self, tree: list[ArgumentationNode], store: RawParagraphStore
    ) -> WritebackResult: ...


@dataclass
class Agents:
    """一组可注入的子智能体契约。

    中枢按固定顺序调用这些契约；每个契约在本切片都可用桩占位，后续切片逐个替换。
    typed dataclass 保字段访问类型安全（ADR-0014：不取 dict registry）。
    """

    parse: ParseFn
    hitl1: Hitl1Fn
    verification: VerifyFn
    hypothesis: HypothesisFn
    merge: MergeFn
    impact: ImpactFn
    consistency: ConsistencyFn
    hitl2: Hitl2Fn
    writeback: WritebackFn


# --------------------------------------------------------------------------- #
# 桩实现
# --------------------------------------------------------------------------- #


def _stub_parse(store: RawParagraphStore) -> list[ArgumentationNode]:
    """解析桩：每段一个只读 background 影子节点。

    影子节点不参与校验与传导、状态恒 ``unverified``、永不进入 ``adopted``，
    故回写对每段都走逐字节拷回通道——tracer bullet 的字节级承诺由此成立。
    真实解析（#2）将在此识别 main_claim/sub_claim/evidence/qualification 并建父子树。
    """

    return [
        ArgumentationNode(
            node_id=f"n-{pid}",
            node_type=NodeType.BACKGROUND,
            paragraph_id=pid,
            content=store.get(pid).decode("utf-8", errors="surrogateescape"),
        )
        for pid in store.paragraph_ids()
    ]


def _stub_hitl1(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """HITL-1 桩：跳过结构确认，树原样返回（跳过即不改原文一个字）。"""

    return tree


def _stub_verification(tree: list[ArgumentationNode]) -> dict[str, ArgumentationNode]:
    """体检桩：不校验、不更新状态。"""

    return {}


def _stub_hypothesis(tree: list[ArgumentationNode]) -> dict[str, ArgumentationNode]:
    """开药桩：不生成假设。"""

    return {}


def _merge(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """合并算子（委托纯函数 :func:`agents.merge.merge`）。

    双轨合并是确定性纯函数、无 LLM / 检索依赖（ADR-0006 12 格矩阵），故无桩——
    tracer bullet 与真实装配共用同一实现。桩路径下两线路均返回 ``{}``，输入树全为
    ``unverified`` 且 ``candidate_hypotheses`` 空，合并逐节点判 ``KEEP``、不裁剪、
    不置 ``adopted``，故「无采纳改动 → 终稿逐字节等于原文」承诺继续成立。
    """

    return merge_fn(tree)


def _impact(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """影响传导算子（委托纯函数 :func:`agents.impact.impact`）。

    影响传导是确定性纯函数、无 LLM / 检索依赖（ADR-0003 串行·不产文本、ADR-0013
    剩余支撑率公式），故无桩——tracer bullet 与真实装配共用同一实现。桩路径下两线路均返回
    ``{}``、合并逐节点判 ``KEEP``、影子上层论点无子节点参与传导，故影响传导不动任何节点，
    「无采纳改动 → 终稿逐字节等于原文」承诺继续成立。
    """

    return impact_fn(tree)


def _consistency(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """一致性校验算子（委托纯函数 :func:`agents.consistency.consistency`）。

    一致性校验是确定性纯函数、无 LLM / 检索依赖（ADR-0012 批注门禁·单次扫描·
    只贴 ``issue_tags``），故无桩——tracer bullet 与真实装配共用同一实现。桩路径下
    解析产出每段一个 ``background`` 影子节点：无混段、无多根、无主论点重复、无重复
    限定，一致性校验不贴任何标签，「无采纳改动 → 终稿逐字节等于原文」承诺继续成立。
    """

    return consistency_fn(tree)


def _stub_hitl2(
    tree: list[ArgumentationNode], store: RawParagraphStore
) -> list[ArgumentationNode]:
    """HITL-2 桩（委托保守默认闸门 :class:`ConservativeHitl2Gate`）。

    无待决内容时一键通过（PASS）；桩路径下解析产出每段一个 ``background`` 影子节点，
    无 ``doubtful``/``error``/``conflict``/激活候选 → 一律 PASS、无人采纳 → 逐字节拷回。
    真实人判 ``interrupt`` + checkpointer 属后续切片。
    """

    return hitl2_confirm(tree, store, ConservativeHitl2Gate())


def _stub_writeback(
    tree: list[ArgumentationNode], store: RawParagraphStore
) -> WritebackResult:
    """回写算子（委托纯函数 :func:`agents.writeback.writeback`）。

    回写是确定性纯函数、无 LLM / 检索依赖（ADR-0001/0005/0011、PRD §11 纯函数子缝），
    故无桩——tracer bullet 与真实装配共用同一实现。桩路径下解析产出每段一个
    ``background`` 影子节点、无人采纳 → 全部逐字节拷回，``final_doc`` 逐字节等于原文；
    采纳路径下（HITL-2 #9 注入会采纳的闸门时）按关系分流缝合、翻 ``corrected``。
    """

    return writeback(tree, store)


# --------------------------------------------------------------------------- #
# stage 降级兜底（issue #11 · PRD §13；随 build 闭包落于本 wiring 模块）
# --------------------------------------------------------------------------- #

# 体检覆盖范围（PRD §5）：claim & evidence。整体异常时把范围内未判决节点就地置 error。
_VERIFY_SCOPE: frozenset[NodeType] = frozenset(
    {NodeType.MAIN_CLAIM, NodeType.SUB_CLAIM, NodeType.EVIDENCE}
)


def _log_error_patch(stage: str, exc: BaseException) -> dict[str, list[str]]:
    """构造异常兜底日志 patch：``{"errors": ["[stage] ExcType: msg"]}``。"""

    return {"errors": [f"[{stage}] {type(exc).__name__}: {exc}"]}


def _mark_verify_scope_error(
    tree: list[ArgumentationNode], reason: str
) -> list[ArgumentationNode]:
    """体检整体异常时把覆盖范围内、仍处未判决态的节点就地置 error（PRD §13）。

    ``claim`` / ``evidence`` 节点若仍 ``unverified`` / ``pending_verification``（体检本应
    判决却整体失败），就地置 ``error`` + 贴 ``orchestrator_error`` 标签——既兑现「目标节点
    置错误状态」，又使这些节点以 ``error``（待决）态流入 HITL-2 被驳回 → 原文逐字节还原。
    已判决（``credible`` / ``doubtful`` / ``error``）或非覆盖节点不动。
    """

    out: list[ArgumentationNode] = []
    for node in tree:
        if node.node_type in _VERIFY_SCOPE and node.status in (
            NodeStatus.UNVERIFIED,
            NodeStatus.PENDING_VERIFICATION,
        ):
            out.append(mark_node_error(node, reason=reason))
        else:
            out.append(node.model_copy())
    return out


def _guarded(
    stage: str,
    body: Callable[[], dict[str, object]],
    fallback: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """stage 异常兜底：``body()`` 正常返回 patch；异常（非 :class:`Hitl2GateError`）→
    ``fallback()`` + 日志、单向向前推进（PRD §13）。

    9 个下游 stage 的兜底形状此前各自重复 ``try / except Hitl2GateError: raise /
    except Exception: log + fallback``——收口于此：各 stage 只声明「正常返回」与
    「降级 patch」两件本质之事，样板集中一处（locality）。``Hitl2GateError`` 为硬闸门
    正确性硬停，**原样上抛、不兜底**（绝不无人拍板自动采纳，ADR-0010）。
    """

    try:
        return body()
    except Hitl2GateError:
        raise
    except Exception as exc:
        return {**_log_error_patch(stage, exc), **fallback()}


# --------------------------------------------------------------------------- #
# build 闭包：Agents 字段 → NodeFn（含 _guarded 兜底；拓扑 seam 的实现层）
# --------------------------------------------------------------------------- #


def _partition_node(_agents: Agents) -> NodeFn:
    def partition_node(state: PipelineState) -> dict[str, object]:
        """确定性段落切分 + 固化只读原文表（纯代码·零 LLM）。

        纯代码、无智能体波动，不包兜底——分区不变式自检失败即正确性 bug、应硬停。
        """

        raw_text: bytes = state["raw_text"]
        store = RawParagraphStore.from_text(raw_text)
        # store 自检：分区不变式（字节级还原是代码级确定的，不依赖任何模型）。
        rebuilt = b"".join(store.get(pid) for pid in store.paragraph_ids())
        assert rebuilt == raw_text, "分区不变式自检失败：拼接 ≠ 原始输入"
        return {"store": store, "tree": []}

    return partition_node


def _parse_node(agents: Agents) -> NodeFn:
    def parse_node(state: PipelineState) -> dict[str, object]:
        """论证结构解析（#2 接入真实 LLM）。异常 → 记日志 + 保留空树向前（PRD §13）。"""

        store = state["store"]
        return _guarded(
            "parse",
            lambda: {"tree": agents.parse(store)},
            lambda: {"tree": []},
        )

    return parse_node


def _hitl1_node(agents: Agents) -> NodeFn:
    def hitl1_node(state: PipelineState) -> dict[str, object]:
        """HITL-1 结构确认（#2 接入，可跳过）。异常 → 记日志 + 保留 stale 树向前。"""

        tree = state["tree"]
        return _guarded(
            "hitl1",
            lambda: {"tree": agents.hitl1(tree)},
            lambda: {"tree": tree},
        )

    return hitl1_node


def _verification_node(agents: Agents) -> NodeFn:
    def verification_node(state: PipelineState) -> dict[str, object]:
        """线路 1 · 体检（#4 接入 ReAct）。异常 → 覆盖范围内未判决节点置 error + 日志。

        整体异常（非单节点 LLM 抛错——单节点异常由体检内部已置 ``error``）时，把
        ``claim`` / ``evidence`` 范围内仍 ``unverified`` / ``pending`` 的节点就地置
        ``error``（PRD §13「目标节点置错误状态」），整树写入 ``tree``；不写
        ``verification_updates``（无 partial 可信）。下游合并据此见 ``error`` 状态。
        """

        tree = state["tree"]
        return _guarded(
            "verification",
            lambda: {"verification_updates": agents.verification(tree)},
            lambda: {"tree": _mark_verify_scope_error(tree, reason="verify")},
        )

    return verification_node


def _hypothesis_node(agents: Agents) -> NodeFn:
    def hypothesis_node(state: PipelineState) -> dict[str, object]:
        """线路 2 · 开药（#5 接入）。异常 → 记日志 + 无假设向前（不置节点 error）。

        开药不持有节点 ``status``（只产 ``candidate_hypotheses``），整体异常即「本轮
        无假设」——不置节点 ``error``（避免覆盖体检判决），记日志、空 partial 向前。
        """

        tree = state["tree"]
        return _guarded(
            "hypothesis",
            lambda: {"hypothesis_updates": agents.hypothesis(tree)},
            lambda: {},
        )

    return hypothesis_node


def _merge_node(agents: Agents) -> NodeFn:
    def merge_node(state: PipelineState) -> dict[str, object]:
        """双轨合并算子（#6 接入 12 格矩阵）。异常 → 记日志 + 保留已合流的 combined 向前。

        先字段级合流两线路 partial（``status`` ← 体检、``candidate_hypotheses`` ← 开药），
        再跑矩阵裁决。合并的两步 staging 收口于 :func:`merge_with_partials`（不再由调度层
        显式串接）。合并算子本身异常时，partial 已合流的中间态可信——保留之向前（无
        ``merge_decision``，下游以体检/开药结果继续），记日志。
        """

        tree = state["tree"]
        v_updates = state.get("verification_updates", {})
        h_updates = state.get("hypothesis_updates", {})
        return _guarded(
            "merge",
            lambda: {"tree": merge_with_partials(tree, v_updates, h_updates, agents.merge)},
            # 兜底取「已合流、未裁决」中间态——apply_partial_updates 为 merge 模块公开
            # 纯函数（兜底语义需要、非 staging 串接，不构成内部步骤泄漏）。
            lambda: {"tree": apply_partial_updates(tree, v_updates, h_updates)},
        )

    return merge_node


def _impact_node(agents: Agents) -> NodeFn:
    def impact_node(state: PipelineState) -> dict[str, object]:
        """影响传导（#7 接入，串行·不产文本）。异常 → 记日志 + 保留 stale 树向前。"""

        tree = state["tree"]
        return _guarded(
            "impact",
            lambda: {"tree": agents.impact(tree)},
            lambda: {"tree": tree},
        )

    return impact_node


def _consistency_node(agents: Agents) -> NodeFn:
    def consistency_node(state: PipelineState) -> dict[str, object]:
        """一致性校验（#8 接入，批注门禁·不打回）。异常 → 记日志 + 保留 stale 树向前。"""

        tree = state["tree"]
        return _guarded(
            "consistency",
            lambda: {"tree": agents.consistency(tree)},
            lambda: {"tree": tree},
        )

    return consistency_node


def _hitl2_node(agents: Agents) -> NodeFn:
    def hitl2_node(state: PipelineState) -> dict[str, object]:
        """HITL-2 修订确认（#9 接入，不可跳过硬闸门）。异常 → 记日志 + 保留 stale 树向前。

        接收标注完成的树 + 只读原文表（HITL-2 对比左栏数据源，ADR-0005），返回采纳后的树。
        :class:`Hitl2GateError`（含硬闸门拒绝越权 PASS）为正确性硬停，**原样上抛、不兜底**
        （绝不无人拍板自动采纳，ADR-0010）；其余异常兜底：记日志 + 保留 stale 树（无人采纳）
        → 回写逐字节还原。
        """

        tree = state["tree"]
        store = state["store"]
        return _guarded(
            "hitl2",
            lambda: {"tree": agents.hitl2(tree, store)},
            lambda: {"tree": tree},
        )

    return hitl2_node


def _writeback_node(agents: Agents) -> NodeFn:
    def writeback_node(state: PipelineState) -> dict[str, object]:
        """修订回写（#10 接入真实分流·幂等）。异常 → 记日志 + 回退原文 bytes 向前（PRD §13）。

        按段落原子缝合终稿 bytes、翻正采纳节点状态。回写整体异常时保护原文底线：回退为
        只读原文表逐字节拼接（== 原始输入，分区不变式），``adopted`` 节点保留 ``adopted`` 不
        翻正、待 :meth:`runtime.orchestrator.Orchestrator.resume_writeback` 续跑（issue #11 衔接
        #10）；记日志。幂等：续跑据持久化的 ``adopted_hypothesis_id`` 重新推导、不重复注入。
        """

        tree = state["tree"]
        store = state["store"]

        def success() -> dict[str, object]:
            result = agents.writeback(tree, store)
            return {"final_doc": result.final_doc, "tree": result.tree}

        return _guarded(
            "writeback",
            success,
            # 回退原文 bytes（保护原文底线）；adopted 节点不动、待续跑。
            lambda: {
                "final_doc": b"".join(store.get(pid) for pid in store.paragraph_ids()),
                "tree": tree,
            },
        )

    return writeback_node


# --------------------------------------------------------------------------- #
# manifest：单一数据源驱动 Agents 构造 + default_pipeline（ADR-0014）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RealDeps:
    """``create_real_agents`` 的注入参数包，供 manifest 条目的 ``real`` 工厂按需取用。"""

    llm: LlmClient
    hitl1_gate: Hitl1Gate
    verify_llm: VerifyLlmClient | None = None
    hypothesis_llm: HypothesisLlmClient | None = None
    retrieval: RetrievalLayer | None = None
    hitl2_gate: Hitl2Gate | None = None
    max_iterations: int = 8


@dataclass(frozen=True)
class AgentEntry:
    """manifest 条目：一个 stage / Agent 的装配描述（ADR-0014）。

    :attr:`name` 为图节点名 / stage 名；:attr:`field` 为 :class:`Agents` dataclass 字段名
    （``partition`` 无 Agent 字段 → ``None``）；:attr:`stub` 为桩 fn（``partition`` → ``None``），
    异质 callable 故标 ``Any``——**字段访问**仍经 typed :class:`Agents` 保类型安全；
    :attr:`real` 为条件替换工厂（``RealDeps → fn | None``，返回 ``None`` 即保留桩；纯函数
    Agent 与 ``partition`` 为 ``None``）；:attr:`deps` 为上游 stage 名（``()`` 接 START）；
    :attr:`build` 据 :class:`Agents` 产出 :data:`runtime.orchestrator.NodeFn`（含
    :func:`_guarded` 兜底）。
    """

    name: str
    field: str | None
    stub: Any
    real: Callable[[RealDeps], Any] | None
    deps: tuple[str, ...]
    build: Callable[[Agents], NodeFn]


MANIFEST: tuple[AgentEntry, ...] = (
    AgentEntry(
        name="partition",
        field=None,
        stub=None,
        real=None,
        deps=(),
        build=_partition_node,
    ),
    AgentEntry(
        name="parse",
        field="parse",
        stub=_stub_parse,
        real=lambda d: partial(parse_fn, llm=d.llm),
        deps=("partition",),
        build=_parse_node,
    ),
    AgentEntry(
        name="hitl1",
        field="hitl1",
        stub=_stub_hitl1,
        real=lambda d: partial(hitl1_confirm, gate=d.hitl1_gate),
        deps=("parse",),
        build=_hitl1_node,
    ),
    AgentEntry(
        name="verification",
        field="verification",
        stub=_stub_verification,
        real=lambda d: (
            partial(
                verify_fn,
                llm=d.verify_llm,
                retrieval=d.retrieval,
                max_iterations=d.max_iterations,
            )
            if d.verify_llm is not None and d.retrieval is not None
            else None
        ),
        deps=("hitl1",),
        build=_verification_node,
    ),
    AgentEntry(
        name="hypothesis",
        field="hypothesis",
        stub=_stub_hypothesis,
        real=lambda d: (
            partial(
                hypothesize_fn,
                llm=d.hypothesis_llm,
                retrieval=d.retrieval,
                max_iterations=d.max_iterations,
            )
            if d.hypothesis_llm is not None and d.retrieval is not None
            else None
        ),
        deps=("hitl1",),
        build=_hypothesis_node,
    ),
    AgentEntry(
        name="merge",
        field="merge",
        stub=_merge,
        real=None,
        deps=("verification", "hypothesis"),
        build=_merge_node,
    ),
    AgentEntry(
        name="impact",
        field="impact",
        stub=_impact,
        real=None,
        deps=("merge",),
        build=_impact_node,
    ),
    AgentEntry(
        name="consistency",
        field="consistency",
        stub=_consistency,
        real=None,
        deps=("impact",),
        build=_consistency_node,
    ),
    AgentEntry(
        name="hitl2",
        field="hitl2",
        stub=_stub_hitl2,
        real=lambda d: partial(
            hitl2_confirm, gate=d.hitl2_gate or ConservativeHitl2Gate()
        ),
        deps=("consistency",),
        build=_hitl2_node,
    ),
    AgentEntry(
        name="writeback",
        field="writeback",
        stub=_stub_writeback,
        real=None,
        deps=("hitl2",),
        build=_writeback_node,
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
    verify_llm: VerifyLlmClient | None = None,
    hypothesis_llm: HypothesisLlmClient | None = None,
    retrieval: RetrievalLayer | None = None,
    hitl2_gate: Hitl2Gate | None = None,
    max_iterations: int = 8,
) -> Agents:
    """返回「真实解析 + 真实 HITL-1 +（可选）真实体检/开药 + 真实合并 + 真实影响传导 +
    真实一致性校验 + 真实 HITL-2 + 真实回写」的智能体组。

    在 :func:`create_stub_agents` 基础上替换桩为真实实现。合并算子（#6）、影响传导（#7）、
    一致性校验（#8）与回写（#10）均为确定性纯函数、无 LLM / 检索依赖，已随
    :func:`create_stub_agents` 真实接入（桩路径与真实装配共用同一实现），此处不再替换——
    故「无采纳改动 → 终稿逐字节等于原文」的 tracer bullet 承诺继续成立（解析产出真实树、
    HITL-1 可编辑结构、HITL-2 默认保守闸门不采纳）。``llm`` 为解析 seam（具体 provider
    适配器属生产装配）；``hitl1_gate`` 为 HITL-1 注入闸门（真实 interrupt+checkpointer
    属后续切片）。当且仅当 ``verify_llm`` 与 ``retrieval`` 同时给出时，体检桩（#4）替换为
    真实 ReAct 实现——体检只写回节点状态、不改 ``content``，故字节级承诺依然成立。当且仅当
    ``hypothesis_llm`` 与 ``retrieval`` 同时给出时，开药桩（#5）替换为真实「投机生成 +
    逐条取证」实现——开药只写回 ``candidate_hypotheses``、不改 ``content`` 或 ``status``
    （与体检乐观并行、不读体检结论，ADR-0002），字节级承诺依然成立。合并（#6）读两线路
    合流后的 ``status`` × ``candidate_hypotheses`` 矩阵裁决，只贴 ``merge_decision`` /
    ``issue_tags`` / 裁剪假设、不置 ``adopted``、不改 ``content``；影响传导（#7）读合并后的
    树按剩余支撑率判 ``invalid`` / 贴 ``weakening``、复用既有成立假设激活，亦不改
    ``content`` / 不新建假设；一致性校验（#8）在影响传导之后、HITL-2 之前单次扫描那棵标注
    完成的树，只追加 ``issue_tags`` 批注（去重）、不打回、不改 ``content`` / ``status`` /
    ``merge_decision``——字节级承诺依然成立。HITL-2（#9）为不可跳过的硬闸门：
    ``hitl2_gate`` 缺省时用 :class:`ConservativeHitl2Gate`（无待决→一键通过、有待决→全驳回、
    绝不自动采纳，ADR-0010）；HITL-2 采纳即置 ``adopted`` + 持久化
    ``adopted_hypothesis_id``（ADR-0011）。回写（#10）据 ``adopted_hypothesis_id`` 按关系
    分流缝合（对立→替换、递进→改写、扩展→段尾追加带审计标识），成功翻 ``corrected``、失败
    停留 ``adopted`` 并贴 ``writeback_error``、重跑幂等不重复注入——故注入会采纳的闸门时终稿
    不再逐字节等于原文（变更段已缝合），未变更段仍逐字节还原。真实人判 ``interrupt`` +
    ``Command(resume)`` + checkpointer 属后续切片；回写幂等续跑入口见
    :meth:`runtime.orchestrator.Orchestrator.resume_writeback`（#11）。

    manifest 驱动：遍历 :data:`MANIFEST`，对有 ``real`` 工厂的条目调 ``real(deps)``，返回非
    ``None`` 者替换对应 ``field``；纯函数 Agent（``real=None``）与 ``partition`` 不替换。
    """

    deps = RealDeps(
        llm=llm,
        hitl1_gate=hitl1_gate,
        verify_llm=verify_llm,
        hypothesis_llm=hypothesis_llm,
        retrieval=retrieval,
        hitl2_gate=hitl2_gate,
        max_iterations=max_iterations,
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
