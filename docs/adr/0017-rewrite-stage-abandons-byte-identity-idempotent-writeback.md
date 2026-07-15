# ADR-0017：重写阶段放弃字节一致 / content 不被 LLM 改写 / 幂等纯函数回写（仅被触达段）

## 状态

已接受（2026-07-13）。部分覆盖 ADR-0011 与 `docs/DEVELOPMENT.md` §1 的 tracer bullet 承诺。
本 ADR 是流水线重构的偏离记录之一，配套见 ADR-0018 ~ ADR-0021。

## 背景

既有终稿产出是**确定性纯函数回写**：

- 段落原文 `original_content`（存于 `ParagraphRecord`，见 ADR-0025）永不被 LLM 改写（逐字节从只读表解码、`Argument` 不再存原句字段，by construction）。
- 回写据「假说与原文的语义关系」做**子串替换 / 段尾追加**（ADR-0004 / ADR-0006 三操作：oppose→REPLACE、advance→REWRITE、expand→SUPPLEMENT 段尾追加）。
- 回写幂等：始终从原始 bytes 重新推导整篇终稿，`supplement` 永不累积（ADR-0011）。
- tracer bullet 承诺（`DEVELOPMENT.md` §1）：「无任何采纳改动时，终稿与原始输入逐字节完全一致」，贯穿全部 stage。

但新产品方向要求：对**被证据 / 假说触达的段落**，由 LLM 基于**全文证据**起草一版**连贯重写文本**，而非机械子串替换。
这与上述三条承诺冲突：重写文本由 LLM 生成、不逐字节等于原文、无法以纯函数幂等回写还原。

## 决策

对**被触达段**放弃三条承诺，对**未触达段**与「HITL-2 为唯一决策闸门」承诺保持：

1. **重写阶段（被触达段）放弃**：
   - 「终稿逐字节一致」；
   - 「段落原文永不被 LLM 改写」——被触达段的终稿文本由 LLM 提议重写（`rewrite_loop` 节点；`original_content` 仅作推理输入喂 LLM，终稿落 `proposed_rewrites`、不回写 `ParagraphRecord.original_content`）；
   - 「幂等纯函数回写」——重写是 LLM 生成式，非纯函数子串替换。
2. **未触达段仍逐字节忠实**：未被任何 supported 假说 / 相关 citation 触达的段落，终稿逐字节拷回原文（底线不变）。
3. **HITL-2 仍是唯一决策闸门**（ADR-0010 不动）：`rewrite_loop` 只**提议**重写文本（`proposed_rewrites: dict[paragraph_id, str]`），HITL-2 逐段确认 / 编辑 / 驳回后才落 `final_document`。
   - 被确认段用提议文本、被驳回段回退原文、未触达段逐字节原文 → 拼成终稿。
   - 绝不替人拍板自动采纳（承诺保留）。
4. **原 `writeback` 节点裁撤**：终稿在 HITL-2 落地，不再有独立回写节点；`adopted → corrected` 状态机（ADR-0011）的回写幂等续跑语义随 `rewrite_loop` 重写适配（`tests/test_orchestrator_resume.py`）。
5. **字节一致弱化不变式**：E2E 断言「无触达 / 继续路径下 `final_document == original_doc`」仍成立（hitl1 继续 / rewrite 无触达段）。

## 权衡

- 放弃「段落原文永不被 LLM 改写」换取基于全文证据的连贯重写能力——这是新产品方向的核心诉求，机械子串替换无法实现。
- 代价：被触达段终稿不再字节级确定，回写幂等性弱化为「未触达段逐字节 + 被触达段以 HITL-2 确认文本为准」。
- 用「仅被触达段放弃、未触达段逐字节忠实」二分把破坏面收敛到最小；用「HITL-2 确认闸门」把生成式重写的决策权仍锁在人手里。

## 影响

- 新增 `rewrite_loop` 节点（逐段 LLM 提议重写）与 `proposed_rewrites: dict[str, str]` channel（单写者=rewrite_loop、读者=hitl2）。
- `hitl2` 重定位为**终稿文本确认闸门**（见 ADR-0018 同期变更的 hitl1 闸门语义对照）。
- 裁撤 `writeback` 节点；`final_document` 由 hitl2 拼装落地。
- `tests/test_writeback.py` 改 / 扩为 rewrite_loop + hitl2 终稿拼装的纯函数子缝（提议→确认→拼接）。
- 字节级还原承诺的范围**收窄**为「未触达段逐字节忠实」，需同步更新 `CONTEXT.md` / `DEVELOPMENT.md` §1 的 tracer bullet 表述。
