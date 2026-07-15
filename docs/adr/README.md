# ADR 索引

架构决策记录（Architecture Decision Records）。编号永久、不重排；被取代的记录保留状态注记而非删除。
术语见 `CONTEXT.md`；状态树字段流向见 `docs/STATE.md`；模块边界与装配见 `docs/DEVELOPMENT.md`。
格式遵循 `domain-modeling` skill 的 ADR-FORMAT（`## 状态` / `## 背景` / `## 决策` / `## 权衡` / `## 影响`）。

## 活跃（Active）

### 段落与原文存储

- [0001 — 段落为回写原子单位](0001-paragraph-as-atomic-rewrite-unit.md) — 回写不依赖绝对偏移量；一节点不可跨段。
- [0005 — 原文与论证树两层存储](0005-two-layer-storage-raw-store-vs-tree.md) — 只读字节表 + 论证树；原文永不整篇进 Agent 上下文。**决策 2 被 ADR-0025 取代**（节点存原句 → 段落存原句），决策 1 不动。
- [0009 — 段落切分为确定性无损纯代码步骤](0009-deterministic-lossless-paragraph-partition.md) — 零 LLM 切分 + 分区不变式。**确定性被 ADR-0017 §4 部分打破**（partition 变 prompt 驱动），无损性须保。
- [0025 — 段落为聚合根](0025-paragraph-as-aggregate-root.md) — 原句由段落侧单份持有；`Argument` 退役 `paragraph_id`/`content`。取代 ADR-0005 决策 2。

### 假说 / 合并 / 影响传导

- [0002 — 全量投机假设生成 + 节点类型白名单](0002-optimistic-speculative-hypothesis-generation.md) — 不依赖验证结论；仅覆盖 evidence/sub_claim。
- [0003 — 影响传导为 Merge 后串行阶段、不产文本](0003-impact-propagation-is-serial-and-textless.md) — 不与事实验证并行；修订假设唯一来源是假设生成。
- [0006 — 双轨合并决策矩阵（12 格）](0006-merge-decision-matrix.md) — 原文×假说全 12 格；「① 成立」列按语义关系分流。取代原 ADR-0004。
- [0007 — 假说语义关系由假设生成 Agent 标注](0007-hypothesis-relation-classification.md) — 一假说一关系（oppose/advance/expand）。
- [0008 — 假说显式状态枚举](0008-hypothesis-explicit-status-enum.md) — supported/doubtful/refuted；与原文状态机对称。
- [0012 — 一致性校验 HITL-2 前扫一次](0012-consistency-check-single-pass-pre-hitl2.md) — 采纳后不回炉；只贴 `issue_tags`。
- [0013 — 权重 rubric + 剩余支撑率失效公式](0013-argument-weight-and-invalidation-formula.md) — `<0.5` invalid、`0.5–0.7` 弱化；阈值待回归调优。

### 状态机 / HITL 闸门

- [0010 — HITL-2 不可跳过硬闸门](0010-hitl2-is-mandatory-gate.md) — 绝不自动采纳；「无待办一键通过」≠ 跳过。
- [0011 — 状态机收尾：adopted 中间态 / corrected 由回写触发](0011-status-machine-finalization.md) — `adopted→corrected` 幂等可重试。**回写幂等部分被 ADR-0017 §1 部分覆盖**（writeback 裁撤、改为 rewrite_loop+hitl2）。

### 装配 / 包布局

- [0014 — 包重构 src/ 扁平 + manifest 驱动装配](0014-package-restructure-infra-agents-runtime.md) — `package-dir={""="src"}`；加 Agent 触点 7→3。

### 流水线重构（Slice 1–6）

- [0017 — 流水线重构（Slice 1–6）](0017-pipeline-refactor-slices-1-6.md) — **整合自原 0017/0018/0019/0020/0021 + 原 0016 存活残余**。
  §1 rewrite_loop 放弃字节一致 / 裁撤 writeback；§2 hitl1 partition 闸门 + 有界打回；§3 五合一 judgment（删 ReAct infra）；§4 partition prompt 驱动；§5 贯穿 state 落 PipelineState；§6 RunnableConfig 承载 langgraph 原生机制。

### 检索后端 / 子智能体迁入

- [0026 — 迁入 SearchAgent V12 作为真实检索后端](0026-real-retrieval-backend-via-searchagent-v12.md) — 挂 retrieval seam、vendor + carve-out、daemon worker loop + `run_coroutine_threadsafe` 桥接、`with_llm=False` 丢 verdict、domain whitelist 作废（已记录 PRD §6 偏差）。配套 `docs/prd-search-agent-integration.md`。

### 运行时控制面 / 可视化

- [0022 — interrupt + PostgresSaver 异步 HITL](0022-async-hitl-via-interrupt-postgres-saver.md) — `thread_id=session_id`；执行锁用 `session_locks` 表行。
- [0023 — 显示层为 trace_events 只读尾随视图](0023-display-layer-read-only-trace-events-tail.md) — WS 断开不中止 run；`LISTEN/NOTIFY` 回放。
- [0024 — HTTP 控制面 FastAPI + fresh-run `document` 字段](0024-http-control-plane-fastapi-and-document-field.md) — `POST /api/agent/run` + `GET /api/agent/graph`。

## 已合并 / 已删除（历史）

本次清理（2026-07-15）合并 / 删除的记录，编号永久作废、不复用：

- **原 0004**（回写按 node_type 分流）— 已合并入 [0006](0006-merge-decision-matrix.md)（其分流改由语义关系驱动）。
- **原 0015**（工具框架 seam 骨架移植）— 引入的 `infra/tool_protocol.py`/`retrieval_tool.py`/`runtime/tool_registry.py` 随 Slice 5 五合一删除，零外部引用，整条废弃。
- **原 0016**（历史对话 seam）— `HistoryStore` 部分随 ReAct 删除废弃；`RunnableConfig` 承载 langgraph 原生机制的存活残余并入 [0017 §6](0017-pipeline-refactor-slices-1-6.md)。
- **原 0018 / 0019 / 0020 / 0021** — 分别并入 [0017 §2 / §3 / §4 / §5](0017-pipeline-refactor-slices-1-6.md)。

> 注：第三方 vendored 代码 `src/infra/search_agent_vendor/` 内出现的 `ADR-0004` 字样指向该 vendored 项目自身的 ADR，与本仓 `docs/adr/` 无关，清理未触碰。
