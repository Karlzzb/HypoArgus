# ADR-0005：原文与论证树分两层存储，原文永不整篇进 Agent 上下文

## 状态

已接受（2026-07-10）。**决策 2 被 ADR-0025 取代**（「节点存原句」→「段落存原句」：原句改由段落聚合根 `ParagraphRecord.original_content` 单份持有，`Argument` 移除 `paragraph_id` / `content`）；决策 1（layer-1 只读字节表）不动。

## 背景

原文有三重身份且互相拉扯：(1) 回写时未变更段落的字节级还原源，不可丢弃；(2) 若整篇进 Agent prompt 会爆窗口、串扰，须防污染；(3) HITL-2 需要「原文左栏 vs 候选假设右栏」的对比。
若论证树节点 `content` 直接存全量原文，则 Agent 消费树时必然把原文全带进上下文，污染无法避免。

## 决策

物理分两层，用 `paragraph_id` 做唯一外键：

1. **只读原文段落表 (Raw Paragraph Store)**：`{ paragraph_id → 原始 bytes }`，不可变。是字节级还原的唯一真相源，也是 HITL-2 对比左栏的数据源。**任何 Agent 的 prompt 都不整篇加载它。**
2. **论证树（在 state 中流转）**：节点 `content` **只存该节点自己那一句/那一小段原文**（作为推理输入）+ `paragraph_id` 外键，**不存整篇原文**。

- **HITL-2 对比**：前端按 `doubtful/error` 节点的 `paragraph_id` 回只读表取该段原文，配 `candidate_hypotheses`，按需拉取，不进 Agent context。
- **回写**：未变更段落按 `paragraph_id` 从只读表流式拷贝 bytes；变更段落只把「该段原文 + 采纳假设」小片喂给重写 Agent。

## 权衡

- 节点携带自身一句原文属轻度冗余，但那是生成/校验的合理推理输入，量级可控。真正防的是「整篇文章」进 context。
- 选「节点存原句」而非「纯指针回表切片」：换取 HITL 句子级 diff 的工程简单性。

## 影响

- 新增领域实体：只读原文段落表 (Raw Paragraph Store)。
- 论证树是「轻」结构，可整树进 Agent 而不爆窗口。
- 字节级还原、HITL 对比、回写拷贝三者共享同一份只读原文表。
