"""解析 Agent 纯函数（PRD §4、issue #2）。

在只读原文段落底座上**唯一语义解析入口**：识别论证节点、构建论证树、回填指针——
无权新建、改写或重排段落。LLM 只做「识别」：按段返回节点提议（段归属、类型、父索引、
权重）。解析器强制 LLM 不可信的所有结构硬约束（控制流落代码而非 prompt 散文，
见 ``docs/DEVELOPMENT.md`` §11）：

- ``paragraph_id`` 必须存在于只读表——不凭空造段、不跨段（ADR-0001）。
- ``original_content`` 逐字节从只读表按 ``paragraph_id`` 拷回到段落聚合根
  :class:`ParagraphRecord`——LLM 无权改写原文（by construction，节点文本永不来自 LLM 输出）。
- ``argument_weight`` ∈ [0,100]（越界 clamp；影子恒 0）。
- ``parent_index`` 解析为 ``argument_id``：拒绝越界/自指；环则断为根（绝不产出成环树）。
- 覆盖软启发式：无核心节点提议的段落归为只读 ``background`` 影子节点，
  **绝不硬造论点**（PRD §4「解析器不得为无论点的段落硬造论点」）。
- 节点初始 ``unverified``；父子指针回填并通过 :func:`validate_tree` 自检。

> 原文段落按段（``ParagraphView``）喂给 LLM，**不整篇 dump**：每个 view 只含一段
> 原文加 ``paragraph_id``。解析器是唯一读取段落文本的环节；此后段落原文每段一份存于
> ``ParagraphRecord.original_content``，论证节点（纯推理结构）在智能体间流转（ADR-0005）。
"""

from __future__ import annotations

from agents.parser.contract import (
    LlmClient,
    ParagraphView,
    ParsedNodeProposal,
    ParseOutput,
    ParseResult,
    is_substantive,
)
from domain import (
    DEFAULT_QUERY_TIME_RANGE,
    Argument,
    ArgumentStatus,
    ArgumentType,
    ParagraphRecord,
)
from original_paragraphs import OriginalParagraphs
from tree_invariants import rebuild_children, validate_tree

__all__ = ["parse"]


def _decode(paragraph_bytes: bytes) -> str:
    """逐字节解码段落原文（与 stub 一致：surrogateescape 兜底非 UTF-8 字节）。"""

    return paragraph_bytes.decode("utf-8", errors="surrogateescape")


def _clamp_weight(value: int, argument_type: ArgumentType) -> int:
    """越界 clamp 到 [0,100]；影子节点恒 0（不参与传导，ADR-0013 rubric）。

    真实 LLM 偶尔返回 101 或负数——pydantic 边界会整体崩溃，故提议层不加约束、
    在此宽容 clamp。影子节点（background/evaluation）权重一律归零，无论 LLM 给何值。
    """

    if argument_type.is_shadow:
        return 0
    return max(0, min(100, value))


def _core_argument_id(index: int) -> str:
    """LLM 提议列表中第 ``index`` 个核心节点的稳定 id（``parent_index`` 据此解析）。"""

    return f"n{index:04d}"


def _shadow_argument_id(paragraph_id: str) -> str:
    """覆盖软启发式为无提议段落生成的影子节点 id。"""

    return f"bg-{paragraph_id}"


def _break_cycles(arguments: list[Argument]) -> None:
    """就地断开父子环：若某节点的祖先链回到自身，把它的 parent 置 None。

    保证解析器绝不产出成环树——这是 LLM 不可信、必须代码兜底的硬约束。
    """

    by_id = {n.argument_id: n for n in arguments}
    n = len(arguments)
    for argument in arguments:
        cur = argument.parent_id
        steps = 0
        while cur is not None and cur in by_id and steps <= n:
            if cur == argument.argument_id:
                argument.parent_id = None  # 断环
                break
            cur = by_id[cur].parent_id
            steps += 1


def parse(original_paragraphs: OriginalParagraphs, llm: LlmClient) -> ParseOutput:
    """在只读底座上构建初始论证树，并顺产时间范围（桩）与段落摘要。

    流程：
    1. 把**实质段落**（去空白）按段喂给 LLM → 结构化节点提议（``ParseResult``，含
       ``paragraph_summaries``）。
    2. 为每个提议铸造稳定 ``argument_id``、clamp 权重、
       按 ``parent_index`` 解析 ``parent_id``（越界/自指 → 根）。
    3. 断环、回填 ``children_ids``。
    4. 覆盖软启发式：无提议段落一律生成只读 ``background`` 影子节点（绝不硬造论点）。
    5. :func:`validate_tree` 自检结构不变式。

    节点初始 ``unverified``；空白段落不喂 LLM、自动归影子。

    ``query_time_range`` 当前为桩（``DEFAULT_QUERY_TIME_RANGE``，agent 注入、不真实调 LLM 识别，
    真实时间识别属后续切片，PRD Out of Scope）；段落摘要顺产自 parse seam（``ParseResult``
    的 ``paragraph_summaries``，真实 adapter 两阶段：树一次 + 摘要按 8 分块，见
    ``infra/llm_adapters.py``），折叠进 ``paragraph_list.summary``（摘要的单一定义点）。
    返回 :class:`ParseOutput` 供 ``parse+partition`` build 闭包写回三 channel
    （``argument_tree`` / ``query_time_range`` / ``paragraph_list``）。
    """

    paragraph_ids = list(original_paragraphs.paragraph_ids())
    substantive = [
        pid for pid in paragraph_ids if is_substantive(original_paragraphs.get(pid))
    ]

    views = [
        ParagraphView(paragraph_id=pid, text=_decode(original_paragraphs.get(pid)))
        for pid in substantive
    ]
    result: ParseResult = llm.parse(views)

    # 2. 铸造核心节点。
    arguments: list[Argument] = []
    covered: set[str] = set()
    # paragraph_id 必须存在于只读表——先过滤凭空造段提议，保留幸存提议及其原始索引。
    # n-id 按幸存顺序连续赋值（n0000、n0001、…）；原始索引→幸存序号映射，使
    # parent_index 指向被丢弃提议时落空为根——而非悬空 parent_id 触发 validate_tree
    # （真实 LLM 偶尔产凭空 paragraph_id 提议，旧实现按枚举索引赋 n-id 留空位，
    # 后续提议 parent_index 指向空位即解析出不存在的 parent_id）。
    surviving: list[tuple[int, ParsedNodeProposal]] = [
        (i, prop)
        for i, prop in enumerate(result.proposals)
        if prop.paragraph_id in original_paragraphs
    ]
    orig_to_surviving: dict[int, int] = {
        orig_i: surv_i for surv_i, (orig_i, _) in enumerate(surviving)
    }
    # 段落→节点 id 正向索引（T-04：Argument 无 paragraph_id，故在此随建树同步累积，
    # 不再事后扫树按 argument.paragraph_id 反向 join）。
    nodes_by_paragraph: dict[str, list[str]] = {pid: [] for pid in paragraph_ids}
    for surv_i, (orig_i, prop) in enumerate(surviving):
        # parent_index 指向 LLM 输出列表中的位置：自指 / 越界 / 指向被丢弃提议 → 根。
        parent_id: str | None = None
        if prop.parent_index is not None:
            idx = prop.parent_index
            if (
                0 <= idx < len(result.proposals)
                and idx != orig_i
                and idx in orig_to_surviving
            ):
                parent_id = _core_argument_id(orig_to_surviving[idx])
        nid = _core_argument_id(surv_i)
        covered.add(prop.paragraph_id)
        nodes_by_paragraph[prop.paragraph_id].append(nid)
        arguments.append(
            Argument(
                argument_id=nid,
                argument_type=prop.argument_type,
                parent_id=parent_id,
                argument_weight=_clamp_weight(prop.argument_weight, prop.argument_type),
                status=ArgumentStatus.UNVERIFIED,
            )
        )

    # 3. 断环 + 回填 children。
    _break_cycles(arguments)
    rebuild_children(arguments)

    # 4. 覆盖软启发式：无提议段落 → 只读 background 影子节点（绝不硬造论点）。
    for pid in paragraph_ids:
        if pid in covered:
            continue
        shadow_id = _shadow_argument_id(pid)
        nodes_by_paragraph[pid].append(shadow_id)
        arguments.append(
            Argument(
                argument_id=shadow_id,
                argument_type=ArgumentType.BACKGROUND,
                parent_id=None,
                argument_weight=0,
                status=ArgumentStatus.UNVERIFIED,
            )
        )

    # 5. 结构不变式自检（LLM 不可信 → 代码兜底）。
    validate_tree(arguments)
    # LLM 面向 list[ParagraphSummary] → 下游面向 dict（ParseOutput 契约不变）。
    summaries = {ps.paragraph_id: ps.summary for ps in result.paragraph_summaries}
    # 6. 段落聚合根：按规范段序每段一条，正向拥有该段全部节点 id（核心 + background 影子），
    #    段落原文每段唯一一份（取自只读表解码）。T-04：Argument 不再存 paragraph_id/content，
    #    段落侧为原文与摘要的单一定义点。
    paragraph_list = [
        ParagraphRecord(
            paragraph_id=pid,
            summary=summaries.get(pid, ""),
            original_content=_decode(original_paragraphs.get(pid)),
            argument_tree_ids=nodes_by_paragraph[pid],
        )
        for pid in paragraph_ids
    ]
    return ParseOutput(
        argument_tree=arguments,
        query_time_range=DEFAULT_QUERY_TIME_RANGE,
        paragraph_list=paragraph_list,
    )
