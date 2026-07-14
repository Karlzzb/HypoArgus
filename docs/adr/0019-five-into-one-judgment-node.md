# ADR-0019：verification / hypothesis 取证 / merge / impact / consistency 五合一为 judgment 节点

## 状态

已接受（2026-07-13）。重构 ADR-0002（乐观并行）/ ADR-0006（12 格矩阵合流）的双线路并行设计。
本 ADR 是流水线重构的偏离记录之一，配套见 ADR-0017 / ADR-0018 / ADR-0020 / ADR-0021。

## 背景

既有「检索之后的判断」分散在**五个图节点**（`DEVELOPMENT.md` §1）：

- `verification`（体检，线路 1）：ReAct 逐段检索取证，写 `argument_credibility` partial channel（`ArgumentStatus`）。
- `hypothesis`（开药，线路 2）：propose 生成假说 + ReAct 逐条取证，写 `hypotheses` partial channel（`list[Hypothesis]`）。
- `merge`：读两 partial channel，字段级合流后跑 12 格矩阵裁决（ADR-0006），写 `argument_tree`。
- `impact`：剩余支撑率传导，判 `invalid` / `weakening`，写 `argument_tree`。
- `consistency`：单次扫描贴 `issue_tags`，写 `argument_tree`。

体检 ∥ 开药的并行前提是「两线路各自 ReAct 逐段逐点重复发起检索」（ADR-0002 乐观并行）。
但新产品方向把检索统一前置为**单一批量检索节点 `retrieval`**（一次发起、统一返回全部 citations）。
检索既已统一前置，并行双线路失去并行前提——两线路不再各自检索，而是消费同一批 citations，并行收益消失。
此时仍保留五节点则控制流冗长、节点数偏多。

## 决策

合并检索之后的判断节点为**单一 `judgment` 节点**：

1. **节点数压成 1**：裁撤 `verification` / `hypothesis`（取证）/ `merge` / `impact` / `consistency` 五个 `AgentEntry`，新增单一 `judgment` 的 `AgentEntry`（`deps=("retrieval",)`）。
2. **LLM 取证集中**：judgment 节点引入新 LLM seam，吃 `(argument_tree, hypotheses, citations, session_context, query_time_range)` → 产 `(ArgumentStatus per argument, HypothesisStatus per hypothesis)`。
   - 该 seam 取证职责承接原 `VerifyLlmClient.next_step` 与 `HypothesisLlmClient.next_verify_step`：吃 `citations` 直接判终态，**不再 ReAct 逐段逐点检索**（检索已由 retrieval 统一前置）。
   - 真实 adapter 用扁平信封 schema（延续 `infra/llm_adapters.py` 既有风格，规避 `oneOf` 判别联合不稳）。
3. **纯函数逻辑保留、不交 LLM 裁决**：
   - `merge`（12 格矩阵，ADR-0006）、`impact`（剩余支撑率，ADR-0003/0013）、`consistency`（标签批注，ADR-0012）的纯函数逻辑**不动**，并入 judgment build 闭包按序调用。
   - 即「五合一」是**控制流合并**，不是「把裁决交给 LLM」——确定性裁决逻辑仍是纯函数。
4. **partial channel 收口**：取证不再分两线路写两个 partial channel。
   - `argument_credibility`：若 judgment 直接写回 `argument_tree`（终态写回树），则**裁撤**该 partial（倾向裁撤，PRD §24）；终局决定在实现时据 judgment 是否仍分阶段产 partial 定。
   - `hypotheses` channel 形状随取证落终态更新：propose 阶段写入的 `Hypothesis.status` 为 pending，由 judgment 取证后落终态（supported / doubtful / refuted）。
5. **兜底**：judgment 整体异常 → 覆盖范围内未判决节点置 `error`（沿用现 `_mark_verify_scope_error` 语义）；经 `_guarded` 降级 + 单向向前。

## 权衡

- 选「五合一」而非「保留五节点」：检索统一前置后并行双线路失去并行前提，五节点控制流冗长；合并为单节点简化后端控制流。
- 代价：judgment 节点职责变重（取证 + 三套纯函数裁决）；用「纯函数逻辑不动、按序调用」把复杂度收口在 build 闭包内，确定性裁决不退化。
- 选「merge/impact/consistency 保纯函数」而非「整体交 LLM」：保留确定性裁决、可单测、可解释（ADR-0006/0012/0013 的不变性不破）。
- 选「倾向裁撤 `argument_credibility`」而非「保留 partial」：judgment 单写者直接写回树时，partial channel 是死字段；裁撤使 state 不留死字段（PRD §24）。

## 影响

- `MANIFEST`：裁撤五 `AgentEntry`、新增 `judgment` 一条；`default_pipeline()` 派生 `retrieval → judgment` 边。
- 新 judgment LLM seam 契约（`agents/judgment/contract.py`）；真实 adapter 落 `infra/llm_adapters.py`。
- `merge` / `impact` / `consistency` 纯函数单测（`tests/test_merge.py` / `test_impact.py` / `test_consistency.py`）不变——验证「并入但逻辑不动」。
- `argument_credibility` 裁撤决定在 STATE.md 同步（§1 标记「倾向裁撤、待实现终局」）。
- `tests/test_orchestrator_fallback.py` 覆盖 judgment 降级（未判决节点置 `error`）。
