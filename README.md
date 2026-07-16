# HypoArgus

论证驱动型文档修订多智能体系统：纯文本进、修订后的终稿出。
系统把一篇文档解构为论证树，对每个论点取证（检索 + 裁决）、开药（候选修订假说）、按段提议重写，最后经人确认（HITL-2）拼装终稿。

本文件是整项目当前已实现工作的总结入口，便于后续接手者快速建立全景。
术语定义见 `CONTEXT.md`；模块边界与装配见 `docs/DEVELOPMENT.md`；测试分层见 `docs/TESTING.md`；状态树字段流向见 `docs/STATE.md`；架构决策见 `docs/adr/`。

## 1. 系统定位

HypoArgus 把「文档修订」建模为**论证状态机上的有向流水线**，而非自由文本生成。
核心承诺贯穿全链：**无任何采纳改动时，终稿与原始输入逐字节完全一致**（含空行 / 缩进 / 换行 / 末尾空格）。
这条 tracer-bullet 不变量是所有 stage 的共同断言，任何破坏它的改动会被参数化样例立即捕获。

控制流落边（代码）而非 prompt 散文：流水线是一张 LangGraph `StateGraph`，stage 间经 state channel 路由、无跨模块直接调用。
流控严格单向——唯 `hitl1` 经条件边有**有界打回**（回 `parse+partition`，max retries 默认 3；ADR-0017），其余 stage 绝不打回。
异常即记日志、就地降级、继续向前（PRD §13），唯 HITL-2 硬闸门正确性硬停、绝不替人拍板自动采纳（ADR-0010）。

## 2. 流水线（7 阶段）

```
START → parse+partition → hitl1 → hypothesis_propose → retrieval
      → judgment → rewrite_loop → hitl2 → END
```

| # | 节点 | 职责 | 状态 |
|---|---|---|---|
| ① | `parse+partition` | 段落切分（零 LLM、字节级无损）+ 论证树解析（唯一读段落文本的环节）+ `paragraph_list` + `query_time_range` | 真实 LLM 已接入 |
| ② | `hitl1` | partition 确认闸门；可打回重跑 parse+partition（有界） | 真实 CLI 闸门 + interrupt 异步闸门 |
| ③ | `hypothesis_propose` | 逐 argument 仅 propose 产 pending 候选假说（不取证） | 真实 LLM 已接入 |
| ④ | `retrieval` | 批量检索，统一返回 `citations` | 真实后端已接入（SearchAgent V12） |
| ⑤ | `judgment` | 五合一：取证 + merge + impact + consistency 串联 | 真实 LLM 已接入 |
| ⑥ | `rewrite_loop` | 逐段提议重写；未触达段逐字节拷回 | 真实 LLM 已接入 |
| ⑦ | `hitl2` | 终稿文本确认硬闸门：逐段确认 / 编辑 / 驳回 → 拼 `final_document` | 真实 CLI 闸门 + interrupt 异步闸门 |

贯穿 state：`session_context`（session/user/current_time/user_prompt）+ `query_time_range`（ADR-0017）。

## 3. 已实现能力

### 3.1 真实 LLM 四 seam

四条 LLM seam（解析 / 开药 / 裁决 / 重写提议）的第二 adapter 全部落地：
`infra/llm_provider.py` 把 `ChatOpenAI` 指向 DashScope OpenAI-compatible 端点（`qwen-max`，API key 只读 `DASHSCOPE_API_KEY`、绝不硬编码）；
`infra/llm_adapters.py` 经 `with_structured_output` 绑各 seam 契约（扁平 schema、无判别联合）。
桩（`Fake*`）与真实实现满足同一 `Protocol` 契约，逐个替换（`create_stub_agents` → `create_real_agents`，manifest 驱动）。

真实联网验证：`test_real_llm_parse.py`（9 篇真实论文 × 8 条行为契约）+ `test_real_llm_pipeline_e2e.py`（2 篇 E2E 终稿逐字节还原），`real_llm` 标记、默认 deselected。

### 3.2 真实检索后端（SearchAgent V12 · ADR-0026）

retrieval 节点从伪代码桩升级为真实检索后端：vendored SearchAgent V12（`src/infra/search_agent_vendor/`）经 `agents/retrieval/agent.py` 的 `real_retrieval` 适配器接入 retrieval seam。
`with_llm=False` 模式运行（装确定性 `DeterministicEvidenceJudge`、不调 LLM、零成本），其 `TaskDecision.verdict` 被丢弃——judgment 节点照旧吃 `citations` 经 `QwenJudgmentLlmClient` 重判终态，judgment 仍是唯一裁决者、无双倍 LLM 成本。

真实后端能力：Volcano 全网检索 + Bisheng KB + 结构化检索。
loop-affine httpx client 经适配器自持的 daemon worker event loop + 进程级单例 runtime + `run_coroutine_threadsafe` 桥接同步 `NodeFn`（零框架改动）。
PII 脱敏（`infra/retrieval.py:redact_query` 出网前对 `target_text`/`paragraph_text` 脱敏）+ 可溯源审计（`Source.origin`/`locator`）由 seam 适配器重承载。
domain whitelist 作废（已记录 PRD §6 偏差）。

落地切片（commit）：`a6d0dc1` vendor+carve-out → `4d5bd51` RetrievalFn 5 输入 → `c5a5d3d` 真实适配器 → `184d39b` real_llm 全链测试 → `6d93dd9` ADR-0026 + 文档同步；5 切片全部落地、e2e 全绿。

### 3.3 异步 HITL + 持久化（ADR-0022）

HITL 闸门经 LangGraph `interrupt` + `AsyncPostgresSaver` 实现跨进程续跑：
`session_id` 作 checkpointer `thread_id`、`trace_id` 标一次修订执行链路；
执行锁用 `session_locks` 表行。
`HypoArgusSerializer`（`JsonPlusSerializer` 子类）保证 `OriginalParagraphs` 经 checkpointer 写读等价。
崩溃恢复续跑入口 `Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)`（按段文本幂等重推导 `final_document`）。

### 3.4 HTTP 控制面 + 显示层（ADR-0023 / ADR-0024）

`src/api_layer/`（FastAPI）提供 `POST /api/agent/run`（fresh-run `document` 字段）+ `GET /api/agent/graph`（图视图）等控制面。
显示层为 `trace_events` 只读尾随视图：WS 断开不中止 run、`LISTEN/NOTIFY` 回放；`event_seq` 保事件时序。
配套 ops / metrics / redaction / session_cache / langfuse_wrap / logging_setup 子模块。

### 3.5 领域核心与装配

- **领域核心**（`src/` 根，共享不变语言）：`domain.py` / `original_paragraphs.py`（只读字节表·ADR-0005）/ `partition.py`（零 LLM 切分·ADR-0009）/ `tree_invariants.py` / `status_machine.py`（ADR-0011）。
- **段落聚合根**（ADR-0025）：`ParagraphRecord` 正向拥有与论证节点的一对多关系，原句由段落侧单份持有；`Argument` 是纯推理结构、退役 `paragraph_id`/`content`。
- **manifest 驱动装配**（ADR-0014）：`agents/assembly.py` 的 `MANIFEST` 是单一装配真相源——每条 `AgentEntry` 含 `stub`/`real`/`deps`/`build`，同时驱动 typed `Agents` dataclass 构造与 `default_pipeline()` 拓扑。加 Agent 触点 7→3。
- **拓扑 seam**：`default_pipeline()` 返回 `tuple[StageSpec, ...]`；传入自定义 spec 即不同拓扑（如省略 `judgment` / `hypothesis_propose`）。

## 4. 仓库布局

```
src/
  domain.py ... (领域核心纯函数)
  agents/      parser hitl1 hypothesis retrieval judgment rewrite_loop hitl2  +  merge/impact/consistency
  infra/       llm_provider llm_adapters retrieval  +  search_agent_vendor/ (vendored V12)
  runtime/     orchestrator cli_gates gates checkpoint run_real
  api_layer/   app server run ws translator trace_store ops metrics redaction ...
docs/
  CONTEXT.md  DEVELOPMENT.md  TESTING.md  STATE.md  adr/  contracts/
markdown/     (9 篇真实论文，供 real_llm 套件)
tests/        (50 个测试文件、678 collected)
```

源码扁平布局（ADR-0014：`package-dir={""="src"}`、`mypy_path=src` 解析顶层裸名 `domain`/`agents`/`infra`/`runtime`）。
vendored 第三方 `src/infra/search_agent_vendor/` 作 carve-out 排除出 `mypy --strict` / `ruff`（`pyproject.toml` exclude + mypy override `search_agent.*`）；wheel 为正典源。

## 5. 运行

所有安装 / 运行 / 测试在 conda 环境 `HypoArgus` 中（`conda run -n HypoArgus ...`）。

```bash
# 离线全量测试（~2min，含 Postgres checkpointer 集成）
conda run -n HypoArgus pytest -m "not real_llm" -q

# 真实全链跑一篇文档
cp .env.example .env                      # 填 DASHSCOPE_API_KEY
conda run -n HypoArgus python -m runtime.run_real input.txt -o final.md
```

真实检索 e2e 需额外 `VOLCANO_SEARCH_API_KEY` + `BISHENG_BASE_URL`（内网）：把 `src/infra/search_agent_vendor/search_agent.env.example` 复制为 `src/infra/search_agent_vendor/search_agent/.env`（`*.env` 已 gitignore）。
真实 LLM 套件 `real_llm` 标记 gating：无凭证整模块 skip、离线门 deselect。

## 6. 质量门

```bash
ruff check src tests          # E/F/I/UP/B，line-length 99，不强制 ruff format
mypy --strict src             # 扁平 src 布局，mypy_path=src；vendored carve-out 排除
pytest -m "not real_llm" -q    # 离线门；真实 LLM（real_llm 标记）单独按需跑
```

不强制 `ruff format`——不重排既有文件。
lint / 类型 / 测试失败一律修，即使非本次改动引入（见 `CLAUDE.md`）。
当前基线：678 collected、离线 665（661 passed / 4 skipped）+ 真实 LLM 13。

## 7. 文档索引

- [`CONTEXT.md`](CONTEXT.md) — 领域术语表（Ubiquitous Language）。
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — 模块边界、seam、装配、扩展点。
- [`docs/TESTING.md`](docs/TESTING.md) — 测试分层、约定、运行、已知边界。
- [`docs/STATE.md`](docs/STATE.md) — 状态树字段流向（主/子智能体 state、LLM seam 输入形式）。
- [`docs/adr/`](docs/adr/) — 架构决策记录（编号永久、不重排）。
- [`docs/contracts/`](docs/contracts/) — 节点契约（parse-partition / retrieval）。
