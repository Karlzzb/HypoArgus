# ADR-0016：引入历史对话 seam，作用于 list[Source]，部分融合 graph_utils

## 状态

已接受（2026-07-12）

## 背景

ReAct 步骤间唯一记忆是内联 `observations: list[Source]`（`verification.py` `_verify_node`、`hypothesis.py` `_verify_hypothesis`），每步把全量历史塞回 LLM。
dev-guide §3/§4 规划的源压缩/三层记忆完全未实现——真实 LLM 一上线，上下文随 ReAct 步数线性膨胀。

仓内已有 `docs/graph_utils.py` 实现零侵入历史裁剪：`CompressionConfig`（max_messages/max_tokens/strategy/start_on/include_system）、`_default_token_counter`（LLM 自带计数→字符数/4 回退）、`build_prompt_llm_chain`（`preprocess | prompt | llm`）。
但其裁剪对象是 `list[BaseMessage]`（聊天消息序列），且 `trim_messages` 是消息类型敏感的（`start_on="human"`、`include_system`）。
HypoArgus 当前 ReAct「历史」是 `list[Source]`（检索观察），无真实 LLM、无 `BaseMessage`——`LlmClient` Protocol 冻结（见 ADR-0015）。
全量融合 `graph_utils` 需重塑 `LlmClient` 为消息轮次形态，触每个 ReAct 循环=业务逻辑变动，且对当前 Fake stub 不可测。

DeepTutor 另有 `services/session/context_builder.py`（token 预算 + 抗漂移摘要 + 分支 guard + LLM 摘要）与三层记忆 `services/memory/`。
摘要/抗漂移属持久化与可观测性范畴（#3/#4），本轮暂缓。

## 决策

1. **载体 = `list[Source]`**：`HistoryStore[Source]` 作用于检索观察列表，不动 `LlmClient` Protocol。
   业务逻辑零改动，对现有 Fake 可测。
2. **作用域 = 每 ReAct 循环（每节点）**：`_verify_node` / `_verify_hypothesis` 各起一个 `HistoryStore`，与现有 `observations` 作用域一致。
   不做跨 Agent / 跨 run 的持久化（属 #4 checkpointer 切片）。
3. **替换内联 list**：Agent 持 `HistoryStore`，调 `append(source)` + `compressed_view(cfg) -> list[Source]`；内联 `observations` list 消失。
   压缩策略集中在 seam 之后。
4. **部分融合 `graph_utils`**：移植 `CompressionConfig` 形状（`max_messages`→`max_items`、`max_tokens`、`strategy`、token-counter 模式）与 `char/4` 近似计数（适配 `Source` 文本字段）。
   **不移植** `trim_messages`、`BaseMessage` 机制、`build_prompt_llm_chain` 链路接线——它们是 `list[BaseMessage]` 聊天历史路径，待真实 provider adapter 落地时再全量融合。
5. **显式推迟**：`start_on` / `include_system`（消息类型特定，对 `Source` 无意义）、抗漂移摘要、三层记忆（`services/memory/`）、持久化跨 run 记忆——均延后至真实 LLM + #3/#4 切片。
6. **session_id 线程为 #4 预备**：`Orchestrator.run(doc, *, session_config=None)` 透传 `config=` 给 `graph.invoke`；`session_id` 存于 `config.metadata`。
   本轮 `HistoryStore` 为内存态，不消费 `session_id`——纯管道预备，待 #4 checkpointer 以 `session_id` 为持久化键。

## 权衡

- 选 `list[Source]` 而非 `list[BaseMessage]`：尊重「不动业务逻辑」与「对 Fake 可测」，代价是 `graph_utils` 的 `trim_messages` 暂不能复用，仅移植其配置形状与计数回退。
   全量融合留作真实 provider adapter 落地时的自然延伸。
- 作用域取每循环而非每 run：贴合当前 `observations` 现实，避免跨边界状态膨胀；持久化跨 run 留给 #4。
- 部分融合而非全量移植 `context_builder`：摘要/抗漂移属 #3/#4 范畴，本轮引入即越界。
- `session_id` 线程现在无用看似超前，但 langgraph 原生 `RunnableConfig` 零侵入，且是 #4 落地时的天然键，现在埋设代价极低。

## 影响

- 新增 `infra/history.py`：`HistoryStore[Source]` + `CompressionConfig`（部分移植自 `docs/graph_utils.py`）。
- `verification` / `hypothesis` 两 Agent 内联 `observations` list 替换为 `HistoryStore`。
- `Orchestrator.run` 增 `session_config` 形参；业务节点不强制读 config（零侵入）。
- 与 ADR-0015 协同：`ToolResult.sources` 经 `HistoryStore.append` 流入历史；ADR-0015 的 `registry.dispatch` 产出统一载体。
- `ADR-0005` 两层存储不受影响——历史 seam 在 ReAct 循环内，与原文段落表正交。
