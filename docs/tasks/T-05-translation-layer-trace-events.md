---
id: T-05
title: 翻译层（astream_events → §6.4 事件）+ trace_events 持久日志
status: done
assignee: "Karlzzb"
blocked_by: ["T-04"]
covers_adr: ["0023"]
covers_prd: ["§1.3", "§4.2.2", "§6.4", "§7.3", "§10.2"]
layer: [api, storage, tests]
type: feature
---

# T-05 — 翻译层 + trace_events 持久日志

## Source

- ADR-0023（翻译层只写 `trace_events`、非阻塞、mint `event_seq`；不写自定义 `BaseCallbackHandler`）。
- PRD §1.3（事件拓扑·翻译层为事件唯一权威落点）、§4.2.2（`trace_events` 表）、§6.4（全生命周期事件定义）、§7.3（`visible=False` 节点事件过滤）、§10.2（事件采集翻译层）。
- 基线：T-04 的 HTTP run 已驱动 `ainvoke`；本切片在其上加 `astream_events(version="v2")` 并写 `trace_events`。

## What to build

在 T-04 的 HTTP run 路径上挂事件采集翻译层：消费 `astream_events`、映射为 PRD §6.4 事件类型、mint `event_seq`、**非阻塞**写 `trace_events` 表。
本切片**不含** WS sender（T-06），但 `trace_events` 已是 durable、可查询的回放可信源——一次 run 后查表可见完整 CoT / 节点 / 工具事件，`event_seq` 单 trace 内单调。

决策性要点：

- `trace_events` 表（PRD §4.2.2）：

  ```sql
  CREATE TABLE trace_events (
    session_id  TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    event_seq   INT  NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trace_id, event_seq)
  );
  CREATE INDEX ON trace_events (session_id, trace_id, event_seq);
  ```

- 翻译层把 `astream_events` 词汇映射为 §6.4 类型：`graph_static`（建连首推，本切片由 T-06 WS 用；翻译层产）、`trace_start`、`node_start`（`node_id` / `node_instance` 本 trace 内第几次触发，从 0 起，区分回放环 / `label` / `type` / `color` / `input`）、`llm_thinking`（`node_id` / `token` / `full_thought`）、`tool_call`、`node_output`、`node_end`、`human_pause`（`node_id` / `question` / `hint`，取自 checkpoint interrupt payload）、`stream_finish`、`stream_abort`（`abort_reason`）、`heartbeat`（`event_seq=-1`，由 T-06 sender 产，翻译层不产）。
- `event_seq` 单 trace 内从 0 自增、mint 后写库；续跑时 `last_event_seq = SELECT max(event_seq) WHERE trace_id=X` 派生，`PauseMeta` 不存 `last_event_seq`。
- 翻译层**非阻塞**写 `trace_events`（不反压图、不因显示侧阻塞——ADR-0023 不变量）；Langfuse `CallbackHandler` 作为并行外部 sink 与 `astream_events` 消费端共存（`RunnableConfig["callbacks"]`），非回放源。
- `visible=False` 节点（T-02 元数据）：翻译层丢弃其 `node_*` / `llm_thinking` / `tool_call`，保留 trace 级事件（`trace_start` / `stream_finish` / `human_pause` 等）。
- `human_pause` 与 HTTP `NEED_HUMAN_INPUT` 同源（`aget_state` 判 interrupt payload），杜绝竞态。
- `config={"configurable":{"thread_id":session_id}, "callbacks":[langfuse_handler]}`；Langfuse 以 `trace_id` 为 tag 关联多 invoke（PRD §10.3 约束 3）。

## Acceptance criteria

- [x] `trace_events` 表落地（schema + 索引）；翻译层每事件写一行、非阻塞。
- [x] §6.4 全事件类型可由 `astream_events` 映射产出（除 `heartbeat`/`graph_static` 由 T-06 产）；`event_seq` 单 trace 内从 0 单调自增、续跑从 `max(event_seq)+1` 顺延无断层。
- [x] `node_instance` 正确区分回放环（同节点多次触发从 0 起）。
- [x] `visible=False` 节点的 `node_*` / `llm_thinking` / `tool_call` 被丢弃，trace 级事件保留。
- [x] `human_pause` 的 `question` / `hint` 取自 checkpoint interrupt payload（`aget_state`），与 HTTP `NEED_HUMAN_INPUT` 同源。
- [x] 翻译层写库**不阻塞**图执行（不变量验证：模拟慢写 / 写失败不影响 run 推进、不杀图）。
- [x] Langfuse handler 与 `astream_events` 消费端共存零冲突；Langfuse 写失败降级记错、不阻塞对话。
- [x] 回放可信源验证：一次完整 run 后查 `trace_events`，事件序列与真实执行流程匹配（同源同词汇）。
- [x] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## Blocked by

- T-04（HTTP run 驱动 `ainvoke`，翻译层挂其上；复用 `trace_id` / `session_id`）。

## Notes

- 本切片产 `trace_events` 但**不消费**它做实时下发（T-06 WS sender 才尾随）；本切片的可演示性 = 查表回放。
- `graph_static` 事件由 T-06 WS-sender 在建连时据 `build_graph_view`（T-02）产，不在本切片；翻译层产 `trace_start` 起的运行时事件。
- 背压（`llm_thinking` token 合并）属 T-06 WS-sender 侧队列，不在翻译层——翻译层只管落库（关键事件已 durable，T-06 live 丢也安全）。
