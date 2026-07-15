# 任务文档：段落为聚合根——数据结构与处理流程重对齐

> 本文档由 `docs/prd-paragraph-aggregate-root.md` 拆解为可独立认领的垂直切片（tracer bullet）。
> 每个切片是一条贯穿「领域模型 → state channel → parse → seam → HITL → CLI → checkpoint → 测试 → 文档」的窄但完整的端到端路径。
> 每个切片落地后须保持质量门全绿：`conda run -n HypoArgus ruff check src tests` + `conda run -n HypoArgus mypy --strict src` + `conda run -n HypoArgus pytest -q`。
> 不强制 `ruff format`（勿重排既有文件缩进）。lint / 类型 / 测试失败一律修，即使非本次改动引入。
> 领域术语见 `CONTEXT.md`；状态树字段流向见 `docs/STATE.md`；架构决策见 `docs/adr/`；模块边界与装配见 `docs/DEVELOPMENT.md`。

## 切片总览

| 切片 | 标题 | 依赖 | 状态 |
|---|---|---|---|
| T-01 | 领域模型 + paragraph_list channel + parse 产出（双写） | — | 已完成 |
| T-02 | LLM seam + consistency 迁移读取 paragraph_list；HITL-1 op 同步 + 一致性自检 | T-01 | 已完成 |
| T-03 | CLI 渲染反查 + checkpoint 往返 paragraph_list | T-02 | 未开始 |
| T-04 | 翻转：移除 Argument.paragraph_id/content，退役 paragraph_summaries，真实 LLM 测试适配 | T-02, T-03 | 未开始 |
| T-05 | 文档（STATE.md / CONTEXT.md）+ 新 ADR 取代 ADR-0005 决策 2 | T-04 | 未开始 |

依赖图：`T-01 → T-02 → T-03 → T-04 → T-05`（T-04 同时依赖 T-02 与 T-03）。
切片须按依赖序认领与落地，前序切片未完成不得开始后续。

---

## T-01：领域模型 + paragraph_list channel + parse 产出（双写）

- **状态**：已完成
- **依赖**：无 — 可立即开始
- **覆盖用户故事**：1（部分）、2（部分）、6、7、22、30

### What to build

新增段落聚合记录 `ParagraphRecord`（每条对应一个段落，按 `OriginalParagraphs` 规范段序），字段 `paragraph_id` / `summary` / `original_content`（该段原文解码文本，每段唯一一份）/ `argument_tree_ids`（该段所含全部 `argument_id` 列表——核心节点 + 无提议段降级的 background 影子节点）。
在 `PipelineState` 新增 `paragraph_list: list[ParagraphRecord]` channel，配一个按 `paragraph_id` upsert 的 reducer（形如 `merge_argument_tree`，单写者 = parse+partition，无冲突亦沿用同形以策安全）。
parse 阶段（真实 `QwenParseLlmClient` 与离线 `FakeLlmClient` 同步）在产出 `argument_tree` 的同点产出 `paragraph_list`：按 `OriginalParagraphs.paragraph_ids()` 规范段序、每段一条、`original_content` 取自该段 bytes 的解码文本、`summary` 来自 parse 摘要阶段、`argument_tree_ids` 覆盖该段全部节点（含 background 影子）。
本切片为**双写过渡态**：`Argument.paragraph_id` / `Argument.content` 与 `paragraph_summaries` channel **暂不移除、暂不退役**，parse 同时产新（`paragraph_list`）与旧（`argument_tree` 各节点的 `paragraph_id`/`content` + `paragraph_summaries` dict）两份，保证下游读取路径未迁移前不读已删字段。

### 决策-rich 原型（字段形状，非可运行 demo）

```python
class ParagraphRecord(BaseModel):
    paragraph_id: str
    summary: str = ""
    original_content: str = ""
    argument_tree_ids: list[str] = Field(default_factory=list)
```

### Acceptance criteria

- [x] `src/domain.py` 新增 `ParagraphRecord`（命名避开与 partition 内部 `Paragraph` 冲突），字段如上原型。
- [x] `src/runtime/orchestrator.py` `PipelineState` 新增 `paragraph_list: Annotated[list[ParagraphRecord], <upsert-by-paragraph_id reducer>]` channel 与对应 reducer。
- [x] `src/agents/parser/agent.py` 的 `parse` 在产出 `argument_tree` 同点产出 `paragraph_list`，按 `OriginalParagraphs.paragraph_ids()` 规范段序、每段一条。
- [x] 每条 `ParagraphRecord.original_content` 等于该段 `OriginalParagraphs.get(pid)` 的解码文本（`surrogateescape`），每段唯一一份。
- [x] 每条 `ParagraphRecord.argument_tree_ids` 覆盖该段全部节点（核心 + background 影子），与 `argument_tree` 中该段节点集一致。
- [x] 真实 `QwenParseLlmClient`（`src/infra/llm_adapters.py`）与离线 `FakeLlmClient`（`src/agents/parser/contract.py`）同步产出 `paragraph_list`。
- [x] `Argument.paragraph_id` / `Argument.content` / `paragraph_summaries` channel 在本切片**保留不动**（双写过渡）。
- [x] parser 单测扩展：断言 `paragraph_list` 覆盖 `OriginalParagraphs` 全部段、`original_content` 与解码 bytes 逐字节相等、`argument_tree_ids` 成员属于 `argument_tree`。
- [x] 质量门全绿（ruff check + mypy --strict + pytest）。
  - ruff check src tests ✓；mypy --strict src ✓；pytest 非 real_llm 子集 ✓（618 passed, 3 skipped）。
  - real_llm 子集对 T-01 为纯加法零回归（不读 paragraph_list），按决策不阻塞本次提交。

### Blocked by

- 无 — 可立即开始。

---

## T-02：LLM seam + consistency 迁移读取 paragraph_list；HITL-1 op 同步 + 一致性自检

- **状态**：已完成
- **依赖**：T-01（需要 parse 产出 `paragraph_list`）
- **覆盖用户故事**：8、9、10、11、12、13、14、15、28

### What to build

把三处 LLM seam（hypothesis propose / judgment / rewrite）与 `consistency` 的数据访问路径从 `Argument.content` / `Argument.paragraph_id` 迁移到 `paragraph_list`：
propose / judgment / rewrite seam 经 `argument_id → ParagraphRecord` 反查取该段 `original_content`（+ `summary`），不再读 `Argument.content`；
judgment prompt 由「逐节点 `a.content`」改为「按段聚合节点 + 段原文一次」，更紧凑（属 prompt 语义变更，须在测试中体现）；
`consistency` 签名收 `paragraph_list`（+ `argument_id → Argument` 索引或整树），按 `argument_tree_ids` 分组、用 `original_content` 去重，不再读 `Argument.paragraph_id` / `.content`；`merge` / `impact` 不读被移除字段，签名不变。
`rewrite_loop` 直接遍历 `paragraph_list`、按 `argument_tree_ids` 解析节点，不再反向 join。
HITL-1 三 op 在改节点集合时**同步维护 `argument_tree_ids`**：`merge` 从该段 ids 移除被合并 id、保留幸存者；`split` 把新 id（`{source}-s{n}`）加进源段 ids；`mark_no_op` 经 `argument_tree_ids` 定位该段节点；「同段才能合并」断言改由 `argument_tree_ids` 归属判定；`reparent` / `set_type` 不动成员关系。
在结构不变式校验处加一条自检：`argument_tree` 中每个 `argument_id` 恰出现于一个段落的 `argument_tree_ids`、且 `argument_tree_ids` 中每个 id 都存在于 `argument_tree`，不符即硬停（挂到既有 `validate_tree` 同侧或 `paragraph_list` 落地校验，遵循「正确性硬停、不兜底」惯例）。
本切片仍为**双读过渡态**：`Argument.paragraph_id` / `content` 字段尚未移除，但所有读取点已切到 `paragraph_list`，为 T-04 的字段移除扫清依赖。

### Acceptance criteria

- [x] hypothesis propose seam（`src/agents/hypothesis/agent.py` + `src/infra/llm_adapters.py::_build_propose_prompt`）取该段 `original_content`（经 `paragraph_list` 反查），不再读 `Argument.content`。
- [x] judgment seam（`_build_judgment_prompt`）改为按段聚合节点 + 段原文一次，prompt 语义变更在测试中体现。
- [x] rewrite seam（`_build_rewrite_prompt` + `propose_rewrites`）遍历 `paragraph_list`、按 `argument_tree_ids` 解析节点、取该段 `original_content`，不再读 `Argument.content` / 反向 join。
- [x] `src/agents/consistency.py` 按 `argument_tree_ids` 分组、用 `original_content` 去重，不再读 `Argument.paragraph_id` / `.content`；`merge` / `impact` 签名不变。
- [x] HITL-1 `_apply_merge` / `_apply_split` / `_apply_mark_no_op`（`src/agents/hitl1/agent.py`）同步维护 `argument_tree_ids`；`reparent` / `set_type` 不动成员关系。
- [x] 「同段才能合并」断言改由 `argument_tree_ids` 归属判定（取代 `Argument.paragraph_id` 比较），违例抛 `TreeInvariantError`。
- [x] 新增结构自检：`argument_id` 恰出现于一个段落 `argument_tree_ids`、且 ids 中每个 id 存在于 `argument_tree`，不符即硬停。
- [x] HITL-1 单测扩展：merge / split / mark_no_op 后断言 `argument_tree_ids` 与 `argument_tree` 实际节点集一致。
- [x] Fake-LLM spy 测试：用 `FakeRewriteLlmClient` 的 `propose_factory` 捕获 rewrite seam 收到的载荷，断言被触达段的 `original_content` 出现在传给 LLM 的渲染 prompt 中（把引发本重构的原始问题锁为回归）。
- [x] 质量门全绿。

### Blocked by

- T-01

---

## T-03：CLI 渲染反查 + checkpoint 往返 paragraph_list

- **状态**：未开始
- **依赖**：T-02（渲染需经 `paragraph_list` 反查 `argument_id → paragraph`；checkpoint 需 `paragraph_list` 落地稳定）
- **覆盖用户故事**：18、19

### What to build

HITL-1 / HITL-2 终端渲染改为经 `paragraph_list` 反查 `argument_id → paragraph`（或直接渲染 `argument_id` + 段落上下文），不再读 `Argument.paragraph_id`。
`src/runtime/checkpoint.py` 的 `HypoArgusSerializer` 核实能往返 `paragraph_list`（pydantic v2 ext + `_MSGPACK_TYPE_MODULES` allowlist），新增 channel 值类型按注释要求补入 allowlist；resume 路径不破。
本切片为**渲染/持久化侧扫尾**：完成后仅剩 `Argument` 字段移除与文档未做。

### Acceptance criteria

- [ ] `_print_tree`（`src/runtime/cli_gates.py`）与 `_render_hitl1_question`（`src/runtime/run_real.py`）不再读 `Argument.paragraph_id`，改经 `paragraph_list` 反查或直接渲染 `argument_id` + 段落上下文。
- [ ] HITL-2 终端渲染（`_render_hitl2_question`）已段落级、不动；核实仍正确。
- [ ] `HypoArgusSerializer` 往返 `paragraph_list`（编/解码），新增值类型补入 `_MSGPACK_TYPE_MODULES` allowlist。
- [ ] checkpoint round-trip 测试覆盖 `paragraph_list`，resume 路径不破。
- [ ] 质量门全绿。

### Blocked by

- T-02

---

## T-04：翻转——移除 Argument.paragraph_id / content，退役 paragraph_summaries，真实 LLM 测试适配

- **状态**：未开始
- **依赖**：T-02（所有读取点已迁移）、T-03（渲染/持久化不读被移除字段）
- **覆盖用户故事**：3、4、5、8、16、17、24、25、26、29

### What to build

所有读取点已切到 `paragraph_list` 后，执行不可逆翻转：`Argument` 移除 `paragraph_id` 与 `content` 两个字段，只保留推理字段（`argument_id` / `argument_type` / `parent_id` / `children_ids` / `argument_weight` / `status` / `issue_tags` / `candidate_hypotheses` / `merge_decision` / `adopted_hypothesis_id`）。
`paragraph_summaries` channel 退役（其承载已并入 `paragraph_list.summary`），`PipelineState` 移除该 channel 与其 reducer。
`argument_tree` channel 与 `merge_argument_tree` reducer 保持不变（Argument 仍有 `argument_id`，树侧形状、整树写回语义、reducer 全不动）；`OriginalParagraphs`（layer-1 字节真相源）保持不动。
HITL-2 / 出口侧 `assemble_final_document` / `resume_rewrite` 不变（仍 `OriginalParagraphs` + `resolved_rewrites` 逐字节缝合）；`build_review` 左栏原文可改取 `paragraph_list.original_content`（与解码 bytes 等价，简化路径）。
真实 LLM provider 的 parse / rewrite 测试按新形状更新：既有「节点文本 == 段落 bytes」的字节保护断言改为段级 `original_content` 对照。
e2e 行为层断言三类外部可观测不变式：① tracer-bullet 字节一致（无采纳改动时 `final_document` 与 `original_doc` 逐字节相等）；② 段落↔节点一致性（`paragraph_list.argument_tree_ids` 与 `argument_tree` 实际节点集一致，经 HITL-1 编辑后仍一致）；③ 原文到达改写节点（Fake spy，T-02 已建，此处复用）。

### Acceptance criteria

- [ ] `src/domain.py::Argument` 移除 `paragraph_id` 与 `content`，仅保留推理字段。
- [ ] `PipelineState` 移除 `paragraph_summaries` channel 与其 reducer；`paragraph_list.summary` 为摘要单一定义点。
- [ ] `argument_tree` channel 与 `merge_argument_tree` reducer 不变；`OriginalParagraphs` 不动。
- [ ] `assemble_final_document` / `resume_rewrite` 不变；`build_review` 左栏原文可改取 `paragraph_list.original_content`。
- [ ] `tests/test_real_llm_parse.py` 的字节保护断言由节点级 `content.encode(surrogateescape) == op.get(pid)` 改为段级 `original_content` 对照。
- [ ] orchestrator e2e（`tests/test_orchestrator_e2e.py`）断言 tracer-bullet 字节一致 + 段落↔节点一致性不变式（经 HITL-1 编辑后仍一致）。
- [ ] Fake-LLM spy 断言「改写 seam 收到该段 `original_content`」（T-02 建，本切片复用）。
- [ ] 全仓无残留对 `Argument.paragraph_id` / `Argument.content` / `paragraph_summaries` 的读取（grep 核实）。
- [ ] 质量门全绿。

### Blocked by

- T-02
- T-03

---

## T-05：文档（STATE.md / CONTEXT.md）+ 新 ADR 取代 ADR-0005 决策 2

- **状态**：未开始
- **依赖**：T-04（字段移除与 channel 退役已落地，文档反映最终形状）
- **覆盖用户故事**：20、21、30

### What to build

`docs/STATE.md` §1 / §1.1 / §1.2 / §2 / §3 / §4 更新：`paragraph_list` 为字段流向单一定义点（取代 `paragraph_summaries`）、`Argument` 移除字段在 §2 反映、各 seam 输入在 §4 反映、各子智能体局部契约在 §3 反映。
`CONTEXT.md`「实现映射」与相关 ADR（0001 / 0009 / 0017）中提及 `Argument.content` / `paragraph_id` 的措辞同步更新，使术语不漂移。
新增 ADR 取代 ADR-0005 决策 2（「节点存原句」→「段落存原句」），rationale：ADR-0017 已把重写 / 评审段级化（`proposed_rewrites` / `ParagraphRewriteReview` 均段级），原「HITL 句子级 diff 工程简单性」rationale 已废止；layer-1 字节表（`OriginalParagraphs`）与「不整篇进 Agent 上下文」不变式保持不动。
遵循 `CONTEXT.md` 统一语言与 `STATE.md`「单一定义点、避免漂移」维护约定——字段增删只改 `STATE.md` 对应小节，勿在多处重复描述。
不动 `CHANGELOG.md` 或任何标记 auto-generated 的文件。

### Acceptance criteria

- [ ] `docs/STATE.md` 以 `paragraph_list` 为字段流向单一定义点，`paragraph_summaries` 相关描述移除，`Argument` 移除字段在 §2 反映，各 seam 输入 / 子智能体契约在 §3/§4 反映。
- [ ] `CONTEXT.md`「实现映射」更新，无 `Argument.content` / `paragraph_id` 残留措辞。
- [ ] ADR-0001 / 0009 / 0017 中提及 `Argument.content` / `paragraph_id` 的措辞同步更新。
- [ ] 新增 ADR（编号顺延）取代 ADR-0005 决策 2，rationale 如实记录 ADR-0017 段级化使原 rationale 失效；layer-1 字节表与「不整篇进 Agent 上下文」不变式标注保持不动。
- [ ] 未改动 `CHANGELOG.md` 或任何 auto-generated 文件。
- [ ] 质量门全绿（文档变更无代码影响，仍核验无回归）。

### Blocked by

- T-04

---

## 维护约定

- **状态值**：`未开始` / `进行中` / `已完成`。认领时置 `进行中` 并填认领人；完成并经质量门全绿后置 `已完成`。
- **验收准则复选框**：完成一项勾 `[x]`；进行中可标 `[~]`（部分完成）。
- **依赖硬约束**：切片须按依赖序落地；前序切片未 `已完成` 不得开始后续。
- **质量门**：每切片落地须 `conda run -n HypoArgus ruff check src tests` + `conda run -n HypoArgus mypy --strict src` + `conda run -n HypoArgus pytest -q` 全绿；不强制 `ruff format`。
- **范围纪律**：各切片严格遵循 PRD `Out of Scope`——不动 `argument_tree` channel 形状 / `merge_argument_tree` reducer / `OriginalParagraphs` / merge·impact·consistency·rewrite_loop 的业务逻辑（只改数据访问路径）；不做嵌套单 channel 变体（已否决）。
