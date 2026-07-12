"""线路 2 · 开药 Agent：投机生成 + 逐条取证（PRD §5、issue #5、ADR-0002/0007/0008/0011）。

对节点投机生成可证伪修订假设、再对每条假设经公共检索层取证，产出
``supported / doubtful / refuted`` 三态。与体检线路（#4）**乐观并行**：投机生成不读取
体检结论（ADR-0002 解法 A），两线路结果由双轨合并算子（#6）在并行层之后裁剪。

两阶段（每节点）：

1. **投机生成**——LLM 一步产出 0..N 条 ``HypothesisProposal``（``text`` + 单一
   ``relation`` + ``confidence``）。一条假设只承载一种关系（ADR-0007），混合意图须由
   LLM 拆成多条（结构上：每条 proposal 恰一个 ``relation`` 字段）。「无假设」= 空列表
   （ADR-0008，非第四种枚举）。生成不依赖体检结论，亦不读检索。
2. **逐条取证**——对每条假设跑「推理—行动」循环（镜像体检 #4 的 ReAct），LLM 每步
   做一个极窄结构化决策：继续检索（新/调检索词 + 通道）或就地结论
   （``supported / doubtful / refuted`` + 简短理由）。查到明确比对素材或触发迭代硬上限
   即退出。

控制流落代码而非 prompt 散文（``docs/langgraph-dev-guide.md`` §0 铁律）：

- 迭代硬上限（``max_iterations``）为参数、非 prompt 请求——绝不卡死流程。
- 取证任何异常（LLM 抛、检索抛/合规违规、结构非法、迭代耗尽）→ 该假设落 ``doubtful``
  （取证失败 ≠ 被推翻；不因检索波动丢弃假设，弱呈现供人判，ADR-0008）。
- 生成任何异常 → 该节点无假设（空列表，保守、不抛出、不卡死）。
- ``content`` 永不被改写（节点文本只来自只读表，``parser.py`` / ``verification.py`` 先例）。

覆盖范围（ADR-0002 成本闸）：``evidence`` + ``sub_claim``。跳过 ``main_claim``（由影响
传导 #7 处理、不直接替换）、``qualification``、影子节点（``background / evaluation``，
只读不参与校验）——被跳过节点不出现在 partial 更新中（``candidate_hypotheses`` 维持空）。

状态语义（ADR-0008 对称）：假设侧 ``supported / doubtful / refuted`` ↔ 原文侧
``credible / doubtful / error``。``confidence`` 仅用于同节点多条 ``supported`` 假设的
排序展示，**绝不**参与取证判决或合并矩阵裁决（ADR-0008 铁律）。

``HypothesisLlmClient`` 为注入 seam：真实适配器生成用
``with_structured_output(list[HypothesisProposal])``、取证用
``with_structured_output(HypothesisVerifyStep)`` 保证结构合法（dev-guide §6.3）；本切片
提供 ``FakeHypothesisLlmClient`` 供离线单测——provider-free、确定、可断言。

注：体检（#4）写回节点 ``status``、开药（#5）写回 ``candidate_hypotheses``，二者在
``merge_tree`` reducer 处合流到同一节点；双轨合并算子（#6）据此读
``原文.status × 假设.status`` 矩阵裁决。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

from hypoargus.domain import (
    ArgumentationNode,
    Hypothesis,
    HypothesisRelation,
    HypothesisStatus,
    NodeType,
)
from hypoargus.retrieval import (
    KnowledgeBaseRetrievalRequest,
    NetworkRetrievalRequest,
    RetrievalKind,
    RetrievalLayer,
    RetrievalRequest,
    Source,
)

__all__ = [
    "HypothesisRelation",
    "HypothesisStatus",
    "Hypothesis",
    "HypothesisVerdict",
    "HypothesisProposal",
    "HypothesisSearchStep",
    "HypothesisConcludeStep",
    "HypothesisVerifyStep",
    "HypothesisLlmClient",
    "FakeHypothesisLlmClient",
    "hypothesize",
]


# --------------------------------------------------------------------------- #
# 结构化 ReAct 步（取证 · discriminated union · dev-guide §6.3）
# --------------------------------------------------------------------------- #


class HypothesisVerdict(StrEnum):
    """假设取证终判（假设侧三态，ADR-0008/0011）。"""

    SUPPORTED = "supported"
    DOUBTFUL = "doubtful"
    REFUTED = "refuted"


class HypothesisProposal(BaseModel):
    """投机生成 seam 的产出：一条待取证的假设（尚无 status，status 由取证赋予）。

    ``relation`` 单一（ADR-0007）；``confidence`` 0-1，仅排序、不裁决（ADR-0008）。
    """

    text: str
    relation: HypothesisRelation
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class HypothesisSearchStep(BaseModel):
    """取证继续检索：调整检索词、选通道、附通道特有参数。

    通道为 ``network`` 或 ``knowledge_base``（镜像体检 #4 的正向检索范围）；结构化数据检索
    （``structured``）不在取证通道内，``_build_request`` 拒绝 → 假设落 ``doubtful``。
    """

    action: Literal["search"] = "search"
    query: str
    channel: RetrievalKind
    domain: str | None = None  # 网络检索白名单域名
    user_id: str | None = None  # 知识库检索授权用户
    type_filter: str | None = None
    time_filter: str | None = None


class HypothesisConcludeStep(BaseModel):
    """就地结论：写回取证终判 + 简短理由（可复算、可解释）。"""

    action: Literal["conclude"] = "conclude"
    verdict: HypothesisVerdict
    reasoning: str = ""


HypothesisVerifyStep = Annotated[
    HypothesisSearchStep | HypothesisConcludeStep, Field(discriminator="action")
]
"""单步取证决策：检索或结论（按 ``action`` 判别）。"""


# --------------------------------------------------------------------------- #
# LLM seam + 离线桩（provider-free，供单测）
# --------------------------------------------------------------------------- #


class HypothesisLlmClient(Protocol):
    """开药 LLM seam。

    - :meth:`propose`：节点 → 0..N 条假设提案（投机生成，不读体检结论/检索）。
    - :meth:`next_verify_step`：假设文本 + 已累积 observations → 下一步取证决策。

    真实适配器生成用 ``with_structured_output(list[HypothesisProposal])``、取证用
    ``with_structured_output(HypothesisVerifyStep)`` 保证结构合法（dev-guide §6.3）。
    本 seam 不绑任何 provider。
    """

    def propose(self, node: ArgumentationNode) -> list[HypothesisProposal]: ...

    def next_verify_step(
        self, hypothesis_text: str, observations: list[Source]
    ) -> HypothesisSearchStep | HypothesisConcludeStep: ...


class FakeHypothesisLlmClient:
    """离线开药 LLM 桩。provider-free、确定（供单测）。

    生成（``propose``）：
    - ``propose_factory``：``callable(node) -> list[HypothesisProposal]``，可据节点决策。
    - 二者皆无 → 返回 ``[]``（无假设，等价于不生成的最简桩）。

    取证（``next_verify_step``）：
    - ``verify_factory``：``callable(hypothesis_text, observations) -> HypothesisVerifyStep``，
      可据假设文本与累积 observations 动态决策（多假设断言用此）。
    - ``verify_script``：``list[HypothesisVerifyStep]``，按序、跨所有取证调用全局消费
      （用尽即抛，模拟 LLM 未给结论 → 由迭代硬上限兜底为 ``doubtful``）。
    - 二者皆无 → 立即结论 ``supported``（无检索，等价于取证通过的最简桩）。
    """

    def __init__(
        self,
        *,
        propose_factory: Callable[[ArgumentationNode], list[HypothesisProposal]]
        | None = None,
        verify_factory: Callable[[str, list[Source]], HypothesisSearchStep | HypothesisConcludeStep]
        | None = None,
        verify_script: list[HypothesisSearchStep | HypothesisConcludeStep] | None = None,
    ) -> None:
        self._propose_factory = propose_factory
        self._verify_factory = verify_factory
        self._verify_script = list(verify_script) if verify_script is not None else None
        self._verify_cursor = 0

    def propose(self, node: ArgumentationNode) -> list[HypothesisProposal]:
        if self._propose_factory is not None:
            return self._propose_factory(node)
        return []

    def next_verify_step(
        self, hypothesis_text: str, observations: list[Source]
    ) -> HypothesisSearchStep | HypothesisConcludeStep:
        if self._verify_factory is not None:
            return self._verify_factory(hypothesis_text, observations)
        if self._verify_script is not None:
            if self._verify_cursor >= len(self._verify_script):
                raise RuntimeError("verify script 用尽未给结论（应由迭代硬上限兜底）")
            step = self._verify_script[self._verify_cursor]
            self._verify_cursor += 1
            return step
        return HypothesisConcludeStep(verdict=HypothesisVerdict.SUPPORTED)


# --------------------------------------------------------------------------- #
# 主逻辑：纯函数 seam，可独立单测
# --------------------------------------------------------------------------- #


_HYPOTHESIS_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.EVIDENCE, NodeType.SUB_CLAIM}
)


def _should_hypothesize(node: ArgumentationNode) -> bool:
    """开药覆盖 evidence + sub_claim；跳过 main_claim / qualification / 影子节点。"""

    return node.node_type in _HYPOTHESIS_TYPES


_VERDICT_TO_STATUS: dict[HypothesisVerdict, HypothesisStatus] = {
    HypothesisVerdict.SUPPORTED: HypothesisStatus.SUPPORTED,
    HypothesisVerdict.DOUBTFUL: HypothesisStatus.DOUBTFUL,
    HypothesisVerdict.REFUTED: HypothesisStatus.REFUTED,
}


def _hypothesis_id(
    node_id: str, relation: HypothesisRelation, text: str, idx: int
) -> str:
    """确定性 hypothesis_id：节点 id + 关系 + 文本 + 序号派生（非计数器，可重算）。

    供 HITL-2（#9）采纳与回写（#10）幂等链稳定引用同一假设。
    """

    digest = hashlib.blake2b(
        f"{node_id}|{relation.value}|{text}|{idx}".encode(), digest_size=6
    ).hexdigest()
    return f"h-{digest}"


def _build_request(step: HypothesisSearchStep) -> RetrievalRequest:
    """把取证 ``HypothesisSearchStep`` 翻译为检索层 ``RetrievalRequest``；通道/参数不全 → 抛错。

    合规（白名单域名 / 授权用户）由检索层 ``validate_request`` 在接口层强制；此处只构造
    请求形状。结构化数据通道（``structured``）不在取证范围 → 抛错（→ 假设 ``doubtful``）。
    """

    if not step.query.strip():
        raise ValueError("检索词不可为空")
    if step.channel is RetrievalKind.NETWORK:
        if not step.domain:
            raise ValueError("网络检索须指定 domain")
        return NetworkRetrievalRequest(query=step.query, domain=step.domain)
    if step.channel is RetrievalKind.KNOWLEDGE_BASE:
        if not step.user_id:
            raise ValueError("知识库检索须指定 user_id")
        return KnowledgeBaseRetrievalRequest(
            query=step.query,
            user_id=step.user_id,
            type_filter=step.type_filter,
            time_filter=step.time_filter,
        )
    raise ValueError(
        f"取证不支持通道 {step.channel!r}（仅 network / knowledge_base）"
    )


def _verify_hypothesis(
    hypothesis_text: str,
    llm: HypothesisLlmClient,
    retrieval: RetrievalLayer,
    max_iterations: int,
) -> HypothesisStatus:
    """单条假设的取证 ReAct 循环：bounded、绝不卡死。

    任何异常 / 迭代硬上限 / 结构非法 → ``doubtful``（取证失败 ≠ 被推翻，ADR-0008）。
    """

    observations: list[Source] = []
    for _ in range(max_iterations):
        try:
            step = llm.next_verify_step(hypothesis_text, observations)
        except Exception:
            return HypothesisStatus.DOUBTFUL
        try:
            if isinstance(step, HypothesisConcludeStep):
                return _VERDICT_TO_STATUS[step.verdict]
            if isinstance(step, HypothesisSearchStep):
                request = _build_request(step)
                response = retrieval.retrieve(request)
                observations.extend(response.materials)
                continue
            return HypothesisStatus.DOUBTFUL  # 结构非法（非 union 成员）
        except Exception:
            return HypothesisStatus.DOUBTFUL
    return HypothesisStatus.DOUBTFUL  # 迭代硬上限（超时）


def hypothesize(
    tree: list[ArgumentationNode],
    llm: HypothesisLlmClient,
    retrieval: RetrievalLayer,
    *,
    max_iterations: int = 8,
) -> dict[str, ArgumentationNode]:
    """对覆盖范围内的节点跑「投机生成 → 逐条取证」，返回 partial 更新（by ``node_id``）。

    - 覆盖 ``evidence / sub_claim``；``main_claim / qualification / 影子`` 节点不在 dict 中
      （保持空 ``candidate_hypotheses``，下游合并据此识别未开药节点）。
    - 每节点写回 ``candidate_hypotheses``（0..N 条，各带取证终态）；``content`` 与
      ``status`` 不动（``status`` 由体检 #4 写回，``candidate_hypotheses`` 由本切片写回，
      二者在 reducer 处合流，供 #6 矩阵裁决）。
    - 不修改输入树：返回的节点为 ``model_copy`` 新实例（输入节点不变）。
    """

    updates: dict[str, ArgumentationNode] = {}
    for node in tree:
        if not _should_hypothesize(node):
            continue
        try:
            proposals = llm.propose(node)
        except Exception:
            proposals = []
        hypotheses: list[Hypothesis] = []
        for idx, proposal in enumerate(proposals):
            status = _verify_hypothesis(
                proposal.text, llm, retrieval, max_iterations
            )
            hypotheses.append(
                Hypothesis(
                    hypothesis_id=_hypothesis_id(
                        node.node_id, proposal.relation, proposal.text, idx
                    ),
                    text=proposal.text,
                    relation=proposal.relation,
                    status=status,
                    confidence=proposal.confidence,
                )
            )
        updates[node.node_id] = node.model_copy(
            update={"candidate_hypotheses": hypotheses}
        )
    return updates
