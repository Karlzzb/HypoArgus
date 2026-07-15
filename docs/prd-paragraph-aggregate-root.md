# PRD：段落为聚合根——重对齐数据结构与处理流程方向

> 本 PRD 独立完整，可在新 session 中直接据此计划与执行。
> 领域术语见 `CONTEXT.md`；状态树字段流向见 `docs/STATE.md`；架构决策见 `docs/adr/`；
> 模块边界与装配见 `docs/DEVELOPMENT.md`。本 PRD 与上述文档分层维护，勿在多处重复定义同一字段。
> 执行须在 conda 环境 `HypoArgus` 中（`conda run -n HypoArgus ...`）。
> 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`；不强制 `ruff format`（勿重排既有文件缩进）。

## Problem Statement

当前流水线里，**数据结构的方向与数据处理的方向是反的**，且段落原文被重复拷进每个论证节点。

- 处理流程以 **段落（`paragraph_id`）** 为外层循环单位：`rewrite_loop` 逐段遍历、`hitl2` 终稿拼装按段序缝合、`resume_rewrite` 按段幂等重推导。段落是回写 / 重写 / 终稿的原子单位（ADR-0001）。
- 但数据结构却是 **argument → paragraph 的反向指针**：`Argument` 持有 `paragraph_id`（指向所属段）和 `content`（该段原文的逐字节拷贝），一个段内若有 N 个节点，该段原文就被拷 N 份。
- 于是段落表 `OriginalParagraphs`（`{paragraph_id → bytes}`）**不反向引用** `argument_tree`；任何「取某段的节点集合」的读者都得在用点处**临时反向 join**（扫全树按 `argument.paragraph_id` 分组）。
- 这带来两个后果：① 段落与论证节点天然的一对多关系是**隐式、反向、用点现算**的，读 `OriginalParagraphs` API 时看不到「它在树里必有对应节点」的保证，令人疑惑；② 段落原文在**每个节点**各拷一份，是结构性的重复。

从维护者视角：结构与流程方向相反，关系隐式且需到处现算，原文重复存储——既不清晰，也增加了每次读懂数据流的心智成本。

## Solution

把段落做成**聚合根**，让它**正向拥有**与论证节点的一对多关系，并把段落原文收到段落侧（每段一份），`Argument` 退化为纯推理结构。

具体：

1. **新增段落聚合记录 `ParagraphRecord`**（每条对应一个段落，按规范段序），字段：
   - `paragraph_id`
   - `summary`（该段摘要）
   - `original_content`（该段原文的解码文本，**每段唯一一份**）
   - `argument_tree_ids`（该段所含论证节点的 `argument_id` 列表——正向一对多关系）
2. **`Argument` 去掉 `paragraph_id` 与 `content` 两个字段**，只保留推理字段（`argument_id` / `argument_type` / `parent_id` / `children_ids` / `argument_weight` / `status` / `issue_tags` / `candidate_hypotheses` / `merge_decision` / `adopted_hypothesis_id`）。
3. **`paragraph_summaries` channel 重构为 `paragraph_list`**（`list[ParagraphRecord]`），承载 summary + original_content + argument_tree_ids。
4. **`argument_tree` channel 与其 `merge_argument_tree` reducer（按 `argument_id` upsert）保持不变**——Argument 仍有 `argument_id`，树侧形状、整树写回语义、reducer 全不动。
5. **`OriginalParagraphs`（不可变 bytes 表，ADR-0005 layer-1 字节真相源）保持不动**——字节级还原、未触达段逐字节忠实、partition 自检的真相源仍是它。

结果：段落↔节点关系变为**正向、第一类、存储于段落侧**；段落原文每段一份（去重）；`rewrite_loop` 直接遍历 `paragraph_list`（按 `argument_tree_ids` 解析节点，不再反向 join）；`argument_tree` 与字节表保持最小且不动。

> 决策-rich 原型（字段形状，非可运行 demo）：
> ```python
> class ParagraphRecord(BaseModel):
>     paragraph_id: str
>     summary: str = ""
>     original_content: str = ""
>     argument_tree_ids: list[str] = Field(default_factory=list)
>
> class Argument(BaseModel):           # 去掉 paragraph_id 与 content
>     argument_id: str
>     argument_type: ArgumentType
>     parent_id: str | None = None
>     children_ids: list[str] = Field(default_factory=list)
>     # paragraph_id / content —— 移除
>     argument_weight: int = Field(default=0, ge=0, le=100)
>     status: ArgumentStatus = ArgumentStatus.UNVERIFIED
>     issue_tags: list[str] = Field(default_factory=list)
>     candidate_hypotheses: list[Hypothesis] = Field(default_factory=list)
>     merge_decision: MergeDecision | None = None
>     adopted_hypothesis_id: str | None = None
> ```

## User Stories

1. 作为流水线维护者，我希望段落是聚合根、正向拥有其论证节点引用，使得段落↔节点的一对多关系成为第一类、正向关系。
2. 作为流水线维护者，我希望每段原文只存一份（在段落记录上），使得原文不再被重复拷进该段的每个节点。
3. 作为流水线维护者，我希望 `Argument` 只承载推理字段（无 `paragraph_id`、无 `content`），使得论证树成为纯推理结构。
4. 作为流水线维护者，我希望 `argument_tree` 仍是扁平 `list[Argument]` 且 `merge_argument_tree`（按 `argument_id` upsert）不变，使得树侧代码与整树写回语义零改动。
5. 作为流水线维护者，我希望 `OriginalParagraphs`（不可变 bytes 表）仍是字节还原真相源，使得字节级还原与未触达段逐字节忠实得以保留。
6. 作为 parse 阶段开发者，我希望 parse 在产出 `argument_tree` 的同时产出 `paragraph_list`（`paragraph_id` + `summary` + `original_content` + `argument_tree_ids`），使得段落聚合与建树同点产出。
7. 作为 parse 阶段开发者，我希望 `paragraph_list` 按 `OriginalParagraphs` 的规范段序排列、且每段都有一条记录（含被降级为 background 影子节点的段），使得段落集合与字节表完全对齐。
8. 作为 rewrite_loop 开发者，我希望直接遍历 `paragraph_list`、按 `argument_tree_ids` 解析节点，使得循环方向与数据结构方向一致、不再反向 join。
9. 作为 rewrite LLM seam 开发者，我希望改写 prompt 能取到该段 `original_content`，使得改写 LLM 拿到的是原文（而非仅摘要）。
10. 作为 hypothesis LLM seam 开发者，我希望为节点产假设时能取到其所在段的 `original_content`，使得假设生成 LLM 拿到原文。
11. 作为 judgment LLM seam 开发者，我希望裁决 prompt 按段聚合节点、每段原文只出现一次，使得裁决 LLM 拿到原文且无逐节点重复。
12. 作为 consistency 检查开发者，我希望按 `paragraph_list.argument_tree_ids` 分组、用 `original_content` 去重，使得一致性检查不再依赖 `Argument.paragraph_id` / `Argument.content`。
13. 作为 HITL-1 维护者，我希望 merge / split / mark_no_op 在改节点集合时同步维护 `argument_tree_ids`，使得段落↔节点关系不漂移。
14. 作为 HITL-1 维护者，我希望「同段才能合并」的断言改由 `argument_tree_ids` 归属判定（而非 `Argument.paragraph_id`），使得该约束在字段移除后仍生效。
15. 作为正确性守门者，我希望有一个结构不变式自检：`paragraph_list.argument_tree_ids` 与 `argument_tree` 实际节点集一致，使得任何漂移硬停（符合项目「正确性硬停」惯例）。
16. 作为 HITL-2 / 终稿拼装维护者，我希望 `assemble_final_document` 与 `resume_rewrite` 仍走 `OriginalParagraphs` + `resolved_rewrites` 逐字节缝合，使得终稿字节忠实与幂等续跑得以保留。
17. 作为 HITL-2 维护者，我希望 `build_review` 的左栏原文可取自 `paragraph_list.original_content`（与解码 bytes 等价），使得呈现与数据结构同源。
18. 作为 CLI / 运行时维护者，我希望 HITL-1 / HITL-2 终端渲染不依赖 `Argument.paragraph_id`，使得字段移除后显示仍正确。
19. 作为 checkpoint / 恢复维护者，我希望序列化能往返 `paragraph_list` 与精简后的 `Argument`，使得崩溃恢复与 resume 仍可用。
20. 作为领域文档维护者，我希望 `STATE.md` 更新为以 `paragraph_list` 为字段流向单一定义点、并反映 `Argument` 移除的字段，使得文档与代码不漂移。
21. 作为架构记录维护者，我希望新增 ADR 取代 ADR-0005 决策 2（「节点存原句」→「段落存原句」），使得该决策的 rationale（HITL 句子级 diff，已被 ADR-0017 段级化废止）被如实记录。
22. 作为 stub 装配维护者，我希望离线 Fake parse 桩产出 `paragraph_list`（含 `original_content` 与 `argument_tree_ids`），使得无真实 LLM 时 tracer-bullet 字节一致路径仍成立。
23. 作为 manifest / 装配维护者，我希望 parse+partition 节点把 `paragraph_list` 写入 state，使得下游 stage 可读。
24. 作为质量门执行者，我希望重构后 `ruff check` + `mypy --strict` + `pytest` 全绿，使得项目质量门成立。
25. 作为流水线维护者，我希望 tracer-bullet 承诺（无采纳改动 → 终稿与原文逐字节一致）依然成立，使得基础不变式得以保留。
26. 作为测试作者，我希望用单一高层 e2e seam（orchestrator + stub agents）断言字节一致 + 段落/节点一致性，使得重构在行为层被验证。
27. 作为测试作者，我希望用 Fake-LLM spy 断言「改写 seam 收到该段原文」，使得「原文传到改写节点」被锁为回归。
28. 作为测试作者，我希望 HITL-1 编辑测试（merge / split / mark_no_op）断言 `argument_tree_ids` 保持一致，使得同步义务被覆盖。
29. 作为真实 LLM 测试维护者，我希望真实 provider 的 parse / rewrite 测试按新形状更新，使得真实链路仍校验 seam 契约。
30. 作为新 session 的计划者，我希望本 PRD 引用 `CONTEXT.md` / `docs/STATE.md` / `docs/adr/` 并使用统一语言，使得我无需本对话即可定向。

## Implementation Decisions

- **领域模型**：新增 `ParagraphRecord`（命名避开与 partition 内部 `Paragraph` 冲突，如 `ParagraphRecord`/`ParagraphEntry`），字段如上原型。`Argument` 移除 `paragraph_id` 与 `content`，其余字段与状态机 / 假说 / 合并裁决字段不变。
- **状态 channel**：`paragraph_summaries` 重构为 `paragraph_list: list[ParagraphRecord]`，配一个按 `paragraph_id` upsert 的 reducer（单写者 = parse+partition；reducer 沿用 `merge_argument_tree` 同形以策安全，即便单写者无冲突）。`argument_tree` channel 与 `merge_argument_tree` reducer 不变。`OriginalParagraphs` channel 不变。
- **parse 阶段**（真实 adapter 与 stub 同步）：产出 `paragraph_list`——按 `OriginalParagraphs.paragraph_ids()` 规范段序，每段一条；`original_content` = 该段 bytes 解码文本（每段唯一一份，替代原先每个节点各拷一份）；`summary` 来自 parse 摘要阶段；`argument_tree_ids` = 该段全部节点（核心节点 + 无提议段降级的 background 影子节点）的 `argument_id` 列表。保留「无提议段降级为 background 影子节点」的全覆盖不变式，确保 `argument_tree` 按 `paragraph_id` 全覆盖 `OriginalParagraphs`。
- **LLM seam 契约**：hypothesis propose / judgment / rewrite 三 seam 需要段落 `original_content`——向这些 seam 传入 `ParagraphRecord`（或其 `original_content` + `summary`）。judgment prompt 由「逐节点 `a.content`」改为「按段聚合节点 + 段原文一次」，更紧凑（属 prompt 语义变更，需在测试中体现）。parse seam 不变（仍经 `ParagraphView` 喂 `OriginalParagraphs` 解码文本，不整篇 dump）。
- **consistency**：签名收 `paragraph_list`（+ `argument_id → Argument` 索引或整树）；按 `argument_tree_ids` 分组；去重用 `original_content`。`merge` / `impact` 不读被移除字段，签名不变。
- **HITL-1 同步义务**：`merge` 从该段 `argument_tree_ids` 移除被合并掉的 id、保留幸存者；「同段才能合并」断言改由 `argument_tree_ids` 归属判定。`split` 把新 id（`{source}-s{n}`）加进源段 `argument_tree_ids`。`mark_no_op` 经 `argument_tree_ids[op.paragraph_id]` 定位该段节点。`reparent` / `set_type` 不动成员关系，不变。
- **一致性自检**：在结构不变式校验处加一条：`argument_tree` 中每个 `argument_id` 恰出现于一个段落的 `argument_tree_ids`、且 `argument_tree_ids` 中每个 id 都存在于 `argument_tree`；不符即硬停。建议挂到既有 `validate_tree` 同侧或 `paragraph_list` 落地校验，遵循项目「正确性硬停、不兜底」惯例。
- **HITL-2 / 出口**：`assemble_final_document` / `resume_rewrite` 不变（仍 `OriginalParagraphs` + `resolved_rewrites` 逐字节缝合）。`build_review` 左栏原文可改取 `paragraph_list.original_content`（与解码 bytes 等价，简化路径）。
- **CLI / 运行时**：HITL-1 / HITL-2 终端渲染改为经 `paragraph_list` 反查 `argument_id → paragraph`（或直接渲染 `argument_id` + 段落上下文），不再读 `Argument.paragraph_id`。
- **checkpoint**：序列化往返 `paragraph_list` 与精简后 `Argument`（pydantic）；核实 checkpoint 编/解码器处理新 channel 与移除字段，resume 路径不破。
- **stub 装配**：离线 Fake parse 桩产出 `paragraph_list`（`original_content` 取自 `OriginalParagraphs` 解码、`argument_tree_ids` 含 background 影子），使 tracer-bullet 字节一致路径在无真实 LLM 时成立。
- **文档**：`STATE.md` §1 / §1.1 / §1.2 / §2 / §3 / §4 更新——`paragraph_list` 为字段流向单一定义点（取代 `paragraph_summaries`）、`Argument` 移除字段在 §2 反映、各 seam 输入在 §4 反映、各子智能体局部契约在 §3 反映。新增 ADR 取代 ADR-0005 决策 2（节点存原句 → 段落存原句），rationale：ADR-0017 已把重写 / 评审段级化（`proposed_rewrites` / `ParagraphRewriteReview` 均段级），原「HITL 句子级 diff 工程简单性」rationale 已废止；layer-1 字节表（`OriginalParagraphs`）与「不整篇进 Agent 上下文」不变式保持不动。同步更新 `CONTEXT.md`「实现映射」与相关 ADR（0001/0009/0017）中提及 `Argument.content` / `paragraph_id` 的措辞，使术语不漂移。
- **不做嵌套单 channel 变体**：本 PRD 明确采用「两 channel + id 引用」方案（`paragraph_list` 持 `argument_tree_ids`、`argument_tree` 保持扁平），不采用「`ParagraphRecord` 直接持 `arguments`、`argument_tree` 变 `list[ParagraphRecord]`」的嵌套变体——后者虽零同步，但会改 `argument_tree` 形状 / reducer / `merge`·`impact`·`consistency`·`judgment` 签名为双层遍历，改造更大。两 channel 方案以 HITL-1 三 op 的同步义务 + 一致性自检为代价，换取树侧与出口侧零改动。

## Testing Decisions

- **只测外部行为，不测实现细节。** 理想单 seam：orchestrator 端到端 + stub（Fake）agents——项目既有的最高层行为 seam。在该 seam 断言三类外部可观测不变式：
  1. **tracer-bullet 字节一致**：无采纳改动时 `final_document` 与 `original_doc` 逐字节相等（贯穿全 stage 的基础承诺）。
  2. **段落↔节点一致性不变式**：`paragraph_list.argument_tree_ids` 与 `argument_tree` 实际节点集一致，且经 HITL-1 结构编辑（merge / split / mark_no_op）后仍一致。
  3. **原文到达改写节点**：用一个 Fake-LLM spy（`FakeRewriteLlmClient` 的 `propose_factory`）捕获 rewrite seam 收到的输入，断言被触达段的 `original_content` 出现在传给 LLM 的载荷 / 渲染后的 prompt 中——把引发本重构的原始问题锁为回归。
- **HITL-1 单测**：merge / split / mark_no_op 后断言 `argument_tree_ids` 与树一致（既有 HITL-1 测试为先验，扩展断言）。
- **真实 LLM 测试**：更新真实 provider 的 parse / rewrite 测试以适配新形状；既有「节点文本 == 段落 bytes」的字节保护断言改为段级 `original_content` 对照。
- **先验 / 复用**：`Fake*` LLM 桩（确定性、provider-free、可注入 `propose_factory` spy）、orchestrator e2e、真实 LLM parse / rewrite 套件。
- **质量门**：`conda run -n HypoArgus ruff check src tests` + `conda run -n HypoArgus mypy --strict src` + `conda run -n HypoArgus pytest -q` 全绿；lint / 类型 / 测试失败一律修，即使非本次改动引入。

## Out of Scope

- 真实检索后端（`citations` 保持桩、不联网）。
- 真实 LLM 时间范围识别（`query_time_range` 保持桩）。
- partition 的 prompt 驱动重切（ADR-0020 桩）。
- `text_span` / `fix_boundary`（ADR-0001 延后项）。
- 改动 `merge` / `impact` / `consistency` / `rewrite_loop` 的**业务逻辑**——只改其**数据访问路径**（从 `Argument.paragraph_id` / `.content` 改为 `paragraph_list`）。
- 改动 `argument_tree` channel 形状或 `merge_argument_tree` reducer。
- 移除或重构 `OriginalParagraphs`（layer-1 字节表保持不动）。
- 嵌套单 channel 变体（已否决，见 Implementation Decisions 末段）。

## Further Notes

- **迁移顺序建议**（每步保持字节一致绿）：
  1. 领域模型（`ParagraphRecord` + 精简 `Argument`）+ state channel（`paragraph_list` + reducer）；
  2. parse（真实 + stub）产出 `paragraph_list`；
  3. 更新 seam 契约（hypothesis / judgment / rewrite）+ `consistency` 改读 `paragraph_list`（与 `Argument` 字段移除同批落地，避免中间态读已删字段）；
  4. HITL-1 三 op 同步 + 一致性自检；
  5. CLI 渲染 + checkpoint 往返；
  6. 文档（`STATE.md` / `CONTEXT.md`）+ 新 ADR（取代 ADR-0005 决策 2）；
  7. 测试（e2e 字节一致 + 一致性不变式 + Fake spy + HITL-1 同步 + 真实 LLM 形状适配）。
- **引发本重构的原始问题**（「分段后是否只有段落 id 与总结、段落原文是否传到改写节点」）在本方案下被结构性回答：原文以 `paragraph_list.original_content` 单份存储，经 `argument_tree_ids` 正向解析节点后传入改写 seam；并以 e2e Fake spy 锁为回归。
- **ADR-0005 取代的 rationale 站得住**：layer-1 字节表与「不整篇进 Agent 上下文」不变式均不动；仅把「节点级存原句」升为「段落级存原句」（每段一份，去重），而 ADR-0017 已使重写 / 评审段级化，原「句子级 HITL diff」理由不复适用。
- **术语与单一定义点**：遵循 `CONTEXT.md` 统一语言与 `STATE.md`「单一定义点、避免漂移」维护约定——字段增删只改 `STATE.md` 对应小节，勿在多处重复描述。
