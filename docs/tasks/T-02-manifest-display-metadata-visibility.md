---
id: T-02
title: MANIFEST 展示元数据 + 可见性旋钮 + 图结构内省
status: todo
assignee: ""
blocked_by: []
covers_adr: []
covers_prd: ["§10.1", "§5.4", "§7.3"]
layer: [graph]
type: prefactor
---

# T-02 — MANIFEST 展示元数据 + 可见性 + 图结构内省

## Source

- PRD §10.1（MANIFEST 单一源 + 展示元数据 + 可见性）。
- PRD §5.4（`GET /api/agent/graph` 与 WS `graph_static` 同源）。
- PRD §7.3（步骤可见性配置）。
- 基线：`AgentEntry`（`src/agents/assembly.py:721-742`，`@dataclass(frozen=True)`）当前**无** `label` / `node_type` / `color` / `desc` / `visible`；`MANIFEST`（`:745-818`）为 7 条 `AgentEntry`。

## What to build

为 `AgentEntry` 增加可选展示字段并提供**单一源**的图结构内省函数，供后续 `GET /api/agent/graph`（T-04）与 WS `graph_static`（T-06）共享，避免漂移。
本切片**纯数据 + 纯函数**，不引入 web。

决策性要点：

- `AgentEntry` 增可选字段（缺省从 `name` 推导，HITL 节点强制约束见下）：

  ```python
  label: str | None = None        # 缺省 = name
  node_type: str | None = None    # system / parse / hitl / hypothesis / retrieval / judgment / rewrite / hitl2 …
  color: str | None = None        # 展示色
  desc: str | None = None
  visible: bool = True            # 单一可见性旋钮
  ```

- MANIFEST 7 条各补展示元数据；`hitl1` / `hitl2` 强制 `visible=True`、`interrupt=True`（配置 override 忽略并告警）。
- override 配置 `config/visibility.yaml`：`hidden: [parse+partition]`，部署时改、**不重启代码**、不做运行时热切。
- 纯内省函数（暂名 `build_graph_view(manifest, visibility) -> GraphView`）：
  - 输出 §5.4 节点 / 边结构（节点含 `id` / `label` / `type` / `color` / `visible` / `interrupt`）。
  - `visible=False` 节点不出现在 `nodes`；其前后可见节点补直连边；回放边（`hitl1 → parse+partition`，`max_replays=3`，ADR-0018）若收缩成自环则丢弃（执行照跑）。
  - 含 `hitl1 → parse+partition` 条件回放边（`cond: replay`, `max: 3`）。
  - 节点名当不透明字符串（`parse+partition` 含 `+`，勿拆分）。
- 起始 / 终止边从 orchestrator 的 START/END 内省（与 `MANIFEST` 单一源对齐，不另写拓扑）。

## Acceptance criteria

- [ ] `AgentEntry` 含 `label` / `node_type` / `color` / `desc` / `visible`（均带缺省，向后兼容现有构造）。
- [ ] `hitl1` / `hitl2` 节点标注 `interrupt: true` 且强制 `visible=True`，配置 override 时告警并忽略。
- [ ] `config/visibility.yaml` 生效：`hidden` 节点不出现在 `build_graph_view` 输出、其前后可见节点补直连边、回放边自环被丢弃。
- [ ] `build_graph_view(...)` 输出形状满足 §5.4 响应示例（节点 / 边字段齐备），含 `hitl1→parse+partition` 回放边。
- [ ] `build_graph_view` 为纯函数且有单元测试覆盖：默认全可见、隐藏中间节点补直连、回放边自环丢弃、HITL 强制可见告警。
- [ ] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## Blocked by

None — 可立即开始。

## Notes

- 本切片是 T-04（`/api/agent/graph`）与 T-06（WS `graph_static`）的共享前置；二者都调 `build_graph_view`，单一源不漂移。
- `visible=False` 节点在事件层的过滤（`node_*` / `llm_thinking` / `tool_call` 丢弃）由 T-05 翻译层消费 `visible` 实现，本切片只产元数据。
