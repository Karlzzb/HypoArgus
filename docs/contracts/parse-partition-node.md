# parse+partition 节点 — State 输入/输出契约

> 面向外部子代理开发人员。本文档描述 `parse+partition` 节点消费与产出的 state 树字段，用于规划输入/输出、保证后续接入。
>
> 基于分支 `dev/manifest-assembly` 源码逐行核对。所有断言附 `file:line` 出处。

## 1. 节点身份

- **图/阶段名**：`parse+partition`（合并节点，ADR-0021 / Slice 1）
- **位置**：`START` 之后的第一个节点，`deps=()`
- **节点入口闭包**：`_parse_partition_node(agents)` → `parse_partition_node(state)`
  - 源码：`src/agents/assembly.py:473-497`
- **内部两个子步**（代码在不同模块，但合并为一个节点）：
  - **partition**：纯代码、无 LLM、字节级无损切分。
    `OriginalParagraphs.from_text(original_doc)`（`assembly.py:485`），底层 `partition()` @ `src/partition.py:71`。
  - **parse**：LLM seam，建论证树 + 产 `paragraph_list` + `query_time_range`。
    纯函数 `parse()` @ `src/agents/parser/agent.py:94`，经 `Agents.parse`（Protocol `ParseFn` @ `assembly.py:97-105`）注入。

> 设计要点：partition 的字节级自检（`assert rebuilt == original_doc`，`assembly.py:488`）在 `_guarded` 之外，失败即**硬停**（正确性 bug，不兜底）。
> 仅 parse 部分经 `_guarded` 兜底，异常时写回 `original_paragraphs` + 空树 + 桩 `query_time_range` + 空 `paragraph_list` 单向向前（PRD §13）。

## 2. 输入（从 state 通道读取）

| 字段 | 类型 | 读取处 | 通道定义 | 说明 |
|---|---|---|---|---|
| `original_doc` | `bytes` | `assembly.py:484`（`state["original_doc"]`） | `orchestrator.py:150` | 用户原始输入。**节点闭包唯一从 state 读取的字段**，是 partition 子步的唯一输入。 |

- `original_doc` / `session_context` 在图启动时注入：
  `graph.invoke({"original_doc": bytes(original_doc), "session_context": ctx}, ...)`（`orchestrator.py:382-385`，`runtime/run_real.py:279`）。
- `session_context` 虽在 START 注入并在链路中只读贯穿，但 **parse+partition 节点闭包不读取它**（它只**产出** `query_time_range`，不消费 session 上下文）。
  下文"输出"列表无 `session_context`，请勿误接。

parse 纯函数 `parse(original_paragraphs, llm)` 的入参（`OriginalParagraphs` + 注入的 LLM seam）**在节点内部由 `original_doc` 派生**，不来自 state 通道。

## 3. 输出（写回 state 通道）

节点返回 patch dict，经 LangGraph 通道 reducer 路由。
源码：`assembly.py:495`（`return {**out, "original_paragraphs": original_paragraphs}`）+ `_parse_output_patch` @ `assembly.py:500-507`。

| 字段 | 类型 | reducer | 写入处 | 产生细节 |
|---|---|---|---|---|
| `original_paragraphs` | `OriginalParagraphs` | 无（单写者） | `assembly.py:495` | `OriginalParagraphs.from_text(original_doc)` @ `assembly.py:485`；类型 @ `src/original_paragraphs.py:21-67`。REPLAY 重跑整体覆盖。 |
| `argument_tree` | `list[Argument]` | `merge_argument_tree` | `assembly.py:504` | 取自 `ParseOutput.argument_tree`，由 `parse()` @ `parser/agent.py:160-168` + 影子节点 @ `parser/agent.py:180-188` 构建。 |
| `query_time_range` | `TimeRange` | 无（单写者） | `assembly.py:505` | 取自 `ParseOutput.query_time_range`，**桩值** `DEFAULT_QUERY_TIME_RANGE`（`domain.py:238-242`，start=2025-01-01，end=2026-12-31）。当前无真实 LLM 时间识别。设置 @ `parser/agent.py:208`。 |
| `paragraph_list` | `list[ParagraphRecord]` | `merge_paragraph_list` | `assembly.py:506` | 取自 `ParseOutput.paragraph_list`，构建 @ `parser/agent.py:197-205`。 |
| `errors` | `list[str]` | `_append_errors`（append） | `assembly.py:465`（`_guarded` 兜底经 `_log_error_patch` @ `assembly.py:411-414`） | **仅异常路径**写一条 `"[parse+partition] ExcType: msg"`。兜底 patch 为空 `ParseOutput()`（空树 + 桩 qtr + 空 paragraph_list），**但 `original_paragraphs` 写入照常发生**（`assembly.py:495` 在 `_guarded` 之外）。 |

## 4. `paragraph_list` 元素 schema — `ParagraphRecord`

定义：`src/domain.py:181-199`（Pydantic `BaseModel`）。

| 字段 | 类型 | 默认 | parse+partition 写入处 | 说明 |
|---|---|---|---|---|
| `paragraph_id` | `str` | （必填） | `parser/agent.py:199` | 形如 `p0001`（4 位零填充），对齐自 `OriginalParagraphs.paragraph_ids()`（源自 `partition.py:133`）。 |
| `summary` | `str` | `""` | `parser/agent.py:200` | 折叠自 `ParseResult.paragraph_summaries`（LLM seam 输出），`summaries.get(pid, "")`（`parser/agent.py:193`）。段落摘要的**唯一定义点**（退役的 `paragraph_summaries` channel 已折叠于此，ADR-0025）。 |
| `original_content` | `str` | `""` | `parser/agent.py:201` | 该段 bytes 经 `surrogateescape` 解码（`_decode()` @ `parser/agent.py:45-48`）。**LLM 永不撰写原文**。 |
| `argument_tree_ids` | `list[str]` | `[]` | `parser/agent.py:202` | 该段所含全部节点 id（核心 `n0000` 式 id + `bg-{pid}` 影子 id），构建于 `nodes_by_paragraph`（`parser/agent.py:145, 159, 179`）。 |

> 段落↔节点关系为**正向、第一类、存于段落侧**：
> `argument_tree_ids` ↔ `Argument.argument_id` 互引，不嵌套。
> 一致性由硬停自检保证（每个 `argument_id` 恰出现于一个段落的 `argument_tree_ids`，反之亦然）。

## 5. `argument_tree` 元素 schema — `Argument`（parse 写入的字段子集）

定义：`src/domain.py:145-178`。
parse 仅写入以下字段：`argument_id`、`argument_type`、`parent_id`、`children_ids`、`argument_weight`、`status=UNVERIFIED`（`parser/agent.py:160-168, 180-188`）。
其余字段（`issue_tags` / `candidate_hypotheses` / `merge_decision` / `adopted_hypothesis_id`）由下游节点写。

## 6. 通道/合并语义

- `paragraph_list`：顶层通道键，`merge_paragraph_list` reducer（`orchestrator.py:113-136`）——**按 `paragraph_id` upsert、保持首见顺序**。
  每次节点写入提交**整份 `list[ParagraphRecord]`**（非逐元素 append），reducer 负责合并。
  写者=parse+partition（创建 / 打回重跑整列表）+ hitl1（EDIT 同步 `argument_tree_ids`）。
  upsert 保证不重复、不丢序、按段覆盖更新。
- `argument_tree`：`merge_argument_tree` reducer（`orchestrator.py:74-98`）——按 `argument_id` upsert 整树写入。
- `original_paragraphs` / `query_time_range`：无 reducer，单写者整体覆盖。

## 7. agent 层返回类型（非 state 通道，仅作内部桥接）

`ParseOutput` @ `src/agents/parser/contract.py:119-134`：
`argument_tree` / `query_time_range` / `paragraph_list`，由 `_parse_output_patch` 摊成三个 state 写入。

LLM seam 合约：`LlmClient.parse(paragraphs: list[ParagraphView]) -> ParseResult`（`parser/contract.py:137-144`）。
