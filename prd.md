---

# HypoArgus 可视化服务 PRD V4.0

## 文档基础信息
- **文档名称**：HypoArgus 论证驱动文档修订多智能体可视化服务需求文档
- **版本**：V4.0
- **修订日期**：2026-07-14
- **适用阶段**：第一期开发（单 Python 实例 + Postgres 持久化、零侵入 agent 业务纯函数）
- **受众**：Python 后端开发、前端开发、后续 Java 服务对接开发、测试

## 核心目标
1. 零侵入 agent **业务纯函数**（`confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document`），实现前端完整可视化（CoT、节点状态、中间产出、HITL）。
   侵入面严格限定在 **gate seam + orchestrator 装配层 + MANIFEST 展示元数据**。
2. 彻底解决前端展示与智能体执行时序不同步、数据错乱问题。
3. 对外 HTTP API 永久兼容，当前前端直调、后期 Java 转发无需改动接口。
4. 页面嵌入式 HITL 交互卡片，无弹窗，上下文完整可见。
5. 一期即落地持久化（Postgres），刷新/抖动/退出后续跑均成立。

## 核心约束
- 一期单 Python 服务实例部署；持久化使用 Postgres，不引入 Redis。
- 持久化与跨实例事件扇出均由 Postgres 承担。
- 不修改 LangGraph State 的业务语义；State 序列化由 checkpointer 序列化器承担。
- WebSocket 数据流直连 Python，Java 上层不处理流式消息。
- **架构不变量**：前端显示连接不得影响智能体执行机制（见 §1.3）。

## 关键非功能指标（POC 目标）
- 并发会话数：≥ 50 个活跃会话同时可视化，无明显延迟。
- 事件推送延迟：事件从落 `trace_events` 到前端渲染 ≤ 200ms（P50）。
- 端到端对话延迟（从 HTTP 请求到首个可视化事件）：≤ 2s。
- 资源上限：活跃会话数保护阈值 100 个（达上限返回 `SESSION_LIMIT`）。

---

# 一、整体架构与调用链路

## 1.1 分层架构
1. 前端展示层：智能体可视化工作台（单页面一体化布局）。
2. 上层业务层（可选，二期接入）：Java 业务服务（鉴权 + HTTP 透传，不处理 WebSocket）。
3. 信任边界层：一期 Nginx（HTTPS/WSS 终结 + 注入 `X-User-Id`），二期 Java 接管。
4. Python 接入层：
   - HTTP 统一接口：发起对话、HITL 人工回填、获取图结构。
   - WebSocket 长连接：`trace_events` 的实时尾随视图，内置心跳保活与背压控制。
5. 调度管理层：Postgres side 表（PauseMeta / session_owner / 会话注册表）、`pg_advisory_lock`、事件时序管理、闲置清理、会话数量上限。
6. LangGraph 执行层：原生 `StateGraph`（`interrupt()` + `Command(resume=...)` + `PostgresSaver` checkpointer）、`astream_events` 事件采集翻译层。
7. 观测层：`trace_events` 持久化事件日志（回放可信源） + 可选 Langfuse 外部 trace sink + `/health` / `/metrics`。

## 1.2 两条调用链路（接口完全统一，无差异化开发）
### 链路 A（一期）
前端工作台 → Nginx（强制 HTTPS/WSS、注入 `X-User-Id`） → Python HTTP/WebSocket
### 链路 B（二期接入 Java，接口零改动）
前端工作台 → Java 服务（鉴权后注入 `X-User-Id`、透传全部入参与请求头） → Python HTTP
> WebSocket 前端始终直连 Python，流式数据不经过 Java/Nginx 业务面。

## 1.3 架构不变量与事件拓扑
**不变量：前端显示连接不得影响智能体执行机制。**
智能体 run 的唯一控制输入是 `/api/agent/run`（`query` 发起、`human_response` 续跑）。
WS 连接是只读的 `trace_events` 尾随视图——连、断、慢都不启动、不中止、不阻塞 run。

由此推出的事件拓扑：
1. **翻译层（图侧，`astream_events` 驱动）→ 只写 Postgres `trace_events` 表**，非阻塞，mint `event_seq`。
   事件唯一权威落点；翻译层永不因显示侧阻塞图。
2. **WS-sender（每条 WS 一个）→ 尾随 `trace_events`**：`LISTEN/NOTIFY` 近实时 + 重连按 `event_seq` 回放已落库事件。
   WS-sender 自持一个有界队列做背压（满则合并 `llm_thinking` token）；
   关键事件已在 Postgres 落库，即使 live 队列丢弃，前端重连必从 `trace_events` 补回，故 live 丢关键事件也安全。
3. **`stream_abort` 触发源**：执行锁 TTL 孤儿、PauseMeta 30min TTL 孤儿、（未来）显式 HTTP cancel。
   **WS 断开永不触发 abort。**
4. 每条事件携带 `event_seq`（单 trace 内自增），前端按序号过滤乱序、滞后消息。
5. 单 `session_id` 同一时间仅允许一条执行链路运行（`session_locks` 表行拦截并发请求，见 §4.2.5）。
6. 所有事件绑定 `session_id + trace_id` 双标识。

## 1.4 改造起点（现有代码基线）
- 现有图：`StateGraph(PipelineState)` 于 `orchestrator.py:260` 编译，`graph.compile()`（`:294`）当前无 checkpointer——一期接入 `PostgresSaver`。
- 拓扑（`MANIFEST`，`assembly.py:745-818`）：
  `START → parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → END`，
  其中 `hitl1 → parse+partition` 为条件回放边（`max_replays=3`，ADR-0018）。
- HITL 现状：`Hitl1Gate` / `Hitl2Gate` 为同步注入 Protocol（`review()` 阻塞返回 `Hitl*Decision`），无 `interrupt()`/`Command(resume=)`，图从不暂停——一期改造为 interrupt + checkpointer。
- 现有 ID：仅 `session_id`、`user_id`（`SessionContext`，`domain.py:209-210`，env 来源）；`session_id` 在 `orchestrator.py:309` 预留为未来 checkpointer key——一期消费。
- 现有可观测：Langfuse 可选 `CallbackHandler` 注入 `RunnableConfig["callbacks"]`（`run_real.py:59-69`），元数据约定 `langfuse_session_id`/`langfuse_user_id`/`langfuse_tags`——保留为可选外部 sink。
- 现有入口：CLI `python -m runtime.run_real`（`run_real.py:133`），同步 `graph.invoke`——一期改为异步 `ainvoke` resume 循环驱动（见 §10.4）。

## 1.5 模块布局
- **后端服务**：`src/api_layer/`（Python 包，扁平 src 子包，遵循 ADR-0014；与 `runtime`/`agents`/`infra` 同级，合法标识符用下划线）。
  承载 HTTP 路由、WS server、翻译层、`SessionCacheBase` 的 Postgres 实现、鉴权中间件、`/health`/`/metrics`、可见性配置加载。导入现有核心，不改业务纯函数。
- **前端**：仓库根 `web/`（React，独立 node 工具链，与 conda 环境 `HypoArgus` 解耦）。
  不放 `src/` 下——`src/` 是 ADR-0014 定义的 Python 包根，`packages.find where=["src"]` 会扫包发现；混入 JS 项目会污染 setuptools/ruff/mypy 边界。
- **Web 框架**（后端，待定）：async-native 以适配 `ainvoke`/`astream_events`/WS，推荐 FastAPI/Starlette；前端框架 React（Vite）。

---

# 二、全局唯一 ID 规范（全链路统一）

## 2.1 ID 定义与归属
| ID | 归属（谁 mint） | Python 角色 | 基数 |
|---|---|---|---|
| `user_id` | 外部（前端/Java，`X-User-Id`） | 接收 + 校验归属 | 每用户 |
| `session_id` | 外部——一期前端生成存 localStorage；二期 Java 登录后下发 | **不生成**；作主键（checkpointer `thread_id`、PauseMeta key、owner key、锁 key、注册表 key）；首次见即登记绑定 `X-User-Id` | 1 session : N trace |
| `trace_id` | **Python 服务内部 mint** | 收到无活跃 `PauseMeta` 的 `query` 时 `uuid4()` 生成；续跑复用 `PauseMeta.trace_id` | 每轮（initial + resume 共享一个） |
| `event_seq` | Python 翻译层 mint | 单 trace 内从 0 自增；`trace_events.event_seq` 落库 | 每事件 |

- `trace_id` 必须由 Python mint：fresh-run vs resume 由「是否有活跃 `PauseMeta`」判定，仅 Python 知晓；前端只发 `query` 或 `human_response`，不感知轮次。
- `session_id` 与 `trace_id` 关系：`session_id` = 工作台（一个浏览器标签页，跨刷新稳定，外部拥有）；`trace_id` = 工作台上的一次修订（可能横跨 initial + 多次 HITL resume，Python 拥有）。两级聚合。

## 2.2 ID 传递规则
1. HTTP 请求必填 `session_id`；`trace_id`/`event_seq` 由 Python 内部生成、关联、存储。
2. WebSocket 连接地址必须携带 `session_id`；建连校验 `X-User-Id` 与 `session_id` 归属（见 §3）。
3. `trace_events`、PauseMeta、session_owner、执行锁、Langfuse 元数据全部组合 `session_id + trace_id` 隔离数据。

---

# 三、安全设计（一期：信任边界 + 所有权强制 + 脱敏钩子）

## 3.1 传输与信道安全
- 全站强制 HTTPS，WebSocket 使用 `wss://`，Nginx 层 SSL 终结。
- Python 侧不终结 mTLS、不验签；信任来自 Nginx 注入的 `X-User-Id`（网关后服务模式）。

## 3.2 会话所有权校验
- Python 维护 `session_owner`（Postgres side 表，`session_id → user_id`）。
- 所有 HTTP 请求及 WebSocket 建连时校验 `X-User-Id` 与 `session_id` 归属，不允许跨用户访问。
- `session_id` 未见登记 → 登记该外部下发的 id 并绑定 `X-User-Id`（Python 登记不生成）。
- 已登记但 `user_id` 不匹配 → `403 Forbidden`，WS 关闭（自定义 close code 4001）。
- 校验集中在中间件层。
> `session_id` 是 Postgres 行 key，伪造 `user_id` 可续跑他人 HITL 中断；故此校验在共享存储下是真实安全栏。

## 3.3 敏感信息保护
- 工具调用入参/返回、LLM 思考内容在推送前端和落库前，经**可配置脱敏钩子**（一期默认关闭，留正则接口如手机号/身份证）。
- 日志中禁止记录完整凭证，仅记哈希。

## 3.4 接口访问控制（一期）
- 一期信任 Nginx 注入的 `X-User-Id`，不调用户中心、不做 JWT 验签。
- 二期 Java 接管信任边界后，补齐 JWT/用户中心/mTLS（见 §12）。

---

# 四、存储层设计（Postgres 持久化，一期即落地）

## 4.1 分层原则与 seam
持久化被两道 seam 隔离，agent 业务纯函数对后端无感知：
1. LangGraph checkpointer 接口（换后端实例即可）。
2. side-metadata 抽象基类 `SessionCacheBase`（仅 side 元数据，不碰 state）。
- state 是 checkpointer 的契约，不是 cache 的；`SessionCacheBase` 不含 `get_state`/`save_state`。

### 抽象基类 SessionCacheBase
```python
class SessionCacheBase:
    def get_pause_meta(self, session_id: str): pass
    def set_pause_meta(self, session_id: str, meta): pass
    def get_session_owner(self, session_id: str) -> str: pass
    def set_session_owner(self, session_id: str, user_id: str): pass
    def lock_session(self, session_id: str) -> bool: pass   # session_locks 行
    def unlock_session(self, session_id: str): pass
    def get_active_count(self) -> int: pass
    def clean_idle(self): pass
```

## 4.2 一期实现：PostgresSaver + Postgres side 表
### 1. 图 state + interrupt 暂停：`PostgresSaver`
- `graph.compile(checkpointer=PostgresSaver(...))`。
- `thread_id = session_id`（消费 `orchestrator.py:309` 预留位）。
- checkpointer 承担：完整 `PipelineState` 落盘、interrupt 暂停点、`Command(resume=...)` 续跑能力。进程重启后仍在。

### 2. 事件日志：`trace_events` 表（回放可信源）
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
- 翻译层每事件写一行（非阻塞）；WS-sender 尾随此表。
- `last_event_seq` 续跑时 `SELECT max(event_seq) WHERE trace_id=X` 派生，PauseMeta 不再存 `last_event_seq`。

### 3. HITL 断点元信息：`pause_meta` 表
```sql
CREATE TABLE pause_meta (
  session_id  TEXT PRIMARY KEY,
  trace_id    TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  pause_time  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
- `human_question`/`hint` 不存——从 checkpoint 的 interrupt payload 用 `aget_state(config)` 读。
- 闲置 30 min 自动清理（后台轻量 sweep 或惰性清理），到期失效。

### 4. 会话所有权：`session_owner` 表
```sql
CREATE TABLE session_owner (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, last_seen TIMESTAMPTZ NOT NULL DEFAULT now());
```

### 5. 并发执行锁：`session_locks` 表（跨请求持有）
```sql
CREATE TABLE session_locks (
  session_id     TEXT PRIMARY KEY,
  trace_id       TEXT NOT NULL,
  acquired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
  ttl_seconds    INT NOT NULL DEFAULT 900
);
```
- fresh `query`：`INSERT ... ON CONFLICT DO NOTHING`；若已存在且未过期 → `LOCK_EXIST`。
- HITL 暂停期间锁**不释放**：run 在 checkpoint 暂停、无活动进程，但逻辑锁行留存；续跑复用现有锁行（不再 INSERT，故续跑不会误触 `LOCK_EXIST`）。
- `stream_finish` / `abort` → 删除锁行。
- 心跳：运行中翻译层每写一批 `trace_events` 即更新 `last_heartbeat` 以表存活；HITL 暂停期不写心跳（由 `pause_meta` 的 30min TTL 统一管辖，见 §9.6）。
- 不用 `pg_advisory_lock`（连接级，HTTP 请求返回即随连接释放，撑不过 HITL 暂停）。

### 6. 会话注册表 + 上限
- 活跃会话数 = `session_owner` 中 `last_seen` 近 30 min 的计数。
- 达上限（默认 100）且无法淘汰 → 新会话返回 `SESSION_LIMIT`。

## 4.3 一期能力边界
### 支持
- 前端全量可视化：CoT 流式渲染、节点状态、中间产出、流程图动态展示。
- 单页面 HITL 人机交互、断点续跑；刷新/退出后重连即从 `trace_events` 恢复。
- WS 时序防乱序、事件隔离、执行中断通知。
- 单用户多对话窗口隔离；一套 API 兼容前端直调、Java 转发。
- `trace_events` 完整采集，回放与真实执行流程完全匹配（同源同词汇）。
- 会话所有权强制校验，跨用户访问拒绝。
- 进程重启后 session/断点/事件日志仍在。

### 一期不支持（二期补齐）
- 多实例水平扩容的 WS 事件扇出（数据层已通，事件层靠 `LISTEN/NOTIFY`，见 §4.4）。
- HITL 结构化 `ops` 编辑（一期仅 `action` + 自由文本，见 §7.2 / §8.2）。
- 全套安全（JWT/用户中心/mTLS，见 §3 / §12）。

## 4.4 二期扩展规划
1. 多实例水平扩容：数据层 Postgres 已共享；事件层用 Postgres `LISTEN/NOTIFY` 跨实例扇出（哪个实例持 WS 连接，哪个实例 LISTEN 本会话事件并下发）+ 连接持有实例注册表。
2. Java 业务服务接管 Nginx 信任边界（鉴权后注入 `X-User-Id`，HTTP 透传；WS 仍前端直连 Python）。
3. HITL 结构化 `ops` 编辑（`human_response` 允许 JSON ops，纯函数不变）。
4. 全套安全（JWT/用户中心/mTLS）。

---

# 五、HTTP 统一接口规范（永久不变，兼容前后端调用）

## 5.1 基础信息
- **发起对话/续跑**：`POST /api/agent/run`
- **获取图结构**：`GET /api/agent/graph`
- 鉴权请求头：`X-User-Id`（一期 Nginx 注入，Python 信任）；二期补 `X-Token`。
- 全局 HTTP 超时：覆盖一次图分段（initial→首 interrupt，或 resume→下个 interrupt/finish），默认 120s；Graph 内部执行超时单独配置。`/api/agent/run` 阻塞到终止态（NEED_HUMAN_INPUT / SUCCESS / FAILED）才返回。
- 接口用途：统一承载普通对话发起、HITL 人工回填续跑，不新增额外路由。

## 5.2 `/api/agent/run` 请求入参
```json
{
  "session_id": "string 必填，会话窗口唯一标识（外部下发）",
  "query": "string 可选，用户原始提问，普通对话场景传入",
  "human_response": "string 可选，HITL 断点人工回复（一期=自由文本），续跑场景传入",
  "biz_trace_id": "string 可选，上层 Java 业务链路 ID，仅透传日志"
}
```
- `query` 与 `human_response` 互斥，接口层强制校验拦截（`PARAM_ERROR`）。
- fresh-run 判定：该 `session_id` 无活跃 `pause_meta` 且 `query` 非空 → mint 新 `trace_id`。
- resume 判定：该 `session_id` 有活跃 `pause_meta` 且 `human_response` 非空 → 复用 `pause_meta.trace_id`，`Command(resume=human_response)` 续跑。

## 5.3 `/api/agent/run` 返回体固定结构
```json
{
  "code": "SUCCESS | NEED_HUMAN_INPUT | FAILED",
  "msg": "通用状态描述文本",
  "data": {
    "final_content": "流程最终回答，仅 code=SUCCESS 存在",
    "human_question": "人机交互提问文本，仅 code=NEED_HUMAN_INPUT 存在",
    "hint": "用户输入提示，仅 code=NEED_HUMAN_INPUT 存在"
  },
  "error_info": {
    "error_code": "细分错误枚举",
    "detail": "异常详情，仅 code=FAILED 展示"
  }
}
```
- `NEED_HUMAN_INPUT` 判定来自 `graph.aget_state(config)`：`state.next` 含 hitl 节点且 `state.tasks` 带 interrupt。
  与 WS 的 `human_pause` 同源判定，杜绝「WS 说暂停、HTTP 说成功」的竞态。
- `human_question`/`hint` 从 checkpoint 的 interrupt payload 读取，不另存。

### 细分错误码枚举
| 错误码 | 含义 |
|--------|------|
| `LOCK_EXIST` | 会话正在执行/存在未处理 HITL 断点，禁止重复提交 |
| `PAUSE_EXPIRED` | HITL 断点闲置超时失效 |
| `GRAPH_TIMEOUT` | 智能体执行超时 |
| `PARAM_ERROR` | 入参格式非法、参数互斥冲突 |
| `FORBIDDEN` | 会话不属于当前用户 |
| `SESSION_LIMIT` | 活跃会话数达到上限 |

## 5.4 `GET /api/agent/graph`
- 用途：返回静态节点和边拓扑，供前端初始渲染骨架。含 `hitl1→parse+partition` 条件回放边。
- 节点含可见性 + 展示元数据 + HITL 标注（见 §10.1 / §7.3）。
- 响应示例：
```json
{
  "code": "SUCCESS",
  "data": {
    "nodes": [
      { "id": "start", "label": "开始", "type": "system", "color": "#909399", "visible": true, "interrupt": false },
      { "id": "parse+partition", "label": "解析与分段", "type": "parse", "color": "#409EFF", "visible": false, "interrupt": false },
      { "id": "hitl1", "label": "分段确认", "type": "hitl", "color": "#E6A23C", "visible": true, "interrupt": true }
    ],
    "edges": [
      { "source": "start", "target": "parse+partition" },
      { "source": "hitl1", "target": "parse+partition", "cond": "replay", "max": 3 }
    ]
  }
}
```
- `visible=false` 的节点不出现在 `nodes`；其前后可见节点补直连边；回放边若收缩成自环则丢弃（执行照跑）。
- 节点名当不透明字符串处理（含 `+`，勿拆分）。

---

# 六、WebSocket 实时事件规范（可视化核心）

## 6.1 连接地址与维持
- 连接 URL：`wss://域名/ws/agent/stream?session_id={session_id}`
- 鉴权：建连校验 `X-User-Id` 与 `session_id` 归属，失败关闭（close code 4001）。
- 连接唯一性：同一 `session_id` 仅允许一条 WS（一期单实例）；新连接建立时旧连接被关闭（服务端停止向其下发，不触发 run abort）；新连接从 `trace_events` 按 `event_seq` 回放接续。
- 心跳：服务端每 30s，若 30s 内未发数据帧则发 `heartbeat`（`event_seq=-1`），前端丢弃。
  静默期 90s 无消息视为连接异常，前端主动重连。

## 6.2 背压与流控（仅 WS-sender 侧，不阻塞图）
- WS-sender 内 `asyncio.Queue`（最大 256）缓冲待发事件。
- 队列满：`llm_thinking` token 合并到队列末尾同类事件；其余事件因已在 `trace_events` 落库，live 丢弃安全（重连必补）。
- 关键事件（`human_pause`/`stream_finish`/`stream_abort`）durable 在 `trace_events`，永不真正丢失。
- `/metrics` 暴露 `ws_event_queue_size`、`ws_event_queue_full_total`。

## 6.3 单条消息统一结构
```json
{
  "session_id": "会话 ID",
  "trace_id": "单次链路 ID",
  "event_seq": "数字，同 trace 内自增时序序号，heartbeat 为 -1",
  "event_type": "事件类型",
  "payload": "对应事件数据对象"
}
```

## 6.4 全生命周期事件定义
- `graph_static`：连接建立后首推（`event_seq=-1`），含图静态结构（来自 `MANIFEST`，§5.4），前端据此渲染骨架。
- `trace_start`：新 trace 开始（fresh-run），前端清空动态渲染状态，payload 空。
- `node_start`：节点开始执行，payload 含 `node_id`、`node_instance`（本 trace 内该节点第几次触发，从 0 起，区分回放环）、`label`、`type`、`color`、`input`。
- `llm_thinking`：CoT 增量文本，payload 含 `node_id`、`token`、`full_thought`。
- `tool_call`：工具调用，payload 含工具名、入参、返回。
- `node_output`：节点中间产出，payload 含 `node_id`、`node_instance`、`output`。
- `node_end`：节点结束，payload 含 `node_id`、`node_instance`。
- `human_pause`：触发 HITL，payload 含 `node_id`、`question`、`hint`（来自 checkpoint interrupt payload）。
- `stream_finish`：全链路正常结束，payload 空。
- `stream_abort`：链路异常终止（锁 TTL 孤儿/PauseMeta TTL 孤儿/显式 cancel），payload 含 `abort_reason`。WS 断开不触发。
- `heartbeat`：保活，`event_seq=-1`，前端丢弃。

## 6.5 前端强制同步处理逻辑
1. 收到 `graph_static` 渲染静态骨架（仅可见节点）。
2. 收到 `trace_start` 清空动态数据，只处理当前 trace 事件。
3. 记录当前最大 `event_seq`，丢弃序号小于该值的滞后消息。
4. 收到 `stream_abort` 停止等待，展示「执行中断」。
5. 切换会话/刷新：断开 WS、销毁本地缓存；重连后按 `event_seq` 从 `trace_events` 回放当前 trace 到最新再接 live。
6. 忽略 `heartbeat`。

---

# 七、前端工作台页面交互规范（无弹窗，单页面一体化）

## 7.1 页面：智能体可视化工作台
四大区域，无跳转、无弹窗。
### 区域 1：顶部会话管理栏
1. 新建对话：前端生成新 `session_id`，清空缓存、重连 WS。
2. 历史会话列表：当前用户所有存活 session（`session_owner` 近 30 min）；点击切换、重连 WS、按 `trace_id` 列出该会话各轮（多轮模型，§2.1）。
3. 状态栏：空闲/执行中/待人工输入。
4. 风险提示浮层：「会话已持久化，但请勿在执行中关闭未保存输入」。

### 区域 2：左侧智能体流程图
1. 骨架来自 `graph_static`（`MANIFEST` 内省），节点状态由事件动态更新。
2. 节点状态：未执行/运行中/已完成/待人工输入/执行中断。
3. HITL 节点高亮「待输入」。
4. 回放环：同节点多次触发（`node_instance`）以角标「×N」或执行栈展开。
5. 点击节点 → 中间面板加载该节点（按 `node_instance`）输入、完整 CoT、中间产出。

### 区域 3：中间推理详情面板（双 Tab）
#### Tab1 实时推理（默认）
1. 按 `node_id`+`node_instance` 分组流式 CoT，增量 token 打字机渲染。
2. 节点下展示中间产出。
3. HITL 暂停：面板底部**嵌入式交互卡片**（不遮挡流程图）：机器提问、输入提示、文本输入框、提交按钮；提交后卡片销毁。
#### Tab2 历史回放
1. 查询 `trace_events`（按 `session_id`+`trace_id`+`event_seq`），拉取该 trace 全部事件。
2. 100% 复用实时渲染组件，按 `event_seq` 顺序复现 CoT、节点流转、人机交互。
3. 回放源即实时流同源同表，天然「与真实执行流程完全匹配」。

### 区域 4：底部对话输入区
1. 空闲：文本框 + 发送，输入 `query` 调 `/api/agent/run`。
2. 执行中：置灰锁定。
3. HITL 暂停：底部锁定，仅上方嵌入式卡片提交 `human_response`（一期自由文本）。

## 7.2 HITL 完整交互流程（无弹窗，时序同步）
1. 发 `query`，建 WS，收 `graph_static`，Python mint `trace_id`，图开始，推 `trace_start`。
2. 图执行至 interrupt 节点（`hitl1`/`hitl2`），`aget_state` 检测到 interrupt → 推 `human_pause`（含 checkpoint interrupt payload 的 question/hint），HTTP 返回 `NEED_HUMAN_INPUT`。
3. 流程图标记节点「待输入」，中间面板渲染嵌入式卡片。
4. 用户提交自由文本，调 `/api/agent/run` 传 `session_id + human_response`。
5. Python 校验活跃 `pause_meta`，复用 `trace_id`，`Command(resume=human_response)` 续跑；服务路径解析自由文本 → `action`-only `Hitl*Decision`（空 `ops`），喂给未改动的纯函数。
6. WS 持续推后续事件，`event_seq` 从 `max(event_seq)+1` 顺延。
7. 完毕推 `stream_finish`，卡片销毁。

> 一期 `human_response` = 自由文本，仅承载 `action`（确认/打回/保守拒绝）+ 可选备注；结构化 `ops` 编辑推二期。

## 7.3 步骤可见性配置
1. 每节点 `visible` 标志，挂 `AgentEntry`（§10.1），缺省 `True`，单一源。
2. override 配置文件 `config/visibility.yaml`：`hidden: [parse+partition]`，部署时改、不重启代码；不做运行时热切。
3. HITL 节点（`hitl1`/`hitl2`）强制 `visible=True`，配置忽略并告警。
4. `visible=False` 节点：`trace_events`/WS 丢弃其 `node_*`/`llm_thinking`/`tool_call`；保留 trace 级事件；`graph_static` 边收缩。
5. 一期单 `visible` 旋钮，不引入「显节点但藏其 CoT」二级控制。

---

# 八、全场景业务流程

## 8.1 普通对话流程（无 HITL）
1. 前端生成/读取 `session_id`，建 WS，完成归属校验，收 `graph_static`。
2. 用户输入 `query`，调 `/api/agent/run`。
3. Python：无活跃 `pause_meta` → mint `trace_id`；`pg_advisory_lock` 加锁；`ainvoke` + `astream_events` 启动图；翻译层写 `trace_events`、mint `event_seq`；WS-sender 尾随下发。
4. 完成：更新 checkpoint、释放锁、推 `stream_finish`。
5. HTTP 返回 `code=SUCCESS` + 最终回答。

## 8.2 HITL 人机暂停 + 续跑流程
1. 图执行触发 `interrupt()`（`hitl1`/`hitl2` 节点）。
2. checkpointer 落盘 state + interrupt 暂停点；写 `pause_meta`（`trace_id`/`node_id`/`pause_time`）。
3. `aget_state` 检测 interrupt → 推 `human_pause`（question/hint 来自 interrupt payload），HTTP 返回 `NEED_HUMAN_INPUT`，释放执行线程（**锁不释放**——`session_locks` 行留存，由 `pause_meta` 30min TTL / 孤儿 TTL 兜底，见 §9.6）。
4. 用户提交 `human_response`（自由文本），Python 校验活跃 `pause_meta`。
5. `Command(resume=human_response)` 续跑，复用 `trace_id`；服务路径解析自由文本 → `action`-only `Hitl*Decision` 喂纯函数（纯函数不动）。
6. `event_seq` 从 `max(event_seq)+1` 顺延。
7. 完成删 `pause_meta`，推 `stream_finish`，释放锁。

---

# 九、异常场景完整处理方案

## 9.1 执行中刷新/关闭浏览器（WS 断开）
- WS 断开不中止 run（§1.3 不变量）。run 在服务端继续至完成或 HITL。
- 半截事件已落 `trace_events`；前端重连 → 按 `event_seq` 回放当前 trace 到最新 → 接 live。
- 若 run 已在断开期间到达 HITL：`human_pause` 已落库，重连即见卡片。
- 若 run 已完成：重连见 `stream_finish`。

## 9.2 HITL 断点闲置 30 min 超时
- 后台清理过期 `pause_meta`，释放锁。
- 再提交返回 `PAUSE_EXPIRED`，提示「交互已超时，请重新发起对话」。

## 9.3 同一窗口重复点击发送
- 锁检测会话存在未过期锁或有效断点 → 返回 `LOCK_EXIST`，提示「当前对话正在处理」。

## 9.4 多标签页同一 `session_id`
- 新连接关闭旧连接（不下发 `stream_abort`，仅停止下发）；新连接从 `trace_events` 回放接续。
- 前端规范：每标签页独立 `session_id`，从源头规避。

## 9.5 Python 服务重启/发布
- 内存态（advisory lock、WS 连接）清空；Postgres 数据（checkpoint/pause_meta/trace_events/session_owner）保留。
- 重启后 run 不自动续跑（进程内 `ainvoke` 上下文已丢），但 session/断点/事件日志仍在：用户重连可回放历史、若有活跃 `pause_meta` 可续跑 HITL。

## 9.6 执行锁超时 / 孤儿 run
- `session_locks.ttl_seconds` 覆盖一次完整 LLM run（默认 900s，避免误杀长 run）；运行中靠 `last_heartbeat` 续命。
- 后台扫描 `session_locks`：行 `last_heartbeat` 超过 TTL 时——
  - 若该 session 有活跃 `pause_meta` → 合法 HITL 暂停，**跳过**（由 `pause_meta` 30min TTL 管辖，§9.2）；
  - 若无活跃 `pause_meta` → 孤儿 run，中断图（cancel token），推 `stream_abort`，删除锁行。
- `pause_meta` 过期（30min）→ 删除 `pause_meta` + 对应 `session_locks` 行（HITL 超时，§9.2）。

## 9.7 活跃会话数超限
- `session_owner` 近 30 min 计数达上限且无法淘汰 → 新会话返回 `SESSION_LIMIT`。

## 9.8 WS 心跳丢失
- 90s 无消息（含 heartbeat）视为异常，前端主动重连；重连即恢复（§9.1）。

## 9.9 背压队列溢出
- `ws_event_queue_full_total` 持续增长 → 运维扩容/降速。
- 极端：live 丢 `llm_thinking`（仅留最终 `node_output`），前端提示「实时思考暂不可用」；关键控制事件在 `trace_events` 不丢。

---

# 十、底层 LangGraph 改造规范（零侵入业务纯函数）
侵入面 = gate seam + orchestrator 装配 + MANIFEST 展示元数据；业务纯函数不动。

## 10.1 MANIFEST 单一源 + 展示元数据 + 可见性
- `AgentEntry`（`assembly.py:721-742`）扩展可选字段：`label`、`node_type`、`color`、`desc`、`visible: bool = True`，缺省从 `name` 推导。
- `/api/agent/graph` 与 `graph_static` 从 `MANIFEST` + orchestrator 的 START/END 边内省生成，单一源、不漂移。
- HITL 节点（`hitl1`/`hitl2`）标注 `interrupt: true`，前端预渲染即知交互点。
- 节点名当不透明字符串（`parse+partition` 含 `+`，勿拆）。

## 10.2 事件采集翻译层
- `graph.ainvoke(input_or_Command_resume, config={"configurable":{"thread_id":session_id}, "callbacks":[langfuse_handler]})` + `astream_events(version="v2")`。
- 翻译层将 `astream_events` 词汇映射为 §6.4 WS 事件类型，mint `event_seq`，写 `trace_events`（非阻塞）。
- Langfuse handler 为并行外部 sink，与 `astream_events` 消费端共存，零冲突。
- `NEED_HUMAN_INPUT` 由 `aget_state` 判定，非事件。

## 10.3 LangGraph 执行约束
1. 图服务启动全局单例初始化，禁止每请求重建。
2. HITL 恢复仅用 `Command(resume=...)`，禁止直接改 State 字段。
3. 单 trace 生命周期内 `trace_id` 复用；Langfuse 以 `trace_id` 为 tag 关联多 invoke。
4. State 序列化由 `PostgresSaver` 序列化器承担（注意 `OriginalParagraphs` 为 slots + `MappingProxyType`，需验证其可序列化）。
5. `session_id` = checkpointer `thread_id`（消费 `orchestrator.py:309` 预留位）。

## 10.4 CLI 与服务共用一套机制（两个驱动者）
- 图只有一套：`interrupt()` + `PostgresSaver` + `ainvoke`。
- **服务**：HTTP/WS resume 驱动。
- **CLI**（`run_real.py` 改写）：本地 resume 循环——`ainvoke` → 检测 interrupt → 终端打印 question + 读输入 → `ainvoke(Command(resume=reply))` → 循环。
- gate seam 的 `review()` 拆为 `formulate_question()` + `parse_reply()`，服务注入 InterruptDrivenGate、CLI 注入 TerminalGate，均实现拆分后 Protocol。
- 测试重构：`FakeHitl*Gate` 注入式 e2e 测试改为「驱动图 + `Command(resume=fake_decision)`」形式（质量门 `mypy --strict + pytest` 必须保持绿）。

---

# 十一、可观测性与运维

## 11.1 健康检查与指标端点
- `GET /health` → `{db: ok, active_sessions, active_locks, ws_connections}`。
- `GET /metrics`（Prometheus）至少：
  - `active_sessions`、`active_locks`、`ws_connections`
  - `event_push_latency_seconds`（落 `trace_events` 到前端渲染）
  - `graph_execution_duration_seconds`
  - `ws_event_queue_size`、`ws_event_queue_full_total`
  - `langfuse_errors_total`（外部 sink 写失败计数，降级用）

## 11.2 日志与追踪
- 结构化 JSON 日志（stdlib + JSON formatter，不引新依赖），每条带 `session_id`/`trace_id`/`user_id`（脱敏后）。
- Langfuse 不可用时降级：仅本地记错，不阻塞对话。

## 11.3 回放可信源
- 回放源 = Postgres `trace_events`（durable、常开、自包含）；Langfuse 为可选外部 sink，非回放源。
- 资源监控：活跃会话数超 80% 阈值告警。

---

# 十二、二期 Java 业务服务接入兼容方案（Python 接口零修改）

## 12.1 Java 服务职责
1. 用户登录后生成/下发 `session_id`，存前端 localStorage。
2. 接收前端 HTTP，完整透传入参与请求头（`X-User-Id`、`biz_trace_id`），转发 `/api/agent/run`。
3. 原样返回 Python JSON，仅按 `code` 做业务侧日志/告警。
4. 接管信任边界：Java 鉴权后注入 `X-User-Id`，Python 信任；不得修改/丢弃 `X-User-Id`。
5. 不处理 WS：前端直连 Python，Java 不转发流式数据。

## 12.2 兼容保障
- Python HTTP 入参/出参永久不变，Java 转发无需改造。
- 可视化数据流直连 Python，Java 无需适配 CoT/节点/HITL。
- ID 体系、错误码、交互流程统一，Java 仅透传。

---

# 十三、开发验收 Checklist

## 13.1 存储与持久化
- [ ] `PostgresSaver` 接入，`session_id` 作 `thread_id`，进程重启后 session/断点仍在。
- [ ] `SessionCacheBase` 瘦身（无 `get_state`/`save_state`），side 表（pause_meta/session_owner）落地。
- [ ] `trace_events` 表落地，翻译层每事件写入，回放与实时同源同表。
- [ ] 执行锁用 `session_locks` 表（跨请求持有），`last_heartbeat` + TTL 兜底覆盖长 LLM run（默认 900s），HITL 暂停期由 `pause_meta` TTL 管辖不误杀。
- [ ] 闲置会话/断点 30 min 清理，会话数达上限拒绝。

## 13.2 安全
- [ ] HTTPS/WSS 全站；`X-User-Id` 由 Nginx 注入，Python 信任。
- [ ] 跨用户访问 session 拦截，403 / WS 4001。
- [ ] 脱敏钩子存在、可配置、默认关。
- [ ] （二期）JWT/用户中心/mTLS。

## 13.3 时序同步与不变量
- [ ] 翻译层只写 `trace_events`、非阻塞；WS-sender 尾随；WS 断开不中止 run（不变量验证）。
- [ ] `event_seq` 单 trace 自增；前端过滤乱序/滞后。
- [ ] `graph_static` 来自 `MANIFEST`，含 `hitl1→parse+partition` 回放边。
- [ ] `trace_start` 清空动态数据；HITL 续跑 `event_seq` 顺延无断层。
- [ ] 历史回放复用实时组件，与真实执行完全匹配。
- [ ] 静默 30s 仍收 heartbeat。

## 13.4 背压与流控
- [ ] LLM 生成超网络发送时队列满触发 token 合并，无事件丢失（关键事件在 `trace_events` 不丢）。
- [ ] `/metrics` 可观测队列深度与满次数。

## 13.5 HITL
- [ ] 嵌入式交互卡片，无弹窗。
- [ ] 一期 `human_response` = 自由文本 → `action`-only Decision；纯函数未改。
- [ ] 续跑复用 `trace_id`，Langfuse 按 `trace_id` tag 关联。
- [ ] 断点 30 min 超时失效，返回 `PAUSE_EXPIRED`。
- [ ] 执行中/待输入拦截重复提交，`LOCK_EXIST`。

## 13.6 多会话隔离
- [ ] 不同 `session_id` 数据/事件/Trace 隔离，无串会话。
- [ ] 新建对话生成新 `session_id`，状态重置。
- [ ] 切换会话重连 WS、按 `trace_id` 列轮次。

## 13.7 可见性配置
- [ ] `AgentEntry` 加 `visible`，override 文件生效，HITL 强制可见。
- [ ] `visible=False` 节点不出现在 `graph_static`、事件被翻译层过滤。

## 13.8 Java 对接兼容
- [ ] 一套 HTTP 接口支持前端直调与 Java 转发。
- [ ] WS 前端直连 Python，Java 不参与流式。
- [ ] 上层仅维护 `session_id`，不感知 `trace_id`/缓存/断点。

## 13.9 底层稳定性
- [ ] `astream_events` 翻译层采集全量节点/CoT/工具事件，无丢失。
- [ ] 页面关闭不残留僵尸 run（孤儿 TTL 兜底）。
- [ ] 事件全量落 `trace_events`，可作为展示不一致排查基准。
- [ ] `/health`、`/metrics` 正确返回。

---

# 十四、风险与产品提示文案

## 14.1 一期方案风险
1. 单实例：无法多实例负载均衡（二期 `LISTEN/NOTIFY` 解）。
2. 内存上限：在线会话过多占内存，靠 30 min 闲置清理 + 上限控制。
3. HITL 一期粒度：仅 `action` + 自由文本，不支持结构化编辑（二期补）。

## 14.2 前端提示文案
1. 执行中关闭未保存输入提示：「请勿在执行中关闭页面，未提交输入将丢失」。
2. HITL 超时：「交互已超时，请重新发起对话」。
3. 重复提交：「对话正在处理中，请稍后再试」。
4. 会话数达上限：「同时打开的对话窗口过多，请关闭其他窗口后重试」。
5. 权限拒绝：「无权访问此会话」。
6. 实时思考暂不可用（背压极端）：「实时思考文本暂不可用，可查看历史回放」。

---

**附录 A：HITL 交互关键时序示意（interrupt/resume + trace_events）**
```
前端                     Python服务                  LangGraph(PostgresSaver)
 |                          |                          |
 |-- WS connect (session) ->|  归属校验                 |
 |<-- graph_static ---------|  (MANIFEST 内省)          |
 |-- POST /run (query) ---->|                          |
 |                          |-- mint trace_id          |
 |                          |-- advisory_lock          |
 |                          |-- ainvoke + astream ---->|
 |<-- WS trace_start -------|  (tailing trace_events)  |
 |<-- WS node_start --------|                          |
 |<-- WS llm_thinking ------|                          |
 |                          |<-- interrupt() (HITL) ---|  checkpoint 落盘
 |                          |-- write pause_meta       |
 |                          |-- aget_state -> interrupt|
 |<-- WS human_pause -------|                          |
 |<-- HTTP NEED_HUMAN_INPUT |                          |
 |                          |  (锁不释放, 孤儿 TTL 兜底) |
 |                          |                          |
 |-- POST /run (human_resp)>|                          |
 |                          |-- 复用 trace_id           |
 |                          |-- ainvoke(Command(resume))>|
 |<-- WS llm_thinking ------|                          |
 |<-- WS node_end ----------|                          |
 |<-- WS stream_finish -----|                          |
 |<-- HTTP SUCCESS ---------|                          |
```

---
