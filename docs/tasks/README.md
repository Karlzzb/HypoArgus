# 可视化服务任务索引（Tracking）

本目录承载 PRD V4.0（`prd.md`，配合 ADR-0022 / ADR-0023）的可视化服务一期开发任务文档。
每个任务是一个**垂直切片（tracer bullet）**：切穿其涉及的所有集成层（图 / 存储 / API / WS / 前端 / 测试），完成后可独立演示或验证。

任务文档独立、自包含，但通过本文索引与各文档 frontmatter 的 `blocked_by` 字段形成**跟踪能力**：
依赖顺序、阻塞关系、当前状态、覆盖范围一目了然。

## 状态字段约定

每个任务文档顶部 frontmatter 含：

- `id`：`T-0X`。
- `status`：`todo` → `in_progress` → `done`（阻塞未解前不得置 `in_progress`）；
  另有 `blocked`（依赖未完成）、`dropped`（裁撤）。
- `assignee`：认领者，空表示未认领。
- `blocked_by`：必须先完成的任务 id 列表（空 = 可立即开始）。
- `covers_adr` / `covers_prd`：覆盖的 ADR 与 PRD 章节，供回溯。
- `layer`：涉及层（`graph` / `storage` / `api` / `ws` / `web` / `tests` / `ops`），可多选。

> 维护约定：开始某任务时把 `status` 改 `in_progress`、补 `assignee`；
> 完成后改 `done` 并勾选其验收清单。状态变更无需另起 commit message 仪式，但须保持本文表格与各文档 frontmatter 同步。

## 依赖图

```
T-01  拆分 HITL gate seam (prefactor)      T-02  MANIFEST 展示元数据 + 可见性 (prefactor)
  │                                          │
  └────────────┐                    ┌────────┘
               ▼                    ▼
            T-03  持久化异步 HITL（interrupt + PostgresSaver + CLI resume）  ← ADR-0022 spine
               │
               ▼
            T-04  HTTP 控制面（/api/agent/run + /graph + 所有权 + 锁 + pause_meta）
               │                    │
               ▼                    ▼
            T-05  翻译层 + trace_events 持久日志  ──────────────┐
               │                                              │
               ▼                                              ▼
            T-06  WS sender（trace_events 只读尾随）       T-08  运维加固（/health /metrics 扫孤儿/超时/上限/脱敏）
               │                     ▲                     ▲
               ▼                     │                     │
            T-07  React 工作台（单页 live + 回放 + 嵌入式 HITL 卡片）
                                     ▲
            T-07 同时依赖 T-04（HTTP）、T-02（graph_static / 可见性）。
```

## 任务状态总表

| ID | 标题 | 状态 | 阻塞于 | ADR | PRD |
|----|------|------|--------|-----|-----|
| T-01 | 拆分 HITL gate seam | done | — | 0022 | §10.4, §7.2 |
| T-02 | MANIFEST 展示元数据 + 可见性 | done | — | — | §10.1, §5.4, §7.3 |
| T-03 | 持久化异步 HITL（spine） | done | T-01 | 0022 | §1.4, §4.1–4.2.1–2, §10.2–10.4 |
| T-04 | HTTP 控制面 | done | T-03, T-02 | 0022, 0024 | §5, §3.2, §4.2.4–5, §8 |
| T-05 | 翻译层 + trace_events | todo | T-04 | 0023 | §6.4, §10.2, §4.2.2 |
| T-06 | WS sender（只读尾随） | todo | T-05 | 0023 | §6.1–6.5, §1.3 |
| T-07 | React 工作台 | todo | T-06, T-04, T-02 | — | §7, §6.5 |
| T-08 | 运维加固 | todo | T-04, T-05 | — | §9, §11, §3.3 |

## 零侵入边界（贯穿所有任务）

业务纯函数**零改动**：`confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document`。
侵入面严格限定在：

1. **gate seam**（`Hitl1Gate` / `Hitl2Gate` Protocol 拆分 + InterruptDrivenGate / TerminalGate 实现）。
2. **orchestrator 装配层**（`graph.compile(checkpointer=...)`、`thread_id`、interrupt 节点、resume 驱动）。
3. **MANIFEST 展示元数据**（`AgentEntry` 增字段、可见性、图结构内省）。

新服务代码落 `src/api_layer/`（扁平 src 子包，遵循 ADR-0014）；前端落仓库根 `web/`（React + Vite，与 conda 环境 `HypoArgus` 解耦）。
质量门：`ruff check` + `mypy --strict` + `pytest`（conda 环境 `HypoArgus`）；`ruff format` 不强制，不重排既有文件。

## 与 ADR 的关系

ADR-0022（异步 HITL via interrupt + PostgresSaver）与 ADR-0023（显示层只读尾随 trace_events、WS 断开不中止 run）已是 accepted 决策。
任务文档**引用**它们，不重新决策；如执行中发现需偏离，另起 ADR-0024+ 再改任务。
