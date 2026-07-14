# HTTP 控制面：FastAPI + fresh-run `document` 字段

## Status

accepted (2026-07-14)

## Context

T-04 落地 `src/api_layer/` 的 HTTP 控制面（`POST /api/agent/run` + `GET /api/agent/graph` +
会话所有权 + `session_locks` + `pause_meta`）。落地前需收敛两个任务文档留下的待定项：

1. **Web 框架选型**。任务文档明确「Web 框架选 async-native（推荐 FastAPI/Starlette……选型记入
   ADR 或 PRD §1.5 待定项收敛）」。控制面的核心驱动是 ``ainvoke`` / ``aget_state`` /
   ``Command(resume=...)``（均 async），HITL 暂停 / 续跑 over HTTP 是请求级 async I/O，且 T-05
   起的翻译层（``astream_events``）与 T-06 的 WS 都需要 async-native 框架。
2. **fresh-run 的文档来源**。任务文档列出的 ``/api/agent/run`` 入参为 ``session_id`` / ``query``
   / ``human_response`` / ``biz_trace_id``，**未含文档本体**。但 ``Orchestrator.run_with_report``
   要求 ``original_doc: bytes``（与 ``session_context`` 同入 START、全链只读）——业务纯函数
   （``confirm`` / ``confirm_partition`` / ``resolve_rewrites`` / ``assemble_final_document``）
   零改动铁律下，HTTP 层必须**自行** sourcing ``original_doc``；否则 fresh run 无法发起、
   验收「发起 → NEED_HUMAN_INPUT → 回填 → SUCCESS」的集成测试无从驱动。

## Decision

1. **Web 框架选 FastAPI**（基于 Starlette + pydantic v2）。
   - async-native：原生 ``async def`` 路由直驱 ``ainvoke`` / ``aget_state``，不经 sync-bridge。
   - pydantic v2 请求 / 响应模型与仓内既有 ``BaseModel`` 契约（``Hitl*Reply`` / ``Hitl*Question``）
     同源，``with_structured_output`` 链路无阻抗。
   - T-06 WS 由 ``starlette.websockets`` 承载（同栈，无第二框架）。
   - 不引入 Flask / Django（sync，需 ``run_in_threadpool`` 桥接 async 图，反复杂）。
2. **fresh-run 请求体新增 ``document: str`` 字段**（resume 路径不消费、可缺省）。
   - ``query`` 仍为修订提示词（写入 ``session_context.user_prompt``，与 ``original_doc`` 同入 START）。
   - ``document`` 为待修订文档本体；HTTP 层 ``document.encode()`` 成 bytes 喂入
     ``Orchestrator``，**不触碰任何业务纯函数**。
   - resume 时 ``original_doc`` 已在 checkpoint（fresh run 时入 START），无需重喂；故 ``document``
     仅 fresh 路径必填、resume 路径忽略。

## Considered Options

- **Starlette 裸用**：可行但需手搓请求模型校验 / OpenAPI / 依赖注入；FastAPI 在其之上加的正是
  这些，且不增运行时负担（同 pydantic v2 栈）。选 FastAPI。
- **Flask + async threadpool**：sync 框架桥接 async 图需 ``run_in_threadpool`` 或事件循环嫁接，
  与 ADR-0022 的 async spine 相悖。否决。
- **fresh-run 不带 document、改由独立「文档注册」端点预登记**：增第二个端点、增 session 与
  文档的绑定态、增 fresh-run 前置依赖；本切片「一个 curl 脚本发起完整 HITL 流程」验收更难。
  ``document`` 内联请求体最简、最可测。否决独立注册端点（二期需要时可再加，不影响本切片契约）。
- **把 document 塞进 query**：语义错（``query`` = 修订提示词，见 CONTEXT「会话上下文」），且
  破坏 ``session_context.user_prompt`` 单一语义。否决。

## Consequences

- ``pyproject`` 增 ``fastapi`` / ``uvicorn`` / ``langgraph-checkpoint-postgres`` / ``psycopg`` /
  ``psycopg-pool`` 依赖（conda ``HypoArgus`` 已装可跑）；dev 增 ``httpx``（ASGI 传输集成测试）。
- ``SessionCacheBase`` 为 side-meta 抽象基类（``pause_meta`` / ``session_owner`` / ``session_locks``），
  **不含** ``get_state`` / ``save_state``（state 是 checkpointer 契约，ADR-0022）。两个 adapter
  （``InMemorySessionCache`` 单测 + ``PostgresSessionCache`` 生产）使之成为真 seam。
- ``GET /api/agent/graph`` 复用 T-02 ``build_graph_view``，HTTP 层不另写拓扑（单一源不漂移）。
- T-06 接 WS 时复用本切片的 ``aget_state`` 判定（NEED_HUMAN_INPUT 同源，ADR-0023 不变量）。
- ``document`` 字段是本切片相对任务文档入参清单的唯一显式增量；二期若 PRD §5 收敛出独立文档传输
  契约，可据此再调整（不破坏 fresh/resume 二态判定）。
