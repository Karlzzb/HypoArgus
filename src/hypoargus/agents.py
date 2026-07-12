"""智能体契约与桩（tracers bullet #1）。

全局调度中枢只与这些契约打交道，子环节间数据经中枢路由、无跨模块直接调用
（PRD §13）。后续每个切片只往本框架注册一个真实子智能体、替换对应桩，框架本身
不再重写。

桩的行为：不生产任何真实变更、不读写原文全文、绝不打回或重调度——
确保「无采纳改动 → 终稿逐字节等于原文」这一 tracer bullet 承诺。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from hypoargus.consistency import consistency as consistency_fn
from hypoargus.domain import ArgumentationNode, NodeType
from hypoargus.hitl1 import Hitl1Gate
from hypoargus.hitl1 import confirm as hitl1_confirm
from hypoargus.hitl2 import ConservativeHitl2Gate, Hitl2Gate
from hypoargus.hitl2 import confirm as hitl2_confirm
from hypoargus.hypothesis import HypothesisLlmClient
from hypoargus.hypothesis import hypothesize as hypothesize_fn
from hypoargus.impact import impact as impact_fn
from hypoargus.merge import merge as merge_fn
from hypoargus.parser import LlmClient
from hypoargus.parser import parse as parse_fn
from hypoargus.raw_store import RawParagraphStore
from hypoargus.retrieval import RetrievalLayer
from hypoargus.verification import VerifyLlmClient
from hypoargus.verification import verify as verify_fn
from hypoargus.writeback import writeback

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
    "Agents",
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
    """修订回写（#10 接入真实分流·幂等）。返回终稿 bytes。"""

    def __call__(
        self, tree: list[ArgumentationNode], store: RawParagraphStore
    ) -> bytes: ...


@dataclass
class Agents:
    """一组可注入的子智能体契约。

    中枢按固定顺序调用这些契约；每个契约在本切片都可用桩占位，后续切片逐个替换。
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
    """合并算子（委托纯函数 :func:`hypoargus.merge.merge`）。

    双轨合并是确定性纯函数、无 LLM / 检索依赖（ADR-0006 12 格矩阵），故无桩——
    tracer bullet 与真实装配共用同一实现。桩路径下两线路均返回 ``{}``，输入树全为
    ``unverified`` 且 ``candidate_hypotheses`` 空，合并逐节点判 ``KEEP``、不裁剪、
    不置 ``adopted``，故「无采纳改动 → 终稿逐字节等于原文」承诺继续成立。
    """

    return merge_fn(tree)


def _impact(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """影响传导算子（委托纯函数 :func:`hypoargus.impact.impact`）。

    影响传导是确定性纯函数、无 LLM / 检索依赖（ADR-0003 串行·不产文本、ADR-0013
    剩余支撑率公式），故无桩——tracer bullet 与真实装配共用同一实现。桩路径下两线路均返回
    ``{}``、合并逐节点判 ``KEEP``、影子上层论点无子节点参与传导，故影响传导不动任何节点，
    「无采纳改动 → 终稿逐字节等于原文」承诺继续成立。
    """

    return impact_fn(tree)


def _consistency(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """一致性校验算子（委托纯函数 :func:`hypoargus.consistency.consistency`）。

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
    真实人判 ``interrupt`` + checkpointer 属 #11。
    """

    return hitl2_confirm(tree, store, ConservativeHitl2Gate())


def _stub_writeback(
    tree: list[ArgumentationNode], store: RawParagraphStore
) -> bytes:
    """回写桩（委托纯函数 :func:`hypoargus.writeback.writeback`）。

    #1：无采纳改动 → 全部逐字节拷回。分流（replace/rewrite/supplement）由 #10 接入。
    """

    return writeback(tree, store)


def create_stub_agents() -> Agents:
    """返回全套桩智能体，用于 tracer bullet 端到端回路。"""

    return Agents(
        parse=_stub_parse,
        hitl1=_stub_hitl1,
        verification=_stub_verification,
        hypothesis=_stub_hypothesis,
        merge=_merge,
        impact=_impact,
        consistency=_consistency,
        hitl2=_stub_hitl2,
        writeback=_stub_writeback,
    )


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
    真实一致性校验 + 真实 HITL-2 + 回写桩」的智能体组。

    在 :func:`create_stub_agents` 基础上替换桩为真实实现，回写分流（#10）仍为桩——故
    「无采纳改动 → 终稿逐字节等于原文」的 tracer bullet 承诺继续成立（解析产出真实树、
    HITL-1 可编辑结构、HITL-2 默认保守闸门不采纳）。合并算子（#6）、影响传导（#7）与
    一致性校验（#8）均为确定性纯函数、无 LLM / 检索依赖，已随 :func:`create_stub_agents`
    真实接入，此处不再替换。

    ``llm`` 为解析 seam（具体 provider 适配器属生产装配）；``hitl1_gate`` 为 HITL-1
    注入闸门（真实 interrupt+checkpointer 属 #11）。当且仅当 ``verify_llm`` 与
    ``retrieval`` 同时给出时，体检桩（#4）替换为真实 ReAct 实现——体检只写回节点状态、
    不改 ``content``，故字节级承诺依然成立。当且仅当 ``hypothesis_llm`` 与 ``retrieval``
    同时给出时，开药桩（#5）替换为真实「投机生成 + 逐条取证」实现——开药只写回
    ``candidate_hypotheses``、不改 ``content`` 或 ``status``（与体检乐观并行、不读体检结论，
    ADR-0002），字节级承诺依然成立。合并（#6）读两线路合流后的 ``status`` ×
    ``candidate_hypotheses`` 矩阵裁决，只贴 ``merge_decision`` / ``issue_tags`` / 裁剪假设、
    不置 ``adopted``、不改 ``content``；影响传导（#7）读合并后的树按剩余支撑率判 ``invalid``
    / 贴 ``weakening``、复用既有成立假设激活，亦不改 ``content`` / 不新建假设；一致性校验
    （#8）在影响传导之后、HITL-2 之前单次扫描那棵标注完成的树，只追加 ``issue_tags`` 批注
    （去重）、不打回、不改 ``content`` / ``status`` / ``merge_decision``——字节级承诺依然
    成立。HITL-2（#9）为不可跳过的硬闸门：``hitl2_gate`` 缺省时用 :class:`ConservativeHitl2Gate`
    （无待决→一键通过、有待决→全驳回、绝不自动采纳，ADR-0010）；HITL-2 采纳即置 ``adopted``
    + 持久化 ``adopted_hypothesis_id``（ADR-0011），故注入会采纳的闸门时需配 #10 真实回写。
    其余下游桩待 #10 接入。
    """

    stubs = create_stub_agents()
    agents = replace(
        stubs,
        parse=lambda store: parse_fn(store, llm),
        hitl1=lambda tree: hitl1_confirm(tree, hitl1_gate),
        hitl2=lambda tree, store: hitl2_confirm(tree, store, hitl2_gate or ConservativeHitl2Gate()),
    )
    if verify_llm is not None and retrieval is not None:
        agents = replace(
            agents,
            verification=lambda tree: verify_fn(
                tree, verify_llm, retrieval, max_iterations=max_iterations
            ),
        )
    if hypothesis_llm is not None and retrieval is not None:
        agents = replace(
            agents,
            hypothesis=lambda tree: hypothesize_fn(
                tree, hypothesis_llm, retrieval, max_iterations=max_iterations
            ),
        )
    return agents
