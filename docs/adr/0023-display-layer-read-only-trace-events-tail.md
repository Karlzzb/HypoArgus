# 显示层为 trace_events 只读尾随视图，WS 断开不中止 run

## Status
accepted (2026-07-14)

## Context
可视化要避免「网络抖动杀掉一条长 LLM run」（`judgment` / `rewrite_loop` 是重节点），并消除 WS 与 HTTP 时序错位（「WS 说暂停、HTTP 说成功」竞态）。
若 WS 与图直连、用有界背压队列且队列满时阻塞关键事件，会反压到图——等于「WS 慢 → 阻塞智能体」。

## Decision
**架构不变量：前端显示连接不得影响智能体执行机制。**
run 的唯一控制输入是 `/api/agent/run`（`query` 发起、`human_response` 续跑）；WS 连接是只读的 `trace_events` 尾随视图——连、断、慢都不启动、不中止、不阻塞 run。
- 翻译层（`astream_events` 驱动）只写 Postgres `trace_events` 表（非阻塞、mint `event_seq`）；不写自定义 `BaseCallbackHandler`。
- WS-sender 用 `LISTEN/NOTIFY` 尾随 `trace_events` + 重连按 `event_seq` 回放；背压队列只作用于 WS-sender→WS 之间（满则合并 `llm_thinking` token，关键事件在 `trace_events` 已 durable）。
- `stream_abort` 只由锁 TTL 孤儿、PauseMeta TTL 孤儿、显式 HTTP cancel 触发；**WS 断开永不触发 abort**。
- 回放源 = `trace_events`（durable、与实时流同源同词汇）；Langfuse 降为可选外部 sink，非回放源。

## Considered Options
- WS 断开即 abort（简单，但抖动杀长 run，与 ADR-0022 的持久化投资相悖）。
- WS 与图直连的 in-process 背压队列、队列满阻塞关键事件（反压到图，违反不变量）。

## Consequences
- 刷新 / 抖动重连即从 `trace_events` 按 `event_seq` 回放恢复（PRD §9.1 改写为「重连即恢复」）。
- 关键事件 durable 不丢，live 队列丢弃也安全（重连必补）。
- `event_seq` 连续性由 `max(event_seq)` 派生，`PauseMeta` 不再存 `last_event_seq`；`human_question`/`hint` 从 checkpoint interrupt payload 经 `aget_state` 读。
- `NEED_HUMAN_INPUT` 由 `aget_state` 判定，与 WS 的 `human_pause` 同源，杜绝竞态。
- `LISTEN/NOTIFY` 二期复用做跨实例事件扇出（多实例无需 Redis）。
