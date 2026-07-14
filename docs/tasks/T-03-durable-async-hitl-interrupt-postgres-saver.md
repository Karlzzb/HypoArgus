---
id: T-03
title: 持久化异步 HITL（interrupt + PostgresSaver + CLI resume 循环）
status: todo
assignee: ""
blocked_by: ["T-01"]
covers_adr: ["0022"]
covers_prd: ["§1.4", "§4.1", "§4.2.1", "§4.2.2", "§10.2", "§10.3", "§10.4"]
layer: [graph, storage, tests]
type: feature
---

# T-03 — 持久化异步 HITL（ADR-0022 spine）

## Source

- ADR-0022（提前消费代码标注的「后续切片」：`interrupt()` + `Command(resume=...)` + `PostgresSaver`，`thread_id = session_id`）。
- PRD §1.4（改造起点：`orchestrator.py:294` `graph.compile()` 无 checkpointer；`:309` `session_id` 预留位；HITL 现同步注入、无 interrupt）。
- PRD §4.1–4.2.1–2（seam、`PostgresSaver` + `trace_events` 表）、§10.2–10.4（事件采集、执行约束、CLI 与服务共用机制）。
- 基线：`Orchestrator.run_with_report`（`src/runtime/orchestrator.py:318-349`）用同步 `self.graph.invoke(...)`（`:343`）；`PipelineState` TypedDict（`:111-134`）；`SessionContext`（`src/domain.py:201-212`）；`OriginalParagraphs` slots + `MappingProxyType`（`src/original_paragraphs.py:28` / `:33`）。

## What to build

把图从「同步注入、无 checkpointer、`graph.invoke`」改造为「`interrupt()` 暂停 + `PostgresSaver` 持久化 + `ainvoke` resume 驱动」，并以 **CLI resume 循环**作为第一个驱动者验证整条 spine 可用、可持久、可跨进程续跑。
本切片**不含** HTTP / WS / 前端——那是 T-04 起。这是 ADR-0022 的最小完整可演示路径。

决策性要点：

- `graph.compile(checkpointer=PostgresSaver(...))`（消费 `orchestrator.py:294` 与 `:309` 预留位）。
  `thread_id = session_id`（来自 `SessionContext.session_id`，外部下发、Python 仅登记，见 ADR-0022）。
- `hitl1` / `hitl2` 节点改用 `interrupt(formulate_question(...))` 暂停；
  resume 用 `Command(resume=parse_reply_payload)`，喂给 T-01 拆分后 seam 的 `parse_reply(...)` → `Hitl*Decision`，**纯函数不动**。
  - interrupt payload = `formulate_question` 产出（hitl1 分段结构 / hitl2 逐段 `proposed_rewrites` 待确认表 + question + hint）。
  - resume value = `parse_reply` 输入（一期自由文本 / `action`-only，空 `ops`）。
- 全局单例初始化图（禁止每请求重建，PRD §10.3）。
- `OriginalParagraphs`（slots + `MappingProxyType` + `bytes` value）**必须验证** `PostgresSaver` 序列化器可处理；若 `JsonPlusSerializer` 不能直序列化 `MappingProxyType`，为其注册自定义编解码器（`BaseSerializer` 的 `dumps`/`loads` 钩子或类型注册），并加单测断言「checkpoint 写入 → 读回 → `OriginalParagraphs` 等价」。
- **CLI 驱动**（`src/runtime/run_real.py`，`main()` 在 `:133`、`run_real_pipeline` 在 `:72-115`）：改写为本地 resume 循环——
  `ainvoke(input)` → 检测 `aget_state(config)` 是否含 interrupt → 终端打印 question + hint → 读输入 → `ainvoke(Command(resume=reply))` → 循环，直到 `state.next` 空（终态）。
  注入 `TerminalGate`（实现 T-01 拆分后 seam，`formulate_question` 后阻塞 `input()`、`parse_reply` 喂回）。
- `trace_id` mint 规则：fresh-run（无活跃 `pause_meta`）时 `uuid4()` 生成；resume 复用。本切片 CLI 单 trace，但 mint 函数与服务共享（T-04 复用）。
- `event_seq` 由翻译层 mint（T-05）；本切片**不**写 `trace_events`，但 `pause_meta` / `session_locks` 暂不强制（CLI 单进程可直接走 checkpointer）。`pause_meta` 表随 T-04 落地，本切片先验证 checkpointer + interrupt + 跨进程续跑。

## Acceptance criteria

- [ ] `graph.compile(checkpointer=PostgresSaver(...))`；`thread_id = session_id`；进程重启后 checkpoint / interrupt 暂停点仍在。
- [ ] `hitl1` / `hitl2` 用 `interrupt()` 暂停；resume 用 `Command(resume=...)`；未直接改 State 字段（PRD §10.3 约束 2）。
- [ ] 业务纯函数 `confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document` 未改。
- [ ] `OriginalParagraphs` 经 `PostgresSaver` 序列化写读等价（有断言测试）；若需自定义编解码器已落地。
- [ ] CLI resume 循环可驱动完整一次修订（含 hitl1 / hitl2 两次暂停 + 续跑）直至终态 `final_document`。
- [ ] CLI 跨进程续跑成立：在一个进程中跑至 hitl 暂停（进程退出），新进程以同 `session_id` 启动、`aget_state` 见 interrupt、读输入、`Command(resume)` 续跑至完成。
- [ ] e2e 测试重构：`FakeHitl*Gate` 注入式 e2e 改为「驱动图 + `Command(resume=fake_decision)`」形式（PRD §10.4 / ADR-0022 Consequences）；`tests/test_orchestrator_e2e.py`、`test_orchestrator_resume.py`、`test_real_llm_wiring.py` 等全绿。
- [ ] 新增 Postgres 测试 fixture（`tests/conftest.py` 当前仅有 `sample_doc`，无 db fixture）；CI / 本地可在 conda `HypoArgus` 跑（testcontainer 或共享 PG，任选其一并记录在 `docs/TESTING.md`）。
- [ ] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## Blocked by

- T-01（gate seam 拆分；`InterruptDrivenGate` / `TerminalGate` 需拆分后契约）。

## Notes

- 本切片是整条服务的 spine；HTTP（T-04）只是换一个驱动者，复用同一张 `interrupt` + `PostgresSaver` 图。
- `trace_events` 翻译层在 T-05 落地；本切片 CLI 不强求事件日志，但 `trace_id` mint 函数要写成服务可复用形式。
- `pause_meta` / `session_locks` 表属于 T-04（HTTP 需要跨请求判定 fresh vs resume + 并发锁）；本切片 CLI 走 checkpointer 直驱即可，勿提前耦合 HTTP 概念。
