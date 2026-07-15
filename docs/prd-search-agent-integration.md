# PRD — 迁入 SearchAgent V12 检索子智能体作为真实检索后端

> 状态：未发布（本地独立 PRD，供新会话接手任务规划与实施）。
> 来源：一次 `/grill-with-docs` 访谈会话的决议综合。
> 硬约束（用户原话）：**不要修改主智能体框架去适配子智能体**。

## Problem Statement

HypoArgus 主智能体的检索节点（retrieval seam）当前是伪代码桩：不真实检索、产空 citations。
这使下游裁决（judgment）永远见无素材、重写循环（rewrite_loop）无触达段、终稿逐字节等于原文——tracer bullet 回路成立但无真实事实验证能力。
用户需要把一个已存在的、完整的事实核验子智能体（SearchAgent V12，暂存于仓内 standby 区）迁入主智能体，作为真实检索后端，使 citations 非空、judgment 据真实证据判终态、rewrite_loop 据触达段提议重写。
迁移必须在不改动主智能体框架结构（manifest 驱动装配、节点同步签名、judgment 单写者契约、citations 单写者契约、Source schema、reducer、拓扑）的前提下完成。

## Solution

把 SearchAgent V12 作为真实检索 provider 接入 retrieval seam——即填上 manifest 中 retrieval 条目的 `real=None` 空位，与 judgment 同形管理（`real=` 工厂 + `RealDeps` 注入）。
子智能体以 `with_llm=False` 模式运行（装确定性 `DeterministicEvidenceJudge`、不调 LLM、零成本，但 judge 仍嵌在子图 flow 内不可跳过）。
丢弃子智能体的 `TaskDecision.verdict`；主智能体的 judgment 节点照旧吃 `citations` 经 `QwenJudgmentLlmClient` 重判终态——judgment 仍是唯一裁决者、无双倍 LLM 成本。
子智能体的 HTTP/LLM/KB 客户端由 retrieval 适配器（seam 侧）自持的一条专用长驻 daemon worker event loop 承载，经 `run_coroutine_threadsafe` 桥接同步检索节点，全程不触碰 `NodeFn` 的同步签名。
子智能体产出的 `CitationRecord` 经适配器映射为框架 `Source`，按 `item_id`（forward→argument_id / reverse→hypothesis_id）落入 `citations` channel。
vendor 的子智能体源码入仓，作 vendored third-party 排除出严格质量门；新增的适配器代码全量纳入 `mypy --strict` / `ruff`。

## User Stories

1. 作为 HypoArgus 主智能体维护者，我想让 retrieval 节点产出真实 citations，这样下游 judgment 能据真实证据判终态而非永远空裁决。
2. 作为主智能体维护者，我想让 retrieval 真实后端填 manifest 的 `real` 工厂空位、与 judgment 同形管理，这样装配路径一致、不引入第二种节点管理范式。
3. 作为主智能体维护者，我想让子智能体以 `with_llm=False` 跑、verdict 被丢弃、judgment 重判，这样无双倍 LLM 成本、judgment 仍是唯一裁决者。
4. 作为主智能体维护者，我想让 retrieval 适配器自持 daemon worker loop + 单例 runtime，这样子智能体的 loop-affine HTTP 客户端不跨 loop 炸、且跨请求复用（不每次重建）。
5. 作为主智能体维护者，我想让 retrieval 节点的同步签名（`NodeFn`）不变，这样 LangGraph 仍把同步节点丢 threadpool 跑、框架节点类型不动。
6. 作为主智能体维护者，我想让 retrieval 节点增读 `paragraph_list`、forward `target_text` 取段 `original_content`，这样子智能体能拿到待核验的段落原文（`Argument` 无文本字段是 ADR-0025 的既成代价）。
7. 作为主智能体维护者，我想让 `CitationRecord` 映射为 `Source`（`snippet ← content`，全 ACCEPTED+DEGRADED 映射），这样 judgment LLM 能读到真实证据片段而非关系模板句。
8. 作为主智能体维护者，我想让出网查询经 PII 脱敏、每条 citation 带可溯源 origin/locator，这样即使放弃域名白名单，脱敏与审计两道合规关注仍存活。
9. 作为主智能体维护者，我想让结构化检索靠子智能体自有的 scenario-only + SQL 拦截，这样比框架占位模板更严、框架层结构化校验对真实后端 N/A。
10. 作为主智能体维护者，我想让知识库检索靠 Bisheng 服务端鉴权，这样弃用框架占位 `authorized_users` 闸也不损失真实访问控制。
11. 作为主智能体维护者，我想让 vendor 子智能体源码作 vendored third-party 排除出 strict/ruff，这样不被 50+ 既有文件的既有风格拖垮质量门、同时适配器代码全量进严格门。
12. 作为主智能体维护者，我想让子智能体自带依赖（langfuse、langgraph、volcano/bisheng 客户端等）加进 `pyproject.toml`，这样 vendor 后能真实安装与运行。
13. 作为运维者，我想让长驻 async 服务的检索 runtime 是进程级单例、退出随进程回收，这样无 per-request 资源泄漏、与既有 LLM client 不-close 先例一致。
14. 作为运维者，我想让 CLI 一次性运行路径同样可用真实检索，这样离线/程序化调用也能端到端验证检索链路。
15. 作为测试者，我想能在 `RetrievalFn` seam 注入伪 runtime 产 canned citations，这样不联网、不调 LLM 即可单测适配器的映射与桥接契约。
16. 作为测试者，我想跑真实子智能体全链（Volcano/Bisheng 真实联网）作为带 `real_llm` 标记的慢集成测试，这样 CI 质量门不被网络/凭证卡住、真实链路有覆盖。
17. 作为测试者，我想保留 tracer bullet 不变（无触达段终稿逐字节等于原文），这样真实后端未配置/未触达时既有契约不破。
18. 作为文档读者，我想让 ADR-0026 捕获这次迁移的全部架构决议（含 PRD §6 白名单偏差），这样未来无上下文者能理解为何这样接、哪些是已记录的偏差。
19. 作为文档读者，我想让 `CONTEXT.md` 增"检索子智能体 (SearchAgent)"glossary 条目，这样术语表覆盖真实检索后端。
20. 作为文档读者，我想让 retrieval-node 契约与 STATE 速查在实现落地时同步从 4 输入改 5 输入，这样文档不描述未落地状态。
21. 作为新会话接手者，我想这份 PRD 自含全部决议与定位线索，这样无需回放访谈会话即可进入规划与实施。

## Implementation Decisions

### Q1 — 挂载点：retrieval seam，否掉"替换 retrieval+judgment"拓扑

子智能体接入 retrieval seam（填 manifest 的 `real=None`）。
judgment 节点 + 其内串联的 merge/impact/consistency 纯函数**原样不动**（ADR-0017：judgment 是检索后唯一整树写者；子智能体不复制 12 格矩阵 / invalid 传导 / issue_tags）。
否掉 standby 区集成文档主张的"SearchAgent 替换 retrieval + judgment 两节点 / `hypothesis_propose → search_agent → downstream`"拓扑。
verdict 处理（精化版 a）：子智能体以 `with_llm=False` 跑（装 `DeterministicEvidenceJudge`、确定性、不调 LLM、零成本，但 judge 仍嵌 flow 内不可跳过）；**丢弃** `TaskDecision.verdict`；judgment 的 `QwenJudgmentLlmClient` 照旧吃 `citations` 判终态。
否掉"复用 verdict"方案（需新增 state channel 传 verdict = 改框架，或 judgment 再调一次子智能体 = 双倍调用）。

### Q2 — paragraph_text 缺口：(α) 最小放宽

retrieval 节点闭包增读 `state["paragraph_list"]`；`RetrievalFn` Protocol 增第 5 形参 `paragraph_list`（当前 4 输入：`argument_tree` / `hypotheses` / `query_time_range` / `session_context`）。
正向 `ForwardItem.target_text` = 该段 `ParagraphRecord.original_content`（`Argument` 无文本字段，ADR-0025 代价；同段多节点共享同 target_text，靠 `item_id=argument_id` + `required_slots` 区分，保留 per-argument verdict 粒度）。
`Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约**全不动**。
`RetrievalFn` Protocol 决策形（来自既有 seam 的扩展）：

```
def __call__(
    self,
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    query_time_range: TimeRange,
    session_context: SessionContext,
    paragraph_list: list[ParagraphRecord],   # 新增第 5 形参
) -> dict[str, list[Source]]: ...
```

### Q3 + B1 — 物理形态：(B) vendor 源码入仓 + carve-out；落地路径 B1

vendor 的子智能体源码入仓，作 vendored third-party 排除出 `mypy --strict` / `ruff`；新增适配器代码全量纳入严格门。
落地两个物理落点：
- seam 侧：retrieval 契约 + 适配器 + 包 `__init__`，三个模块，**全量 strict/ruff**。补齐 ADR-0014 子包结构（retrieval 现是唯一无子包的 seam）。
- 真实后端侧：vendor 的子智能体源码，**carve-out 排除**。
真实后端放 `infra/` 层，与既有 `QwenJudgmentLlmClient`（在 infra 层、不在 agents 层）同层，且填 `infra/retrieval.py` 注释里预留的"真实后端后续切片接入"槽。
retrieval 在 MANIFEST 里与 judgment 同形管理（`real=` 工厂 + `RealDeps` 注入）。
因 Q2 丢弃 judge、当 retrieval **provider** 用，家在 `infra/`（`RetrievalLayer` 所在地）才与既有分层一致。
否掉 B2（全放 agents/retrieval/、vendor 在 backend/ 子目录）：会让 agents 层混进 provider 子树 + 要守 `__init__` 纪律避免 `load_env()`-on-import 污染 seam 契约单测。

### Q4 — 生命周期：(i) 零框架改动，daemon worker loop + 单例

子智能体 `SearchAgentRuntime` 持编译好的子图 + HTTP/LLM/KB client，需 `aclose()`。
框架无节点生命周期 / shutdown 钩子（仅 checkpointer 的 `async with`）。
**关键代码事实**：V12 的 `VolcanoWebSearchClient` / `BishengRetrieveClient` 在 `from_env` 构造期就建 `httpx.AsyncClient`，**loop-affine**（首次请求绑当时 loop，换 loop 用即 `attached to a different loop`）。
故"singleton runtime + 每次节点调用 `asyncio.run(runtime.ainvoke(...))` 桥接"**会坏**（spine 里 `create_real_agents` 跑在 uvicorn loop、client 绑之；retrieval 节点在 LangGraph threadpool worker 无 loop、`asyncio.run` 开新 loop → 炸）。
决议：(i) 适配器自持一条**专用长驻 daemon worker event loop**（独立线程 `loop.run_forever()`），`real` 工厂在 worker loop 上建单例 `SearchAgentRuntime`（4 个 httpx client 全绑 worker loop）；retrieval 节点（同步 `NodeFn`，跑在 threadpool worker、无 loop）经 `asyncio.run_coroutine_threadsafe(runtime.ainvoke(payload), worker_loop).result(timeout)` 同步桥接——非-loop 线程向长驻 loop 投递协程的正典，`NodeFn` 类型不动。
spine（长驻 uvicorn）只建一次 agents → runtime 是进程级单例、跨所有请求复用（INTEGRATION §4 reuse ✓）、无 per-request 泄漏（§4 ✓）、无 loop-bind 炸点。
关闭：worker 线程作 daemon，进程退出即死，OS 回收 socket——与既有 `build_qwen_chat_model()` 建 `BaseChatModel` 后**全程不 `aclose`**、spine `finally` 只 cancel sweep 的先例同形。
可选：适配器内挂 `atexit` 做尽力而为的 `aclose()`（`run_coroutine_threadsafe(runtime.aclose(), loop).result()` + `loop.call_soon_threadsafe(loop.stop)`）——**住在适配器、不碰框架**。
否掉 (ii)（给 `Agents` 加泛型 `aclose()`、spine `finally` 调）：唯一真·框架改动，换来进程退出时 httpx 正经 drain、无 ResourceWarning；判断不违反"不为主智能体改框架适配子智能体"（是通用 lifecycle 设施），但 (i) 已与既有先例一致、零改动，(ii) 留作未来 ops 硬要求时的升级位。
否掉"每次调用新建 runtime"：不 reuse、贵、违背 INTEGRATION §4。

### Q5 — 合规：(A′) domain whitelist 作废 + 存活关注重承载（已记录 PRD §6 偏差）

框架 `infra/retrieval.py` 的 `validate_request` 编码三条合规：网络域名白名单（Mock 占位 `stats.example.com` 等）/ KB `authorized_users`（占位 `analyst-1`）/ 结构化模板。仓内无 PRD 文件；"PRD §6"是 `infra/retrieval.py` docstring 对未纳入仓库的外部 PRD 的引用。
V12 的 Volcano 是**开放式全网检索**（无域名白名单），与"网络仅白名单、禁泛网搜索"直接冲突；V12 的 scope_guard 做的是语义 scope（年份/地域/主体/指标/单位，且词表是具身智能/人形机器人行业专属），**非域名白名单**。
决议：
- **domain whitelist 作废**：Mock 白名单本就是占位、真实后端预期就是开网事实验证。**作为已记录的 PRD §6 偏差**写进 ADR-0026 + CONTEXT。
- **PII 脱敏重承载**：seam 适配器在构造 V12 payload 前对 forward/reverse 的 `target_text`/`paragraph_text` 跑框架 `redact_query`（V12 不做、框架有现成纯函数）——保住"出网请求脱敏"意图。
- **可溯源审计重承载**：`Source.origin` / `locator` 从 `CitationRecord.source_name` / `url` 填，每条 citation 可审计（与 Q6 映射一并满足）。
- **结构化**：靠 V12 自承——`StructuredQueryClient` 头注"Scenario-only, no SQL API"、`_SQL_PATTERN` 正则主动拦 `select/insert/.../union`、配 `SCENARIO_REGISTRY`，比框架占位模板更严。框架层结构化校验对此真实后端 N/A。
- **KB**：靠 Bisheng 服务端鉴权（cookie/token、按 knowledge_id 圈定可访问库），弃框架占位 `authorized_users` 闸。
否掉 (C) route-through（把 V12 provider 经框架合规层）：`validate_request` 作用在 `RetrievalRequest` 形状上，V12 是自洽子智能体图、非 `RetrievalLayer` 实现；大改换不来收益（结构化已更严、web 白名单化等于废掉 V12 用途）。

### Q6 — CitationRecord → Source 映射

key = `item_id`（forward→`argument_id`，reverse→`hypothesis_id`，来自 `CitationRecord.task_ids` → task → item）。

| Source 字段 | ← CitationRecord |
|---|---|
| `source_id` | `citation_id` |
| `kind` | `source_type`（WEB→network / KNOWLEDGE_BASE→knowledge_base / STRUCTURED_DATA→structured） |
| `origin` | `source_name` |
| `title` | `title` |
| `snippet` | `content` |
| `locator` | `url` |

- **`snippet ← content`（非 summary）**：V12 `content` = `" ".join(quoted_spans)[:600]`（judge 从原文抽的真实证据引用片段，≤600 字，本就是 snippet 语义，且有 `snippet_only` 标志佐证）；V12 `summary` = 模板句"该来源给出了支持'X'的直接事实"（只含 claim 文本 + 关系模板、**零证据原文**）。judgment LLM 靠 `snippet` 文本重判可信度（prompt 形如 `- [素材 key=… | kind | origin] title\n  {snippet}`），喂 summary 等于没给证据可读。访谈手记原倾向 summary 是基于"summary 是忠实摘要"的错判，代码纠偏为 content。
- **全映射（ACCEPTED + DEGRADED）**：`status="DEGRADED" if snippet_only else "ACCEPTED"`。DEGRADED 非拒、是"仅片段"提取但已过 V12 全部质量闸（scope_compatible / confidence≥min / directness≥min / 完整事实句 / 无 blocker），同样带 `content`、同样绑 claim。`Source` schema 无 status 字段（Q2 定不动），框架 judgment 无从区分，不如全映射让它按内容自加权。
全部 citation 映射；`Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约不动。

### Q7 — required_slots：传空

`ForwardItem`/`ReverseItem.required_slots` 默认空 list。`required_slots` 在 V12 喂两处：`slot_aggregation.infer_slot_evidence`（数值槽位绑定）与 `build_prepared_context.metric_terms`（查询 hint）。
两处空 list **都不阻断 citation 产出**——`_citation` 发射闸是 scope_compatible / confidence / directness / 完整事实句 / blocker，**不查 required_slots**。
传空 = 损失"数值槽位绑定 + 指标类查询 hint"，但 citation 的 `content`（含数字）照常产出、judgment LLM 读 `content` 自能判数值。
与 Q1 `with_llm=False` / retrieval 无 LLM seam 一致；slot 绑定属 V12 数值层、与 judgment 读 content 冗余，真要补是 judgment 层事而非 retrieval。
否掉"用 LLM 抽关键事实点填 slots"：retrieval 现无 LLM、新增即偏框架 + 成本 + 与 Q1 矛盾。

### Q8 — argument_context / argument_path：传空

`ArgumentPathItem.text` 要祖先论证节点文本，但 `Argument` 类白纸黑字"节点不再持有段落原文与段落归属"（ADR-0025）——框架域模型里**根本无此字段**。
传空 `ArgumentContext()` / 空 argument_path。降级 = 丢"上位论证"查询 hint，可接受。

### Q9 — id 映射

`SearchAgentInputState` 要求 `request_id` / `document_id` 均 `min_length=1`；`user_id: str | None = None` **可选**（访谈手记"均 min_length=1"不准确，代码纠偏）。
state 里**无 `trace_id` channel**（手记提的 trace_id 不在 PipelineState）。
- `request_id ← session_context.session_id`（运行时必非空；空则 mint uuid 兜底）。
- `document_id ← "doc-" + blake2b(original_doc, digest_size=12).hexdigest()`（内容指纹，确定性、跨段稳定、跨 resume 稳定；只是 hash 串不外泄原文）。适配器读 `state["original_doc"]`（与 Q2 的 `paragraph_list` 同属节点 state-read 扩增）。
- `user_id ← session_context.user_id or None`（可选，无校验风险，不用兜底）。

### 子智能体侧契约要点（vendor 源码，不改）

- `SearchAgentRuntime.from_env(with_llm=False)` / `.ainvoke(payload) -> dict` / `.aclose()` / async context manager；`ainvoke` 是 **async**。
- `load_env()` 在子智能体包 `__init__` 时执行（读自己 `.env`，env 命名 `LLM_*`/`VOLCANO_*`/`BISHENG_*`/`LANGFUSE_*`，与主 `DASHSCOPE_API_KEY` 不冲突）。
- `SearchAgentInputState`：`request_id`/`document_id`（min1）/`user_id`（可选）/`paragraph: ParagraphInput`（含 `paragraph_id` / `paragraph_text`（必填非空）/ `forward_items` / `reverse_items` / `argument_context`）/ 可选 `organization_context` / `knowledge_context` / `retrieval_policy` / `trace_context`。
- `SearchAgentOutputState`：`paragraph_id` / `run_status` / `results: list[TaskDecision]` / `citations: list[CitationRecord]` / `warnings` / `trace`。

### 质量门与环境

- conda env `HypoArgus`（所有 install/run/test 必须 `conda run -n HypoArgus ...`）。
- 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`；**不强制 `ruff format`**（勿重排既有文件）。vendor 源码 carve-out 排除；适配器代码全量进。
- 子智能体自带依赖（langfuse≥3、langgraph≥1.2、volcano/bisheng 客户端等）加进 `pyproject.toml`。
- vendor 后子智能体从 standby 区移入 `infra/` 层的 vendor 目录。

## Testing Decisions

### Seam 选择：单一 `RetrievalFn` Protocol seam

最高、最少 seam：既有 `RetrievalFn` Protocol（retrieval 节点经 `agents.retrieval(...)` 调用）。新适配器实现该 Protocol；测试在该 seam 注入。
理想 seam 数 = 1。不新增测试 seam。

### 好测试的标准

只测外部行为（契约），不测实现细节。
适配器测：给定 canned `SearchAgentOutputState`（或伪 runtime），映射出的 `Source` 列表形状正确（字段映射、key=item_id、ACCEPTED+DEGRADED 全映射、`snippet=content`）。
桥接测：同步 `RetrievalFn` 在"调用方无运行 loop"（threadpool 模拟）下经 worker loop 正确拿到 async runtime 结果。
节点测：注入真适配器（背伪 runtime）到 `Agents`，跑 retrieval 节点，断言 `citations` channel 写入正确、`paragraph_list` 被读用于 `target_text`。
tracer-bullet 不变：无触达段终稿逐字节等于原文（真实后端未配置/未触达时既有契约不破）。

### 模块与既有先例

- 适配器映射纯函数：参考既有 `infra/retrieval.py` Mock 层的"先校验合规、再返回受控固定素材、同一请求同一 source_id"确定性范式。
- 节点级测试：参考既有 retrieval/judgment 节点在 `assembly.py` build 闭包下的注入式单测（`Agents(retrieval=fake_fn, ...)` 构造）。
- 真实联网慢集成测试：参考既有 `real_llm` 标记的真实 LLM 测试套件范式（~40min、已知瞬态坑、带 `real_llm` 标记、CI 质量门不卡网络/凭证）。真实 Volcano/Bisheng 全链跑在此标记下、不在默认质量门。
- async 桥接：参考 `runtime/run_real.py` 既有"同步节点丢 threadpool、`asyncio.run` / checkpointer `async with`"模式；但注意 V12 client loop-affine，故桥接走 `run_coroutine_threadsafe` + 专用 worker loop 而非 `asyncio.run`（Q4 决议）。

## Out of Scope

- 不修改主智能体框架结构：不动 manifest 装配范式、`NodeFn` 同步签名、judgment 单写者契约、citations 单写者契约、`Source` schema、`_merge_dict` reducer、图拓扑（硬约束）。
- 不实现"复用 V12 verdict"（需改框架或双倍调用，已否）。
- 不实现"V12 provider 经框架合规层 route-through"（架构不合身，已否）。
- 不给 `Agents` 加泛型 `aclose()` shutdown 钩子（(ii)，留作未来 ops 硬要求升级位）。
- 不用 LLM 在 retrieval 抽 required_slots（retrieval 无 LLM seam，已否）。
- 不重写 V12 内部逻辑（vendor 源码原样；Q5 脱敏/审计在适配器侧承载，不改 V12）。
- 默认质量门不含真实联网检索（走 `real_llm` 标记慢集成测试）。
- 不强制 `ruff format` 既有文件。

## Further Notes

### 落地后要写的文档（决议全定后一次性写，勿写半成品）

- **ADR-0026**（新，编号续 0025）：捕获 Q1（挂 retrieval seam、否 replace-judgment）+ Q2（retrieval 读 paragraph_list 的最小放宽、forward target_text=original_content 的 ADR-0025 代价）+ Q3/B1（vendor 决定 + B1 落地路径）+ Q4（daemon worker loop、零框架改动、loop-affine 纠偏）+ Q5（domain whitelist 作废的已记录 PRD §6 偏差 + 脱敏/审计重承载）+ Q6~Q9（映射细节）。满足三准则（难逆 / 无上下文会困惑 / 真实权衡）。格式见 `domain-modeling` skill 的 ADR-FORMAT。
- **`CONTEXT.md`**：加 glossary 条目"检索子智能体 (SearchAgent)：真实检索后端，挂 retrieval real，with_llm=False（确定性 judge 免费）、verdict 丢弃、judgment 重判、domain whitelist 作废（PRD §6 已记录偏差）、脱敏/审计由 seam 重承载"。
- **`docs/contracts/retrieval-node.md`** + **`docs/STATE.md §3.1 retrieval 行`**：4 输入→5 输入（+`paragraph_list`）。**实现时改**（现改会描述未落地状态）。
- **`docs/DEVELOPMENT.md §2/§3`**：retrieval 从 inline 桩 → seam 子包 + `infra/` vendor 目录（B1）。**实现时改**。

### 定位线索（供新会话探索，非 brittle 路径承诺）

- 主智能体框架：manifest 驱动装配模块（`MANIFEST`/`AgentEntry`/`RealDeps`/`create_real_agents`）；retrieval 的 `RetrievalFn` Protocol / `_stub_retrieval` / `_retrieval_node` build 闭包 / MANIFEST `real=None` 空位均在此模块。
- 同步 orchestrator（`NodeFn = Callable[[PipelineState], dict]`、`PipelineState` TypedDict、reducer `merge_argument_tree`/`merge_paragraph_list`/`_merge_dict`）。
- 真实装配入口（`run_real_pipeline` 同步、`arun_real_pipeline`/`run_resume_loop` 异步 resume 循环、`async with build_async_checkpointer`）。
- 长驻 spine（`api_layer/server.py` 的 `serve`：进程级单例 `Orchestrator(agents=_real_agents(), ...)`、`async with` 持连接池作用域、`finally` 仅 cancel sweep）。
- infra 检索契约（`RetrievalLayer` Protocol / `_MockRetrievalLayer` / `validate_request` / `Source` / `RetrievalKind` / `redact_query`，注释"真实后端后续切片接入"）；infra LLM adapter（`QwenJudgmentLlmClient` 等，真实 adapter 在 infra、不在 agents——B1 论据）。
- judgment 契约（`JudgmentLlmClient.judge(argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range)`——已有 `paragraph_list`）。
- 域模型（`Argument` 无 text/content、`Hypothesis` 有 text、`ParagraphRecord` 的 `original_content`/`summary`/`argument_tree_ids`、`SessionContext`/`TimeRange`）。
- 子智能体源码（vendor 对象）：standby 区的 `SearchAgent_V12_Delivery_v2/`——`api.py`（`SearchAgentRuntime`）、`evidence_retrieval/public_contracts.py`（`SearchAgentInputState`/`OutputState`/`CitationRecord`/`TaskDecision`）、`evidence_retrieval/schemas.py`（`ForwardItem`/`ReverseItem`/`ParagraphSearchInput`/`ArgumentPathItem`）、`evidence_retrieval/dependencies.py`（`with_llm` vs `defaults`）、`evidence_retrieval/output_adapter.py`（`content` vs `summary` 的真实来源、`status` 判定）、`evidence_retrieval/scope_guard.py`（语义 scope、非域名白名单）、`evidence_retrieval/providers/{volcano_web,bisheng_retrieve,structured_query,...}.py`（loop-affine httpx client、scenario-only no-SQL）、`evidence_retrieval/tracing.py`（`redact` 仅 trace、非出网查询）。
- 既有 ADR：0014（manifest 装配）、0019（judgment 五合一单写者）、0025（paragraph 聚合根 / Argument 无文本）、0022（async HITL spine）、0010（HITL 硬闸门）、0017（rewrite loop）、0018（HITL-1）、0021（session_context）。

### 已有 memory（供新会话）

- conda env `HypoArgus`（install/run/test 必须 `conda run -n HypoArgus ...`）。
- 质量门 = ruff check + mypy --strict + pytest；ruff format 不强制。
- `PRD §X` 代码引用指向未纳入仓库的外部 PRD（不是文件）；doc-sync 时勿 scrub。
- 既有真实 LLM 测试套件范式（`real_llm` 标记、~40min、已知瞬态坑）。
