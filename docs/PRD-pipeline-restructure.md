# PRD：流水线重构（parse+partition 合并 / 批量检索节点 / judgment 合一 / 逐段重写循环 / 贯穿 session state）

本文是**独立可执行**的 PRD：后续新会话可仅凭本文 + `CONTEXT.md` / `docs/STATE.md` / `docs/DEVELOPMENT.md` / `docs/adr/` 直接进入计划与实现。
术语一律沿用 `CONTEXT.md` 的统一语言；字段名以 `src/domain.py` / `src/runtime/orchestrator.py` 为准。
本 PRD 同时登记对既有 ADR 的偏离（见「Implementation Decisions · ADR 偏离」），需以新 ADR 固化。

## Problem Statement

当前流水线 `partition → parse → hitl1 → (verification ∥ hypothesis) → merge → impact → consistency → hitl2 → writeback` 在五个点上不满足新的产品方向：

1. partition 与 parse 是两个图节点，但语义上是同一次"读文档→产结构"的工作，且后续检索所需的"数据查询时间范围"无处产出、无法传递给下游。
2. 检索（retrieval）藏在 verification / hypothesis 各自的 ReAct 循环里、逐段逐点重复发起，不是统一的批量检索口；真实检索后端接入时无单一入口承接。
3. 检索之后的 merge / impact / consistency 三个判断节点在"检索已统一前置"后失去并行/分步收益，节点数偏多、控制流冗长。
4. 终稿产出是确定性纯函数回写（按假说关系做子串替换/段尾追加），无法做"基于全文证据重写整段"的 LLM 生成式修订。
5. 主智能体 state 缺少贯穿全链的运行上下文（session / 用户 / 时间 / 用户提示词），子智能体无法在 LLM 输入里携带这些背景。

## Solution

把流水线重构为如下单向图（打回仅在 hitl1→partition 一处、有界）：

```
START → parse+partition
          产 argument_tree + paragraph_summaries + query_time_range
        → hitl1                  （确认 partition；可打回重跑 partition，有界）
        → hypothesis_propose     （逐 argument 产候选假说，status 待定）
        → retrieval              （批量检索，统一返回 citations）
        → judgment               （LLM 取证 + 纯函数 merge/impact/consistency）
        → rewrite_loop           （逐段 LLM 提议重写；未触达段逐字节拷回）
        → hitl2                  （人确认终稿文本）
        → final_document
END
贯穿 state：session_context(session/user/current_time/user_prompt) + query_time_range
```

核心取舍：**重写阶段**（被触达段）放弃"终稿逐字节一致 / content 永不被 LLM 改写 / 幂等纯函数回写"三条承诺；**未触达段**仍逐字节忠实；**HITL-2 仍为唯一决策闸门**，重写只"提议"、HITL-2 确认终稿文本后才落 `final_document`。

## User Stories

1. 作为文档作者，我想提交一篇文档 + 一条修订提示词，这样系统就能据我的意图产出修订后的终稿。
2. 作为文档作者，我想让系统先判断"本文所需的数据查询时间范围"，这样下游检索能限定在正确的时间窗内。
3. 作为文档作者，我想在 partition 之后由人确认段落切分是否合理，这样不合理的切分能在生成假说/检索之前被纠正。
4. 作为人审员，我想在 hitl1 给出"切分不合理"的修改意见（或仅表示打回）后，系统能按我的 prompt 重跑 partition，这样切分能向我期望的方向调整。
5. 作为人审员，我想 hitl1 的打回有最大重试上限，这样不会因为人/LLM 反复卡住而无限循环。
6. 作为系统，我想为每个论证点（evidence / sub_claim）逐点生成可证伪候选假说，这样后续能对这些假说批量取证。
7. 作为系统，我想让假说生成读取"段落摘要"而非整段原文，这样一次性/逐点喂入时上下文不致爆炸。
8. 作为系统，我想有一个批量检索节点接收"全部论证点 + 全部假说 + 对应 paragraph_id + 时间范围"，这样检索只发起一轮、统一返回全部 citations。
9. 作为系统，我想检索节点当前以伪代码桩运行、只穿 state、产出下游所需 citations channel，这样真实检索后端可后续切片接入而拓扑不动。
10. 作为系统，我想在检索之后用一个 judgment 节点一次性完成 verification 取证 + hypothesis 取证 + merge 裁决 + impact 传导 + consistency 批注，这样后端控制流简化为单节点。
11. 作为系统，我想 judgment 内部仍以纯函数执行 merge 的 12 格矩阵裁决、impact 的剩余支撑率、consistency 的标签批注，这样确定性裁决逻辑不被 LLM 取代。
12. 作为文档作者，我想对每个被触达的段落由 LLM 起草一版重写文本，这样修订是基于全文证据的连贯重写而非机械子串替换。
13. 作为人审员，我想在 rewrite_loop 之后由 HITL-2 逐段确认/编辑/驳回提议的重写文本，这样终稿内容由人拍板、系统绝不替我决定。
14. 作为文档作者，我想未被任何证据/假说触达的段落逐字节保留原文，这样不动的段落保持字节级忠实。
15. 作为系统，我想在 LLM 检索与生成的输入里都带上"当前运行时刻"与"文章所需查询时间范围"两类时间背景，这样 LLM 决策有时间上下文。
16. 作为系统，我想在 LLM 检索与生成的输入里带上 session / 用户 / 用户提示词，这样同一会话内的多轮调用有一致的运行上下文。
17. 作为系统，我想贯穿 state 以单一 `session_context` 嵌套对象在 PipelineState 中流转，这样不污染顶层 channel、typed 契约能写明依赖。
18. 作为开发者，我想新增节点都经 manifest（`agents/assembly.py`）驱动 typed `Agents` + `default_pipeline`，这样加节点触点仍为 3、拓扑自动纳入。
19. 作为开发者，我想每个新/改节点的异常仍走 `_guarded` 统一兜底（HITL-2 硬闸门原样上抛），这样单点波动不卡死整篇。
20. 作为开发者，我想保留 partition 的字节级无损性（各段拼接 == 原始输入），这样回写/还原的真相源底线不破。
21. 作为系统，我想 partition 的"按 prompt 重切"当前以伪代码桩运行，这样真实 prompt 驱动重切可后续切片接入、当前不阻塞主线。
22. 作为开发者，我想 `query_time_range` 当前以默认值（2025–2026）伪代码桩运行，这样真实 LLM 时间识别可后续切片接入。
23. 作为人审员，我想 HITL-2 仍是不可跳过的硬闸门，这样"绝不替人拍板自动采纳"的承诺在重写阶段依然成立。
24. 作为开发者，我想重构后 `argument_credibility` 若被 judgment 直接写回 `argument_tree` 则可裁撤该 partial channel，这样 state 不留死字段。
25. 作为文档作者，我想终稿对未触达段逐字节等于原文、对触达段为 HITL-2 确认后的重写文本，这样修订有据、未改部分无损。
26. 作为系统，我想假说在 retrieval 之前生成（propose）、取证在 retrieval 之后的 judgment 内完成，这样检索能拿到假说作为查询输入。
27. 作为开发者，我想 rewrite_loop 写一个 `proposed_rewrites: dict[paragraph_id, str]` channel 供 HITL-2 消费，这样"提议/确认"两阶段在 state 上清晰分离。
28. 作为开发者，我想新拓扑的 fallback 语义随各 build 闭包落于 manifest（与现有一致），这样降级语义不散落到调度层。
29. 作为开发者，我想本重构不强制 `ruff format`、不重排既有文件，这样既有缩进风格保持。
30. 作为开发者，我想质量门仍是 `ruff check` + `mypy --strict` + `pytest`，这样重构后 lint/类型/测试失败一律修。

## Implementation Decisions

### 拓扑

- 合并 partition + parse 为单一图节点 `parse+partition`（一条 `AgentEntry` / `StageSpec`，`deps=()` 接 START）。
  partition 的纯代码切分 + 字节级自检、parse 的建树/content 逐字节拷回主逻辑**不动**；新增的是同一 LLM 调用多吐结构化输出。
- hitl1 重定义为 **partition 确认闸门**：人确认段落切分是否合理；不合理则打回重跑 `parse+partition`（按用户 prompt），合理则继续下游。
  打回**打破"绝不打回"**（PRD §13 / DEVELOPMENT §1），须**有界**（max retries，建议 3）。
  超限仍向下推进 + 贴 `partition_retry_exhausted` 标签。
- 新增 `hypothesis_propose` 节点：逐 argument 调 `propose`（**不取证**），产 `list[Hypothesis]`（status 待定）。
  覆盖范围沿用 evidence + sub_claim；propose 读取 `paragraph_summaries`（非整段 `content`）。
- 新增 `retrieval` 节点：批量接收 `argument_tree` + `hypotheses` + `paragraph_id` 列表 + `query_time_range` + `session_context`，内部循环查询，统一返回全部 citations。
  **当前为伪代码桩**：不真实检索，只把 state 接过、产出下游所需 `citations` channel（占位/空）。
  真实检索后端后续切片接入（白名单/权限/模板在 `infra/retrieval.py` 接口层强制，不变）。
- 合并检索之后的判断节点为单一 `judgment` 节点：LLM 取证（verification + hypothesis，吃 `citations` 判 `ArgumentStatus` / `HypothesisStatus`）+ 纯函数 `merge`（12 格矩阵）+ 纯函数 `impact`（剩余支撑率）+ 纯函数 `consistency`（标签批注）。
  即节点数压成 1，但 merge/impact/consistency 的纯函数逻辑保留、不交 LLM 裁决。
- 新增 `rewrite_loop` 节点：逐 `paragraph_id` 跑；对**被触达**段（该段有 supported 假说 / 相关 citations）由 LLM 提议重写文本；**未触达段逐字节拷回**。
  输入（每段）：`paragraph_summaries[pid]` + 该段节点（argument/evidence 结构 + 其 `candidate_hypotheses`）+ 该段聚合 citations + `session_context` + `query_time_range`。
  输出：`proposed_rewrites: dict[paragraph_id, str]`（仅被触达段）。
- hitl2 重定位为**终稿文本确认闸门**（Model P）：逐段确认/编辑/驳回 `proposed_rewrites`；被确认段用提议文本、被驳回段回退原文、未触达段逐字节原文 → 拼成 `final_document`。
  hitl2 仍不可跳过、`Hitl2GateError` 原样上抛、绝不替人拍板（承诺 4 保留）。
- 装配仍由 `agents/assembly.py` 的 `MANIFEST` 驱动：新增/合并节点各加/改一条 `AgentEntry`；`default_pipeline()` 自动派生新拓扑，不改 `runtime/orchestrator.py` 的图装配。

### 新增 / 变更 state 字段（`PipelineState`，`runtime/orchestrator.py`）

- `query_time_range: TimeRange`（`start: date | None` / `end: date | None` / `rationale: str`），单写者=`parse+partition`，读者=retrieval/rewrite/judgment。
  **当前伪代码桩**：默认 `TimeRange(start=2025, end=2026, rationale="默认值·真实识别待后续")`。
- `paragraph_summaries: dict[str, str]`（`paragraph_id → 摘要文本`），单写者=`parse+partition`（parse LLM 同一次调用顺产），读者=hypothesis_propose/rewrite_loop。
  不并入 `OriginalParagraphs`（保其字节级无损只读表身份，ADR-0005/0009）。
- `citations: dict[str, list[Source]]`，key 为 `argument_id`（`n0001`/`bg-...`）或 `hypothesis_id`（`h-...`，两套 id 不冲突），单写者=retrieval，读者=judgment/rewrite_loop。
  reducer 用 `_merge_dict`（单写者无冲突）。
- `proposed_rewrites: dict[str, str]`（`paragraph_id → 提议重写文本`），单写者=rewrite_loop，读者=hitl2。
- `session_context: SessionContext`（`session_id: str` / `user_id: str` / `current_time: datetime` / `user_prompt: str`），单写者=入口注入（`run_real.py`，与 `original_doc` 同入 START），全链只读。
- `argument_credibility` / `hypotheses` 两个 partial channel：若 judgment 直接写回 `argument_tree`，则可裁撤；是否裁撤在实现时据 judgment 是否仍分阶段产 partial 决定（倾向裁撤 `argument_credibility`，judgment 直接产整树）。
- `hypotheses` channel 形状变更：propose 阶段写入的 `Hypothesis.status` 为"待定"（需在 `HypothesisStatus` 加 `pending` 或以 `None` 表示），由 judgment 取证后落终态。
- `errors` channel 不变；新节点异常经 `_guarded` 追加 `[stage] ExcType: msg`。

### LLM seam 变更（`infra/llm_adapters.py` + 各 `agents/<name>/contract.py`）

- `LlmClient.parse` 的 `ParseResult` 扩为 `{proposals, query_time_range, paragraph_summaries}`（同一次 LLM 调用多吐两块结构化输出）。
  `query_time_range` 当前桩值由 agent 注入、不真实调 LLM 识别。
- `HypothesisLlmClient.propose` 输入从单 `Argument`（`argument_type`+`content`）改为 `(argument, paragraph_summary)`；逐 argument 调用（不一次性）。
  `HypothesisLlmClient.next_verify_step` 取证职责**移出** hypothesis 节点、并入 judgment（吃 `citations` 判 `HypothesisStatus`）。
- `VerifyLlmClient.next_step` 取证职责同理**移出** verification、并入 judgment。
- judgment 节点引入新 LLM seam：吃 `(argument_tree, hypotheses, citations, session_context, query_time_range)` → 产 `(ArgumentStatus per argument, HypothesisStatus per hypothesis)`，再喂纯函数 merge/impact/consistency。
  真实 adapter 用扁平信封 schema（延续 `infra/llm_adapters.py` 既有风格，规避 `oneOf` 判别联合不稳）。
- rewrite_loop 引入新 LLM seam：吃 `(paragraph_summary, 该段节点+假说, 该段 citations, session_context, query_time_range)` → 产提议重写文本（str）。
- 输入压缩铁律延续：三条以上 LLM seam 只把"原文摘要 + 检索 snippet + 假说文本 + 背景"喂 LLM，不回灌 `status`/`argument_weight`/`parent_id`/`children_ids`/`issue_tags`/`merge_decision` 等内部状态字段。

### ADR 偏离（须以新 ADR 固化，编号建议 0017–0021）

1. **重写阶段放弃字节一致 / content 不被 LLM 改写 / 幂等纯函数回写**（仅对被触达段；未触达段仍逐字节忠实；HITL-2 改为确认终稿文本）。
   部分覆盖 ADR-0011（幂等回写）与 DEVELOPMENT §1 的 tracer bullet 承诺。
2. **hitl1 重定义为 partition 确认闸门 + 打回重跑**，打破"绝不打回"（PRD §13 / DEVELOPMENT §1），有界。
3. **verification / hypothesis 取证 / merge / impact / consistency 五合一为 judgment 节点**（取证外包给批量 retrieval 后，并行双线路失去并行前提；merge/impact/consistency 保纯函数逻辑、并进同节点）。
   重构 ADR-0002（乐观并行）/ ADR-0006（12 格矩阵合流）的双线路并行设计。
4. **partition 变 prompt 驱动**（按用户 prompt 重切，当前伪代码）。
   打破 ADR-0009 确定性；**无损性（各段拼接 == `original_doc`）须保**。
5. **贯穿 state 落 PipelineState**（业务消费 `session_context` + `query_time_range`），部分覆盖 ADR-0016（业务相关贯穿 state 不再仅走 RunnableConfig；RunnableConfig 仍承载 langgraph 原生 metadata/callbacks/checkpointer）。

### 兜底与单向流控

- 新节点（retrieval / judgment / rewrite_loop）的 build 闭包经 `_guarded` 统一兜底：正常返回 patch；非 `Hitl2GateError` 异常 → 降级 patch + `_log_error_patch`、单向向前。
- judgment 整体异常 → 覆盖范围内未判决节点置 `error`（沿用现 `_mark_verify_scope_error` 语义）。
- rewrite_loop 整体异常 → 回退未触达段原文 bytes 拼接（保护原文底线），被触达段停留、贴 `writeback_error`，待续跑。
- hitl1 打回超 max retries → 向下推进 + 贴 `partition_retry_exhausted`。
- hitl2 `Hitl2GateError` 原样上抛、不兜底。

## Testing Decisions

- **最高 seam 优先**：整条流水线的端到端断言落在 `tests/test_orchestrator_e2e.py`（现有最高 seam，`run_with_report` → 断言 `final_document` + `errors` + 关键 state channel）。
  本重构以**一个** E2E seam 覆盖拓扑顺序、state 穿线、未触达段逐字节忠实、hitl2 确认终稿——理想 seam 数为 1。
- **拓扑 seam**：`tests/test_orchestrator_topology.py`（现有 `StageSpec` / `default_pipeline` 断言）扩展为断言新拓扑的节点序列与 deps 边（parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → END）。
- **兜底 seam**：`tests/test_orchestrator_fallback.py`（现有 `_guarded` 降级断言）扩展为覆盖新节点（retrieval/judgment/rewrite_loop）的降级 patch 与 hitl1 打回超限。
- **纯函数 seam**（各 agent 单测，沿用现有 `tests/test_<name>.py` 模式）：
  - `test_parser.py`：扩 `ParseResult` 新增 `query_time_range`/`paragraph_summaries` 的铸造；partition+parse 合并节点的字节级自检不变。
  - `test_hypothesis.py`：propose 输入含 `paragraph_summary`、产 pending 假说；取证移出后的契约。
  - `test_merge.py` / `test_impact.py` / `test_consistency.py`：纯函数逻辑不变，验证"并入 judgment 节点但逻辑不动"。
  - `test_writeback.py` → 改/扩为 rewrite_loop + hitl2 终稿拼装的纯函数子缝（提议→确认→拼接，未触达段逐字节、被触达段用确认文本）。
  - `test_hitl1.py`：partition 确认 + 打回重跑（伪代码桩）+ 有界。
  - `test_hitl2.py`：终稿文本确认/编辑/驳回、不可跳过、`Hitl2GateError` 原样上抛。
- **伪代码桩 seam**：`query_time_range` 默认值、`paragraph_summaries` 桩、`retrieval` 空 citations、partition 重跑桩——均以确定性桩可断言（tracer bullet 回路：桩路径下终稿对未触达段逐字节等于原文）。
- **测试只断言外部行为**：不测节点内部私有函数、不测 LLM 真实调用（真实联网冒烟仅 `tests/test_real_llm_wiring.py` 既有 `-k dashscope_smoke`，默认 skip）。
- **字节一致弱化不变式**：E2E 断言"无触达/继续路径下 `final_document == original_doc`"仍成立（hitl1 继续 / rewrite 无触达段）。
- 既有 `tests/test_orchestrator_resume.py`（回写续跑）须随 rewrite_loop 重写适配。

## Out of Scope

- 真实 LLM 时间识别（`query_time_range` 真实化）——后续切片，当前伪代码默认 2025–2026。
- 真实检索后端接入（`retrieval` 节点真实化）——后续切片，当前伪代码桩。
- partition 真实 prompt 驱动重切（含 (a) LLM 驱动 vs (b) 结构化 hint 参数的抉择、max retries 调参）——后续切片，当前伪代码桩。
- hitl1 / hitl2 真实 `interrupt` + `Command(resume)` + checkpointer——后续切片，当前同步注入闸门。
- `argument_credibility` 是否最终裁撤的终局决定——实现时据 judgment 写回方式定，本 PRD 仅登记倾向。
- 真实消息轮次融合（`session_context` 升级为 `BaseMessage` 聊天轮次，ADR-0016 扩展点）——后续切片。
- 不重排既有文件、不强制 `ruff format`（既有缩进风格保持）。
- 不改 `OriginalParagraphs` 的字节级无损只读表身份（ADR-0005/0009 不动）。

## Further Notes

- 本 PRD 不含具体文件路径与代码片段（按 `/to-prd` 约定，避免漂移）；字段名以 `src/domain.py` / `src/runtime/orchestrator.py` 代码实名为准。
- 新会话进入实现时的建议顺序：
  1. 先写 ADR-0017–0021（5 条偏离，最难逆转、先锁定）。
  2. 更新 `CONTEXT.md`（新术语：`query_time_range` / `paragraph_summary` / `judgment 节点` / `rewrite_loop` / `session_context`；hitl1 语义变更）。
  3. 更新 `docs/STATE.md` §1（新字段）+ `docs/DEVELOPMENT.md` §1 拓扑大框 + §2 模块表。
  4. 最后动代码：`MANIFEST` 加/改节点、`PipelineState` 加 channel、各伪代码桩、E2E/拓扑/兜底测试扩。
- 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`；lint/类型/测试失败一律修，即使非本次改动引入。
- 维护约定：`docs/STATE.md` 字段增删只改对应小节表格、单一定义点；本 PRD 与 STATE.md 不重复描述同一字段流向。
