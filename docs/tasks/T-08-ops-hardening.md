---
id: T-08
title: 运维加固（/health /metrics + 扫孤儿/超时/上限 + 脱敏钩子）
status: done
assignee: "Karlzzb"
blocked_by: ["T-04", "T-05"]
covers_adr: []
covers_prd: ["§9", "§11", "§3.3", "§13.1", "§13.9"]
layer: [ops, api, storage, tests]
type: hardening
---

# T-08 — 运维加固

## Source

- PRD §9（异常场景完整处理方案）、§11（可观测性与运维）、§3.3（敏感信息保护）、§13.1 / §13.9（验收清单）、§6.2（`/metrics` 队列指标端点）。
- 基线：T-04 已落 `session_locks` / `pause_meta` / `session_owner` 与惰性清理；T-05 已埋 `ws_event_queue_*` 计数；本切片补后台 sweep、端点、脱敏。

## What to build

补齐后台清扫、健康 / 指标端点、会话上限淘汰与脱敏钩子，把 §9 枚举的异常场景从「惰性命中」补成「后台主动兜底 + 可观测」。

决策性要点：

- **`GET /health`** → `{db: ok, active_sessions, active_locks, ws_connections}`。
- **`GET /metrics`**（Prometheus）至少：`active_sessions` / `active_locks` / `ws_connections` / `event_push_latency_seconds`（落 `trace_events` 到前端渲染）/ `graph_execution_duration_seconds` / `ws_event_queue_size` / `ws_event_queue_full_total` / `langfuse_errors_total`。
- **执行锁超时 / 孤儿 run 扫描**（PRD §9.6）：后台扫 `session_locks`，`last_heartbeat` 超 TTL（默认 900s，覆盖一次完整长 LLM run）时——
  - 该 session 有活跃 `pause_meta` → 合法 HITL 暂停，**跳过**（由 `pause_meta` 30min TTL 管辖）；
  - 无活跃 `pause_meta` → 孤儿 run，cancel token 中断图、推 `stream_abort`（`abort_reason`）、删锁行。
- **HITL 断点 30min 超时**（§9.2）：后台清理过期 `pause_meta` + 对应 `session_locks` 行；再提交返回 `PAUSE_EXPIRED`。
- **闲置会话清理 / 上限**（§9.7）：`session_owner` 近 30 min 计数；达上限（默认 100）且无法淘汰 → 新会话 `SESSION_LIMIT`；活跃会话数超 80% 阈值告警。
- **心跳续命**：运行中翻译层每写一批 `trace_events` 即更新 `session_locks.last_heartbeat`（T-05 已埋，本切片确保 sweep 消费它）；HITL 暂停期不写心跳、由 `pause_meta` 30min TTL 统一管辖，不误杀。
- **脱敏钩子**（§3.3）：工具调用入参 / 返回、LLM 思考内容在推送前端和落库前经可配置脱敏钩子（一期默认关闭，留正则接口如手机号 / 身份证）；日志禁止记录完整凭证、仅记哈希。
- 结构化 JSON 日志（stdlib + JSON formatter，不引新依赖），每条带 `session_id` / `trace_id` / `user_id`（脱敏后）；Langfuse 不可用时降级、仅本地记错、不阻塞对话。
- 资源监控：活跃会话数超 80% 阈值告警。

## Acceptance criteria

- [x] `GET /health` 返回 `db` / `active_sessions` / `active_locks` / `ws_connections`。
- [x] `GET /metrics`（Prometheus）暴露 §11.1 全量指标，含 `event_push_latency_seconds` / `ws_event_queue_*` / `langfuse_errors_total`。
- [x] 后台 sweep：`session_locks` TTL 孤儿（无活跃 `pause_meta`）→ cancel + `stream_abort` + 删锁行；有活跃 `pause_meta` → 跳过不误杀。
- [x] `pause_meta` 30min 过期 → 删 `pause_meta` + 对应 `session_locks` 行；再提交返回 `PAUSE_EXPIRED`。
- [x] `session_owner` 近 30 min 计数达上限且无法淘汰 → 新会话 `SESSION_LIMIT`；80% 阈值告警。
- [x] 脱敏钩子存在、可配置（正则接口）、默认关；日志不记完整凭证、仅哈希。
- [x] 结构化 JSON 日志每条带 `session_id` / `trace_id` / `user_id`（脱敏后）。
- [x] Langfuse 写失败降级记错、不阻塞对话（`langfuse_errors_total` 计数）。
- [x] 集成测试：模拟孤儿锁 → `stream_abort`；模拟 pause 30min 过期 → `PAUSE_EXPIRED`；模拟达上限 → `SESSION_LIMIT`；`/health`、`/metrics` 正确。
- [x] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## Blocked by

- T-04（`session_locks` / `pause_meta` / `session_owner` 表与惰性清理逻辑）。
- T-05（`trace_events` 翻译层写库批次驱动 `last_heartbeat`、队列指标计数）。

## Notes

- 本切片不新增控制流语义，只把 §9 / §11 的兜底 / 可观测从惰性补成主动 + 可见；不破坏 ADR-0023 不变量（`stream_abort` 仍只由锁 / pause 孤儿 / 显式 cancel 触发，WS 断开不触发）。
- 二期多实例 / Java 接管 / 结构化 `ops` / 全套安全（JWT/用户中心/mTLS）不在本切片范围（PRD §4.4 / §12）。

## Result

新增模块（均落 `src/api_layer/`，业务纯函数零改动）：

- `redaction.py`：脱敏钩子（`RedactionConfig` 正则规则集，默认空=关；`Redactor.redact_text/redact_obj` 递归 string 叶；`hash_credential` SHA-256 前 16 hex；`DEFAULT_SENSITIVE_KEY_PATTERNS`）。
- `metrics.py`：无依赖 Prometheus 文本 exposition（`Counter` / `Gauge` / `MetricSample` / `render_prometheus`）+ `OpsMetrics`（`langfuse_errors_total` / `event_push_latency_seconds` / `graph_execution_duration_seconds`）。
- `langfuse_wrap.py`：`wrap_langfuse_handler` 经 `__getattr__` 代理任意 `on_*` 回调，异常 → `langfuse_errors_total`+1 + 记错、不外抛。
- `logging_setup.py`：stdlib JSON formatter（不引新依赖）+ contextvars 注入 `session_id`/`trace_id`/`user_id`（`log_context` / `set_trace_id`）+ 敏感键名哈希 / 正则脱敏 + `configure_logging`。
- `ops.py`：`OpsService`（`health` / `metrics_text` / `sweep`）+ `OpsConfig` / `SweepReport`。sweep：Phase A 过期 `pause_meta` → `stream_abort("pause_expired")` + 删 pause + 删锁；Phase B 过期锁无活跃 `pause_meta` → `stream_abort("lock_orphan")` + 删锁 + `cancel_orphan`，有活跃 `pause_meta` 跳过；80% 上限 warning 告警。

侵入面（既有模块）：

- `session_cache.py`：扩 `LockInfo` + `list_expired_locks` / `list_expired_pauses` / `count_active_locks` / `ping`（InMemory + Postgres 双 adapter）。
- `translator.py`：注入 `metrics`（drainer 记 `event_push_latency_seconds`）+ `redactor`（`_mint` 落库前脱敏）。
- `run.py`：注入 `metrics`（`_drive` 记 `graph_execution_duration_seconds`）+ `redactor` + `log_context`/`set_trace_id` 运行身份 + `cancel_orphan` 在跑任务登记 seam。
- `ws.py`：`WSSenderService.active_connection_count` 供 `/health`。
- `app.py`：`create_app(ops_service=)` 挂 `GET /health` + `GET /metrics`。
- `server.py`：装配 `OpsMetrics` / `Redactor` / Langfuse 计数代理 / `configure_logging` / `OpsService` + 后台 `_sweep_loop`。

质量门真实输出（conda 环境 `HypoArgus`）：

```
$ ruff check src/ tests/
All checks passed!

$ mypy --strict src/
Success: no issues found in 54 source files

$ pytest -q
571 passed, 3 skipped in 117.36s
```

新增测试：`tests/test_redaction.py` / `test_metrics.py` / `test_langfuse_wrap.py` / `test_ops_latency.py` / `test_session_cache_ops.py` / `test_ops_service.py` / `test_cancel_orphan.py` / `test_ops_session_limit.py` / `test_ops_integration.py`（PG）/ `test_logging_setup.py`。3 个 skip 为既有（`test_real_llm_wiring` 需 DashScope key+网络、`test_writeback` 样例不足两段），与本切片无关。
