# ADR-0025：段落为聚合根，原句由段落侧单份持有（取代 ADR-0005 决策 2）

## 状态

已接受（2026-07-15）。取代 ADR-0005 决策 2（「节点存原句」→「段落存原句」）。
本 ADR 是「段落为聚合根」数据结构重对齐的记录，配套 PRD 见 `docs/prd-paragraph-aggregate-root.md`、任务拆解见 `docs/tasks/paragraph-aggregate-root.md`。

## 背景

ADR-0005 把原文分两层存储：layer-1 只读字节表（`OriginalParagraphs`）+ layer-2 论证树节点。
其决策 2 选择「节点 `content` 只存该节点自己那一句/那一小段原文 + `paragraph_id` 外键」，当时的 rationale 是「换取 HITL 句子级 diff 的工程简单性」。

但有两点与该 rationale 冲突：

1. **处理流程以段落为外层循环单位**（`rewrite_loop` 逐段遍历、`hitl2` 终稿按段序缝合、`resume_rewrite` 按段幂等重推导；ADR-0001/0017）。
   数据结构却是 argument → paragraph 的**反向指针**（`Argument` 持 `paragraph_id` + `content`），
   段落表不反向引用 `argument_tree`，「取某段的节点集合」需在用点处临时反向 join（扫全树按 `argument.paragraph_id` 分组）。
   段落↔节点的一对多关系隐式、反向、用点现算，读 `OriginalParagraphs` API 时看不到「它在树里必有对应节点」的保证。
2. **ADR-0017 已把重写 / 评审段级化**（`proposed_rewrites` / `ParagraphRewriteReview` 均段级，`rewrite_loop` 逐段提议、`hitl2` 逐段确认）。
   原「HITL 句子级 diff」的工程简单性 rationale 已不复适用——HITL 已不在句子粒度工作。
   且段内若有 N 个节点，该段原文就被逐节点拷 N 份，是结构性重复。

## 决策

把段落做成**聚合根**，让它**正向拥有**与论证节点的一对多关系，原句收到段落侧（每段一份）。

1. **新增段落聚合记录 `ParagraphRecord`**（`src/domain.py`，每条对应一个段落、按 `OriginalParagraphs.paragraph_ids()` 规范段序），字段：
   `paragraph_id` / `summary`（该段摘要）/ `original_content`（该段原文解码文本，每段唯一一份）/ `argument_tree_ids`（该段所含全部 `argument_id` 列表——正向一对多关系，含核心节点 + 无提议段降级的 background 影子节点）。
2. **`Argument` 去掉 `paragraph_id` 与 `content`**，退化为纯推理结构
   （仅 `argument_id` / `argument_type` / `parent_id` / `children_ids` / `argument_weight` / `status` / `issue_tags` / `candidate_hypotheses` / `merge_decision` / `adopted_hypothesis_id`）。
3. **`paragraph_summaries` state channel 退役**，重构为 `paragraph_list: list[ParagraphRecord]`（`PipelineState` channel），
   配按 `paragraph_id` upsert 的 reducer `merge_paragraph_list`（形如 `merge_argument_tree`，单写者 = parse+partition，hitl1 打回重跑时整列表重写、reducer 保证不重复不丢序）。
   摘要的单一定义点收口于 `paragraph_list.summary`（折叠自 LLM seam 输出 `ParseResult.paragraph_summaries`，该 seam 输出字段保留不动）。
4. **`argument_tree` channel 与 `merge_argument_tree` reducer 不变**——Argument 仍有 `argument_id`，树侧形状、整树写回语义、reducer 全不动。
5. **`OriginalParagraphs`（layer-1 不可变字节表）不变**——字节级还原、未触达段逐字节忠实、partition 分区自检的真相源仍是它；`paragraph_list.original_content` 是其解码文本的同源等价（每段一份）。
6. **LLM seam 契约**：hypothesis propose / judgment / rewrite 三 seam 经 `argument_id → ParagraphRecord` 反查取该段 `original_content`（+ `summary`），不再读 `Argument.content`；
   judgment prompt 由「逐节点 `a.content`」改为「按段聚合节点 + 段原文一次」，更紧凑。parse seam 不变（仍经 `ParagraphView` 喂 `OriginalParagraphs` 解码文本，不整篇 dump）。
7. **HITL-1 同步义务**：`merge` / `split` / `mark_no_op` 在改节点集合时同步维护所属段的 `argument_tree_ids`；「同段才能合并」断言改由 `argument_tree_ids` 归属判定（取代 `Argument.paragraph_id` 比较）。
8. **一致性自检（硬停）**：`argument_tree` 中每个 `argument_id` 恰出现于一个段落的 `argument_tree_ids`、且 `argument_tree_ids` 中每个 id 都存在于 `argument_tree`，不符即硬停（遵循项目「正确性硬停、不兜底」惯例）。

不变式保持不动：layer-1 字节表（`OriginalParagraphs`）与「不整篇进 Agent 上下文」（parse 按段喂、`ParagraphRecord.original_content` 仅取触达段）。

## 权衡

采用「两 channel + id 引用」方案（`paragraph_list` 持 `argument_tree_ids`、`argument_tree` 保持扁平 `list[Argument]`），
而非「`ParagraphRecord` 直接持 `arguments`、`argument_tree` 变 `list[ParagraphRecord]`」的嵌套单 channel 变体。

- 嵌套变体虽零同步，但会改 `argument_tree` 形状 / `merge_argument_tree` reducer / `merge`·`impact`·`consistency`·`judgment` 签名为双层遍历，改造更大、动到 PRD `Out of Scope` 明令不动的树侧。
- 两 channel 方案以 HITL-1 三 op 的同步义务 + 一致性自检为代价，换取树侧（`argument_tree` / reducer）与出口侧（`assemble_final_document` / `resume_rewrite` 仍走 `OriginalParagraphs` + `resolved_rewrites` 逐字节缝合）零改动。
- 同步义务被一致性自检兜底（漂移即硬停），代价可控。

## 影响

- 段落↔节点关系变为正向、第一类、存储于段落侧；原句每段一份（去重）。
- `rewrite_loop` 直接遍历 `paragraph_list`（按 `argument_tree_ids` 解析节点，不再反向 join）。
- `consistency` 按 `argument_tree_ids` 分组、用 `original_content` 去重，不再读 `Argument.paragraph_id` / `.content`；`merge` / `impact` 签名不变（只改数据访问路径）。
- CLI 渲染经 `paragraph_list` 反查 `argument_id → paragraph`；checkpoint 序列化往返 `paragraph_list` 与精简后 `Argument`。
- ADR-0005 决策 2 的 rationale（HITL 句子级 diff）如实记录为被本 ADR 取代；ADR-0005 决策 1（layer-1 字节表）不动。
- 字段流向单一定义点为 `docs/STATE.md` §1（`paragraph_list`）/ §2（`Argument` 移除字段）/ §2.3（`ParagraphRecord`）；术语见 `CONTEXT.md`。
