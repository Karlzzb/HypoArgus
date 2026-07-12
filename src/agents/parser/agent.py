"""解析 Agent 纯函数（PRD §4、issue #2）。

在只读原文段落底座上**唯一语义解析入口**：识别论证节点、构建论证树、回填指针——
无权新建、改写或重排段落。LLM 只做「识别」：按段返回节点提议（段归属、类型、父索引、
权重）。解析器强制 LLM 不可信的所有结构硬约束（控制流落代码而非 prompt 散文，
见 ``docs/langgraph-dev-guide.md``）：

- ``paragraph_id`` 必须存在于只读表——不凭空造段、不跨段（ADR-0001）。
- ``content`` 逐字节从只读表按 ``paragraph_id`` 拷回——LLM 无权改写原文（by construction，
  节点文本永不来自 LLM 输出）。
- ``argument_weight`` ∈ [0,100]（越界 clamp；影子恒 0）。
- ``parent_index`` 解析为 ``node_id``：拒绝越界/自指；环则断为根（绝不产出成环树）。
- 覆盖软启发式：无核心节点提议的段落归为只读 ``background`` 影子节点，
  **绝不硬造论点**（PRD §4「解析器不得为无论点的段落硬造论点」）。
- 节点初始 ``unverified``；父子指针回填并通过 :func:`validate_tree` 自检。

> 原文段落按段（``ParagraphView``）喂给 LLM，**不整篇 dump**：每个 view 只含一段
> 原文加 ``paragraph_id``。解析器是唯一读取段落文本的环节；此后只节点（各携自身一段）
> 在智能体间流转（ADR-0005）。
"""

from __future__ import annotations

from agents.parser.contract import LlmClient, ParagraphView, ParseResult
from domain import ArgumentationNode, NodeStatus, NodeType
from raw_store import RawParagraphStore
from tree_invariants import rebuild_children, validate_tree

__all__ = ["parse"]


def _decode(paragraph_bytes: bytes) -> str:
    """逐字节解码段落原文（与 stub 一致：surrogateescape 兜底非 UTF-8 字节）。"""

    return paragraph_bytes.decode("utf-8", errors="surrogateescape")


def _clamp_weight(value: int, node_type: NodeType) -> int:
    """越界 clamp 到 [0,100]；影子节点恒 0（不参与传导，ADR-0013 rubric）。

    真实 LLM 偶尔返回 101 或负数——pydantic 边界会整体崩溃，故提议层不加约束、
    在此宽容 clamp。影子节点（background/evaluation）权重一律归零，无论 LLM 给何值。
    """

    if node_type.is_shadow:
        return 0
    return max(0, min(100, value))


def _core_node_id(index: int) -> str:
    """LLM 提议列表中第 ``index`` 个核心节点的稳定 id（``parent_index`` 据此解析）。"""

    return f"n{index:04d}"


def _shadow_node_id(paragraph_id: str) -> str:
    """覆盖软启发式为无提议段落生成的影子节点 id。"""

    return f"bg-{paragraph_id}"


def _break_cycles(nodes: list[ArgumentationNode]) -> None:
    """就地断开父子环：若某节点的祖先链回到自身，把它的 parent 置 None。

    保证解析器绝不产出成环树——这是 LLM 不可信、必须代码兜底的硬约束。
    """

    by_id = {n.node_id: n for n in nodes}
    n = len(nodes)
    for node in nodes:
        cur = node.parent_id
        steps = 0
        while cur is not None and cur in by_id and steps <= n:
            if cur == node.node_id:
                node.parent_id = None  # 断环
                break
            cur = by_id[cur].parent_id
            steps += 1


def parse(store: RawParagraphStore, llm: LlmClient) -> list[ArgumentationNode]:
    """在只读底座上构建初始论证树。

    流程：
    1. 把**实质段落**（去空白）按段喂给 LLM → 结构化节点提议。
    2. 为每个提议铸造稳定 ``node_id``、从只读表逐字节拷回 ``content``、clamp 权重、
       按 ``parent_index`` 解析 ``parent_id``（越界/自指 → 根）。
    3. 断环、回填 ``children_ids``。
    4. 覆盖软启发式：无提议段落一律生成只读 ``background`` 影子节点（绝不硬造论点）。
    5. :func:`validate_tree` 自检结构不变式。

    节点初始 ``unverified``；空白段落不喂 LLM、自动归影子。
    """

    paragraph_ids = list(store.paragraph_ids())
    substantive = [pid for pid in paragraph_ids if store.get(pid).strip()]

    views = [
        ParagraphView(paragraph_id=pid, text=_decode(store.get(pid)))
        for pid in substantive
    ]
    result: ParseResult = llm.parse(views)

    # 2. 铸造核心节点。
    nodes: list[ArgumentationNode] = []
    proposal_ids: list[str] = []
    covered: set[str] = set()
    for i, prop in enumerate(result.nodes):
        # paragraph_id 必须存在于只读表——不凭空造段、不跨段。
        if prop.paragraph_id not in store:
            continue
        # 子节点自指 / 越界索引 → 根。
        parent_id: str | None = None
        if prop.parent_index is not None:
            idx = prop.parent_index
            if 0 <= idx < len(result.nodes) and idx != i:
                parent_id = _core_node_id(idx)
        nid = _core_node_id(i)
        proposal_ids.append(nid)
        covered.add(prop.paragraph_id)
        nodes.append(
            ArgumentationNode(
                node_id=nid,
                node_type=prop.node_type,
                parent_id=parent_id,
                paragraph_id=prop.paragraph_id,
                content=_decode(store.get(prop.paragraph_id)),
                argument_weight=_clamp_weight(prop.argument_weight, prop.node_type),
                status=NodeStatus.UNVERIFIED,
            )
        )

    # 3. 断环 + 回填 children。
    _break_cycles(nodes)
    rebuild_children(nodes)

    # 4. 覆盖软启发式：无提议段落 → 只读 background 影子节点（绝不硬造论点）。
    for pid in paragraph_ids:
        if pid in covered:
            continue
        shadow = ArgumentationNode(
            node_id=_shadow_node_id(pid),
            node_type=NodeType.BACKGROUND,
            parent_id=None,
            paragraph_id=pid,
            content=_decode(store.get(pid)),
            argument_weight=0,
            status=NodeStatus.UNVERIFIED,
        )
        nodes.append(shadow)

    # 5. 结构不变式自检（LLM 不可信 → 代码兜底）。
    validate_tree(nodes)
    return nodes
