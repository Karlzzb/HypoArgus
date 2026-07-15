# TASK-SA-2 — 真实检索适配器：映射 + daemon worker loop + manifest real 工厂 + 接线 + 离线测试

> 状态：未开始
> 阻塞：Slice 0（vendored 可导入）、Slice 1（`RetrievalFn` 已 5 输入）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Q1/Q4/Q5/Q6/Q7/Q8/Q9、§Solution、§Testing Decisions）
> 目标会话：任意新 session 读取本文件并执行。**本切片是整条迁移的核心**。

## 任务概述

把 SearchAgent V12 作为真实检索 provider 接入 retrieval seam——填 manifest 中 retrieval 条目的 `real=None` 空位，与 judgment 同形管理（`real=` 工厂 + `RealDeps` 注入）。子智能体以 `with_llm=False` 跑（确定性 `DeterministicEvidenceJudge`、零 LLM 成本、judge 仍嵌 flow 不可跳过）；**丢弃** `TaskDecision.verdict`；judgment 节点照旧吃 `citations` 经 `QwenJudgmentLlmClient` 重判终态。

子智能体的 HTTP/LLM/KB client 由适配器自持的一条专用长驻 daemon worker event loop 承载（loop-affine client 不跨 loop 炸、跨请求复用），retrieval 节点（同步 `NodeFn`、LangGraph threadpool、无 loop）经 `asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), worker_loop).result(timeout)` 同步桥接——`NodeFn` 同步签名不动。子智能体产出的 `CitationRecord` 经适配器映射为框架 `Source`，按 `item_id` 落 `citations` channel。出网查询经框架 `redact_query` 脱敏；每条 citation 带可溯源 `origin`/`locator`。

## 验收标准

### 适配器与映射（PRD §Q6）

- [ ] 实现 `RetrievalFn`（5 输入）的真实 adapter，位于 seam 侧 `src/agents/retrieval/` 子包（补齐 ADR-0014 子包结构：`contract.py`/`agent.py`/`__init__.py`，三模块全量 strict/ruff）。
- [ ] `CitationRecord → Source` 映射按下表（PRD §Q6 原样）：

  | `Source` 字段 | ← `CitationRecord` |
  |---|---|
  | `source_id` | `citation_id` |
  | `kind` | `source_type`（`WEB→network` / `KNOWLEDGE_BASE→knowledge_base` / `STRUCTURED_DATA→structured`） |
  | `origin` | `source_name` |
  | `title` | `title` |
  | `snippet` | `content`（**非 `summary`**） |
  | `locator` | `url` |

- [ ] `snippet ← content`：V12 `content` = `" ".join(quoted_spans)[:600]`（judge 从原文抽的真实证据片段，本就是 snippet 语义）；`summary` 是关系模板句、零证据原文，**不喂给 judgment**。
- [ ] 全映射（ACCEPTED + DEGRADED）：`status="DEGRADED" if snippet_only else "ACCEPTED"`，两者都映射、都带 `content`、都绑 claim；`Source` schema 无 status 字段故全映射让 judgment 按内容自加权。非"拒"的 citation 全落 `citations`。
- [ ] 映射 key = `item_id`（forward→`argument_id`，reverse→`hypothesis_id`，来自 `CitationRecord.task_ids` → task → `item_id`）。
- [ ] `Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约不动。

### Payload 构造（PRD §Q2/Q7/Q8/Q9）

- [ ] forward `ForwardItem.target_text` = 该段 `ParagraphRecord.original_content`（`Argument` 无文本字段是 ADR-0025 代价；同段多节点共享同 target_text，靠 `item_id=argument_id` + 空 `required_slots` 区分，保留 per-argument 粒度）。
- [ ] `required_slots` 传空 list（PRD §Q7：两处空 list 不阻断 citation 产出，发射闸是 scope_compatible/confidence/directness/完整事实句/blocker，不查 required_slots）。
- [ ] `argument_context`/`argument_path` 传空 `ArgumentContext()`（PRD §Q8：`Argument` 无祖先文本字段）。
- [ ] id 映射（PRD §Q9）：`request_id ← session_context.session_id`（空则 mint uuid 兜底）；`document_id ← "doc-" + blake2b(original_doc, digest_size=12).hexdigest()`（内容指纹、确定性、跨段/跨 resume 稳定、只 hash 串不外泄原文，适配器读 `state["original_doc"]`）；`user_id ← session_context.user_id or None`（可选，不兜底）。
- [ ] 合规重承载（PRD §Q5）：构造 V12 payload 前对 forward/reverse 的 `target_text`/`paragraph_text` 跑框架 `redact_query`（V12 不做、框架有现成纯函数）；`Source.origin`←`source_name`、`locator`←`url` 使每条 citation 可审计。

### daemon worker loop + manifest 工厂（PRD §Q4）

- [ ] 适配器自持一条专用长驻 daemon worker event loop（独立线程 `loop.run_forever()`），`real` 工厂在 worker loop 上建单例 `SearchAgentRuntime.from_env(with_llm=False)`（四个 httpx client 全绑 worker loop）。
- [ ] retrieval 节点（同步 `NodeFn`、threadpool、无 loop）经 `asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), worker_loop).result(timeout)` 同步桥接；`NodeFn` 类型不动。
- [ ] spine（长驻 uvicorn）只建一次 agents → runtime 是进程级单例、跨所有请求复用、无 per-request 泄漏、无 loop-bind 炸点。
- [ ] 关闭：worker 线程作 daemon，进程退出即死、OS 回收 socket（与既有 `build_qwen_chat_model()` 不 `aclose`、spine `finally` 仅 cancel sweep 的先例同形）；可选在适配器内挂 `atexit` 尽力 `aclose()`（**住在适配器、不碰框架**）。
- [ ] MANIFEST retrieval 条目 `real=None` → `real=lambda d: partial(_real_retrieval, runtime=d.retrieval_runtime)` 形（与 judgment 的 `partial(judge_and_adjudicate, llm=d.judgment_llm)` 同形）。
- [ ] `RealDeps` 增 `retrieval_runtime: SearchAgentRuntime | None = None` 字段（与 `judgment_llm: ... | None = None` 同形）；`create_real_agents` 增对应 kwarg，当传入时 manifest 工厂替换 stub。

### 接线（PRD §Solution、§定位线索）

- [ ] `create_real_agents(...)` 支持 `retrieval_runtime` 注入。
- [ ] `runtime/run_real.py`：`run_real_pipeline` 与 `arun_real_pipeline` 在装配时建并传入 retrieval runtime（CLI 一次性路径同样可用真实检索，PRD story 14）。
- [ ] `api_layer/server.py`：`_real_agents()` 建并传入 retrieval runtime（spine 进程级单例）。
- [ ] 真实后端未配置/未触达时降级回 stub 行为（空 citations），tracer bullet 守住。

### 离线测试（PRD §Testing Decisions，全部不联网、不调 LLM）

- [ ] **映射测**：给定 canned `SearchAgentOutputState`（或伪 runtime），断言映射出的 `Source` 列表形状正确（字段映射、key=item_id、ACCEPTED+DEGRADED 全映射、`snippet=content`）。
- [ ] **桥接测**：同步 `RetrievalFn` 在"调用方无运行 loop"（threadpool 模拟）下经 worker loop 正确拿到 async runtime 结果。
- [ ] **节点测**：注入真适配器（背伪 runtime）到 `Agents`，跑 retrieval 节点，断言 `citations` channel 写入正确、`paragraph_list` 被读用于 `target_text`。参考既有 `tests/test_orchestrator_e2e.py` 的 `replace(base, retrieval=fake_fn)` 注入式范式。
- [ ] **tracer bullet 测**：无触达段终稿逐字节等于原文（真实后端未配置/未触达时既有契约不破）。
- [ ] seam 数 = 1（`RetrievalFn` Protocol），不新增测试 seam；伪 runtime 在该 seam 注入。

## 实现指引（决策已锁定，勿偏离）

### Q1 — 挂 retrieval seam（否 replace-judgment 拓扑）

- 接 retrieval seam（填 manifest `real=None`）。judgment 节点 + 其内串联的 merge/impact/consistency 纯函数**原样不动**（ADR-0019：judgment 是检索后唯一整树写者；子智能体不复制 12 格矩阵 / invalid 传导 / issue_tags）。
- `with_llm=False` 跑、丢弃 `verdict`、judgment 的 `QwenJudgmentLlmClient` 照旧吃 `citations` 判终态——无双倍 LLM 成本、judgment 仍是唯一裁决者。
- 否掉"复用 verdict"（需新增 state channel 传 verdict = 改框架，或 judgment 再调一次子智能体 = 双倍调用）。

### Q4 — loop-affine 纠偏（关键，勿用 `asyncio.run`）

V12 的 `VolcanoWebSearchClient`/`BishengRetrieveClient` 在 `from_env` 构造期就建 `httpx.AsyncClient`，**loop-affine**（首次请求绑当时 loop，换 loop 用即 `attached to a different loop`）。故"singleton runtime + 每次节点调用 `asyncio.run(runtime.ainvoke(...))`"**会坏**：spine 里 `create_real_agents` 跑在 uvicorn loop、client 绑之；retrieval 节点在 LangGraph threadpool worker 无 loop、`asyncio.run` 开新 loop → 炸。正典是 `run_coroutine_threadsafe` + 专用 worker loop（非 `asyncio.run`）。参考既有 `runtime/run_real.py` 的"同步节点丢 threadpool、`asyncio.run`/checkpointer `async with`"模式，但桥接走 `run_coroutine_threadsafe`。

### Q5 — 合规重承载（domain whitelist 作废，已记录 PRD §6 偏差）

- **domain whitelist 作废**：Mock 白名单本就是占位、真实后端预期就是开网事实验证。V12 的 Volcano 是开放式全网检索（无域名白名单）；V12 scope_guard 做语义 scope（年份/地域/主体/指标/单位，词表是具身智能/人形机器人行业专属），**非域名白名单**。作为已记录偏差写进 ADR-0026 + CONTEXT（Slice 4）。
- **PII 脱敏重承载**：适配器构造 payload 前对 `target_text`/`paragraph_text` 跑框架 `redact_query`（V12 `tracing.redact` 仅 trace、非出网查询）。
- **可溯源审计重承载**：`Source.origin`/`locator` 从 `source_name`/`url` 填。
- **结构化**：靠 V12 自承（`StructuredQueryClient` 头注 Scenario-only、`_SQL_PATTERN` 正则拦 SQL、配 `SCENARIO_REGISTRY`），框架层结构化校验对此真实后端 N/A。
- **KB**：靠 Bisheng 服务端鉴权（cookie/token、按 knowledge_id 圈定），弃框架占位 `authorized_users` 闸。
- 否掉 route-through（V12 是自洽子图、非 `RetrievalLayer` 实现，大改换不来收益）。

### Q6/Q7/Q8/Q9 — 见验收标准与上表。

### 子智能体侧契约（vendor 源码，不改）

- `SearchAgentRuntime.from_env(with_llm=False)` / `.ainvoke(payload) -> dict`（**async**）/ `.aclose()` / async context manager。
- `load_env()` 在子智能体包 `__init__` 时执行（读自己 `.env`，env 命名 `LLM_*`/`VOLCANO_*`/`BISHENG_*`/`LANGFUSE_*`，与主 `DASHSCOPE_API_KEY` 不冲突）。
- `SearchAgentInputState`：`request_id`/`document_id`（min1）/`user_id`（可选）/`paragraph: ParagraphInput`（`paragraph_id`/`paragraph_text` 必填非空/`forward_items`/`reverse_items`/`argument_context`）/ 可选 `organization_context`/`knowledge_context`/`retrieval_policy`/`trace_context`。
- `SearchAgentOutputState`：`paragraph_id`/`run_status`/`results: list[TaskDecision]`/`citations: list[CitationRecord]`/`warnings`/`trace`。
- `CitationRecord` 字段：`citation_id`/`task_ids`/`content`(min1)/`summary`(min1)/`title?`/`source_type`(WEB|KNOWLEDGE_BASE|STRUCTURED_DATA)/`source_name`/`url?`/`relation`(SUPPORT|REFUTE|SUPPLEMENT)/`status`(ACCEPTED|DEGRADED)/`judgment`/`provenance`。
- `ForwardItem`/`ReverseItem`：`item_id`(min1)/`target_text`(min1 必填非空)/`required_slots`(默认空 list)。

### 定位线索（来自探索，非 brittle 承诺）

- MANIFEST retrieval `real=None`：`src/agents/assembly.py:848-859`；judgment `real=` 工厂同形参照：`src/agents/assembly.py:860-875`。
- `RetrievalFn` Protocol（Slice 1 后 5 输入）：`src/agents/assembly.py:165-182`。
- `_retrieval_node`：`src/agents/assembly.py:625-655`（写 `citations` channel，`_merge_dict` reducer，`_guarded` 兜底空 citations）。
- `RealDeps`（待加 `retrieval_runtime`）：`src/agents/assembly.py:753-762`；`create_real_agents`：`src/agents/assembly.py:923-980`。
- `Source` schema / `RetrievalKind` / `redact_query`：`src/infra/retrieval.py:53-66,45-50,215-223`；`_MockRetrievalLayer` 确定性范式参照：`src/infra/retrieval.py:246-317`。
- judgment 真实 adapter 同层先例：`src/infra/llm_adapters.py:492-535`（`QwenJudgmentLlmClient`，在 infra、不在 agents）。
- 接线点：`src/runtime/run_real.py:79-125`（`run_real_pipeline`）、`:300-360`（`arun_real_pipeline`）、`src/api_layer/server.py:113-160`（`serve` + `_real_agents()`）。
- 既有注入式测试范式：`tests/test_orchestrator_e2e.py:199-243`（`recording_retrieval`/`replace(base, retrieval=...)`）、`:431-500`（`FakeJudgmentLlmClient` + `create_real_agents(judgment_llm=...)` 注入）；`tests/test_orchestrator_fallback.py:158-181`（throwing retrieval → `_guarded` 空兜底）。
- vendored 源落点与导入方案：见 Slice 0 记录。

## 质量门

```bash
conda run -n HypoArgus ruff check src tests
conda run -n HypoArgus mypy --strict src
conda run -n HypoArgus pytest -q          # 含本切片离线测试；真实联网走 Slice 3 的 real_llm 标记
```

## 状态追踪

| 日期 | 会话/执行者 | 状态变更 | 备注 |
|---|---|---|---|
| _ | _ | → 进行中 | _ |

## 完成检查

- [ ] 验收标准全勾选。
- [ ] 质量门全绿（离线测试覆盖映射/桥接/节点/tracer-bullet）。
- [ ] `real=` 工厂 + `RealDeps.retrieval_runtime` 与 judgment 同形；`NodeFn` 同步签名未动。
- [ ] daemon worker loop 落地、loop-affine 炸点消除。
- [ ] 更新 `INDEX.md` 状态为 `已完成`（解锁 Slice 3、Slice 4）。
