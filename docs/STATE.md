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

定义于 `src/runtime/orchestrator.py:99`，是一个 `TypedDict(total=False)`，共 7 个顶层字段。
顶层字段是 **StateGraph channel**——带 reducer，子智能体不直接共享可变对象，而是写 channel、reducer 合流。
reducer 见 `orchestrator.py:61-96`。

| 字段 | 类型 | reducer | 来源（创建者） | 谁读 | 证据 |
|---|---|---|---|---|---|
| `original_doc` | `bytes` | — | `【用户】` 入口注入 | partition | `orchestrator.py` invoke；`run_real.py` |
| `original_paragraphs` | `OriginalParagraphs` | — | `【代码】` partition 构造 | parse / hitl2 / writeback（皆只读旁路） | `assembly.py` partition 闭包；`original_paragraphs.py:21` |
| `argument_tree` | `list[Argument]` | `merge_argument_tree`（按 `argument_id` upsert 整树） | `【代码】` partition 初始化 `[]`；`【LLM】` parse 建初始树；`【代码】` merge/impact/consistency；`【HITL】` hitl2 | parse→hitl1→(体检∥开药)→merge→…→writeback 全程 | 写点见 §3 各子智能体；reducer `orchestrator.py:61` |
| `argument_credibility` | `dict[str, ArgumentStatus]` | `_merge_dict` | `【LLM】` verification（partial，**只存可信度裁决**） | merge（读后合流，不回写本 channel） | 写 `assembly.py` verification 闭包；读 `assembly.py` merge 闭包 |
| `hypotheses` | `dict[str, list[Hypothesis]]` | `_merge_dict` | `【LLM】` hypothesis（partial，**只存候选假设列表**） | merge | 写 `assembly.py` hypothesis 闭包；读 `assembly.py` merge 闭包 |
| `final_document` | `bytes` | — | `【代码】` writeback 产出 | `run_with_report` 出口 | `assembly.py` writeback 闭包（兜底回退原文拼接）；`orchestrator.py` 出口 |
| `errors` | `list[str]` | `_append_errors` | `【代码】` `_guarded` 兜底写入（9 个下游 stage 任一异常） | `run_with_report` 出口 | `assembly.py` `_log_error_patch`；`orchestrator.py` 出口 |

**channel 三类**（理解路由的关键）：

- **整树 channel**（`argument_tree`）：多写者、带 `merge_argument_tree` reducer，按 `argument_id` 同 id 覆盖、新 id 追加。
- **partial channel**（`argument_credibility` / `hypotheses`）：单写者 + 单读者。
  为避免并行两线路（体检 ∥ 开药）整节点 upsert 互相覆盖丢字段，体检只产可信度裁决（`ArgumentStatus`）、开药只产候选假设列表（`list[Hypothesis]`），由 merge 字段级合流（ADR-0002；dev-guide §2.2）。
  **partial 不塞整节点**——名字即内容：`argument_credibility` 的 value 就是 `ArgumentStatus` 本身、`hypotheses` 的 value 就是假设列表本身，合流时直接写回对应字段。
- **直通 channel**（`original_doc` / `original_paragraphs` / `final_document` / `errors`）：单写者单读者，无 reducer 冲突。

> `session_config`（`Orchestrator.run(..., session_config=)`）**不是 PipelineState 字段**：透传给 langgraph `RunnableConfig`（ADR-0016），当前内存态、不被业务节点消费。

### 1.1 `OriginalParagraphs`（`original_paragraphs` 字段的形状）

定义于 `src/original_paragraphs.py:21`，构造后冻结（`MappingProxyType` + tuple，无写方法）。
对外只暴露只读 API：`paragraph_ids()` / `get(pid)` / `has(pid)`。
`paragraph_id` 形如 `p0001`（零填充 4 位）；文本为 `bytes`。
**唯一写者是 partition**；parse / hitl2 / writeback 只读旁路，**任何 prompt 都不整篇加载它**——argument 只携带自身那一段原文 + `paragraph_id` 指针（ADR-0005）。

## 2. 论证树节点形状（`argument_tree` channel 的元素）

`Argument` 定义于 `src/domain.py:140`，是 `argument_tree` channel 的元素。
**字段级来源**是子智能体开发者沟通的核心——同一 argument 的不同字段由不同子智能体在不同 stage 写入，互不覆盖。
（两条 partial channel 不再以 `Argument` 为 value——见 §1，它们的 value 是 `ArgumentStatus` / `list[Hypothesis]`。）

| 字段 | 类型 | 来源（写入者） | 进 LLM? | 证据 |
|---|---|---|---|---|
| `argument_id` | `str` | `【代码】` parse 铸造（`n{idx}` 或 `bg-{pid}`）；HITL-1 合并/拆分可改 | 否 | `parser/agent.py` |
| `argument_type` | `ArgumentType` | `【LLM】` parse 提议；`【HITL】` hitl1 可 `SetTypeOp` | 是（parse 全量提议；verify/hypo 只读 `argument_type.value` 进 prompt） | `parser/contract.py`；`llm_adapters.py` |
| `parent_id` | `str \| None` | `【代码】` parse 据 LLM `parent_index` 解析（越界/自指→根）；`【HITL】` hitl1 `ReparentOp` | 否 | `parser/agent.py` |
| `children_ids` | `list[str]` | `【代码】` parse 回填 + 断环 `_break_cycles` | 否 | `parser/agent.py` |
| `paragraph_id` | `str` | `【代码】` parse 从只读表对齐拷入 | 是（parse 进 prompt 的 `[paragraph_id] 文本`） | `parser/agent.py`；`llm_adapters.py` |
| `content` | `str` | `【代码】` parse 逐字节从 `original_paragraphs` 拷回（**LLM 无权改写**）；`【HITL】` hitl2 `EditContentOp` 可覆写 | 是（verify/hypo 进 prompt 的核心输入） | `parser/agent.py`；`llm_adapters.py` |
| `argument_weight` | `int 0-100` | `【LLM】` parse 提议 → `【代码】` clamp 至 [0,100]（影子恒 0）；HITL-1 合并时按规则调 | 否 | `parser/agent.py` |
| `status` | `ArgumentStatus` | `【LLM】` verify 取证终判（`credible/doubtful/error`）；`【代码】` impact 判 `invalid`；`【HITL】` hitl2 置 `adopted`；`【代码】` writeback 翻 `corrected` | 否（不进任何 LLM） | verify `agent.py`；impact `agent.py`；hitl2 `agent.py`；writeback `agent.py` |
| `issue_tags` | `list[str]` | `【代码】` merge 追加 `conflict`；`【代码】` consistency 扫贴；`【代码】` impact 追加 `weakening`；`【代码】` writeback 失败贴 `writeback_error` | 否 | `merge.py`；`consistency.py`；`impact.py`；`writeback.py` |
| `candidate_hypotheses` | `list[Hypothesis]` | `【LLM】` hypothesis 生成 + 取证铸造；`【HITL】` hitl2 `RejectOp` 可移除单条 | 否（hypothesis 取证时只把 `proposal.text` 进 prompt，不回灌 `candidate_hypotheses`） | `hypothesis/agent.py` |
| `merge_decision` | `MergeDecision \| None` | `【代码】` merge 12 格矩阵裁决；`【代码】` impact 复用 supported 假设激活 | 否 | `merge.py`；`impact.py` |
| `adopted_hypothesis_id` | `str \| None` | `【HITL】` hitl2 `AdoptOp` 立即持久化 | 否 | `hitl2/agent.py` |

### 2.1 `Hypothesis`（`candidate_hypotheses` 元素，`domain.py:124`）

| 字段 | 类型 | 来源 | 进 LLM? |
|---|---|---|---|
| `hypothesis_id` | `str` | `【代码】` hypothesis 派生（`blake2b(argument_id\|relation\|text\|idx)` → `h-{digest}`，幂等） | 否 |
| `text` | `str` | `【LLM】` hypothesis propose 返回 | 是（hypothesis 取证阶段作为 `hypothesis_text` 进 prompt） |
| `relation` | `HypothesisRelation` | `【LLM】` hypothesis propose 钉定（oppose/advance/expand） | 否 |
| `status` | `HypothesisStatus` | `【LLM】` hypothesis 逐条取证终判（supported/doubtful/refuted） | 否 |
| `confidence` | `float 0-1` | `【LLM】` hypothesis propose 返回（仅排序、不裁决） | 否 |

### 2.2 状态机（`ArgumentStatus` / `HypothesisStatus`）

`ArgumentStatus`（`domain.py:37`）：`unverified → pending_verification → (credible | doubtful | error) → adopted → corrected`；`invalid` 由 impact 对上层论点单独判定。回写失败停留 `adopted` 可重试（ADR-0011）。
`HypothesisStatus`（`domain.py:67`）：`supported / doubtful / refuted`，与原文侧 `credible/doubtful/error` 对称。
`MergeAction`（`domain.py:80`）：`keep/replace/rewrite/supplement/conflict/freeze`（ADR-0006 12 格矩阵）。

## 3. 子智能体局部契约（按 stage）

子智能体分布在 `src/agents/<name>/`（parser / hitl1 / verification / hypothesis / hitl2）与 `src/agents/<name>.py`（merge / impact / consistency / writeback）。
每个子智能体只读 `PipelineState` 的若干字段、只写自己负责的 channel（见 §1）；**绝不跨模块直接调用**。
下表每个子智能体一节，列出输入（来自主 state 哪个字段）→ 输出（写回主 state 哪个字段）→ 是否调 LLM。

### 3.1 节点速查

| stage | 输入（主 state 字段） | 输出（写回主 state） | LLM? | 纯函数签名 / 证据 |
|---|---|---|---|---|
| partition | `original_doc` | `original_paragraphs`, `argument_tree=[]` | 否（纯代码） | `assembly.py` `_partition_node` |
| parse | `original_paragraphs` | `argument_tree` | **是** | `parse(original_paragraphs, llm) -> list[Argument]` `parser/agent.py` |
| hitl1 | `argument_tree` | `argument_tree` | 否（HITL 桩） | `confirm(argument_tree, gate) -> list[Argument]` `hitl1/agent.py` |
| verification | `argument_tree` | `argument_credibility` | **是** | `verify(argument_tree, llm, retrieval, max_iterations=8) -> dict[str, ArgumentStatus]` `verification/agent.py` |
| hypothesis | `argument_tree` | `hypotheses` | **是** | `hypothesize(argument_tree, llm, retrieval, max_iterations=8) -> dict[str, list[Hypothesis]]` `hypothesis/agent.py` |
| merge | `argument_tree` + `argument_credibility` + `hypotheses` | `argument_tree` | 否（纯函数） | `merge_with_partials(...)` `merge.py` |
| impact | `argument_tree` | `argument_tree` | 否（纯函数） | `impact(argument_tree) -> list[Argument]` `impact.py` |
| consistency | `argument_tree` | `argument_tree` | 否（纯函数） | `consistency(argument_tree) -> list[Argument]` `consistency.py` |
| hitl2 | `argument_tree` + `original_paragraphs` | `argument_tree` | 否（HITL 桩） | `confirm(argument_tree, original_paragraphs, gate) -> list[Argument]` `hitl2/agent.py` |
| writeback | `argument_tree` + `original_paragraphs` | `final_document` + `argument_tree` | 否（纯函数） | `writeback(argument_tree, original_paragraphs) -> WritebackResult` `writeback.py` |

> **覆写规则**：verification 只产 `ArgumentStatus`、hypothesis 只产 `list[Hypothesis]`——二者写 partial channel，merge 字段级合流（`status` ← 体检、`candidate_hypotheses` ← 开药）后整树写 `argument_tree`。
> merge/impact/consistency/hitl2/writeback 直接整树覆盖写 `argument_tree`，但各自只动自己负责的字段、**不碰 `content`**（HITL-2 `EditContentOp` 例外）。

### 3.2 各子智能体可改字段（一句话边界）

- **partition**：产 `original_paragraphs`（只读原文表）+ 空树。纯代码、字节级自检失败硬停（不包 `_guarded`）。
- **parse**：建初始树。铸 `argument_id` / `parent_id` / `children_ids` / `paragraph_id` / `content`(拷回) / `argument_weight`(clamp) / `status=unverified`。无提议段落降级为 `background` 影子节点。
- **hitl1**：人审结构编辑。可改 `argument_id` / `parent_id` / `children_ids` / `argument_type` / `argument_weight`；不改 `content`。
- **verification**：只产可信度裁决 `ArgumentStatus`（终态 `credible/doubtful/error`）；覆盖范围 `main_claim/sub_claim/evidence`。
- **hypothesis**：只产候选假设 `list[Hypothesis]`；覆盖范围 `evidence/sub_claim`。propose 不读体检结论、不读检索（ADR-0002 解法 A）。
- **merge**：合流 partial 后写 `candidate_hypotheses`(裁剪存活) / `issue_tags`(追加 `conflict`) / `merge_decision`；不置 `adopted`。
- **impact**：判 `main_claim/sub_claim`，`invalid` 翻 `status=invalid` + 复用 supported 假设激活 `merge_decision`；`weaken` 仅贴 `weakening` tag。
- **consistency**：只追加 `issue_tags`（`mixed_paragraph_kind` / `multi_primary_per_paragraph` / `multi_main_claim` / `duplicate_qualification`）。
- **hitl2**：`AdoptOp`→`status=adopted`+`adopted_hypothesis_id`；`RejectOp`→移除假设；`EditContentOp`→覆写 `content`。硬闸门，`Hitl2GateError` 原样上抛不兜底。
- **writeback**：据 `adopted_hypothesis_id` 分流缝合（oppose→REPLACE / advance→REWRITE / expand→SUPPLEMENT 段尾追加）；`adopted→corrected`；产 `final_document`。

## 4. LLM seam 总表

全仓**共 4 个 LLM 调用点**，集中在 `src/infra/llm_adapters.py`（真实 adapter `Qwen*` + 离线 `Fake*` 双 adapter）。
merge / impact / consistency / writeback / hitl1 / hitl2 均**无 LLM**（纯函数 + 同步人工桩）。

> **输入压缩铁律**：三条 LLM seam **只把「原文文本 + 检索 snippet」喂给 LLM**，绝不回灌 `status` / `argument_weight` / `parent_id` / `children_ids` / `issue_tags` / `merge_decision` 等内部状态字段。
> parse 按段喂、不整篇 dump；verify/hypothesis 只注 `argument_type + content` + 压缩后 observations（≤20 条）。
> 真实 adapter 用扁平信封（`_VerifyEnvelope` / `_HypothesisVerifyEnvelope` / `_ProposalsEnvelope`）规避部分 provider 对 `oneOf` 判别联合的不稳（`llm_adapters.py` 模块 docstring），逻辑等价于 contract 的判别联合。

| # | stage | Protocol 方法（contract 定义） | 进 LLM 的 state 字段 | 输入形式 | structured output 模型 | 写回字段 | 证据 |
|---|---|---|---|---|---|---|---|
| 1 | parse | `LlmClient.parse(paragraphs: list[ParagraphView]) -> ParseResult`（`parser/contract.py`） | `original_paragraphs` 的段落原文 | prompt 拼接：每段 `[paragraph_id] 文本`，多段 `\n\n` 拼，内嵌 `WEIGHT_RUBRIC` 明文 rubric | `ParseResult`（`proposals: list[ParsedNodeProposal]`，字段 `paragraph_id/argument_type/parent_index/argument_weight`，**不含 content**） | `argument_tree`（agent 据返回铸 `Argument`，逐字节拷回 `content`） | `llm_adapters.py` `QwenParseLlmClient`；调用 `parser/agent.py` |
| 2 | verification | `VerifyLlmClient.next_step(argument, observations) -> SearchStep \| ConcludeStep`（`verification/contract.py`） | `argument.argument_type.value` + `argument.content`；累积 `list[Source]`（来自 `HistoryStore.compressed_view()`） | prompt 拼接：注入 `argument_type` + `content` + observations 压缩列表（`Source.kind/origin/title/snippet`，≤20 条）；**手写 ReAct 循环**（非 langchain tool-calling），LLM 每步产 `SearchStep \| ConcludeStep`，循环外调 `RetrievalTool` 执行检索 | `_VerifyEnvelope`（扁平信封）→ `to_step()` 映射回 `VerifyStep` | `argument_credibility`（仅写 `ArgumentStatus`，映射 `_VERDICT_TO_STATUS`） | `llm_adapters.py` `QwenVerifyLlmClient`；调用 `verification/agent.py` |
| 3 | hypothesis / propose | `HypothesisLlmClient.propose(argument) -> list[HypothesisProposal]`（`hypothesis/contract.py`） | `argument.argument_type` + `argument.content` | prompt 拼接：注入 `argument_type` + `content`，要求 0..N 条可证伪假设，每条恰一种 relation | `_ProposalsEnvelope`（包 `list[HypothesisProposal]`：`text/relation/confidence`） | `hypotheses`（agent 铸 `Hypothesis`，写候选假设列表） | `llm_adapters.py` `QwenHypothesisLlmClient`；调用 `hypothesis/agent.py` |
| 4 | hypothesis / 逐条取证 | `HypothesisLlmClient.next_verify_step(hypothesis_text, observations) -> HypothesisSearchStep \| HypothesisConcludeStep`（`hypothesis/contract.py`） | `proposal.text`（**假设文本，非 `argument.content`**） + 累积 `list[Source]` | prompt 拼接：注入 `hypothesis_text` + observations 列表；同 #2 手写 ReAct 循环 + `RetrievalTool` | `_HypothesisVerifyEnvelope` → `to_step()` 映射回 `HypothesisVerifyStep`（`verdict: supported/doubted/refuted`） | `hypotheses`（写 `Hypothesis.status`，随假设列表一并落回） | `llm_adapters.py` `QwenHypothesisLlmClient`；调用 `hypothesis/agent.py` |

### 4.1 ReAct 历史载体说明

`infra/history.py` 当前「历史」是检索观察 `list[Source]`（非 `BaseMessage` 聊天轮次）。
`Source` 结构（`infra/retrieval.py`）：`source_id / kind / origin / title / snippet / locator`。
`HistoryStore.compressed_view()` 压缩后回喂 LLM（`DEFAULT_COMPRESSION.max_items=20`）。
`session_id` 线程已预留但当前未消费（ADR-0016）——是升级为真实消息轮次的扩展点。

## 5. 维护与扩展约定

- **新增 state 字段**：在 `PipelineState`（`orchestrator.py:99`）加键 + reducer（多写者必需），并在 §1 表格补一行，注明创建者 stage 与读者。
- **新增 argument 字段**：在 `Argument`（`domain.py:140`）加字段，在 §2 表格补一行，标注唯一写入者（**单写者优先**，避免字段级合流歧义）。
- **新增/变更 LLM seam**：在 §4 总表补一行，写清进 LLM 的 state 字段与输入形式；同步更新该子智能体 contract 的 `*LlmClient` Protocol。
- **本文不重复解释业务逻辑**：裁决矩阵、状态机迁移、回写分流等规则见对应 ADR 与 `domain.py` docstring，本文只给字段流向。
