# 可视化服务以 interrupt + PostgresSaver 落地异步 HITL

## Status
accepted (2026-07-14)

## Context
代码当前 HITL 是同步注入 gate（`CliHitl1Gate` / `CliHitl2Gate`，`review()` 阻塞返回 `Hitl*Decision`），`interrupt()` / `Command(resume=...)` / checkpointer 在 `orchestrator.py:21-23` 等 8 处被显式标注为「后续切片」，`graph.compile()`（`orchestrator.py:294`）无 checkpointer。
可视化服务要求异步 HITL（HTTP `NEED_HUMAN_INPUT` → 人工回填 → resume，复用 `trace_id`、顺延 `event_seq`），而 LangGraph 只能靠 `interrupt()` + checkpointer 中途暂停——同步 gate 无法暂停等 HTTP 回复。
同时产品要求「退出后继续能找到会话」，纯内存无法满足。

## Decision
提前消费代码标注的「后续切片」：`hitl1` / `hitl2` 改用 `interrupt()` + `Command(resume=...)`，`graph.compile(checkpointer=PostgresSaver)`，`thread_id = session_id`（消费 `orchestrator.py:309` 预留位）。
零侵入边界精确化为「业务纯函数（`confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document`）不动；侵入面 = gate seam + orchestrator 装配 + MANIFEST 展示元数据」。
checkpointer 选 `PostgresSaver`（LangGraph 核心维护、与 LangGraph 协议 lockstep、只需一个 Postgres）。

## Considered Options
- `MemorySaver`：纯内存，进程重启即丢，不满足「退出后续跑」。
- `RedisSaver`（`langgraph-checkpoint-redis`，Redis Inc. 官方、生产可用）：但需 Redis 8/Stack（带 RedisJSON + RediSearch 模块）、0.x 跨组织（`redis-developer`，非 `langchain-ai`），对单实例 checkpoint 无 Postgres 给不了的好处。
- `SqliteSaver`：落盘半持久（state 持久、side-meta 仍在内存、锁无 advisory lock 可用）。
- `PostgresSaver`：核心维护、全持久；执行锁用 `session_locks` 表行（跨请求持有、HITL 暂停期留存），无需手搓 TTL dict + 后台 sweep 线程。

## Consequences
- 进程重启后 session / 断点仍在，「退出后续跑」一期成立（PRD §4.3「无法恢复」类限制删除）。
- 执行锁用 `session_locks` 表行（跨请求持有、HITL 暂停期留存），孤儿 run 由 `last_heartbeat` TTL 兜底（不用 `pg_advisory_lock`，因其连接级、撑不过 HITL 暂停）。
- 二期无需 Redis；持久化与跨实例事件扇出均由 Postgres 承担。
- `OriginalParagraphs`（slots + `MappingProxyType`）需验证 `PostgresSaver` 序列化器可处理。
- 一期 HITL `human_response` 仅承载 `action` + 自由文本（结构化 `ops` 编辑推二期），纯函数仍只消费 `Hitl*Decision` 不变。
- 测试需重构：`FakeHitl*Gate` 注入式 e2e 改为驱动图 + `Command(resume=fake_decision)`（质量门 `mypy --strict + pytest` 必须保持绿）。
