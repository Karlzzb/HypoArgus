---
id: T-04
title: HTTP 控制面（/api/agent/run + /graph + 所有权 + session_locks + pause_meta）
status: done
assignee: "karl"
blocked_by: ["T-03", "T-02"]
covers_adr: ["0022", "0024"]
covers_prd: ["§5", "§3.2", "§4.2.3", "§4.2.4", "§4.2.5", "§4.2.6", "§8", "§9.2", "§9.3", "§9.7"]
layer: [api, storage, tests]
type: feature
---

# T-04 — HTTP 控制面

## Source

- PRD §5（HTTP 统一接口规范）、§3.2（会话所有权校验）、§4.2.3–4.2.6（`pause_meta` / `session_owner` / `session_locks` / 注册表上限）、§8（全场景业务流程）、§9.2 / §9.3 / §9.7（超时 / 重复提交 / 上限）。
- ADR-0022（`session_locks` 表行跨请求持有、HITL 暂停期留存；不用 `pg_advisory_lock`）。
- 基线：`src/api_layer/` **不存在**；无 FastAPI / uvicorn / postgres 依赖（pyproject 仅 `langgraph>=1.2` 等）。`SessionCacheBase` 抽象基类尚未存在（PRD §4.1 定义）。

## What to build

落地 `src/api_layer/`（扁平 src 子包，遵循 ADR-0014）的 HTTP 服务，用 T-03 已建好的 `interrupt` + `PostgresSaver` 图，把 CLI 驱动换成 HTTP resume 驱动。
本切片**不含** WS / 事件流（T-05 起）——但 HITL 暂停 / 续跑 over HTTP 完整可用：一个 `curl` 脚本可发起对话、收 `NEED_HUMAN_INPUT`、回填 `human_response`、收 `SUCCESS`。

决策性要点：

- Web 框架选 async-native（推荐 FastAPI/Starlette，适配 `ainvoke`/`astream_events`/WS；选型记入 ADR 或 PRD §1.5 待定项收敛）。
- `SessionCacheBase` 抽象基类落地（PRD §4.1）：

  ```python
  class SessionCacheBase:
      def get_pause_meta(self, session_id: str): ...
      def set_pause_meta(self, session_id: str, meta): ...
      def get_session_owner(self, session_id: str) -> str: ...
      def set_session_owner(self, session_id: str, user_id: str): ...
      def lock_session(self, session_id: str) -> bool: ...   # session_locks 行
      def unlock_session(self, session_id: str): ...
      def get_active_count(self) -> int: ...
      def clean_idle(self): ...
  ```

  side 表（Postgres 实现，不含 `get_state`/`save_state`——state 是 checkpointer 契约）：

  ```sql
  CREATE TABLE pause_meta (
    session_id  TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    pause_time  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE TABLE session_owner (
    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE TABLE session_locks (
    session_id     TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    acquired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_seconds    INT NOT NULL DEFAULT 900
  );
  ```

- `POST /api/agent/run`：
  - 入参 `session_id`（必填）/ `query` / `human_response` / `biz_trace_id`；`query` 与 `human_response` 互斥（`PARAM_ERROR`）。
  - fresh-run 判定：无活跃 `pause_meta` 且 `query` 非空 → mint 新 `trace_id`。
  - resume 判定：有活跃 `pause_meta` 且 `human_response` 非空 → 复用 `pause_meta.trace_id`，`Command(resume=human_response)` 续跑。
  - 阻塞到终止态（`NEED_HUMAN_INPUT` / `SUCCESS` / `FAILED`）才返回；全局超时 120s。
  - `NEED_HUMAN_INPUT` 由 `graph.aget_state(config)` 判定（`state.next` 含 hitl 节点且 `tasks` 带 interrupt），与 WS `human_pause` 同源判定；`human_question` / `hint` 从 checkpoint interrupt payload 读，不另存。
  - fresh `query`：`session_locks` `INSERT ... ON CONFLICT DO NOTHING`，已存在未过期 → `LOCK_EXIST`。
  - HITL 暂停期锁**不释放**（行留存，续跑复用，不再 INSERT 故不误触 `LOCK_EXIST`）；`stream_finish` / abort 删锁行。
  - 错误码枚举全量：`LOCK_EXIST` / `PAUSE_EXPIRED` / `GRAPH_TIMEOUT` / `PARAM_ERROR` / `FORBIDDEN` / `SESSION_LIMIT`。
- `GET /api/agent/graph`：调 T-02 的 `build_graph_view(...)`，输出 §5.4 形状。
- 鉴权中间件：一期信任 Nginx 注入的 `X-User-Id`；`session_id` 未见登记 → 登记并绑定 `X-User-Id`（不生成）；已登记不匹配 → `403`；校验集中中间件层。
- 活跃会话数 = `session_owner` `last_seen` 近 30 min 计数；达上限（默认 100）且无法淘汰 → `SESSION_LIMIT`。
- `trace_id` mint 函数复用 T-03 的实现（fresh 时 `uuid4()`，resume 复用）。
- 注入 `InterruptDrivenGate`（实现 T-01 拆分后 seam：`formulate_question` 产出经 interrupt payload 落 checkpoint，`parse_reply` 在 resume 时从 `human_response` 喂回 → `action`-only `Hitl*Decision`，纯函数不动）。

## Acceptance criteria

- [x] `src/api_layer/` 包落地；pyproject 增 async web 框架 + `langgraph-checkpoint-postgres` + `psycopg`/`asyncpg` 依赖（conda `HypoArgus` 可装可跑）。
- [x] `SessionCacheBase` 抽象基类 + Postgres 实现落地；不含 `get_state`/`save_state`。
- [x] `pause_meta` / `session_owner` / `session_locks` 三表 schema 落地（迁移脚本或建表 SQL 记录在仓）。
- [x] `POST /api/agent/run` 支持发起（`query`）与续跑（`human_response`），互斥校验、fresh/resume 判定、终止态阻塞返回、`NEED_HUMAN_INPUT` 由 `aget_state` 判定、`human_question`/`hint` 取自 checkpoint。
- [x] 全量错误码可达且语义正确：`LOCK_EXIST`（重复提交 / 未处理断点）、`PAUSE_EXPIRED`（断点 30min 超时）、`PARAM_ERROR`、`FORBIDDEN`（跨用户）、`SESSION_LIMIT`、`GRAPH_TIMEOUT`。
- [x] 会话所有权强制：跨用户访问 → 403；`session_id` 首见登记绑定。
- [x] `session_locks` 行跨请求持有；HITL 暂停期不释放、续跑复用、`stream_finish` 删行。
- [x] `GET /api/agent/graph` 返回 §5.4 形状，来自 `build_graph_view`，含 `hitl1→parse+partition` 回放边。
- [x] 集成测试：无前端即可用脚本 / `httpx` 驱动完整 HITL 流程（发起 → `NEED_HUMAN_INPUT` → 回填 → `SUCCESS`）与并发 / 跨用户 / 超时 / 重复提交场景。
- [x] 质量门通过（`ruff check` + `mypy --strict` + `pytest`）。

## Blocked by

- T-03（驱动 `interrupt` + `PostgresSaver` 图；复用 `trace_id` mint）。
- T-02（`/api/agent/graph` 内容来源 `build_graph_view`）。

## Notes

- 本切片**不写** `trace_events`（T-05）、**不建** WS（T-06）；HTTP run 当前不产可视化事件，但 HITL 控制 over HTTP 完整成立。
- `NEED_HUMAN_INPUT` 与 WS `human_pause` 同源（`aget_state`）是杜绝「WS 说暂停、HTTP 说成功」竞态的关键（ADR-0023）；本切片先把 HTTP 侧判定落地，T-06 接 WS 时复用同一判定。
- 孤儿锁 / pause_meta TTL 扫描属 T-08（运维加固）；本切片先实现**惰性**清理（请求路径上命中过期即失效返回 `PAUSE_EXPIRED`/`LOCK_EXIST`），后台 sweep 留 T-08。
