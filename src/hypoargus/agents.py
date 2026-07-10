"""智能体契约与桩（tracers bullet #1）。

全局调度中枢只与这些契约打交道，子环节间数据经中枢路由、无跨模块直接调用
（PRD §13）。后续每个切片只往本框架注册一个真实子智能体、替换对应桩，框架本身
不再重写。

桩的行为：不生产任何真实变更、不读写原文全文、绝不打回或重调度——
确保「无采纳改动 → 终稿逐字节等于原文」这一 tracer bullet 承诺。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hypoargus.domain import ArgumentationNode, NodeType
from hypoargus.raw_store import RawParagraphStore
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
    """HITL-2 修订确认（#9 接入，不可跳过硬闸门）。返回采纳后的树。"""

    def __call__(self, tree: list[ArgumentationNode]) -> list[ArgumentationNode]: ...


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


def _stub_merge(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """合并桩：恒等（无两线路结果可合并）。"""

    return tree


def _stub_impact(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """影响传导桩：恒等（串行·不产文本）。"""

    return tree


def _stub_consistency(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """一致性校验桩：不贴批注（批注门禁·不打回）。"""

    return tree


def _stub_hitl2(tree: list[ArgumentationNode]) -> list[ArgumentationNode]:
    """HITL-2 桩：无待决内容时一键通过（无人采纳、原文全可信）。

    真实硬闸门（#9）将在此呈现 doubtful/error 段落与候选假设、逐条采纳；
    本桩只证明「无待决 → 无修订 → 逐字节拷回」的回路成立。
    """

    return tree


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
        merge=_stub_merge,
        impact=_stub_impact,
        consistency=_stub_consistency,
        hitl2=_stub_hitl2,
        writeback=_stub_writeback,
    )
