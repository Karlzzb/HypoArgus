# 任务文档：流水线重构（parse+partition 合并 / 批量检索节点 / judgment 合一 / 逐段重写循环 / 贯穿 session state）

本文件是 `docs/PRD-pipeline-restructure.md` 的**可执行任务追踪文档**，按 `/to-issues` 纵切（tracer bullet）原则拆分。
每个 slice 是一条**端到端窄但完整**的切片（schema / 装配 / 纯函数 / 测试全过），切片完成后流水线保持绿色（`ruff check` + `mypy --strict` + `pytest` 全过）。
术语沿用 `CONTEXT.md` 统一语言；字段名以 `src/domain.py` / `src/runtime/orchestrator.py` 为准；本 PRD 的偏离见 PRD「Implementation Decisions · ADR 偏离」。

## 状态追踪表

| 状态 | 含义 |
|---|---|
| ⬜ 未开始 | 未启动 |
| 🟨 进行中 | 当前正在做 |
| 🟩 完成 | 质量门全过、切片可独立验证 |
| ⬛ 阻塞 | 被前置依赖卡住或发现问题待澄清 |

| Slice | 标题 | 状态 | Blocked by | 备注 |
|---|---|---|---|---|
| 0 | ADR-0017~0021 + 统一语言 + STATE/DEVELOPMENT 文档 | 🟩 | — | 纯文档预重构 |
| 1 | 贯穿 state 脚手架 + parse+partition 合并 + paragraph_summaries | 🟩 | 0 | 含 query_time_range 桩 |
| 2 | hitl1 重定义为 partition 确认闸门 + 有界打回（桩） | 🟩 | 1 | 打回重跑为伪代码桩 |
| 3 | hypothesis_propose 节点（仅 propose，pending 状态） | 🟩 | 2 | 取证移出、并入后续 judgment |
| 4 | retrieval 节点（批量检索桩） | 🟩 | 3 | 空 citations |
| 5 | judgment 节点（五合一：取证 + merge/impact/consistency 纯函数） | 🟩 | 4 | 最大切片·裁撤 verification ReAct 模块 + 独占 infra（history/retrieval_tool/tool_protocol） |
| 6 | rewrite_loop + hitl2 终稿文本确认闸门 + final_document 拼装 | 🟩 | 5 | 裁撤 writeback 节点 |

质量门（每个 slice 完成时）：`ruff check src tests` + `mypy --strict src` + `pytest -q` 全过；lint / 类型 / 测试失败一律修，即使非本切片引入。
不强制 `ruff format`、不重排既有文件（既有缩进风格保持，PRD §29 / Out of Scope）。

---

## Slice 0 — ADR-0017~0021 + 统一语言 + STATE/DEVELOPMENT 文档

### What to build

纯文档预重构（「make the change easy, then make the easy change」），代码不动、流水线保持绿色。
先锁定 5 条最难逆转的 ADR 偏离，再更新统一语言与状态树/模块文档，使后续代码切片有契约可依。

- 新增 `docs/adr/0001`7–`0021`（建议编号）共 5 条 ADR，固化 PRD「ADR 偏离」五项：
  1. 重写阶段放弃字节一致 / content 不被 LLM 改写 / 幂等纯函数回写（仅被触达段；未触达段逐字节忠实；HITL-2 改为确认终稿文本）。部分覆盖 ADR-0011 与 DEVELOPMENT §1 tracer bullet 承诺。
  2. hitl1 重定义为 partition 确认闸门 + 有界打回，打破「绝不打回」（PRD §13 / DEVELOPMENT §1）。
  3. verification / hypothesis 取证 / merge / impact / consistency 五合一为 judgment 节点（取证外包给批量 retrieval 后，并行双线路失去并行前提；merge/impact/consistency 保纯函数逻辑、并进同节点）。重构 ADR-0002 / ADR-0006 双线路并行设计。
  4. partition 变 prompt 驱动（按用户 prompt 重切，当前伪代码桩）。打破 ADR-0009 确定性；**无损性（各段拼接 == `original_doc`）须保**。
  5. 贯穿 state 落 `PipelineState`（业务消费 `session_context` + `query_time_range`），部分覆盖 ADR-0016（RunnableConfig 仍承载 langgraph 原生 metadata/callbacks/checkpointer）。
- 更新 `CONTEXT.md` 统一语言：新术语 `query_time_range` / `paragraph_summary` / `judgment 节点` / `rewrite_loop` / `session_context`；hitl1 语义变更（partition 确认闸门 + 有界打回）；hitl2 语义变更（终稿文本确认闸门）。
- 更新 `docs/STATE.md` §1：新增字段 `query_time_range` / `paragraph_summaries` / `citations` / `proposed_rewrites` / `session_context` 的字段流向（单写者 / 读者）；`argument_credibility` 标记「倾向裁撤、待 Slice 5 终局决定」。
- 更新 `docs/DEVELOPMENT.md` §1 拓扑大框为新单向图（`parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → final_document`）+ §2 模块表（节点增删）。
- ADR 与文档**不重复描述同一字段流向**（PRD 维护约定：STATE.md 为字段唯一描述点）。

### Acceptance criteria

- [x] `docs/adr/` 新增 5 条 ADR（编号建议 0017–0021），每条注明所偏离的原 ADR 编号与「须保」的底线。
- [x] `CONTEXT.md` 收录 `query_time_range` / `paragraph_summary` / `judgment` / `rewrite_loop` / `session_context` 五个新术语；hitl1 / hitl2 语义变更已同步。
- [x] `docs/STATE.md` §1 列出新 5 字段的流向表；`argument_credibility` 标记倾向裁撤待定。
- [x] `docs/DEVELOPMENT.md` §1 拓扑框与 §2 模块表反映新拓扑与节点增删。
- [x] 代码未改动；`ruff check src tests` + `mypy --strict src` + `pytest -q` 仍全绿。

### Blocked by

None — 可立即开始。

---

## Slice 1 — 贯穿 state 脚手架 + parse+partition 合并 + paragraph_summaries

### What to build

把「贯穿全链的运行上下文」与「时间范围」两类 state 落地，同时把 partition 与 parse 合并为单一图节点并在同一次 LLM 调用里多产出 `paragraph_summaries`。
本切片完成后，新拓扑首段（`parse+partition`）成形，`session_context` / `query_time_range` 在 state 上可贯穿到 END。

- 新增域类型 `SessionContext`（`session_id` / `user_id` / `current_time: datetime` / `user_prompt`）与 `TimeRange`（`start: date | None` / `end: date | None` / `rationale: str`）于 `src/domain.py`。
- `PipelineState`（`runtime/orchestrator.py`）新增：
  - `session_context: SessionContext`（单写者=入口注入 `run_real.py`，与 `original_doc` 同入 START；全链只读）。
  - `query_time_range: TimeRange`（单写者=`parse+partition`；读者=retrieval / rewrite / judgment）。
  - `paragraph_summaries: dict[str, str]`（`paragraph_id → 摘要`；单写者=`parse+partition`；读者=hypothesis_propose / rewrite_loop）。**不并入 `OriginalParagraphs`**（保其字节级无损只读表身份，ADR-0005/0009）。
- `agents/assembly.py` 的 `MANIFEST`：把 `partition` + `parse` 两 `AgentEntry` 合并为单一 `parse+partition`（`deps=()` 接 START）。
  partition 的纯代码切分 + 字节级自检、parse 的建树 / content 逐字节拷回主逻辑**不动**；新增的是同一 LLM 调用多吐结构化输出。
- `LlmClient.parse` 的 `ParseResult` 扩为 `{proposals, query_time_range, paragraph_summaries}`（同一次 LLM 调用多吐两块结构化输出）。
  `query_time_range` 当前为**桩值**（agent 注入 `TimeRange(start=2025, end=2026, rationale="默认值·真实识别待后续")`，不真实调 LLM 识别）。
- `hitl1` 的 `deps` 改为 `("parse+partition",)`；其余节点 deps 随合并节点名对齐。
- 入口注入：`run` / `run_with_report` 接收 `session_context`（与 `original_doc` 同入 START）。

### Acceptance criteria

- [x] `SessionContext` / `TimeRange` 域类型存在且 `mypy --strict` 通过。
- [x] `PipelineState` 含 `session_context` / `query_time_range` / `paragraph_summaries` 三 channel，reducer 与单写者与 PRD 一致。
- [x] `MANIFEST` 中 partition + parse 合为单 `parse+partition` 条目；`default_pipeline()` 派生出的拓扑首段为单节点。
- [x] `ParseResult` 含 `query_time_range`（桩 2025–2026）与 `paragraph_summaries`；partition 字节级自检（各段拼接 == 原始输入）仍通过。
- [x] `tests/test_orchestrator_topology.py` 断言新首段为单 `parse+partition` 节点。
- [x] `tests/test_orchestrator_e2e.py` 断言 `session_context` 与 `query_time_range` 贯穿到 END；`final_document` 对未触达段逐字节等于 `original_doc`。
- [x] `tests/test_parser.py` 扩 `ParseResult` 新增两块的铸造；既有 partition / parse 逻辑测试仍过。

### Blocked by

- Slice 0

---

## Slice 2 — hitl1 重定义为 partition 确认闸门 + 有界打回（桩）

### What to build

把 hitl1 从「结构确认（可跳过）」重定义为**partition 确认闸门**：人确认段落切分是否合理，不合理则按用户 prompt 重跑 `parse+partition`。
打回**打破「绝不打回」**（PRD §13 / DEVELOPMENT §1），须**有界**（max retries，建议 3）；超限仍向下推进 + 贴 `partition_retry_exhausted` 标签。

- `hitl1` 节点 build 闭包重定义：读 `parse+partition` 产出的 `argument_tree` + `original_paragraphs` 供人确认；人给「打回 + 修改意见」时，按 prompt 重跑 partition。
- partition 的「按 prompt 重切」当前为**伪代码桩**（不真实 prompt 驱动重切，只穿 state、原样或占位重切；PRD §21 Out of Scope）。
- max retries 有界（建议 3）：计数器随 hitl1 打回递增；超限向前推进 + 在 `errors` 追加 `partition_retry_exhausted`（沿用 `_log_error_patch` 语义，但不视为异常降级）。
- `hitl1` contract：`Hitl1Decision` 的 action 集调整为「确认继续 / 打回重跑」两类语义（既有 `skip/accept/edit` 与新语义对齐或收编，以实现时不破坏既有 `FakeHitl1Gate` 离线桩为准）。
- 兜底：打回超限走「向前 + 贴标签」，**不**经 `_guarded` 的异常降级（它是受控分支，不是异常）。

### Acceptance criteria

- [x] hitl1 作为 partition 确认闸门：可确认继续、可打回重跑 `parse+partition`。
- [x] 打回有界（max retries 可配置，默认 3）；超限向前推进 + `errors` 含 `partition_retry_exhausted`。
- [x] partition 重切为伪代码桩（不真实 LLM 驱动），但桩路径下 `final_document` 仍逐字节等于原文（未触达段忠实）。
- [x] `tests/test_hitl1.py` 覆盖：确认继续、打回一次后继续、打回超限贴标签向前。
- [x] `tests/test_orchestrator_fallback.py` 扩 hitl1 打回超限分支。
- [x] `mypy --strict` + `pytest -q` 全过。

### Blocked by

- Slice 1

---

## Slice 3 — hypothesis_propose 节点（仅 propose，pending 状态）

### What to build

新增 `hypothesis_propose` 节点：逐 argument 调 `propose`（**不取证**），产 `list[Hypothesis]`（status 待定）。
覆盖范围沿用 evidence + sub_claim；propose 读取 `paragraph_summaries`（非整段 `content`）。
hypothesis 节点原有的 `next_verify_step` 取证职责**移出**（后续并入 judgment）。

- 新增 `hypothesis_propose` 的 `AgentEntry`（`deps=("hitl1",)`，处于 hitl1 与 retrieval 之间）。
- `HypothesisStatus` 加 `pending`（或以 `None` 表示待定），propose 阶段写入此态，由后续 judgment 取证后落终态（`supported` / `doubtful` / `refuted`）。
- `HypothesisLlmClient.propose` 输入从单 `Argument` 改为 `(argument, paragraph_summary)`；逐 argument 调用（不一次性）。
- `hypotheses` channel 形状变更：propose 写入的 `Hypothesis.status` 为「待定」；既有 `hypothesis` 节点的 `next_verify_step` 在本切片移除（取证 seam 推迟到 Slice 5 的 judgment 内重接）。
- 输入压缩铁律延续：propose seam 只喂「原文摘要 + argument 结构（type/content）+ 背景」，不回灌 `status` / `argument_weight` / `parent_id` 等内部状态字段。

### Acceptance criteria

- [x] `hypothesis_propose` 节点存在于 `MANIFEST` 与 `default_pipeline()` 拓扑（hitl1 → hypothesis_propose）。
- [x] `HypothesisStatus` 含 `pending`（或等价 `None` 语义）；propose 产出的假说 status 为待定。
- [x] `propose` 签名为 `(argument, paragraph_summary) -> list[HypothesisProposal]`；逐 argument 调用。
- [x] 既有 `hypothesis` 节点的 `next_verify_step` 取证职责已移除（不再调取证）。
- [x] `tests/test_hypothesis.py` 覆盖 propose 含 `paragraph_summary` 输入、产 pending 假说。
- [x] `tests/test_orchestrator_topology.py` 断言 hitl1 → hypothesis_propose 边。
- [x] E2E：未触达段仍逐字节忠实；`mypy --strict` + `pytest -q` 全过。

### Blocked by

- Slice 2

---

## Slice 4 — retrieval 节点（批量检索桩）

### What to build

新增 `retrieval` 节点：批量接收 `argument_tree` + `hypotheses` + `paragraph_id` 列表 + `query_time_range` + `session_context`，统一返回全部 `citations`。
**当前为伪代码桩**：不真实检索，只把 state 接过、产出下游所需 `citations` channel（占位 / 空）。
真实检索后端后续切片接入（白名单 / 权限 / 模板在 `infra/retrieval.py` 接口层强制，不变）。

- 新增 `retrieval` 的 `AgentEntry`（`deps=("hypothesis_propose",)`）。
- `PipelineState` 新增 `citations: dict[str, list[Source]]`（key 为 `argument_id` 如 `n0001`/`bg-...` 或 `hypothesis_id` 如 `h-...`，两套 id 不冲突）；reducer 用 `_merge_dict`（单写者无冲突）；单写者=retrieval，读者=judgment / rewrite_loop。
- 桩实现：返回空 `citations`（或占位），不发起任何真实检索；`session_context` / `query_time_range` 被读取但不触发联网。
- 装配：retrieval 的 build 闭包经 `_guarded` 兜底；异常 → 降级 patch（空 citations）+ `_log_error_patch`、单向向前。
- `infra/retrieval.py` 接口层骨架（若尚无）：声明真实后端后续接入的契约面（白名单 / 权限 / 模板），桩不实现真实逻辑。

### Acceptance criteria

- [x] `retrieval` 节点存在于 `MANIFEST` 与拓扑（hypothesis_propose → retrieval）。
- [x] `PipelineState` 含 `citations: dict[str, list[Source]]`，reducer=`_merge_dict`，单写者=retrieval。
- [x] 桩实现产出空 / 占位 citations，不联网；`session_context` / `query_time_range` 被读取。
- [x] retrieval build 闭包经 `_guarded`，异常降级为空 citations + errors 单向向前。
- [x] 桩路径覆盖（空 citations、读 session_context / query_time_range）：落于 `tests/test_orchestrator_e2e.py`「批量检索节点」节（`test_e2e_retrieval_stub_empty_citations_byte_identity` + `test_e2e_retrieval_node_threads_context_and_query_time_range`）与 `tests/test_orchestrator_fallback.py`（`test_retrieval_exception_falls_back_to_empty_citations_and_logs`）。既有 `tests/test_retrieval.py` 已是 `infra.retrieval` 接口层契约文件的归属名，节点级桩路径测试按 per-stage wiring 惯例落于 e2e / fallback seam（与 verification / hypothesis / merge 等节点一致）。
- [x] `tests/test_orchestrator_topology.py` 断言 hypothesis_propose → retrieval 边。
- [x] E2E：空 citations 路径下 `final_document` 未触达段逐字节等于原文；`mypy --strict` + `pytest -q` 全过（444 passed）。

### Blocked by

- Slice 3

---

## Slice 5 — judgment 节点（五合一：取证 + merge/impact/consistency 纯函数）

### What to build

合并检索之后的判断节点为单一 `judgment` 节点：LLM 取证（verification + hypothesis，吃 `citations` 判 `ArgumentStatus` / `HypothesisStatus`）+ 纯函数 `merge`（12 格矩阵）+ 纯函数 `impact`（剩余支撑率）+ 纯函数 `consistency`（标签批注）。
节点数压成 1，但 merge / impact / consistency 的纯函数逻辑**保留、不交 LLM 裁决**。

- 新增 `judgment` 的 `AgentEntry`（`deps=("retrieval",)`）；裁撤 `verification` / `hypothesis`（取证） / `merge` / `impact` / `consistency` 五个 `AgentEntry`。
- 新 LLM seam（judgment contract）：吃 `(argument_tree, hypotheses, citations, session_context, query_time_range)` → 产 `(ArgumentStatus per argument, HypothesisStatus per hypothesis)`；真实 adapter 用扁平信封 schema（延续 `infra/llm_adapters.py` 既有风格，规避 `oneOf` 判别联合不稳）。
  - 该 seam 取证职责承接原 `VerifyLlmClient.next_step` 与 `HypothesisLlmClient.next_verify_step`：吃 `citations` 直接判终态，不再 ReAct 逐段逐点检索。
- 纯函数 `merge`（12 格矩阵）/ `impact`（剩余支撑率）/ `consistency`（标签批注）逻辑**不动**，并入 judgment build 闭包按序调用。
- `argument_credibility` partial channel：若 judgment 直接写回 `argument_tree`（终态写回树），则**裁撤**该 partial（倾向裁撤，PRD §24）；`hypotheses` channel 形状随取证落终态更新（pending → supported/doubtful/refuted）。
- 兜底：judgment 整体异常 → 覆盖范围内未判决节点置 `error`（沿用现 `_mark_verify_scope_error` 语义）；经 `_guarded` 降级 + 单向向前。
- 输入压缩铁律延续：judgment LLM seam 只喂「原文摘要 + 检索 snippet + 假说文本 + 背景」，不回灌 `status` / `argument_weight` / `parent_id` / `children_ids` / `issue_tags` / `merge_decision` 等内部状态字段。

### Acceptance criteria

- [x] `judgment` 节点存在于 `MANIFEST` 与拓扑（retrieval → judgment）；原五节点已裁撤。
- [x] 新 judgment LLM seam 契约存在，签名吃 `(argument_tree, hypotheses, citations, session_context, query_time_range)`，产 per-argument / per-hypothesis 终态（`JudgmentLlmClient` Protocol + `FakeJudgmentLlmClient` 离线桩 + `QwenJudgmentLlmClient` 真实 adapter）。
- [x] 纯函数 `merge` / `impact` / `consistency` 逻辑不变，并入 judgment build 闭包（`judge_and_adjudicate` 按序串联）；`tests/test_merge.py` / `test_impact.py` / `test_consistency.py` 仍全过（验证「并入但逻辑不动」）。
- [x] `argument_credibility` 据写回方式裁撤的决定已落定并在 STATE.md 同步：judgment 单写者整树写回 `argument_tree`，该 partial channel 已从 `PipelineState` 移除（ADR-0019 终局）。
- [x] `hypotheses` channel 的假说 status 由 pending 落终态（`_apply_hypothesis_verdicts`，pending→supported/doubtful/refuted）。
- [x] judgment 整体异常时未判决节点置 `error`（`_mark_verify_scope_error(reason="judgment")`）；`tests/test_orchestrator_fallback.py::test_judgment_wholesale_exception_marks_in_scope_arguments_error` 覆盖 judgment 降级。
- [x] `tests/test_orchestrator_topology.py` 断言 retrieval → judgment 边、原五节点缺席（默认拓扑 7 stage）。
- [x] E2E：终稿对未触达段逐字节等于原文；`mypy --strict src` + `ruff check src tests` + `pytest -q` 全过（399 passed / 6 skipped）。原 verification ReAct 模块（`agents/verification/`）+ 独占 infra（`infra/history.py` / `retrieval_tool.py` / `tool_protocol.py`）及其单测已删除（用户确认全部删除）。

### Blocked by

- Slice 4

---

## Slice 6 — rewrite_loop + hitl2 终稿文本确认闸门 + final_document 拼装

### What to build

新增 `rewrite_loop` 节点逐段提议重写，并把 hitl2 重定位为**终稿文本确认闸门**（Model P）：逐段确认 / 编辑 / 驳回 `proposed_rewrites`，拼成 `final_document`。
被确认段用提议文本、被驳回段回退原文、未触达段逐字节原文。
hitl2 仍不可跳过、`Hitl2GateError` 原样上抛、绝不替人拍板（承诺 4 保留）。
原 `writeback` 节点裁撤（终稿在 hitl2 落地）。

- 新增 `rewrite_loop` 的 `AgentEntry`（`deps=("judgment",)`）。
  - 逐 `paragraph_id` 跑；对**被触达**段（该段有 supported 假说 / 相关 citations）由 LLM 提议重写文本；**未触达段逐字节拷回**。
  - 输入（每段）：`paragraph_summaries[pid]` + 该段节点（argument / evidence 结构 + 其 `candidate_hypotheses`）+ 该段聚合 citations + `session_context` + `query_time_range`。
  - 输出：`proposed_rewrites: dict[paragraph_id, str]`（仅被触达段）。
  - 新 LLM seam：吃上述每段输入 → 产提议重写文本（str）。
- `PipelineState` 新增 `proposed_rewrites: dict[str, str]`（单写者=rewrite_loop，读者=hitl2）。
- `hitl2` 重定位为终稿文本确认闸门：逐段确认 / 编辑 / 驳回 `proposed_rewrites`；拼装 `final_document`（确认→提议文本、驳回→原文、未触达→逐字节原文）。
- 裁撤 `writeback` 节点（`final_document` 由 hitl2 落地，不再有独立回写节点）。
- 兜底：
  - rewrite_loop 整体异常 → 回退未触达段原文 bytes 拼接（保护原文底线），被触达段停留 + 贴 `writeback_error`，待续跑。
  - hitl2 `Hitl2GateError` 原样上抛、不兜底（硬闸门）。
- `tests/test_orchestrator_resume.py`（原回写续跑）随 rewrite_loop 重写适配（续跑语义改为对 `proposed_rewrites` / 被触达段续跑）。
- 字节一致弱化不变式：E2E 断言「无触达 / 继续路径下 `final_document == original_doc`」仍成立（hitl1 继续 / rewrite 无触达段）。

### Acceptance criteria

- [x] `rewrite_loop` 节点存在于 `MANIFEST` 与拓扑（judgment → rewrite_loop → hitl2 → END）。
- [x] `proposed_rewrites: dict[str, str]` channel 存在，单写者=rewrite_loop、读者=hitl2。
- [x] rewrite_loop：被触达段产 LLM 提议重写、未触达段逐字节拷回；输入压缩铁律延续（不回灌内部状态字段）。
- [x] hitl2 为终稿文本确认闸门：逐段确认 / 编辑 / 驳回；`final_document` 按确认 / 编辑 / 驳回 / 未触达三态拼装。
- [x] hitl2 不可跳过；`Hitl2GateError` 原样上抛、绝不替人拍板。
- [x] `writeback` 节点已裁撤；终稿在 hitl2 落地。
- [x] rewrite_loop 失败等价实现「贴 `writeback_error`」：rewrite_loop **不碰 `argument_tree`**（按段 / 文本工作、与 argument 状态解耦）——失败段记 `[rewrite_loop] {pid}: ExcType: msg` 到 `errors` channel + 省略出 `proposed_rewrites` → 终稿该段回退原文 bytes；whole-node 异常由 `_guarded` 降级为空 `proposed_rewrites` + 日志向前。信号在 `errors` 日志 + 段回退原文，不写 `argument_tree` / 不贴 `argument.issue_tags`（ADR-0017 决定，与验收 #7 等价）。
- [x] `tests/test_writeback.py` 改 / 扩为 rewrite_loop + hitl2 终稿拼装的纯函数子缝（提议→确认→拼接；未触达逐字节、被触达用确认文本）。
- [x] `tests/test_hitl2.py` 覆盖终稿确认 / 编辑 / 驳回、不可跳过、`Hitl2GateError` 上抛。
- [x] `tests/test_orchestrator_resume.py` 随 rewrite_loop 重写适配且通过（`resume_rewrite(resolved_rewrites, original_paragraphs)` 幂等重推导）。
- [x] `tests/test_orchestrator_e2e.py` 断言：无触达路径 `final_document == original_doc`；触达路径终稿为 HITL-2 确认文本、未触达段逐字节原文（`test_e2e_touched_confirmed_rewrite_lands_in_final_document`）。
- [x] `tests/test_orchestrator_topology.py` 断言完整新拓扑：`parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → END`。
- [x] `mypy --strict src` + `ruff check src tests` + `pytest -q` 全过（387 passed / 3 skipped）。

### Blocked by

- Slice 5

---

## Out of Scope（不在本任务文档的 slice 内）

- 真实 LLM 时间识别（`query_time_range` 真实化）——后续切片，当前桩 2025–2026。
- 真实检索后端接入（`retrieval` 真实化）——后续切片，当前桩。
- partition 真实 prompt 驱动重切（含 LLM 驱动 vs 结构化 hint 抉择、max retries 调参）——后续切片，当前桩。
- hitl1 / hitl2 真实 `interrupt` + `Command(resume)` + checkpointer——后续切片，当前同步注入闸门。
- `argument_credibility` 终局裁撤决定——Slice 5 内据 judgment 写回方式定。
- 真实消息轮次融合（`session_context` 升级为 `BaseMessage` 聊天轮次，ADR-0016 扩展点）——后续切片。
- 不重排既有文件、不强制 `ruff format`。
- 不改 `OriginalParagraphs` 的字节级无损只读表身份（ADR-0005/0009 不动）。

## 实现备注

- 本任务文档不含具体文件路径与代码片段（按 `/to-prd` / `/to-issues` 约定，避免漂移）；字段名以 `src/domain.py` / `src/runtime/orchestrator.py` / `src/agents/assembly.py` 代码实名为准。
- 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`；lint / 类型 / 测试失败一律修，即使非本次改动引入。
- 维护约定：`docs/STATE.md` 字段增删只改对应小节表格、单一定义点；本任务文档与 STATE.md 不重复描述同一字段流向。
- 新会话进入实现时的建议顺序与本 slice 编号一致（0 → 6）；每个 slice 完成后更新本文档状态追踪表（⬜ → 🟨 → 🟩）。
