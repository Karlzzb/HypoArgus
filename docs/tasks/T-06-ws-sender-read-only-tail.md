---
id: T-06
title: WS sender（trace_events 只读尾随 + 心跳 + 背压 + 重连回放）
status: done
assignee: "Karlzzb"
blocked_by: ["T-05"]
covers_adr: ["0023"]
covers_prd: ["§1.3", "§6.1", "§6.2", "§6.3", "§6.4", "§6.5", "§9.1", "§9.4", "§9.8"]
layer: [ws, api, tests]
type: feature
---

# T-06 — WS sender（只读尾随 trace_events）

## Source

- ADR-0023（WS-sender 用 `LISTEN/NOTIFY` 尾随 `trace_events` + 重连按 `event_seq` 回放；背压队列只作用于 WS-sender→WS 之间；WS 断开永不触发 abort）。
- PRD §1.3（架构不变量与事件拓扑）、§6.1（连接维持）、§6.2（背压与流控）、§6.3（单条消息结构）、§6.4（`graph_static` / `heartbeat` 事件）、§6.5（前端强制同步逻辑）、§9.1 / §9.4 / §9.8（刷新重连 / 多标签页 / 心跳丢失）。
- 基线：T-05 已落 `trace_events` 表 + 翻译层；本切片挂 WS 读端，**不碰**图执行。

## What to build

每条 WS 一个 WS-sender，**只读尾随** `trace_events`：`LISTEN/NOTIFY` 近实时 + 重连按 `event_seq` 回放已落库事件。
WS 是只读显示视图——连、断、慢都不启动、不中止、不阻塞 run（ADR-0023 不变量）。本切片**不含**前端（T-07），用 `websockets` / `wscat` 客户端即可演示。

决策性要点：

- 连接 `wss://域名/ws/agent/stream?session_id={session_id}`；建连校验 `X-User-Id` 与 `session_id` 归属（复用 T-04 中间件），失败 close code 4001。
- 同一 `session_id` 仅允许一条 WS（一期单实例）；新连接建连时旧连接被关闭（**停止下发、不触发 run abort**），新连接从 `trace_events` 按 `event_seq` 回放接续。
- 单条消息结构（PRD §6.3）：`session_id` / `trace_id` / `event_seq`（heartbeat 为 -1）/ `event_type` / `payload`。
- 建连首推 `graph_static`（`event_seq=-1`，来自 T-02 `build_graph_view`），供前端渲染骨架。
- 心跳：服务端每 30s，若 30s 内未发数据帧则发 `heartbeat`（`event_seq=-1`，前端丢弃）；静默 90s 无消息视为异常、前端主动重连（本切片服务端发心跳，前端侧由 T-07）。
- 背压（仅 WS-sender→WS 之间，不反压图）：WS-sender 内 `asyncio.Queue(maxsize=256)`；
  队列满 → `llm_thinking` token 合并到队列末尾同类事件；其余事件因已在 `trace_events` 落库，live 丢弃安全（重连必补）。
  关键事件（`human_pause` / `stream_finish` / `stream_abort`）durable 在 `trace_events`，永不真正丢失。
- `stream_abort` 触发源：执行锁 TTL 孤儿、PauseMeta 30min TTL 孤儿、显式 HTTP cancel（孤儿扫描在 T-08）；**WS 断开永不触发 abort**。
- `astream_events` 翻译层（T-05）写一批 `trace_events` 即更新 `session_locks.last_heartbeat` 表存活（心跳职责由翻译层写库批次驱动，WS-sender 不背此担）。

## Acceptance criteria

- [x] WS 建连带 `session_id`；归属校验失败 close 4001；跨用户拒绝。
- [x] 同 `session_id` 新连接关闭旧连接（不下发 `stream_abort`，仅停下发）；新连接按 `event_seq` 回放 `trace_events` 到最新再接 live。
- [x] 建连首推 `graph_static`（来自 `build_graph_view`）。
- [x] `LISTEN/NOTIFY` 近实时尾随；重连按 `event_seq` 回放，前端按序号过滤乱序 / 滞后（§6.5）。
- [x] 心跳：30s 无数据帧发 `heartbeat`（`event_seq=-1`）。
- [x] 背压：`asyncio.Queue(256)` 满时合并 `llm_thinking` token、不丢关键事件（关键事件在 `trace_events` 已 durable）；`/metrics` 暴露 `ws_event_queue_size`、`ws_event_queue_full_total`（端点本身在 T-08 落地，指标埋点在本切片）。
- [x] **不变量验证**：WS 断开不中止 run（run 在服务端继续至完成或 HITL；半截事件已落库，重连按 `event_seq` 回放接续）。
- [x] `stream_abort` 仅由锁 TTL 孤儿 / PauseMeta TTL 孤儿 / 显式 cancel 触发，WS 断开不触发。
- [x] 集成测试：用 python `websockets` 客户端连 WS、并发起 HTTP run，观察 live 事件流；中途断开重连，见 `event_seq` 回放补齐、无关键事件丢失。
- [x] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## 验收映射

- 归属 4001 / 跨用户 / `graph_static` / 回放 / live 尾随 / 心跳 / 背压 / 同会话取代（不发 `stream_abort`）/ 指标快照形状：
  `tests/test_ws_sender.py`（进程内 ASGI WS 客户端驱动真实 FastAPI WS 端点）。
- PG E2E（真实 HTTP run via httpx ASGI transport + WS live + 断开 + resume→SUCCESS + 重连回放 + 无关键事件丢失 + 不发 `stream_abort`）：
  `tests/test_ws_sender.py::test_ws_disconnect_does_not_abort_run_and_reconnect_replays`。
- 真实 `websockets` PyPI 客户端互操作（真实 uvicorn TCP 服务 → 建连 → `graph_static` → live → 断开重连回放）：
  `tests/test_ws_websockets_interop.py`（验收 §9「用 python websockets 客户端」字面落地）。
- `subscribe`/`events_for_session` seam（InMemory pub-sub + PG LISTEN/NOTIFY）：
  `tests/test_trace_store.py`。
- 字面 `websockets`/`wscat` 演示入口：`python -m api_layer.server`。

## 质量门结果

```
$ conda run -n HypoArgus ruff check src tests
All checks passed!

$ conda run -n HypoArgus mypy --strict src
Success: no issues found in 49 source files

$ conda run -n HypoArgus python -m pytest -q
525 passed, 3 skipped in 114.29s
```

3 skip 均为预存在（`test_real_llm_wiring` 需 DASHSCOPE key + 网络；`test_writeback` 样例不足两段），与本切片无关。

## 设计注记

- 服务端断开察觉延迟 = 心跳间隔（PRD §6.4 30s；§9.8 前端 90s 静默判异常）。
  真实 `websockets` 客户端断开后，`serve` 的 `_send` 阻塞在 `buf.pop()`（非 WS 读），
  须待下个心跳周期的 `send_text`→`WebSocketDisconnect` 才察觉并收尾。
  互操作测试据此用 0.2s 心跳使断开→收尾→uvicorn 关停在 10s 内确定性完成；
  生产 30s 心跳为 PRD 设计，30s 内的收尾延迟不阻塞 run（ADR-0023 不变量）。
- `_finalize` 经 `asyncio.shield`：真实部署下 uvicorn 取消 `serve` 协程时，
  `create_task` 子任务（tail/send）不随父取消，故显式取消 + shield 排空，
  关 LISTEN 连接、释放订阅、关 WS；`CancelledError` 其后再抛，子任务无泄漏。
  `sub.close()` 的 `except BaseException` 确保 cancel 落在关连接期间亦不跳过
  `unregister` / `_active` pop / `_safe_close`（`ws_event_queue_size` 不漂移）。
- **所有权只读**（ADR-0023 不变量）：WS `_enforce_ownership` 只读校验 T-04 所有权
  seam——不 `set_session_owner`、不 `touch_session_owner`。所有权绑定与活跃计数
  （`session_limit`）由 run 控制面独占；WS 首见未绑定会话仅允许显示先连，不 hijack
  随后 run 的归属，亦不会以 WS-only 流量撑爆 `get_active_count` 拒绝合法 run。
- **回放 / live 交界去重**：`subscribe`→`_replay` 窗口内落库的事件既进 live 队列又被
  `_replay` 读到；`_tail` 按 per-trace `event_seq` 跳过 `<= seen` 的 live 事件
  （`seen` 由 `_replay` 填充），保证不重发。
- **durable 与 NOTIFY 解耦**：PG `append` 先独立提交 INSERT，再单独发 NOTIFY（失败仅记错、
  不回滚已落库行）——display 副信道失败不可丢 durable 回放源行；NOTIFY 在提交后发，
  订阅者唤醒查询时行必可见。
- **取代无孤儿**：同会话新连接 `get` 旧 handle → 先登记自身 → 再 `supersede` 旧，
  使 `_active` 跨 `await` 恒持 handle；`_finalize` 的 `is handle` 守卫令被取代的旧 handle 不误删新连接。


## Blocked by

- T-05（尾随的 `trace_events` 表 + 翻译层；`human_pause` 同源判定）。

## Notes

- 本切片复用 T-02 `build_graph_view` 产 `graph_static`、T-04 所有权中间件与 `session_id` 归属校验、T-05 `trace_events` 与 `human_pause` 同源。
- 跨实例 `LISTEN/NOTIFY` 扇出是二期（PRD §4.4）；一期单实例，WS-sender LISTEN 本实例事件即可。
- `/metrics` 端点正式落地在 T-08；本切片只确保队列深度 / 满次计数器已埋点可读。
