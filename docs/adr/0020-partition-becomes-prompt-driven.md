# ADR-0020：partition 变 prompt 驱动（按用户 prompt 重切）

## 状态

已接受（2026-07-13）。打破 ADR-0009 确定性；**无损性（各段拼接 == `original_doc`）须保**。
本 ADR 是流水线重构的偏离记录之一，配套见 ADR-0017 ~ ADR-0019 / ADR-0021。

## 背景

ADR-0009 把「段落切分」与「论证树解析」彻底解耦：partition 是**纯代码、零 LLM** 的确定性无损切分（Markdown 块级 + 空行边界），灌只读原文表并校验分区不变式 `assert(所有段落按序拼接 == 原始输入)`。
字节级还原由此成为代码级确定，与 LLM 质量无关。

但新产品方向要求：hitl1 打回时，partition 能**按用户 prompt 重切**（ADR-0018），使切分向人期望方向调整（如合并语义连续的跨段、拆分过长段）。
纯代码规则切分无法响应 prompt——它只能按固定块级边界切。
即 partition 需从「纯代码确定」变为「prompt 驱动」，直接打破 ADR-0009 的确定性。

## 决策

1. **partition 变 prompt 驱动**：partition 接收用户 prompt，可据 prompt 重切段落边界（合并 / 拆分 / 调整）。
2. **当前为伪代码桩**（PRD §21 / Out of Scope）：不真实 LLM 驱动重切，只穿 state、原样或占位重切；真实 prompt 驱动重切（含 LLM 驱动 vs 结构化 hint 参数的抉择、max retries 调参）为后续切片。
3. **无损性须保**：即便 partition 变 prompt 驱动，分区不变式 `assert(各段按序拼接 == original_doc)` 仍须通过——这是字节级还原 / 回写 / 还原真相源的底线（ADR-0005/0009）。
   - prompt 驱动只调整切分边界，**不增删字符**；切分后仍须通过字节级自检。
4. **partition + parse 合并为单一图节点 `parse+partition`**（PRD 拓扑）：partition 的纯代码切分 + 字节级自检、parse 的建树 / content 逐字节拷回主逻辑**不动**；新增的是同一 LLM 调用多吐结构化输出（`query_time_range` / `paragraph_summaries`，见 ADR-0021 / STATE.md）。
   - hitl1 打回重跑的靶节点即 `parse+partition`（ADR-0018）。

## 权衡

- 选「partition 变 prompt 驱动」而非「保持纯代码确定」：换取响应人审意见调整切分边界的能力——切分不合理时，固定块级边界无法纠正。
- 代价：打破 ADR-0009 确定性，切分质量与 prompt / LLM 质量相关；用「无损性须保」把破坏面收敛为「边界可调、字符不增删」，字节级还原底线不破。
- 当前桩化：真实 prompt 驱动重切的实现抉择（LLM 驱动 vs 结构化 hint 参数）推迟到后续切片，当前不阻塞主线；桩路径下终稿对未触达段仍逐字节等于原文。

## 影响

- `parse+partition` 节点接收用户 prompt（经 `session_context.user_prompt`，ADR-0021）；partition 重切为伪代码桩。
- partition 字节级自检（各段拼接 == `original_doc`）仍保留、仍硬停（不包 `_guarded`，ADR-0009 语义）。
- hitl1 打回（ADR-0018）的靶节点为 `parse+partition`；打回超限贴 `partition_retry_exhausted`。
- `tests/test_parser.py`：partition + parse 合并节点的字节级自检不变。
- 真实 prompt 驱动重切属后续切片（Out of Scope），当前桩可断言。
