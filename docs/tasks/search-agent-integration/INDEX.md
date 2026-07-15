# SearchAgent V12 检索子智能体迁入 — 任务索引

> 母 PRD：`docs/prd-search-agent-integration.md`（自含全部决议与定位线索）。
> 本索引是 5 个 tracer-bullet 切片的状态看板；每个切片是独立、完整的单任务 md，供任意新 session 读取并执行。
> 硬约束（用户原话）：**不要修改主智能体框架去适配子智能体**。

## 切片依赖图

```
Slice 0 (vendor + carve-out + deps) ─┐
                                      ├─► Slice 2 (real adapter) ─► Slice 3 (real_llm 全链)
Slice 1 (RetrievalFn 5 输入) ─────────┘                       │
                                                              └─► Slice 4 (ADR-0026 + 文档) ◄── Slice 2
```

Slice 0 与 Slice 1 互不依赖、可并行；Slice 2 同时依赖两者；Slice 3、Slice 4 各自依赖 Slice 2。

## 状态看板

| 切片 | 标题 | 阻塞 | 状态 | 文件 |
|---|---|---|---|---|
| 0 | vendor SearchAgent V12 + carve-out + deps + 可导入 | 无 | 已完成 | [task-00-vendor-and-carve-out.md](task-00-vendor-and-carve-out.md) |
| 1 | 拓宽 `RetrievalFn` seam 至 5 输入（+`paragraph_list`） | 无 | 已完成 | [task-01-widen-retrieval-seam.md](task-01-widen-retrieval-seam.md) |
| 2 | 真实检索适配器：映射 + daemon worker loop + manifest real 工厂 + 接线 + 离线测试 | 0, 1 | 已完成 | [task-02-real-retrieval-adapter.md](task-02-real-retrieval-adapter.md) |
| 3 | 真实 V12 全链集成测试（`real_llm` 标记） | 2 | 已完成 | [task-03-real-llm-full-link-tests.md](task-03-real-llm-full-link-tests.md) |
| 4 | 文档：ADR-0026 + CONTEXT 术语 + DEVELOPMENT B1 | 2 | 未开始 | [task-04-adr-and-docs.md](task-04-adr-and-docs.md) |

状态取值：`未开始` / `进行中` / `已阻塞` / `已完成`。接手切片时，先读本索引定位依赖是否就绪，再读对应任务 md 的「实现指引」与「验收标准」，开工时把状态改 `进行中` 并在任务 md 的「状态追踪」表追加一行。

## 跨切片不变量（每个切片都要守住）

- **Story 17 / tracer bullet**：真实后端未配置或未触达任何段时，终稿逐字节等于原文。Slice 1/2/3 的测试都要断言此不变量。
- **硬约束**：不动 manifest 装配范式、`NodeFn` 同步签名、judgment 单写者契约、citations 单写者契约、`Source` schema、`_merge_dict` reducer、图拓扑。
- **质量门**（conda env `HypoArgus`）：`ruff check src tests` + `mypy --strict src` + `pytest -q`；不强制 `ruff format`。vendored 源码 carve-out 排除；适配器代码全量进严格门。
