# ADR-0015：引入工具框架 seam，RetrievalLayer 降格为众多工具之一

## 状态

已接受（2026-07-12）

## 背景

当前无「工具」一等概念。
两个 ReAct Agent（事实验证 `verification.py`、假设生成 `hypothesis.py`）各自复制一份 `_build_request(step: SearchStep) -> RetrievalRequest` 翻译器，再内联调用 `retrieval.retrieve()`、`observations.append()`。
`RetrievalLayer` Protocol 是唯一工具，`SearchStep.channel`（network/knowledge_base/structured）是唯一工具选择器。
工具调用逻辑与 Agent 循环耦合，无 locality——两份近乎复制的翻译器是明显的重复。

DeepTutor（非 langgraph 参考项目）有成熟工具框架：`core/tool_protocol.py`（ToolDefinition/BaseTool/ToolResult）、`runtime/registry/tool_registry.py`、`core/agentic/tool_dispatch.py`（并行/去重/pause-for-user/deferred/OpenAI-schema 发射）。
但其重型机器（并行调度、去重、pause、deferred、OpenAI-schema、ToolEventSink）依赖原生函数调用与多工具并行场景，HypoArgus 当前不具备——ReAct 是单步单迭代、单后端、`LlmClient` Protocol 冻结（无原生函数调用）。

## 决策

1. **骨架移植**：引入 `infra/tool_protocol.py`——`ToolResult`（sources/metadata/success）、`BaseTool`（ABC，`execute(**kwargs) -> ToolResult`）、`ToolRegistry`（`register`/`get`/`dispatch`）。
2. **`RetrievalTool(BaseTool)` 适配器**：包 `RetrievalLayer`，独占 `SearchStep → RetrievalRequest` 翻译逻辑。
   两份 `_build_request` 收口到此一处。
   `RetrievalLayer` Protocol 与 `RetrievalRequest` 判别联合、`validate_request`、`redact_query` 保留——它们是检索领域，非工具框架。
3. **channel 键 dispatch**：`ToolRegistry` 以 `SearchStep.channel` 为键路由到工具。
   诚实于冻结的 `LlmClient`——LLM 仍发 `SearchStep`，无需引入通用 `ToolCall` 翻译层。
   待真实 provider adapter 落地并支持原生函数调用时，再评估 name 键 + 通用 `ToolCall`。
4. **明确不移植**：OpenAI-schema 发射、并行调度（`MAX_PARALLEL`）、重复调用去重、`pause_for_user`、deferred 渐进披露、`ToolEventSink`。
   单工具 + 单步迭代 + 冻结 LLM 下属假设性开销。
5. **ReAct Agent 改造**：两 Agent 的循环由「`_build_request(step)` + `retrieval.retrieve()` + `observations.append`」改为 `registry.dispatch(step)`；`ToolResult.sources` 流入历史 seam（见 ADR-0016）。

## 权衡

- seam 的正当性源于**两处重复的同一翻译逻辑**（locality），而非「未来多工具」假设。
   骨架 `BaseTool`/`ToolRegistry` 同时为未来工具留位，但当前仅 `RetrievalTool` 一个实现。
- channel 键而非 name 键：诚实反映 `LlmClient` 冻结现状。
   代价是未来若引入非检索形态工具，可能需要重构 dispatch 键；但「一个 adapter 是假设 seam，两个才是真 seam」，届时再深化有据。
- 不移植并行/去重：DeepTutor 的 `tool_dispatch` 是其 ReAct 多工具场景的产物，移植即过度工程。
   错误收口（工具异常 → `ToolResult(success=False)` → Agent 判失败/续跑）保留，因其是 ReAct 循环健壮性所需。

## 影响

- 新增 `infra/tool_protocol.py`、`infra/retrieval_tool.py`、`runtime/tool_registry.py`。
- `verification`/`hypothesis` 两 Agent 删除各自 `_build_request`，改调 `registry.dispatch`。
- 业务逻辑（论证树、双轨合并、回写、状态机、HITL）零改动。
- 与 ADR-0016 历史 seam 协同：`ToolResult.sources` 成为 `HistoryStore` 的输入载体。
