"""图结构内省纯函数（PRD §5.4 / §10.1 / §7.3 · T-02）。

把 :data:`agents.assembly.MANIFEST` 的单一源拓扑 + 展示元数据 + 可见性旋钮摊成
:class:`GraphView`（节点 / 边），供后续 ``GET /api/agent/graph``（T-04）与 WS
``graph_static``（T-06）共享，避免漂移。本切片**纯数据 + 纯函数**，不引入 web。

拓扑（START / END / 受控打回边）全部从 ``MANIFEST`` 推导——与 orchestrator 单一源
对齐，不另写拓扑：

- 起始边：``deps == ()`` 的节点接 ``__start__``（orchestrator 的 ``START``）。
- 终止边：不被任何节点依赖的节点接 ``__end__``（orchestrator 的 ``END``）。
- 回放边：``route is not None`` 且 ``max_replays > 0`` 的节点有一条条件回放边回到其上游
  dep（ADR-0018：``hitl1 → parse+partition``，``cond="replay"``、``max=max_replays``）。

可见性旋钮（``config/visibility.yaml`` 经 :func:`load_visibility` 载入）只影响**展示**：
``visible=False`` 节点不出现在 ``nodes``、其前后可见节点补直连边（隐藏中间节点 H 时，每条
入边 P→H × 出边 H→S 补 P→S）；回放边若因此收缩成自环（P == S）则丢弃——流水线执行照跑。
HITL 节点（``interrupt=True``）强制 ``visible=True``：配置 override 隐藏 interrupt 节点会被
忽略并记入 ``GraphView.warnings``（HITL 不可对前端隐身）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agents.assembly import AgentEntry

__all__ = [
    "GraphNode",
    "GraphEdge",
    "GraphView",
    "VisibilityConfig",
    "build_graph_view",
    "load_visibility",
]


# langgraph 约定的虚拟起止节点名（START / END）。作为 system 节点出现在 GraphView，
# 使起止边端点良构、不可隐藏。
_START_NODE = "__start__"
_END_NODE = "__end__"


@dataclass(frozen=True)
class VisibilityConfig:
    """步骤可见性 override（PRD §7.3）。``hidden`` 为不展示的节点名集合（缺省空=全可见）。

    经 :func:`load_visibility` 从 ``config/visibility.yaml`` 载入；部署时改、不重启代码、
    不做运行时热切。interrupt 节点即便列入 ``hidden`` 也会被 :func:`build_graph_view`
    强制可见并告警。
    """

    hidden: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GraphNode:
    """图节点（PRD §5.4）：``id`` 为不透明节点名（``parse+partition`` 含 ``+``、勿拆分）。

    :attr:`label` 缺省从 ``id`` 推导；:attr:`visible`` 对 interrupt 节点恒 ``True``。
    """

    id: str
    label: str
    type: str
    color: str | None
    visible: bool
    interrupt: bool


@dataclass(frozen=True)
class GraphEdge:
    """图边（PRD §5.4）：``cond`` 非空表条件边（``"replay"``）、``max`` 为回放预算。"""

    source: str
    target: str
    cond: str | None = None
    max: int | None = None


@dataclass(frozen=True)
class GraphView:
    """``build_graph_view`` 的纯函数输出：节点 / 边 / 可见性告警。"""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    warnings: tuple[str, ...]


def load_visibility(path: str | Path) -> VisibilityConfig:
    """读 ``config/visibility.yaml`` 为 :class:`VisibilityConfig`（缺省 / 缺文件 → 全可见）。

    文件缺失视为无 override（返回空 config）；``hidden`` 缺省 / 空亦同。``hidden`` 非 list
    时抛 :class:`ValueError`——配置错误应硬暴露而非静默吞。
    """

    p = Path(path)
    if not p.exists():
        return VisibilityConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"visibility.yaml: 顶层必须是映射，收到 {type(data).__name__}"
        )
    hidden_raw = data.get("hidden", [])
    if hidden_raw is None:
        hidden_raw = []
    if not isinstance(hidden_raw, list):
        raise ValueError(
            f"visibility.yaml: 'hidden' 必须是列表，收到 {type(hidden_raw).__name__}"
        )
    return VisibilityConfig(hidden=frozenset(str(x) for x in hidden_raw))


def build_graph_view(
    manifest: tuple[AgentEntry, ...],
    visibility: VisibilityConfig,
) -> GraphView:
    """把 ``manifest`` 单一源拓扑 + 展示元数据 + 可见性摊成 :class:`GraphView`（纯函数）。

    拓扑（含 START / END / 受控回放边）从 ``manifest`` 推导、不另写拓扑；可见性只影响展示
    （隐藏节点不出现在 ``nodes``、前后可见节点补直连边、回放边自环丢弃）。interrupt 节点
    强制可见、override 忽略并告警。详见模块 docstring。
    """

    warnings: list[str] = []

    # 1) 可见性裁决：interrupt 节点强制 visible=True（override 忽略 + 告警）。
    visible_names: set[str] = set()
    for entry in manifest:
        override_hidden = entry.name in visibility.hidden
        if entry.interrupt and (override_hidden or not entry.visible):
            warnings.append(
                f"interrupt 节点 {entry.name!r} 强制 visible=True"
                f"（{'配置 override' if override_hidden else 'manifest visible=False'} 忽略）"
            )
            visible_names.add(entry.name)
        elif entry.visible and not override_hidden:
            visible_names.add(entry.name)
    hidden_names = {e.name for e in manifest} - visible_names

    # 2) 原始边集（含 START / END / 回放边），从 manifest 单一源推导。
    depended = {dep for entry in manifest for dep in entry.deps}
    raw_edges: list[GraphEdge] = []
    for entry in manifest:
        if not entry.deps:
            raw_edges.append(GraphEdge(source=_START_NODE, target=entry.name))
        for dep in entry.deps:
            raw_edges.append(GraphEdge(source=dep, target=entry.name))
        if entry.name not in depended:
            raw_edges.append(GraphEdge(source=entry.name, target=_END_NODE))
        if entry.route is not None and entry.max_replays > 0:
            # ADR-0018 受控打回边：回到上游 dep（hitl1 → parse+partition）。
            # 架构不变式：有 route 且 max_replays>0 的节点恰一上游 dep（打回目标）。
            target = entry.deps[0]
            raw_edges.append(
                GraphEdge(
                    source=entry.name, target=target, cond="replay", max=entry.max_replays
                )
            )

    # 3) 隐藏节点桥接：每条入边 P→H × 出边 H→S 补 P→S（自环丢弃）；再移除所有触 H 的边。
    bridges: list[GraphEdge] = []
    for h in hidden_names:
        in_edges = [e for e in raw_edges if e.target == h]
        out_edges = [e for e in raw_edges if e.source == h]
        for ie in in_edges:
            for oe in out_edges:
                if ie.source != oe.target:
                    bridges.append(GraphEdge(source=ie.source, target=oe.target))
    surviving = [
        e
        for e in raw_edges
        if e.source not in hidden_names and e.target not in hidden_names
    ]
    # 桥接边可能复刻既有正常边（如 A→B 已存在又经 A→H→B 桥出）→ 去重。
    deduped: dict[tuple[str, str, str, int | None], GraphEdge] = {}
    for e in surviving + bridges:
        key = (e.source, e.target, e.cond or "", e.max)
        deduped.setdefault(key, e)

    # 4) 节点：可见 manifest 节点 + 起止 system 节点（不可隐藏）。
    nodes: list[GraphNode] = [
        _START_NODE_OBJ,
        *(
            GraphNode(
                id=entry.name,
                label=entry.label if entry.label is not None else entry.name,
                type=entry.node_type if entry.node_type is not None else "",
                color=entry.color,
                visible=True,
                interrupt=entry.interrupt,
            )
            for entry in manifest
            if entry.name in visible_names
        ),
        _END_NODE_OBJ,
    ]

    edges = tuple(
        sorted(deduped.values(), key=lambda e: (e.source, e.target, e.cond or "", e.max or 0))
    )
    return GraphView(
        nodes=tuple(nodes), edges=edges, warnings=tuple(warnings)
    )


_START_NODE_OBJ = GraphNode(
    id=_START_NODE, label="Start", type="system", color=None, visible=True, interrupt=False
)
_END_NODE_OBJ = GraphNode(
    id=_END_NODE, label="End", type="system", color=None, visible=True, interrupt=False
)
