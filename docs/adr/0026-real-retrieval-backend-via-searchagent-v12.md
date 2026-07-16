# ADR-0026：迁入 SearchAgent V12 作为真实检索后端（挂 retrieval seam、vendor + daemon worker loop）

## 状态

已接受（2026-07-16）。
本 ADR 捕获「把已存在的事实核验子智能体 SearchAgent V12 迁入主智能体作真实检索后端」的全部架构决议。
落地切片（commit）：`a6d0dc1`（Slice 0 vendor+carve-out）/ `4d5bd51`（Slice 1 RetrievalFn 5 输入）/ `c5a5d3d`（Slice 2 真实适配器）/ `184d39b`（Slice 3 real_llm 全链测试）/ `6d93dd9`（Slice 4 本 ADR + 文档同步）；5 切片全部落地。
字段流向见 `docs/STATE.md` §3.1（retrieval 行 / `citations` 行）；术语见 `CONTEXT.md`「智能体角色」。
取代关系：无；承接 ADR-0025 既成代价（`Argument` 无文本字段）的下游消费面，并为 ADR-0014 的 retrieval 子包补齐。

## 背景

主智能体的检索节点（retrieval seam）长期是伪代码桩：产空 `citations`。
下游 judgment 永远见无素材、重写循环（rewrite_loop）无触达段、终稿逐字节等于原文——tracer bullet 回路成立但无真实事实验证能力。
仓内 standby 区已存在一个完整的事实核验子智能体 SearchAgent V12（Volcano 全网检索 + Bisheng KB 检索 + 结构化检索 + 证据裁决子图）。
用户需求是把 V12 迁入主智能体、作真实检索后端，使 `citations` 非空、judgment 据真实证据判终态、rewrite_loop 据触达段提议重写。

硬约束（用户原话）：**不要修改主智能体框架去适配子智能体**——不动 manifest 装配范式、`NodeFn` 同步签名、judgment 单写者契约、citations 单写者契约、`Source` schema、`_merge_dict` reducer、图拓扑。
V12 是自洽子智能体图（loop-affine 的 httpx client、自带 `load_env()` on import、async `ainvoke`），与主框架的同步 `NodeFn`、长驻 uvicorn loop、`asyncio.run` 桥接范式直接冲突。
V12 的合规模型（开放式全网检索、语义 scope、scenario-only 结构化、Bisheng 服务端鉴权）与框架 `infra/retrieval.py` 的占位合规层（域名白名单 / `authorized_users` / 结构化模板）也不合身。

## 决策

### Q1 — 挂载点：retrieval seam，否掉 replace-judgment 拓扑

子智能体接入 retrieval seam——填 manifest 中 retrieval 条目的 `real=None` 空位（`src/agents/assembly.py:877-888`，`real=_real_retrieval_factory`）。
judgment 节点 + 其内串联的 merge / impact / consistency 纯函数**原样不动**（ADR-0017：judgment 是检索后唯一整树写者）。
子智能体**不**复制 12 格矩阵 / invalid 传导 / `issue_tags`。
否掉 standby 区集成文档主张的「SearchAgent 替换 retrieval + judgment 两节点 / `hypothesis_propose → search_agent → downstream`」拓扑——那是改拓扑，违硬约束。

verdict 处理：子智能体以 `with_llm=False` 模式运行（装 `DeterministicEvidenceJudge`、确定性、不调 LLM、零成本，但 judge 仍嵌子图 flow 内不可跳过）。
`TaskDecision.verdict` 被**丢弃**——`map_citations` 从不读 `verdict`（`src/agents/retrieval/contract.py:90-131`）。
judgment 的 `QwenJudgmentLlmClient` 照旧吃 `citations` 判终态——judgment 仍是唯一裁决者、无双倍 LLM 成本。
否掉「复用 verdict」方案（需新增 state channel 传 verdict = 改框架，或 judgment 再调一次子智能体 = 双倍调用）。

### Q2 — paragraph_text 缺口：(α) 最小放宽

retrieval 节点闭包增读 `state["paragraph_list"]`；`RetrievalFn` Protocol 增第 5 形参 `paragraph_list`（`src/agents/assembly.py:182-189`，5 输入：`argument_tree` / `hypotheses` / `query_time_range` / `session_context` / `paragraph_list`）。
正向 `ForwardItem.target_text` = 该段 `ParagraphRecord.original_content`（`src/agents/retrieval/contract.py:209`）。
`Argument` 无文本字段是 ADR-0025 的既成代价；同段多节点共享同 `target_text`，靠 `item_id=argument_id` + `required_slots` 区分，保留 per-argument verdict 粒度。
`Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约**全不动**——这是「最小放宽」的边界：只动一个 Protocol 形参 + 节点闭包一处 state-read，不碰 schema 与 reducer。

### Q3 + B1 — 物理形态：(B) vendor 源码入仓 + carve-out；落地路径 B1

vendor 的子智能体源码入仓 `src/infra/search_agent_vendor/search_agent/`，作 vendored third-party 排除出 `mypy --strict` / `ruff`（`pyproject.toml`：ruff `exclude` :93、mypy `exclude` :106、mypy override `search_agent.*` `ignore_missing_imports` :108-113）。
wheel 为正典源（standby 源不完整）；V12 用相对导入故仅需发现顶层 `search_agent` 包（`pyproject.toml:70-73` `packages.find.where`）。
两个物理落点：

- **seam 侧**：`src/agents/retrieval/{contract,agent,__init__}.py` 三模块，**全量 strict / ruff**。
  补齐 ADR-0014 子包结构（retrieval 曾是唯一无子包的 seam）。
  `contract.py` = `RetrievalRuntime` Protocol + `map_citations` / `build_search_agent_payload` 纯函数 + `FakeSearchAgentRuntime`（provider-free、确定性、离线可单测）。
  `agent.py` = `real_retrieval` 编排 + daemon worker loop + 懒单例 + `build_real_retrieval` + `atexit`。
- **真实后端侧**：`src/infra/search_agent_vendor/search_agent/`，**carve-out 排除**。

真实后端放 `infra/` 层，与既有 `QwenJudgmentLlmClient`（在 `infra/` 层、不在 `agents/` 层）同层，填 `infra/retrieval.py` 注释预留的「真实后端后续切片接入」槽（措辞见 `assembly.py:175`、`:640`）。
retrieval 在 MANIFEST 里与 judgment 同形管理（`real=` 工厂 + `RealDeps.retrieval_runtime` 注入，`assembly.py:764-791`）。
因 Q1 丢弃 judge、当 retrieval **provider** 用，家在 `infra/`（`RetrievalLayer` 所在地）才与既有分层一致。
否掉 B2（全放 `agents/retrieval/`、vendor 在 `backend/` 子目录）：会让 agents 层混进 provider 子树 + 要守 `__init__` 纪律避免 `load_env()`-on-import 污染 seam 契约单测。

### Q4 — 生命周期：(i) 零框架改动，daemon worker loop + 单例

子智能体 `SearchAgentRuntime` 持编译好的子图 + HTTP/LLM/KB client，需 `aclose()`。
框架无节点生命周期 / shutdown 钩子（仅 checkpointer 的 `async with`）。
**关键代码事实**：V12 的 `VolcanoWebSearchClient` / `BishengRetrieveClient` 在 `from_env` 构造期就建 `httpx.AsyncClient`，**loop-affine**（首次请求绑当时 loop，换 loop 用即 `attached to a different loop`）。
故「singleton runtime + 每次节点调用 `asyncio.run(runtime.ainvoke(...))`」会坏：spine 里 `create_real_agents` 跑在 uvicorn loop、client 绑之；retrieval 节点在 LangGraph threadpool worker 无 loop、`asyncio.run` 开新 loop → 炸。

决议：(i) 适配器自持一条**专用长驻 daemon worker event loop**（`src/agents/retrieval/agent.py:66-89`，独立 daemon 线程 `loop.run_forever()`）。
`real` 工厂在 worker loop 上建单例 `SearchAgentRuntime`（`from_env(with_llm=False)`，4 个 httpx client 全绑 worker loop；懒构建 `_LazySearchAgentRuntime` :97-133、进程级单例 `_LAZY_RUNTIME` :136）。
retrieval 节点（同步 `NodeFn`，跑在 threadpool worker、无 loop）经 `asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), worker_loop).result(timeout)` 同步桥接（`agent.py:157-167`）——非-loop 线程向长驻 loop 投递协程的正典，`NodeFn` 类型不动。
spine（长驻 uvicorn）只建一次 agents → runtime 是进程级单例、跨所有请求复用、无 per-request 泄漏、无 loop-bind 炸点。
关闭：worker 线程作 daemon，进程退出即死，OS 回收 socket——与既有 `build_qwen_chat_model()` 建 `BaseChatModel` 后全程不 `aclose`、spine `finally` 只 cancel sweep 的先例同形。
可选 `atexit` 尽力 `aclose()` 住在适配器（`agent.py:223-240`，`run_coroutine_threadsafe(aclose).result(5.0)` + `loop.call_soon_threadsafe(loop.stop)`，`except Exception: pass`）——**不碰框架**。
否掉 (ii)（给 `Agents` 加泛型 `aclose()`、spine `finally` 调）：唯一真·框架改动，换来进程退出时 httpx 正经 drain；判断不违「不为主智能体改框架适配子智能体」（是通用 lifecycle 设施），但 (i) 已与既有先例一致、零改动，(ii) 留作未来 ops 硬要求时的升级位。
否掉「每次调用新建 runtime」：不 reuse、贵、违背 INTEGRATION §4。

### Q5 — 合规：(A′) domain whitelist 作废 + 存活关注重承载（已记录 PRD §6 偏差）

框架 `infra/retrieval.py` 的 `validate_request` 编码三条占位合规：网络域名白名单（Mock 占位 `stats.example.com` 等）/ KB `authorized_users`（占位 `analyst-1`）/ 结构化模板。
「PRD §6」是 `infra/retrieval.py:1` docstring 对未纳入仓库的外部 PRD 的引用（仓内无 PRD 文件；`PRD §X` 代码引用指向外部 PRD，doc-sync 时勿 scrub）。
V12 的 Volcano 是**开放式全网检索**（无域名白名单），与「网络仅白名单、禁泛网搜索」直接冲突；V12 的 scope_guard 做的是语义 scope（年份/地域/主体/指标/单位，词表是具身智能/人形机器人行业专属），**非域名白名单**。
决议：

- **domain whitelist 作废**：Mock 白名单本就是占位、真实后端预期就是开网事实验证。**作为已记录的 PRD §6 偏差**写进本 ADR + `CONTEXT.md`。
- **PII 脱敏重承载**：seam 适配器在构造 V12 payload 前对 forward/reverse 的 `target_text`/`paragraph_text` 跑框架纯函数 `redact_query`（`infra/retrieval.py:215-223`；`contract.py:198`/`:209`/`:218`）——V12 的 `tracing.redact` 只 trace、非出网查询，故脱敏由 seam 侧承载。
- **可溯源审计重承载**：`Source.origin` ← `CitationRecord.source_name`、`Source.locator` ← `url`，每条 citation 可审计。
- **结构化**：靠 V12 自承——`StructuredQueryClient` 头注「Scenario-only, no SQL API」、`_SQL_PATTERN` 正则主动拦 `select/insert/.../union`、配 `SCENARIO_REGISTRY`，比框架占位模板更严。框架层结构化校验对此真实后端 N/A。
- **KB**：靠 Bisheng 服务端鉴权（cookie/token、按 knowledge_id 圈定可访问库），弃框架占位 `authorized_users` 闸。
否掉 (C) route-through（把 V12 provider 经框架合规层）：`validate_request` 作用在 `RetrievalRequest` 形状上，V12 是自洽子智能体图、非 `RetrievalLayer` 实现；大改换不来收益。

### Q6 — CitationRecord → Source 映射

key = `item_id`（forward → `argument_id`、reverse → `hypothesis_id`；由 `CitationRecord.task_ids` → `TaskDecision.task_id` → `item_id` 反查表 `contract.py:110-112`）。

| `Source` 字段 | ← `CitationRecord` | 出处 |
|---|---|---|
| `source_id` | `citation_id` | `contract.py:117` |
| `kind` | `source_type`（WEB→network / KNOWLEDGE_BASE→knowledge_base / STRUCTURED_DATA→structured） | `contract.py:118` + `SOURCE_TYPE_TO_KIND` :78-82 |
| `origin` | `source_name` | `contract.py:119` |
| `title` | `title` | `contract.py:120` |
| `snippet` | `content` | `contract.py:121` |
| `locator` | `url` | `contract.py:122` |

- **`snippet ← content`（非 `summary`）**：V12 `content` = `" ".join(quoted_spans)[:600]`（judge 从原文抽的真实证据引用片段，≤600 字，本就是 snippet 语义，且有 `snippet_only` 标志佐证）；V12 `summary` = 模板句「该来源给出了支持'X'的直接事实」（只含 claim 文本 + 关系模板、**零证据原文**）。judgment LLM 靠 `snippet` 文本重判可信度，喂 `summary` 等于没给证据可读。访谈手记原倾向 `summary` 是基于「`summary` 是忠实摘要」的错判，代码纠偏为 `content`。
- **全映射（ACCEPTED + DEGRADED）**：`map_citations` 无条件映射全部 citation，不读 `CitationRecord.status`、不设任何 status（`contract.py:105-107`）。
  DEGRADED 非拒、是「仅片段」提取但已过 V12 全部质量闸（scope_compatible / confidence≥min / directness≥min / 完整事实句 / 无 blocker），同样带 `content`、同样绑 claim。
  `Source` schema 无 status 字段（Q2 定不动），框架 judgment 无从区分，故全映射让它按内容自加权——这是落地实现的真实形态。
  PRD 草案曾提「`status="DEGRADED" if snippet_only else "ACCEPTED"`」，但 `snippet_only` 仅存于 V12 内部 `EvidenceItem`/`EvidenceCandidate`（`schemas.py:238`/`:261`），**不在**对外 `CitationRecord` 上，无从在映射处派生；落地为「全映射、judgment 按内容自加权」，与 PRD §Q6「`Source` 无 status、不如全映射」结论一致。
全部 citation 映射；一条 citation 跨多 task 时按每个绑定 `item_id` 各落一份（`contract.py:124-130`，测试 `test_retrieval_adapter.py:275`）。
`Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约不动。

### Q7 — required_slots：传空

`ForwardItem`/`ReverseItem.required_slots` 默认空 list（`contract.py:209`/`:220`）。
`required_slots` 在 V12 喂 `slot_aggregation.infer_slot_evidence`（数值槽位绑定）与 `build_prepared_context.metric_terms`（查询 hint）。
两处空 list 都不阻断 citation 产出——`_citation` 发射闸是 scope_compatible / confidence / directness / 完整事实句 / blocker，**不查 required_slots**。
传空 = 损失「数值槽位绑定 + 指标类查询 hint」，但 citation 的 `content`（含数字）照常产出、judgment LLM 读 `content` 自能判数值。
与 Q1 `with_llm=False` / retrieval 无 LLM seam 一致；slot 绑定属 V12 数值层、与 judgment 读 content 冗余，真要补是 judgment 层事而非 retrieval。
否掉「用 LLM 抽关键事实点填 slots」：retrieval 现无 LLM、新增即偏框架 + 成本 + 与 Q1 矛盾。

### Q8 — argument_context / argument_path：传空

`ArgumentPathItem.text` 要祖先论证节点文本，但 `Argument` 类白纸黑字「节点不再持段落原文与段落归属」（ADR-0025）——框架域模型里根本无此字段。
传空 `ArgumentContext()`（`contract.py:233`，`argument_path` 默认 `[]`）。
降级 = 丢「上位论证」查询 hint，可接受。

### Q9 — id 映射

`SearchAgentInputState` 要求 `request_id` / `document_id` 均 `min_length=1`；`user_id: str | None = None` 可选。
state 里无 `trace_id` channel。

- `request_id ← session_context.session_id`（运行时必非空；空则 mint uuid 兜底，`contract.py:189`）。
- `document_id ← "doc-" + blake2b(joined_paragraph_original_content, digest_size=12).hexdigest()`（`contract.py:145-156`）。
  内容指纹、确定性、跨段稳定、跨 resume 稳定；只是 hash 串不外泄原文。
  注：因 Slice 1 把 retrieval 节点输入锁定为 5 输入、`original_doc` 不在其中，指纹改由 `paragraph_list.original_content` 拼接计算——满足 §Q9 确定性/稳定/不外泄原文三意图。
- `user_id ← session_context.user_id or None`（可选，无校验风险，不用兜底，`contract.py:190`）。

## 权衡

本 ADR 是难逆结构决议的记录，真实权衡如下：

- **挂 retrieval seam vs replace-judgment 拓扑**：填 `real=None` 空位与 judgment 同形管理，装配路径一致、不引入第二种节点管理范式；代价是子智能体的 verdict 被丢弃、judgment 重判（但 judgment 本就要据 citations 判终态，verdict 冗余，零净损失）。
- **vendor + carve-out vs route-through 框架合规层**：vendor 让 V12 原样运行、不重写其内部逻辑；carve-out 不被 50+ 既有 vendored 文件的既有风格拖垮严格门；代价是仓内多一份第三方源码、合规靠 V12 自承而非框架统一闸。
- **daemon worker loop + `run_coroutine_threadsafe` vs `asyncio.run` 桥接**：零框架改动、与既有不-close 先例同形；代价是进程退出时 httpx 无正经 drain（仅 `atexit` 尽力 `aclose`，可能丢尾部日志/trace flush），(ii) 泛型 `aclose()` 留作未来 ops 升级位。
- **`with_llm=False` 丢 verdict 换零成本**：但损失子智能体数值层——Q7 `required_slots` 传空丢数值槽位绑定、Q8 `argument_context` 传空丢上位论证 hint；citation `content`（含数字）照常产出，judgment LLM 读 `content` 自能判数值，故可接受。
- **domain whitelist 作废**：换 V12 开放式全网事实验证用途；脱敏（`redact_query`）+ 可溯源审计（`origin`/`locator`）两道存活关注由 seam 重承载，结构化靠 V12 scenario-only、KB 靠 Bisheng 服务端鉴权——作为已记录 PRD §6 偏差。
- **全映射 ACCEPTED+DEGRADED**：`Source` 无 status 字段、judgment 按内容自加权；代价是 judgment 无从区分 ACCEPTED/DEGRADED 证据权重，但 DEGRADED 已过 V12 全部质量闸、内容可用。

### 三准则自检

- **难逆**：vendor 入仓 + carve-out + daemon worker loop + manifest `real=` 工厂是难逆结构决议，回滚需删子包、改 manifest、清配置。
- **无上下文会困惑**：未来读者会问「为何不 route-through 框架合规层」「为何不用 `asyncio.run` 桥接」「为何 domain whitelist 作废」「为何 `snippet←content` 而非 `summary`」「为何 `document_id` 用段落内容指纹」——本 ADR 显式记录否掉理由与代码纠偏。
- **真实权衡**：见上——每条都付出代价（丢 verdict / 丢数值层 / 丢正经 drain / 丢域名白名单 / 丢 status 区分），换对应收益。

## 影响

- retrieval 节点从 inline 桩变为 seam 子包（`src/agents/retrieval/`，ADR-0014 子包结构补齐）+ `infra/` vendor 目录（B1 落地路径）。
- MANIFEST retrieval 条目从 `real=None` 变为 `real=_real_retrieval_factory`（与 judgment 同形），`RealDeps` 增 `retrieval_runtime` 字段（`assembly.py:774`）。
- retrieval 节点产真实非空 `citations`（真实后端配置/触达时）；judgment 据真实证据判终态；rewrite_loop 据触达段提议重写。
- tracer-bullet 不变：真实后端未配置 / 未触达任何段时终稿逐字节等于原文（`test_retrieval_adapter.py:846` 守此不变量）。
- 默认质量门不含真实联网检索——V12 全链走 `real_llm` 标记慢集成测试（`tests/test_real_retrieval_e2e.py`，凭证键 `DASHSCOPE_API_KEY` + `VOLCANO_SEARCH_API_KEY` + `BISHENG_BASE_URL`，默认 deselected）。
- 适配器代码（`src/agents/retrieval/`）全量进 `mypy --strict` / `ruff`；vendored 源码 carve-out 排除。
- 子智能体自带依赖（langfuse / langgraph / volcano/bisheng 客户端等）已加进 `pyproject.toml`。
- 文档同步：`CONTEXT.md` 增「检索子智能体 (SearchAgent)」术语条目；`docs/DEVELOPMENT.md` §2/§3/§4/§8.1 从 inline 桩描述改为 seam 子包 + vendor 目录；`docs/contracts/retrieval-node.md` + `docs/STATE.md` §3.1 「当前伪代码桩」标签翻为真实后端。
