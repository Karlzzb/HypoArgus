# ADR-0021：贯穿 state 落 PipelineState（session_context + query_time_range）

## 状态

已接受（2026-07-13）。部分覆盖 ADR-0016（业务相关贯穿 state 不再仅走 `RunnableConfig`）。
本 ADR 是流水线重构的偏离记录之一，配套见 ADR-0017 ~ ADR-0020。

## 背景

ADR-0016 把 `session_id` 线程埋入 `RunnableConfig`（`Orchestrator.run(doc, *, session_config=None)` 透传 `config=` 给 `graph.invoke`），明确「业务节点不强制读 config（零侵入）」，`session_id` 为 #4 checkpointer 持久化键预备、当前不消费。
`RunnableConfig` 承载 langgraph 原生 metadata / callbacks / checkpointer。

但新产品方向要求 LLM 检索与生成的输入都带上**贯穿全链的运行上下文**（PRD §15/§16/§17）：

- `session_id` / `user_id` / `current_time` / `user_prompt`：同一会话内多轮调用有一致运行上下文。
- `query_time_range`：文章所需的数据查询时间范围，下游检索能限定在正确时间窗内。

这些是**业务消费字段**（要进 LLM prompt），不是 langgraph 原生 metadata。
若仍只走 `RunnableConfig`，则每个业务节点须显式从 config 读 → 触点多、类型契约弱、typed `Agents` 无法写明依赖。

## 决策

1. **贯穿 state 落 `PipelineState`**：新增 `session_context: SessionContext`（`session_id` / `user_id` / `current_time: datetime` / `user_prompt: str`）与 `query_time_range: TimeRange`（`start: date | None` / `end: date | None` / `rationale: str`）为 `PipelineState` 顶层 channel。
2. **单写者**：
   - `session_context` 单写者=入口注入（`runtime/run_real.py`，与 `original_doc` 同入 START），全链只读。
   - `query_time_range` 单写者=`parse+partition`（当前为桩值 `TimeRange(start=2025, end=2026, rationale="默认值·真实识别待后续")`，不真实调 LLM 识别）；读者=retrieval / rewrite / judgment。
3. **以单一嵌套对象流转**：`session_context` 作为单一 `SessionContext` 嵌套对象在 `PipelineState` 中流转，**不污染顶层 channel**（不把 `session_id` / `user_id` / ... 各拆成顶层字段），typed 契约能写明依赖。
4. **`RunnableConfig` 职责收窄**（ADR-0016 部分覆盖）：`RunnableConfig` 仍承载 langgraph 原生 metadata / callbacks / checkpointer；业务相关贯穿 state（`session_context` / `query_time_range`）改走 `PipelineState` channel，业务节点读 channel 而非 config。
5. **输入压缩铁律延续**：`session_context` / `query_time_range` 作为背景进 LLM prompt（检索与生成 seam），但仍不回灌 `status` / `argument_weight` / `parent_id` / `children_ids` / `issue_tags` / `merge_decision` 等内部状态字段。
6. **`current_time` 注入方式**：真实运行时刻由入口注入 `session_context.current_time`（非节点内 `datetime.now()`），保证可测、可复现。

## 权衡

- 选「贯穿 state 落 `PipelineState`」而非「继续走 `RunnableConfig`」：业务消费字段要进 LLM prompt、要 typed 契约写明依赖；走 channel 则单写者 / 读者 / reducer 清晰，节点签名能声明依赖。
- 代价：`PipelineState` 顶层字段从 7 增至更多（新增 `session_context` / `query_time_range` / `paragraph_summaries` / `citations` / `proposed_rewrites`）；用「单一嵌套对象」把 `session_context` 收口为一字段，避免顶层膨胀。
- `RunnableConfig` 不废弃：langgraph 原生机制（callbacks / checkpointer）仍走 config，二者分层（业务字段走 channel、原生机制走 config）。

## 影响

- `src/domain.py` 新增 `SessionContext` / `TimeRange` 域类型。
- `PipelineState`（`runtime/orchestrator.py`）新增 `session_context` / `query_time_range` / `paragraph_summaries` / `citations` / `proposed_rewrites` 五 channel（reducer / 单写者 / 读者见 STATE.md §1）。
- `runtime/run_real.py` 入口接收 `session_context`（与 `original_doc` 同入 START）。
- retrieval / rewrite / judgment / hypothesis_propose 节点读 `session_context` / `query_time_range` 进 LLM prompt。
- ADR-0016 的 `session_id` 线程仍保留于 `RunnableConfig`（#4 checkpointer 键），不与 `session_context.session_id` 冲突——前者是 langgraph 原生机制、后者是业务消费字段。
- `docs/STATE.md` §1 为新 5 字段唯一描述点（本 ADR 不重复字段流向）。
