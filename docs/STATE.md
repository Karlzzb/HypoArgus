# 状态树设计说明（STATE）

本文是**状态树**的唯一索引：把主智能体 state、子智能体局部契约、以及所有 LLM seam 的输入/输出，分层摊开，供各子智能体开发者横向对齐。
术语见 `CONTEXT.md`；架构决策见 `docs/adr/`；模块边界与装配见 `docs/DEVELOPMENT.md`。
本文只回答三件事：**每个字段从哪来**、**谁写谁读**、**哪些字段以什么形式进了 LLM**。

> 维护约定：字段增删或来源变更时，只改对应小节的表格，不要在多处重复描述同一字段——单一定义点、避免漂移。
> 字段名以 `src/domain.py` / `src/runtime/orchestrator.py` 为准；行号会随后续提交漂移，以代码实名为唯一真相。

## 0. 来源标记图例

每个字段标注一个来源标签，全文档统一：

- `【用户】` 用户输入 / 运行入口注入（CLI / stdin / 文件，见 `src/runtime/run_real.py`）。
- `【代码】` 纯代码 stage 产出（确定性、无 LLM、无检索）。
- `【LLM】` LLM 经 `with_structured_output` 返回后，由 agent 纯函数铸造写回。
- `【HITL】` 人工闸门产出（HITL-1 / HITL-2，当前为同步注入桩，真实 `interrupt` 属后续切片）。
- `【依赖】` 注入依赖（`RealDeps` / `session_config`），非 state 业务字段。

## 1. 主智能体 state（`PipelineState`）

定义于 `src/runtime/orchestrator.py`，是一个 `TypedDict(total=False)`。
顶层字段是 **StateGraph channel**——带 reducer，子智能体不直接共享可变对象，而是写 channel、reducer 合流。
reducer 见 `runtime/orchestrator.py`（`merge_argument_tree` / `_merge_dict` / `_append_errors`）。

| 字段 | 类型 | reducer | 来源（创建者） | 谁读 | 证据 |
|---|---|---|---|---|---|
| `original_doc` | `bytes` | — | `【用户】` 入口注入 | partition | `runtime/orchestrator.py` invoke；`runtime/run_real.py` |
| `original_paragraphs` | `OriginalParagraphs` | — | `【代码】` partition 构造 | parse / rewrite_loop / hitl2（皆只读旁路） | `agents/assembly.py` parse+partition 闭包；`original_paragraphs.py` |
| `argument_tree` | `list[Argument]` | `merge_argument_tree`（按 `argument_id` upsert 整树） | `【代码】` partition 初始化 `[]`；`【LLM】` parse 建初始树；`【LLM】` judgment 整树写回（单写者：吃 `citations` 判终态 + 按序调 merge/impact/consistency 纯函数） | parse→hitl1→hypothesis_propose→retrieval→judgment→rewrite_loop→hitl2 全程 | 写点见 §3 各子智能体；reducer `runtime/orchestrator.py` `merge_argument_tree` |
| `hypotheses` | `dict[str, list[Hypothesis]]` | `_merge_dict` | `【LLM】` hypothesis_propose（partial，候选假设列表，`status=pending`）；`【LLM】` judgment 取证后整写回（`pending`→终态 `supported/doubtful/refuted`） | judgment | 写 `agents/assembly.py` hypothesis_propose 闭包；judgment 闭包读后写回 |
| `proposed_rewrites` | `dict[str, str]`（`paragraph_id → 提议重写文本`，仅被触达段） | `_merge_dict`（单写者无冲突） | `【LLM】` rewrite_loop（逐段提议重写） | hitl2（确认 / 编辑 / 驳回后拼 `final_document`） | ADR-0017；写 `agents/assembly.py` rewrite_loop 闭包；读 `agents/hitl2/agent.py` `build_review` |
| `final_document` | `bytes` | — | `【代码】` hitl2 拼装产出 | `run_with_report` 出口 | `agents/assembly.py` hitl2 闭包（兜底回退原文拼接）；`runtime/orchestrator.py` 出口 |
| `errors` | `list[str]` | `_append_errors` | `【代码】` `_guarded` 兜底写入（7 个 stage 任一异常） | `run_with_report` 出口 | `agents/assembly.py` `_log_error_patch`；`runtime/orchestrator.py` 出口 |

**channel 三类**（理解路由的关键）：

- **整树 channel**（`argument_tree`）：多写者、带 `merge_argument_tree` reducer，按 `argument_id` 同 id 覆盖、新 id 追加。
  Slice 5 五合一后 judgment 为检索之后的**唯一**整树写者（吃 `hypotheses` / `citations` partial 判终态、再按序调 merge/impact/consistency 纯函数后整树写回），故裁撤 `argument_credibility` partial channel（ADR-0019）。
  Slice 6（ADR-0017）后 hitl2 不再写 `argument_tree`（按段落 / 文本工作、与 argument 状态解耦），`argument_tree` 在 judgment 之后冻结——rewrite_loop 只读用于触达判定、hitl2 只读用于取原文。
- **partial channel**（`hypotheses` / `citations` / `proposed_rewrites`）：单写者 + 单读者。
  hypothesis_propose 产候选假设列表（`list[Hypothesis]`、`status=pending`）、retrieval 产 citations（`list[Source]`）、rewrite_loop 产 `proposed_rewrites`（`paragraph_id → 提议重写文本`，仅被触达段），judgment 读前两者判终态后整树写回 `argument_tree`、hitl2 读 `proposed_rewrites` 拼装 `final_document`。**partial 不塞整节点**——名字即内容：`hypotheses` 的 value 就是假设列表本身、`citations` 的 value 就是 `Source` 列表本身、`proposed_rewrites` 的 value 就是提议文本本身。
- **直通 channel**（`original_doc` / `original_paragraphs` / `final_document` / `errors`）：单写者单读者，无 reducer 冲突。

> `session_config`（`Orchestrator.run(..., session_config=)`）**不是 PipelineState 字段**：透传给 langgraph `RunnableConfig`（ADR-0016），当前内存态、不被业务节点消费。

### 1.1 `OriginalParagraphs`（`original_paragraphs` 字段的形状）

定义于 `src/original_paragraphs.py`，构造后冻结（`MappingProxyType` + tuple，无写方法）。
对外只暴露只读 API：`paragraph_ids()` / `get(pid)` / `has(pid)`。
`paragraph_id` 形如 `p0001`（零填充 4 位）；文本为 `bytes`。
**唯一写者是 partition**；parse / rewrite_loop / hitl2 只读旁路，**任何 prompt 都不整篇加载它**——argument 只携带自身那一段原文 + `paragraph_id` 指针（ADR-0005）。

### 1.2 重构方向新增字段（ADR-0017~0021·已落地）

下表为流水线重构（ADR-0017~0021）所接受的契约。Slice 1–6 已全部落地
（`session_context` / `query_time_range` / `paragraph_summaries` / `citations` / `partition_retry_count` /
`hitl1_route` 均已并入 `PipelineState`；`proposed_rewrites` Slice 6 落地后已并入 §1 主表，本小节不再重复其定义）。
本小节为过渡期描述点，遵循「单一定义点、避免漂移」维护约定（已并入 §1 主表者此处不再赘述）。
术语见 `CONTEXT.md`「重构方向术语」；ADR 偏离见 `docs/adr/0017`~`0021`。

| 字段 | 类型 | reducer | 来源（创建者） | 谁读 | 证据 / 落地 slice |
|---|---|---|---|---|---|
| `session_context` | `SessionContext`（`session_id` / `user_id` / `current_time: datetime` / `user_prompt`） | —（单写者，无冲突） | `【用户】` 入口注入（`runtime/run_real.py`，与 `original_doc` 同入 START） | retrieval / judgment / hypothesis_propose / rewrite_loop（全链只读，进 LLM 背景） | ADR-0021；Slice 1 |
| `query_time_range` | `TimeRange`（`start: date \| None` / `end: date \| None` / `rationale: str`） | —（单写者） | `【代码】` `parse+partition` 注入桩值（`TimeRange(start=2025, end=2026, rationale="默认值·真实识别待后续")`，不真实调 LLM） | retrieval / judgment / rewrite_loop | ADR-0021；Slice 1（桩），真实识别 Out of Scope |
| `paragraph_summaries` | `dict[str, str]`（`paragraph_id → 摘要`） | `_merge_dict`（单写者无冲突） | `【LLM】` `parse+partition` 两阶段调用顺产（树一次 + 摘要按 8 分块，见 §4） | hypothesis_propose / rewrite_loop | ADR-0021；Slice 1。**不并入 `OriginalParagraphs`**（保字节级无损只读表身份，ADR-0005/0009） |
| `citations` | `dict[str, list[Source]]`（key 为 `argument_id` 如 `n0001`/`bg-...` 或 `hypothesis_id` 如 `h-...`，两套 id 不冲突） | `_merge_dict`（单写者无冲突） | `【代码】` retrieval（当前伪代码桩，产空 / 占位，不联网） | judgment / rewrite_loop | ADR-0019；Slice 4（桩），真实后端 Out of Scope |
| `partition_retry_count` | `int` | —（单写者，覆盖） | `【代码】` hitl1 build 闭包（打回 +1；超限不 +1） | hitl1 自身（循环续传计数） | ADR-0018；Slice 2 |
| `hitl1_route` | `str`（`"continue"` / `"replay"`） | —（单写者，覆盖） | `【代码】` hitl1 build 闭包（据 `confirm_partition` 产 `Hitl1Outcome.route`） | hitl1 条件边 `_hitl1_route`（ADR-0018 受控打回边） | ADR-0018；Slice 2 |

> `proposed_rewrites`（Slice 6·rewrite_loop）已并入 §1 主表（单写者=rewrite_loop、读者=hitl2、reducer=`_merge_dict`）。

**channel 类别**（沿用 §1 三类划分）：

- `session_context` / `query_time_range`：直通 channel（单写者单读者，无 reducer 冲突）。
- `paragraph_summaries` / `citations` / `proposed_rewrites`：单写者 + 单读者，reducer=`_merge_dict`（单写者无冲突，沿用既有 partial 风格）。
- `partition_retry_count` / `hitl1_route`：控制流直通 channel（单写者=hitl1、覆盖式；前者循环续传计数、后者驱动条件边路由）。**不进任何 LLM 输入**——为图层级打回控制态，非业务字段。
- `session_context` 以单一嵌套对象流转，**不污染顶层 channel**（不拆 `session_id` / `user_id` / ... 为顶层字段，ADR-0021）。

> `hypotheses` channel 形状：propose 阶段写入的 `Hypothesis.status` 为 `pending`，由 judgment 取证后落终态（`supported` / `doubtful` / `refuted`）——见 ADR-0019。
> 原 `writeback` 产出的 `final_document` 已改由 hitl2 拼装落地（ADR-0017），`writeback` 节点裁撤——Slice 6 已落地。终稿拼装幂等续跑入口为 `Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)`。

## 2. 论证树节点形状（`argument_tree` channel 的元素）

`Argument` 定义于 `src/domain.py`，是 `argument_tree` channel 的元素。
**字段级来源**是子智能体开发者沟通的核心——同一 argument 的不同字段由不同子智能体在不同 stage 写入，互不覆盖。
（`hypotheses` / `citations` partial channel 不以 `Argument` 为 value——见 §1，它们的 value 是 `list[Hypothesis]` / `list[Source]`。）

| 字段 | 类型 | 来源（写入者） | 进 LLM? | 证据 |
|---|---|---|---|---|
| `argument_id` | `str` | `【代码】` parse 铸造（`n{idx}` 或 `bg-{pid}`）；HITL-1 合并/拆分可改 | 否 | `agents/parser/agent.py` |
| `argument_type` | `ArgumentType` | `【LLM】` parse 提议；`【HITL】` hitl1 可 `SetTypeOp` | 是（parse 全量提议；judgment/hypothesis 只读 `argument_type.value` 进 prompt） | `agents/parser/contract.py`；`infra/llm_adapters.py` |
| `parent_id` | `str \| None` | `【代码】` parse 据 LLM `parent_index` 解析（越界/自指→根）；`【HITL】` hitl1 `ReparentOp` | 否 | `agents/parser/agent.py` |
| `children_ids` | `list[str]` | `【代码】` parse 回填 + 断环 `_break_cycles` | 否 | `agents/parser/agent.py` |
| `paragraph_id` | `str` | `【代码】` parse 从只读表对齐拷入 | 是（parse 进 prompt 的 `[paragraph_id] 文本`） | `agents/parser/agent.py`；`infra/llm_adapters.py` |
| `content` | `str` | `【代码】` parse 逐字节从 `original_paragraphs` 拷回（**LLM 无权改写**） | 是（judgment/hypothesis/rewrite 进 prompt 的核心输入） | `agents/parser/agent.py`；`infra/llm_adapters.py` |
| `argument_weight` | `int 0-100` | `【LLM】` parse 提议 → `【代码】` clamp 至 [0,100]（影子恒 0）；HITL-1 合并时按规则调 | 否 | `agents/parser/agent.py` |
| `status` | `ArgumentStatus` | `【LLM】` judgment 取证终判（`credible/doubtful/error`）；`【代码】` impact 判 `invalid` | 否（不进任何 LLM） | judgment `agents/judgment/agent.py`；impact `agents/impact.py`。**注**：Slice 6 后 `adopted`/`corrected` 在新流程（rewrite_loop + hitl2 终稿闸门）不再被写——hitl2 按段 / 文本工作、与 argument 状态解耦；`adopted`/`corrected`/`adopted_hypothesis_id` 等 domain 字段保留不删（minimizing domain churn） |
| `issue_tags` | `list[str]` | `【代码】` merge 追加 `conflict`；`【代码】` consistency 扫贴；`【代码】` impact 追加 `weakening`（三者经 judgment 串联调用） | 否 | `agents/merge.py`；`agents/consistency.py`；`agents/impact.py`。**注**：原 writeback 失败贴 `writeback_error` 已随 writeback 裁撤移除（Slice 6） |
| `candidate_hypotheses` | `list[Hypothesis]` | `【LLM】` hypothesis_propose 产 pending 假说；`【LLM】` judgment 取证落终态后经 merge 裁剪存活 | 否（propose 只把 `argument`+`paragraph_summary` 进 prompt；judgment 只把假说 `text` 进 prompt；rewrite_loop 只读用于触达判定、不回灌 `candidate_hypotheses`） | `agents/hypothesis/agent.py`；`agents/judgment/agent.py`；`agents/rewrite_loop/agent.py` |
| `merge_decision` | `MergeDecision \| None` | `【代码】` merge 12 格矩阵裁决；`【代码】` impact 复用 supported 假设激活（均经 judgment 串联调用） | 否 | `agents/merge.py`；`agents/impact.py` |
| `adopted_hypothesis_id` | `str \| None` | （新流程不写；原 `【HITL】` hitl2 `AdoptOp` 立即持久化，随 Slice 6 hitl2 重定位移除） | 否 | domain 字段保留不删 |

### 2.1 `Hypothesis`（`candidate_hypotheses` 元素，`domain.py`）

| 字段 | 类型 | 来源 | 进 LLM? |
|---|---|---|---|
| `hypothesis_id` | `str` | `【代码】` hypothesis 派生（`blake2b(argument_id\|relation\|text\|idx)` → `h-{digest}`，幂等） | 否 |
| `text` | `str` | `【LLM】` hypothesis_propose 返回 | 是（judgment 取证阶段作为 `hypothesis.text` 进 prompt） |
| `relation` | `HypothesisRelation` | `【LLM】` hypothesis_propose 钉定（oppose/advance/expand） | 否 |
| `status` | `HypothesisStatus` | `【LLM】` hypothesis_propose 置 `pending`；`【LLM】` judgment 逐条取证落终态（supported/doubtful/refuted） | 否 |
| `confidence` | `float 0-1` | `【LLM】` hypothesis propose 返回（仅排序、不裁决） | 否 |

### 2.2 状态机（`ArgumentStatus` / `HypothesisStatus`）

`ArgumentStatus`（`domain.py`）：`unverified → pending_verification → (credible | doubtful | error) → adopted → corrected`；`invalid` 由 impact 对上层论点单独判定。回写失败停留 `adopted` 可重试（ADR-0011）。
**注（Slice 6）**：新流程（rewrite_loop + hitl2 终稿闸门）按段落 / 文本工作、与 argument 的 `status` / `merge_decision` 解耦——`adopted` / `corrected` / `adopted_hypothesis_id` 在新流程不再被写（domain 字段保留不删）；终稿拼装的幂等续跑入口改为 `Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)`（按段文本重推导，不再依赖 `adopted_hypothesis_id`）。
`HypothesisStatus`（`domain.py`）：`pending → (supported | doubtful | refuted)`，与原文侧 `credible/doubtful/error` 对称。
`MergeAction`（`domain.py`）：`keep/replace/rewrite/supplement/conflict/freeze`（ADR-0006 12 格矩阵）。

## 3. 子智能体局部契约（按 stage）

子智能体分布在 `src/agents/<name>/`（parser / hitl1 / hypothesis / judgment / rewrite_loop / hitl2）与 `src/agents/<name>.py`（merge / impact / consistency——均为纯函数、由 judgment 节点按序串联调用）。
每个子智能体只读 `PipelineState` 的若干字段、只写自己负责的 channel（见 §1）；**绝不跨模块直接调用**。
下表每个子智能体一节，列出输入（来自主 state 哪个字段）→ 输出（写回主 state 哪个字段）→ 是否调 LLM。

### 3.1 节点速查

| stage | 输入（主 state 字段） | 输出（写回主 state） | LLM? | 纯函数签名 / 证据 |
|---|---|---|---|---|
| parse+partition | `original_doc` | `original_paragraphs` / `argument_tree` / `query_time_range` / `paragraph_summaries` | **是**（parse） | `parse(original_paragraphs, llm) -> ParseOutput` `agents/parser/agent.py` |
| hitl1 | `argument_tree` / `partition_retry_count` | `argument_tree` / `hitl1_route` / `partition_retry_count` | 否（HITL 桩） | `confirm_partition(argument_tree, retry_count, gate) -> Hitl1Outcome` `agents/hitl1/agent.py` |
| hypothesis_propose | `argument_tree` / `paragraph_summaries` | `hypotheses` | **是** | `propose_hypotheses(argument_tree, paragraph_summaries, llm) -> dict[str, list[Hypothesis]]` `agents/hypothesis/agent.py` |
| retrieval | `argument_tree` / `hypotheses` / `query_time_range` / `session_context` | `citations` | 否（伪代码桩，真实后端 Out of Scope） | `retrieval(argument_tree, hypotheses, qtr, sc) -> dict[str, list[Source]]` `agents/assembly.py` 桩 |
| judgment | `argument_tree` / `hypotheses` / `citations` / `session_context` / `query_time_range` | `argument_tree`（整树写回） / `hypotheses`（终态化） | **是** | `judge_and_adjudicate(argument_tree, hypotheses, citations, sc, qtr, llm) -> JudgmentOutcome` `agents/judgment/agent.py` |
| rewrite_loop | `argument_tree` / `citations` / `paragraph_summaries` / `original_paragraphs` / `session_context` / `query_time_range` | `proposed_rewrites`[, `errors`] | **是** | `propose_rewrites(argument_tree, citations, paragraph_summaries, original_paragraphs, sc, qtr, llm) -> RewriteLoopOutcome` `agents/rewrite_loop/agent.py` |
| hitl2 | `original_paragraphs` / `proposed_rewrites` | `final_document` | 否（HITL 桩） | `confirm(original_paragraphs, proposed_rewrites, gate) -> Hitl2Confirmation` `agents/hitl2/agent.py` |

> **judgment 内部串联**（纯函数、逻辑不动）：`judge_and_adjudicate` 先 `llm.judge(...)` 取 per-argument / per-hypothesis 终态，构造局部 `argument_credibility` + 终态化 `hypotheses`，再按序调 `merge_with_partials` → `impact` → `consistency`，整树写回 `argument_tree`（单写者，故裁撤 `argument_credibility` partial channel）。
> merge/impact/consistency 均为确定性纯函数、无 LLM / 检索依赖，**不在拓扑中独立成 stage**——它们的串联编排收口于 judgment 节点（ADR-0019）。
> rewrite_loop 只读 `argument_tree` 用于触达判定（段内有 supported 假说 / 命中 citations）+ LLM 输入，**不写 `argument_tree`**（Slice 6：与 argument 状态解耦，失败信号落 `errors` channel + 段回退原文）。hitl2 只读 `original_paragraphs` + `proposed_rewrites`，拼装 `final_document`，亦不写 `argument_tree`。

### 3.2 各子智能体可改字段（一句话边界）

- **parse+partition**：partition 按 ATX 标题边界 + 空行切分产 `original_paragraphs`（只读原文表；P-05：零空行文档按标题边界切、不塌成单段，字节级不变）+ 空树（纯代码、字节级自检失败硬停）；parse 只喂 `is_substantive` 段（去纯 `---` 主题分隔线，P-02）建初始树，铸 `argument_id` / `parent_id` / `children_ids`（n-id 按幸存提议连续赋值、空位 parent 落根，P-04）/ `paragraph_id` / `content`(拷回) / `argument_weight`(clamp) / `status=unverified`。无提议段落（含被 `is_substantive` 排除的 `---` 段）降级为 `background` 影子节点。
- **hitl1**：人审结构编辑。可改 `argument_id` / `parent_id` / `children_ids` / `argument_type` / `argument_weight`；不改 `content`。
- **hypothesis_propose**：只产候选假设 `list[Hypothesis]`（`status=pending`）；覆盖范围 `evidence/sub_claim`。propose 不取证、不读检索（ADR-0002 解法 A）。
- **judgment**：吃 `citations` 判 per-argument / per-hypothesis 终态。落 `status`（原文侧 `credible/doubtful/error`）+ `Hypothesis.status`（`pending`→终态）；经 merge/impact/consistency 串联写 `merge_decision` / `issue_tags` / 裁剪 `candidate_hypotheses` / 翻上层 `invalid`；覆盖范围 `main_claim/sub_claim/evidence`。不改 `content`、不置 `adopted`。
- **rewrite_loop**：对被触达段（段内有 supported 假说 / 命中 citations）调 LLM 产提议重写文本，写 `proposed_rewrites`；未触达段省略；LLM 抛错段省略 + 记 `errors`、不杀全树。**不碰 `argument_tree`**（只读用于触达判定 + LLM 输入）。
- **hitl2**：逐段确认 / 编辑 / 驳回 `proposed_rewrites`，拼装 `final_document`（确认→提议文本、编辑→编辑文本、驳回 / 未触达→逐字节原文）。硬闸门，`Hitl2GateError` 原样上抛不兜底；**不写 `argument_tree`**（按段 / 文本工作，与 argument 状态解耦）。

## 4. LLM seam 总表

全仓**共 4 个 LLM 调用点**，集中在 `src/infra/llm_adapters.py`（真实 adapter `Qwen*` + 离线 `Fake*` 双 adapter）。
merge / impact / consistency / hitl1 / hitl2 / retrieval 均**无 LLM**（纯函数 + 同步人工桩 + 伪代码检索桩）。

> **输入压缩铁律**：四条 LLM seam **只把「原文文本 + 检索 snippet + 运行背景」喂给 LLM**，绝不回灌 `status` / `argument_weight` / `parent_id` / `children_ids` / `issue_tags` / `merge_decision` 等内部状态字段。
> parse 按段喂、不整篇 dump；hypothesis 只注 `argument_type + content` + `paragraph_summary`；judgment 注 `argument_type + content` + 假说 `text` + citation 片段 + `session_context` / `query_time_range` 背景；rewrite 注 `paragraph_summary` + 段内 `argument.content` / `argument_type` + 段内假说 `text` / `relation` + citation 片段 + 运行背景。
> 四条 seam 的 contract schema 均为扁平 BaseModel（`ParseResult` / `_ProposalsEnvelope` / `JudgmentResult` / `_RewriteEnvelope`、无判别联合 `oneOf`）。
> 其中 hypothesis / judgment / rewrite 三 seam 直接用 contract schema 经 `with_structured_output` 绑定；**parse 拆两阶段**——真实 adapter 绑两个内部信封 `_ParseTreeEnvelope`（proposals-only）+ `_SummariesEnvelope`（按 8 段分块的 `ParagraphSummary`），再在 adapter 内折成 `ParseResult`（P-01：单 `ParseResult` 绑定下大论文摘要被系统性少填，故树/摘要拆关注点、分块产出）。

| # | stage | Protocol 方法（contract 定义） | 进 LLM 的 state 字段 | 输入形式 | structured output 模型 | 写回字段 | 证据 |
|---|---|---|---|---|---|---|---|
| 1 | parse | `LlmClient.parse(paragraphs: list[ParagraphView]) -> ParseResult`（`agents/parser/contract.py`） | `original_paragraphs` 的段落原文（经 `is_substantive` 去纯 `---` 主题分隔线，不喂 LLM、归只读 background 影子） | **两阶段**：① 树——每段 `[paragraph_id] 文本`，多段 `\n\n` 拼，内嵌 `WEIGHT_RUBRIC` 明文 rubric；② 摘要——按 `summary_chunk_size=8` 分块、逐块产 `ParagraphSummary`（LLM 漏填/返空者由该段自身文本兜底，P-06） | 真实 adapter 绑 `_ParseTreeEnvelope`（`proposals: list[ParsedNodeProposal]`，字段 `paragraph_id/argument_type/parent_index/argument_weight`，**不含 content**）+ `_SummariesEnvelope`（chunk of `ParagraphSummary`），折成 `ParseResult`（另含 `paragraph_summaries: list[ParagraphSummary]` + `query_time_range` 桩）；Fake 直接返 `ParseResult` | `argument_tree` + `paragraph_summaries` + `query_time_range`（agent 据 proposals 铸 `Argument`、逐字节拷回 `content`；`list[ParagraphSummary]` 折成 `dict[str,str]`） | `infra/llm_adapters.py` `QwenParseLlmClient`；调用 `agents/parser/agent.py` |
| 2 | hypothesis / propose | `HypothesisLlmClient.propose(argument, paragraph_summary) -> list[HypothesisProposal]`（`agents/hypothesis/contract.py`） | `argument.argument_type` + `argument.content` + `paragraph_summary` | prompt 拼接：注入 `argument_type` + `content` + 段落摘要，要求 0..N 条可证伪假设，每条恰一种 relation | `_ProposalsEnvelope`（包 `list[HypothesisProposal]`：`text/relation/confidence`） | `hypotheses`（agent 铸 `Hypothesis`，写候选假设列表，`status=pending`） | `infra/llm_adapters.py` `QwenHypothesisLlmClient`；调用 `agents/hypothesis/agent.py` |
| 3 | judgment | `JudgmentLlmClient.judge(argument_tree, hypotheses, citations, session_context, query_time_range) -> JudgmentResult`（`agents/judgment/contract.py`） | `argument.argument_type` + `argument.content`（覆盖范围内）+ 假说 `text` + `citations` 片段 + `session_context` / `query_time_range` 背景 | prompt 拼接：节点 `[id \| type] content` 列表 + 假说列表 + citation 片段 + 运行背景；LLM 一次产 per-argument / per-hypothesis 终态裁决 | `JudgmentResult`（扁平信封：`argument_verdicts` + `hypothesis_verdicts` 两 list，各 `{id, verdict}`） | `argument_tree`（agent 据返回铸 `ArgumentStatus` / `Hypothesis.status`，再调 merge/impact/consistency 整树写回） | `infra/llm_adapters.py` `QwenJudgmentLlmClient`；调用 `agents/judgment/agent.py` |
| 4 | rewrite_loop | `RewriteLlmClient.propose_rewrite(paragraph_id, paragraph_summary, arguments, citations, session_context, query_time_range) -> str \| None`（`agents/rewrite_loop/contract.py`） | `paragraph_summary` + 段内 `argument.argument_type` / `content` + 段内假说 `text` / `relation` + `citations` 片段 + `session_context` / `query_time_range` 背景 | prompt 拼接：段落摘要 + 段内节点列表 + 段内假说 + citation 片段 + 运行背景；LLM 产一版重写文本（空串=不提议） | `_RewriteEnvelope`（`rewritten_text: str`，空串=选择不提议） | `proposed_rewrites`（agent 据 `paragraph_id` 入表，仅触达段） | `infra/llm_adapters.py` `QwenRewriteLlmClient`；调用 `agents/rewrite_loop/agent.py` |

## 5. 维护与扩展约定

- **新增 state 字段**：在 `PipelineState`（`runtime/orchestrator.py`）加键 + reducer（多写者必需），并在 §1 表格补一行，注明创建者 stage 与读者。
- **新增 argument 字段**：在 `Argument`（`domain.py`）加字段，在 §2 表格补一行，标注唯一写入者（**单写者优先**，避免字段级合流歧义）。
- **新增/变更 LLM seam**：在 §4 总表补一行，写清进 LLM 的 state 字段与输入形式；同步更新该子智能体 contract 的 `*LlmClient` Protocol。
- **本文不重复解释业务逻辑**：裁决矩阵、状态机迁移、回写分流等规则见对应 ADR 与 `domain.py` docstring，本文只给字段流向。
