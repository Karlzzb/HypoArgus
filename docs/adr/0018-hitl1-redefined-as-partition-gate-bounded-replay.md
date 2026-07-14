# ADR-0018：hitl1 重定义为 partition 确认闸门 + 有界打回

## 状态

已接受（2026-07-13）。打破「绝不打回」强约束（原 `docs/DEVELOPMENT.md` §1 单向流控原则「流水线严格单向、绝不打回」）。
本 ADR 是流水线重构的偏离记录之一，配套见 ADR-0017 / ADR-0019 ~ ADR-0021。

## 背景

既有 `hitl1` 是「结构确认、可跳过」闸门（`agents/hitl1/{contract,agent}.py`）：人审结构编辑，动作集为 `SKIP / ACCEPT / EDIT`，图层级单向推进，**绝不打回**上游。
单向流控是既有强约束（`DEVELOPMENT.md` §5）：异常即记日志、就地降级、继续向前，无复杂分布式重试。

但新产品方向要求：在 partition 之后由人确认**段落切分是否合理**，不合理时**按用户 prompt 重跑 `parse+partition`**，使切分能向人期望的方向调整。
这要求 hitl1 具备「打回重跑上游」的能力，直接打破「绝不打回」与「严格单向」。
若打回无界，则人 / LLM 反复卡住可致无限循环。

## 决策

1. **hitl1 重定义为 partition 确认闸门**：人确认段落切分是否合理；合理则继续下游，不合理则打回重跑 `parse+partition`（按用户 prompt）。
2. **打回打破「绝不打回」**：唯一受控打回点为 `hitl1 → parse+partition`，方向为回上游；其余 stage 仍严格单向。
3. **打回须有界**：max retries（默认 3，可配置）。
   - 计数器随 hitl1 打回递增；超限仍**向下推进**，并在 `errors` 追加 `partition_retry_exhausted` 标签（沿用 `_log_error_patch` 语义，但**不视为异常降级**——它是受控分支，不经 `_guarded` 的异常降级路径）。
4. **partition「按 prompt 重切」当前为伪代码桩**（PRD §21 / Out of Scope）：不真实 LLM 驱动重切，只穿 state、原样或占位重切；真实 prompt 驱动重切为后续切片。
5. **hitl1 contract 动作集调整**：`Hitl1Decision` 的 action 对齐为「确认继续 / 打回重跑」两类语义，以实现时不破坏既有 `FakeHitl1Gate` 离线桩为准（既有 `skip/accept/edit` 与新语义对齐或收编）。

## 权衡

- 选「有界打回」而非「保持严格单向」：换取在生成假说 / 检索之前纠正不合理切分的能力——切分不合理时，下游整条链路都建立在错误的段落边界上，事后纠正代价更高。
- 代价：打破既有「绝不打回」强约束，引入受控循环；用 max retries 把循环有界化，避免无限卡死。
- 选「超限向前 + 贴标签」而非「超限硬停」：与既有 `_guarded` 单向流控语义一致（向前推进 + 记录），不引入新硬停点（HITL-2 仍是唯一硬闸门）。

## 影响

- `hitl1` 节点 build 闭包重定义：读 `parse+partition` 产出的 `argument_tree` + `original_paragraphs` 供人确认；打回时按 prompt 重跑。
- `hitl1` 的 `deps` 改为 `("parse+partition",)`（随 ADR-0019 的合并节点对齐）。
- 新增受控打回边 `hitl1 → parse+partition`；图装配（`agents/assembly.py` `MANIFEST` / `runtime/orchestrator.py` `default_pipeline`）须表达该边。
- `tests/test_hitl1.py` 覆盖：确认继续、打回一次后继续、打回超限贴标签向前。
- `tests/test_orchestrator_fallback.py` 扩 hitl1 打回超限分支。
