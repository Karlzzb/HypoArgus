# ADR-0017：流水线重构

## 状态

已接受（2026-07-13 起；2026-07-15 整合为统一偏离记录）。
本 ADR 是流水线重构的**统一偏离记录**，整合并取代下列原单条 ADR：

- 原 ADR-0017（重写阶段放弃字节一致 / 段落原文不被 LLM 改写 / 幂等纯函数回写）→ §1
- 原 ADR-0018（hitl1 重定义为 partition 确认闸门 + 有界打回）→ §2
- 原 ADR-0019（verification / hypothesis 取证 / merge / impact / consistency 五合一为 judgment）→ §3
- 原 ADR-0020（partition 变 prompt 驱动）→ §4
- 原 ADR-0021（贯穿 state 落 PipelineState）→ §5
- 原 ADR-0016（RunnableConfig 承载 langgraph 原生机制）的**存活残余** → §6（其 HistoryStore 部分随五合一删除，见 §3）

配套术语见 `CONTEXT.md`「重构方向术语」；字段流向见 `docs/STATE.md` §1.2；模块边界与装配见 `docs/DEVELOPMENT.md` §1/§2。
本 ADR 部分覆盖 ADR-0009（确定性切分，§4）、ADR-0011（adopted→corrected 回写幂等，§1）；ADR-0005（两层存储）与 ADR-0010（HITL-2 硬闸门）不动。

## 背景

重构前的拓扑（`docs/DEVELOPMENT.md` §1 旧版）把「检索之后的判断」分散在五个图节点：

- `verification`（体检，线路 1）：ReAct 逐段检索取证，写 `argument_credibility` partial channel。
- `hypothesis`（开药，线路 2）：propose 生成假说 + ReAct 逐条取证，写 `hypotheses` partial。
- `merge`：读两 partial，字段级合流后跑 12 格矩阵裁决（ADR-0006），写 `argument_tree`。
- `impact`：剩余支撑率传导，判 `invalid` / `weakening`，写 `argument_tree`。
- `consistency`：单次扫描贴 `issue_tags`，写 `argument_tree`。

体检 ∥ 开药的并行前提是「两线路各自 ReAct 逐段逐点重复发起检索」（ADR-0002 乐观并行）。
既有终稿产出是**确定性纯函数回写**：段落原文永不被 LLM 改写、回写据语义关系做子串替换 / 段尾追加、始终从原始 bytes 重新推导整篇终稿（`supplement` 永不累积，ADR-0011）。
`tracer bullet` 承诺：「无任何采纳改动时，终稿与原始输入逐字节完全一致」，贯穿全部 stage。
`hitl1` 是「结构确认、可跳过」闸门，图层级单向推进、**绝不打回**（`DEVELOPMENT.md` §5 单向流控）。
`session_id` 线程埋于 `RunnableConfig`（原 ADR-0016），业务节点不强制读 config（零侵入）。

新产品方向要求：

1. 对**被证据 / 假说触达的段落**，由 LLM 基于全文证据起草一版**连贯重写文本**，而非机械子串替换——与字节一致 / 原文不改写 / 幂等纯函数回写三条承诺冲突。
2. partition 之后由人确认**段落切分是否合理**，不合理时**按用户 prompt 重跑 `parse+partition`**——要求 hitl1 具备「打回重跑上游」能力，直接打破「绝不打回」。
3. 检索统一前置为**单一批量检索节点 `retrieval`**（一次发起、统一返回全部 citations）——并行双线路失去并行前提，五节点控制流冗长。
4. partition 能**按用户 prompt 重切**——纯代码规则切分无法响应 prompt，直接打破 ADR-0009 确定性。
5. LLM 检索与生成的输入都带上**贯穿全链的运行上下文**（`session_id` / `user_id` / `current_time` / `user_prompt` / `query_time_range`）——这些是业务消费字段（要进 LLM prompt），不能仅走 `RunnableConfig`。

以下六节分别记录五项偏离 + 一项存活残余。

## §1 重写阶段放弃字节一致（原 ADR-0017）

### 背景

既有终稿产出是确定性纯函数回写：段落原文 `original_content`（存于 `ParagraphRecord`，见 ADR-0025）永不被 LLM 改写；回写据「假说与原文的语义关系」做子串替换 / 段尾追加（ADR-0004 / ADR-0006 三操作：oppose→REPLACE、advance→REWRITE、expand→SUPPLEMENT）；回写幂等，始终从原始 bytes 重新推导整篇终稿（ADR-0011）；tracer bullet 承诺贯穿全部 stage。
但新产品方向要求对被证据 / 假说触达的段落由 LLM 基于全文证据起草连贯重写文本——重写文本由 LLM 生成、不逐字节等于原文、无法以纯函数幂等回写还原。

### 决策

对**被触达段**放弃三条承诺，对**未触达段**与「HITL-2 为唯一决策闸门」承诺保持：

1. 重写阶段（被触达段）放弃：「终稿逐字节一致」；「段落原文永不被 LLM 改写」——被触达段的终稿文本由 LLM 提议重写（`rewrite_loop` 节点；`original_content` 仅作推理输入喂 LLM，终稿落 `proposed_rewrites`、不回写 `ParagraphRecord.original_content`）；「幂等纯函数回写」——重写是 LLM 生成式，非纯函数子串替换。
2. 未触达段仍逐字节忠实：未被任何 supported 假说 / 相关 citation 触达的段落，终稿逐字节拷回原文（底线不变）。
3. HITL-2 仍是唯一决策闸门（ADR-0010 不动）：`rewrite_loop` 只**提议**重写文本（`proposed_rewrites: dict[paragraph_id, str]`），HITL-2 逐段确认 / 编辑 / 驳回后才落 `final_document`。被确认段用提议文本、被驳回段回退原文、未触达段逐字节原文 → 拼成终稿。绝不替人拍板自动采纳。
4. 原 `writeback` 节点裁撤：终稿在 HITL-2 落地，不再有独立回写节点；`adopted → corrected` 状态机（ADR-0011）的回写幂等续跑语义随 `rewrite_loop` 重写适配（`tests/test_orchestrator_resume.py`）。
5. 字节一致弱化不变式：E2E 断言「无触达 / 继续路径下 `final_document == original_doc`」仍成立（hitl1 继续 / rewrite 无触达段）。

### 权衡

放弃「段落原文永不被 LLM 改写」换取基于全文证据的连贯重写能力——机械子串替换无法实现。
代价：被触达段终稿不再字节级确定，回写幂等性弱化为「未触达段逐字节 + 被触达段以 HITL-2 确认文本为准」。
用「仅被触达段放弃、未触达段逐字节忠实」二分把破坏面收敛到最小；用「HITL-2 确认闸门」把生成式重写的决策权仍锁在人手里。

### 影响

新增 `rewrite_loop` 节点（逐段 LLM 提议重写）与 `proposed_rewrites: dict[str, str]` channel（单写者=rewrite_loop、读者=hitl2）。
`hitl2` 重定位为**终稿文本确认闸门**（见 §2 同期变更的 hitl1 闸门语义对照）。
裁撤 `writeback` 节点；`final_document` 由 hitl2 拼装落地。
`tests/test_writeback.py` 改 / 扩为 rewrite_loop + hitl2 终稿拼装的纯函数子缝（提议→确认→拼接）。
字节级还原承诺的范围**收窄**为「未触达段逐字节忠实」，已同步更新 `CONTEXT.md` / `DEVELOPMENT.md` §1 的 tracer bullet 表述。

## §2 hitl1 partition 确认闸门 + 有界打回（原 ADR-0018）

### 背景

既有 `hitl1` 是「结构确认、可跳过」闸门（`agents/hitl1/{contract,agent}.py`）：人审结构编辑，动作集 `SKIP / ACCEPT / EDIT`，图层级单向推进、**绝不打回**上游。
单向流控是既有强约束（`DEVELOPMENT.md` §5）：异常即记日志、就地降级、继续向前，无复杂分布式重试。
但新产品方向要求 partition 之后由人确认段落切分是否合理，不合理时按用户 prompt 重跑 `parse+partition`——要求 hitl1 具备「打回重跑上游」能力，直接打破「绝不打回」与「严格单向」。
若打回无界，人 / LLM 反复卡住可致无限循环。

### 决策

1. hitl1 重定义为 **partition 确认闸门**：人确认段落切分是否合理；合理则继续下游，不合理则打回重跑 `parse+partition`（按用户 prompt）。
2. 打回打破「绝不打回」：唯一受控打回点为 `hitl1 → parse+partition`，方向回上游；其余 stage 仍严格单向。
3. 打回须有界：max retries（默认 3，可配置）。计数器随 hitl1 打回递增；超限仍**向下推进**，并在 `errors` 追加 `partition_retry_exhausted` 标签（沿用 `_log_error_patch` 语义，但**不视为异常降级**——受控分支、不经 `_guarded` 的异常降级路径）。
4. partition「按 prompt 重切」当前为伪代码桩（§4）：不真实 LLM 驱动重切，只穿 state、原样或占位重切。
5. hitl1 contract 动作集调整：`Hitl1Decision` 的 action 对齐为「确认继续 / 打回重跑」两类语义，以实现时不破坏既有 `FakeHitl1Gate` 离线桩为准（既有 `skip/accept/edit` 与新语义对齐或收编）。

### 权衡

选「有界打回」而非「保持严格单向」：换取在生成假说 / 检索之前纠正不合理切分的能力——切分不合理时下游整条链路都建立在错误段落边界上，事后纠正代价更高。
代价：打破既有「绝不打回」强约束，引入受控循环；用 max retries 把循环有界化。
选「超限向前 + 贴标签」而非「超限硬停」：与既有 `_guarded` 单向流控语义一致，不引入新硬停点（HITL-2 仍是唯一硬闸门）。

### 影响

`hitl1` 节点 build 闭包重定义：读 `parse+partition` 产出的 `argument_tree` + `paragraph_list` + `original_paragraphs` 供人确认；打回时按 prompt 重跑。
`hitl1` 的 `deps` 改为 `("parse+partition",)`（随 §3 的合并节点对齐）。
新增受控打回边 `hitl1 → parse+partition`；图装配（`agents/assembly.py` `MANIFEST` / `runtime/orchestrator.py` `default_pipeline`）须表达该边。
`tests/test_hitl1.py` 覆盖：确认继续、打回一次后继续、打回超限贴标签向前。
`tests/test_orchestrator_fallback.py` 扩 hitl1 打回超限分支。

## §3 五合一为 judgment 节点（原 ADR-0019）

### 背景

既有「检索之后的判断」分散在五个图节点（`verification` / `hypothesis` 取证 / `merge` / `impact` / `consistency`，见上「背景」）。
体检 ∥ 开药的并行前提是两线路各自 ReAct 逐段逐点重复发起检索（ADR-0002 乐观并行）。
但新产品方向把检索统一前置为单一批量检索节点 `retrieval`（一次发起、统一返回全部 citations）。
检索既已统一前置，并行双线路失去并行前提——两线路不再各自检索，而是消费同一批 citations，并行收益消失；此时仍保留五节点则控制流冗长、节点数偏多。

### 决策

合并检索之后的判断节点为**单一 `judgment` 节点**：

1. 节点数压成 1：裁撤 `verification` / `hypothesis`（取证）/ `merge` / `impact` / `consistency` 五个 `AgentEntry`，新增单一 `judgment` 的 `AgentEntry`（`deps=("retrieval",)`）。
2. LLM 取证集中：judgment 节点引入新 LLM seam，吃 `(argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range)` → 产 `(ArgumentStatus per argument, HypothesisStatus per hypothesis)`。该 seam 取证职责承接原 `VerifyLlmClient.next_step` 与 `HypothesisLlmClient.next_verify_step`：吃 `citations` 直接判终态，**不再 ReAct 逐段逐点检索**。真实 adapter 用扁平信封 schema（延续 `infra/llm_adapters.py` 既有风格，规避 `oneOf` 判别联合不稳）。
3. 纯函数逻辑保留、不交 LLM 裁决：`merge`（12 格矩阵，ADR-0006）、`impact`（剩余支撑率，ADR-0003/0013）、`consistency`（标签批注，ADR-0012）的纯函数逻辑**不动**，并入 judgment build 闭包按序调用。即「五合一」是**控制流合并**，不是「把裁决交给 LLM」——确定性裁决逻辑仍是纯函数。
4. partial channel 收口：取证不再分两线路写两个 partial channel。`argument_credibility` partial 裁撤（judgment 单写者直接整树写回 `argument_tree`，PRD §24）；`hypotheses` channel 形状随取证落终态更新：propose 阶段写入的 `Hypothesis.status` 为 pending，由 judgment 取证后落终态（supported / doubtful / refuted）。
5. 兜底：judgment 整体异常 → 覆盖范围内未判决节点置 `error`（沿用现 `_mark_verify_scope_error` 语义）；经 `_guarded` 降级 + 单向向前。

### 权衡

选「五合一」而非「保留五节点」：检索统一前置后并行双线路失去并行前提，五节点控制流冗长；合并为单节点简化后端控制流。
代价：judgment 节点职责变重（取证 + 三套纯函数裁决）；用「纯函数逻辑不动、按序调用」把复杂度收口在 build 闭包内，确定性裁决不退化。
选「merge/impact/consistency 保纯函数」而非「整体交 LLM」：保留确定性裁决、可单测、可解释（ADR-0006/0012/0013 的不变性不破）。
选「裁撤 `argument_credibility`」而非「保留 partial」：judgment 单写者直接写回树时，partial channel 是死字段；裁撤使 state 不留死字段。

### 影响

`MANIFEST`：裁撤五 `AgentEntry`、新增 `judgment` 一条；`default_pipeline()` 派生 `retrieval → judgment` 边。
新 judgment LLM seam 契约（`agents/judgment/contract.py`）；真实 adapter 落 `infra/llm_adapters.py`。
`merge` / `impact` / `consistency` 纯函数单测（`tests/test_merge.py` / `test_impact.py` / `test_consistency.py`）不变——验证「并入但逻辑不动」。
原 ReAct 独占 infra 删除：`infra/retrieval_tool.py` / `infra/tool_protocol.py` / `infra/history.py`（RetrievalTool / ToolRegistry / HistoryStore，原 ADR-0015/0016 引入）——五合一后 judgment 吃预取 citations、不再有 ReAct 循环与步间历史。
`argument_credibility` 裁撤决定在 `docs/STATE.md` §1 同步。
`tests/test_orchestrator_fallback.py` 覆盖 judgment 降级（未判决节点置 `error`）。

## §4 partition 变 prompt 驱动（原 ADR-0020）

### 背景

ADR-0009 把「段落切分」与「论证树解析」彻底解耦：partition 是纯代码、零 LLM 的确定性无损切分（Markdown 块级 + 空行边界），灌只读原文表并校验分区不变式 `assert(所有段落按序拼接 == 原始输入)`。字节级还原由此成为代码级确定，与 LLM 质量无关。
但新产品方向要求 hitl1 打回时（§2）partition 能按用户 prompt 重切（合并语义连续的跨段、拆分过长段）。纯代码规则切分无法响应 prompt——它只能按固定块级边界切。即 partition 需从「纯代码确定」变为「prompt 驱动」，直接打破 ADR-0009 的确定性。

### 决策

1. partition 变 prompt 驱动：partition 接收用户 prompt，可据 prompt 重切段落边界（合并 / 拆分 / 调整）。
2. 当前为伪代码桩（PRD §21 / Out of Scope）：不真实 LLM 驱动重切，只穿 state、原样或占位重切；真实 prompt 驱动重切（含 LLM 驱动 vs 结构化 hint 参数的抉择、max retries 调参）为后续切片。
3. 无损性须保：即便 partition 变 prompt 驱动，分区不变式 `assert(各段按序拼接 == original_doc)` 仍须通过——这是字节级还原 / 回写 / 还原真相源的底线（ADR-0005/0009）。prompt 驱动只调整切分边界，**不增删字符**；切分后仍须通过字节级自检。
4. partition + parse 合并为单一图节点 `parse+partition`（PRD 拓扑）：partition 的纯代码切分 + 字节级自检、parse 的建树主逻辑**不动**；新增的是同一 LLM 调用多吐结构化输出（`query_time_range` / `paragraph_list`，见 §5 / STATE.md）。hitl1 打回重跑的靶节点即 `parse+partition`（§2）。

### 权衡

选「partition 变 prompt 驱动」而非「保持纯代码确定」：换取响应人审意见调整切分边界的能力——切分不合理时固定块级边界无法纠正。
代价：打破 ADR-0009 确定性，切分质量与 prompt / LLM 质量相关；用「无损性须保」把破坏面收敛为「边界可调、字符不增删」，字节级还原底线不破。
当前桩化：真实 prompt 驱动重切的实现抉择推迟到后续切片，当前不阻塞主线；桩路径下终稿对未触达段仍逐字节等于原文。

### 影响

`parse+partition` 节点接收用户 prompt（经 `session_context.user_prompt`，§5）；partition 重切为伪代码桩。
partition 字节级自检（各段拼接 == `original_doc`）仍保留、仍硬停（不包 `_guarded`，ADR-0009 语义）。
hitl1 打回（§2）的靶节点为 `parse+partition`；打回超限贴 `partition_retry_exhausted`。
`tests/test_parser.py`：partition + parse 合并节点的字节级自检不变。
真实 prompt 驱动重切属后续切片（Out of Scope），当前桩可断言。

## §5 贯穿 state 落 PipelineState（原 ADR-0021）

### 背景

原 ADR-0016 把 `session_id` 线程埋入 `RunnableConfig`（`Orchestrator.run(doc, *, session_config=None)` 透传 `config=` 给 `graph.invoke`），明确「业务节点不强制读 config（零侵入）」，`session_id` 为 checkpointer 持久化键预备、当前不消费。`RunnableConfig` 承载 langgraph 原生 metadata / callbacks / checkpointer。
但新产品方向要求 LLM 检索与生成的输入都带上贯穿全链的运行上下文（PRD §15/§16/§17）：`session_id` / `user_id` / `current_time` / `user_prompt`（同一会话内多轮调用有一致运行上下文）、`query_time_range`（文章所需的数据查询时间范围，下游检索能限定在正确时间窗内）。
这些是**业务消费字段**（要进 LLM prompt），不是 langgraph 原生 metadata。
若仍只走 `RunnableConfig`，则每个业务节点须显式从 config 读 → 触点多、类型契约弱、typed `Agents` 无法写明依赖。

### 决策

1. 贯穿 state 落 `PipelineState`：新增 `session_context: SessionContext`（`session_id` / `user_id` / `current_time: datetime` / `user_prompt`）与 `query_time_range: TimeRange`（`start: date | None` / `end: date | None` / `rationale: str`）为 `PipelineState` 顶层 channel。
2. 单写者：`session_context` 单写者=入口注入（`runtime/run_real.py`，与 `original_doc` 同入 START），全链只读；`query_time_range` 单写者=`parse+partition`（当前为桩值 `TimeRange(start=2025, end=2026, rationale="默认值·真实识别待后续")`，不真实调 LLM 识别），读者=retrieval / rewrite / judgment。
3. 以单一嵌套对象流转：`session_context` 作为单一 `SessionContext` 嵌套对象在 `PipelineState` 中流转，**不污染顶层 channel**（不把 `session_id` / `user_id` / ... 各拆成顶层字段），typed 契约能写明依赖。
4. `RunnableConfig` 职责收窄（原 ADR-0016 部分覆盖）：`RunnableConfig` 仍承载 langgraph 原生 metadata / callbacks / checkpointer（见 §6）；业务相关贯穿 state（`session_context` / `query_time_range`）改走 `PipelineState` channel，业务节点读 channel 而非 config。
5. 输入压缩铁律延续：`session_context` / `query_time_range` 作为背景进 LLM prompt（检索与生成 seam），但仍不回灌 `status` / `argument_weight` / `parent_id` / `children_ids` / `issue_tags` / `merge_decision` 等内部状态字段。
6. `current_time` 注入方式：真实运行时刻由入口注入 `session_context.current_time`（非节点内 `datetime.now()`），保证可测、可复现。

### 权衡

选「贯穿 state 落 `PipelineState`」而非「继续走 `RunnableConfig`」：业务消费字段要进 LLM prompt、要 typed 契约写明依赖；走 channel 则单写者 / 读者 / reducer 清晰，节点签名能声明依赖。
代价：`PipelineState` 顶层字段增多；用「单一嵌套对象」把 `session_context` 收口为一字段，避免顶层膨胀。
`RunnableConfig` 不废弃：langgraph 原生机制（callbacks / checkpointer）仍走 config，二者分层（业务字段走 channel、原生机制走 config）。

### 影响

`src/domain.py` 新增 `SessionContext` / `TimeRange` 域类型。
`PipelineState`（`runtime/orchestrator.py`）新增 `session_context` / `query_time_range` / `citations` / `proposed_rewrites` 等 channel（reducer / 单写者 / 读者见 STATE.md §1）。
`runtime/run_real.py` 入口接收 `session_context`（与 `original_doc` 同入 START）。
retrieval / rewrite / judgment / hypothesis_propose 节点读 `session_context` / `query_time_range` 进 LLM prompt。
`docs/STATE.md` §1 为新字段唯一描述点（本 ADR 不重复字段流向）。

## §6 RunnableConfig 承载 langgraph 原生机制（原 ADR-0016 存活残余）

### 背景

原 ADR-0016 引入 `HistoryStore[Source]`（ReAct 步间记忆）并把 `session_id` 线程埋入 `RunnableConfig`（`Orchestrator.run(doc, *, session_config=None)` 透传 `config=` 给 `graph.invoke`），明确「业务节点不强制读 config（零侵入）」。
§3 五合一删除了 ReAct 循环与步间历史（`infra/history.py` 随之删除），故 `HistoryStore` 部分已废弃。
但 `session_config` 透传 `RunnableConfig`、由其承载 langgraph 原生 metadata / callbacks / checkpointer 的**线程仍在**（§5 把业务字段移出 RunnableConfig 后，原生机制仍走 config），故本节保留该存活残余。

### 决策（存活部分）

1. `Orchestrator.run(doc, *, session_config=None)` 透传 `config=` 给 `graph.invoke`；`session_config` **不是 `PipelineState` 字段**，是透传给 langgraph `RunnableConfig` 的依赖。
2. `RunnableConfig` 仍承载 langgraph 原生 metadata / callbacks / checkpointer（ADR-0022 的 `PostgresSaver` 即经此挂载）；业务相关贯穿 state 改走 `PipelineState` channel（§5），二者分层。
3. 业务节点不强制读 config（零侵入）；`session_id` 为 checkpointer 持久化键（`thread_id = session_id`，ADR-0022）。

### 影响

`Orchestrator.run` 保留 `session_config` 形参；`RunnableConfig` 与 `PipelineState` 分层（原生机制走 config、业务字段走 channel）。
原 `HistoryStore` / `CompressionConfig`（部分移植自 `docs/graph_utils.py`）随 §3 删除，不再存在。

## 影响汇总

- 拓扑（重构后）：`START → parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → END`（hitl1 经条件边有有界打回回 `parse+partition`）。
- 字节级还原承诺范围收窄为「未触达段逐字节忠实」；被触达段由 rewrite_loop 提议、hitl2 确认。
- HITL-2 仍为唯一决策闸门（ADR-0010）；hitl1 为 partition 确认闸门 + 有界打回。
- 删除 ReAct 独占 infra（`tool_protocol.py` / `retrieval_tool.py` / `history.py`）；judgment 吃预取 citations、串联 merge/impact/consistency 纯函数。
- `session_context` / `query_time_range` 落 `PipelineState`；`RunnableConfig` 收窄为 langgraph 原生机制。
- 字段流向单一定义点为 `docs/STATE.md`；术语见 `CONTEXT.md`「重构方向术语」。
