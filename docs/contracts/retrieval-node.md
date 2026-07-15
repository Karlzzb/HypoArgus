# retrieval 节点 — State 输入/输出契约

> 面向外部子代理开发人员。本文档描述 `retrieval`（检索）节点消费与产出的 state 树字段，用于规划输入/输出、保证后续接入。
>
> 基于分支 `dev/manifest-assembly` 源码逐行核对。所有断言附 `file:line` 出处。

## 1. 节点身份

- **图/阶段名**：`retrieval`（MANIFEST @ `src/agents/assembly.py:849-859`）
- **节点入口闭包**：`_retrieval_node(agents)` → `retrieval_node(state)`
  - 源码：`src/agents/assembly.py:625-655`
- **注入的可调用**：`agents.retrieval`（Protocol `RetrievalFn` @ `assembly.py:165-182`）
- **当前实现**：桩 `_stub_retrieval` @ `assembly.py:316-331`，返回空 citations；
  **真实检索后端 = `None`，显式 Out of Scope**（MANIFEST `real=None`，`assembly.py:852`）。
  `infra.retrieval` 接口层在接入真实后端时**不变**。

> 桩读取 `session_context` / `query_time_range` 但不触发联网——仅"穿背景"供真实后端就位。
> 检索 fn 异常即"本轮无 citations"：记日志、空 citations 向前，下游见无素材、终稿逐字节等于原文（PRD §13 单向向前）。

## 2. 输入（从 state 读取）

读取全部发生在节点闭包 `assembly.py:638-641`。
`RetrievalFn` Protocol 形参与之一一对应（`assembly.py:176-182`）。

| 字段 | 类型 | 读取处 | 通道定义 | 写者（上游来源） | 备注 |
|---|---|---|---|---|---|
| `argument_tree` | `list[Argument]` | `assembly.py:638`（`state["argument_tree"]`） | `orchestrator.py:155`，reducer `merge_argument_tree` | parse+partition / judgment | 桩不据此搜索，但携带以便真实后端按 `argument_id` 索引 citations。 |
| `hypotheses` | `dict[str, list[Hypothesis]]` | `assembly.py:639`（`state.get("hypotheses", {})`） | `orchestrator.py:156`，reducer `_merge_dict` | hypothesis_propose（唯一写者） | 缺省 `{}`。`Hypothesis.text` 是检索的 query 输入（`RetrievalFn` docstring `assembly.py:168-169`）。 |
| `query_time_range` | `TimeRange` | `assembly.py:640`（`state.get("query_time_range", DEFAULT_QUERY_TIME_RANGE)`） | `orchestrator.py:153`，无 reducer | parse+partition（桩值） | 缺省 `DEFAULT_QUERY_TIME_RANGE`。 |
| `session_context` | `SessionContext` | `assembly.py:641`（`state["session_context"]`） | `orchestrator.py:152`，无 reducer | entry 注入（`runtime/run_real.py`） | **必填键**，直接索引（非 `.get`）。全链只读。 |

### 支撑类型定义

- `Argument` — `src/domain.py:145-178`（纯推理结构，无 `paragraph_id`/`content`）。
- `Hypothesis` — `src/domain.py:128-142`：`hypothesis_id: str`、`text: str`、`relation: HypothesisRelation`、`status: HypothesisStatus = pending`、`confidence: float 0-1`。
- `TimeRange` — `src/domain.py:211-221`：`start: date | None`、`end: date | None`、`rationale: str`。
- `SessionContext` — `src/domain.py:224-235`：`session_id: str`、`user_id: str`、`current_time: datetime`、`user_prompt: str`。

## 3. 输出（写回 state）

| 字段 | 类型 | reducer | 写入处 | 唯一写者？ |
|---|---|---|---|---|
| `citations` | `dict[str, list[Source]]` | `_merge_dict` | `assembly.py:644-651`（正常路径：`{"citations": agents.retrieval(...)}`）；`assembly.py:652`（兜底：`{"citations": {}}`） | **是**——retrieval 是 `citations` 唯一写者（STATE.md §1 line 44、§1.2 line 69）。 |
| `errors`（仅兜底路径） | `list[str]` | `_append_errors`（append） | `assembly.py:465` 经 `_log_error_patch` | 非 retrieval 的正式输出，仅在异常兜底时 append。 |

写入经 `_guarded("retrieval", body, fallback)`（`assembly.py:642-653`）：
任何非 `Hitl2GateError` / 非 `GraphBubbleUp` 异常降级为 `{"citations": {}}` + `errors` append。

## 4. 检索结果元素 schema — `Source`

`citations` 的值类型为 `dict[str, list[Source]]`。
元素 `Source` 定义：`src/infra/retrieval.py:53-65`（Pydantic `BaseModel`）。

| 字段 | 类型 | 必填？ | 说明 |
|---|---|---|---|
| `source_id` | `str` | 必填 | 稳定、确定性派生（Mock：`blake2b(kind|origin|seed|idx)`，`retrieval.py:237-243`）。 |
| `kind` | `RetrievalKind`（StrEnum） | 必填 | `network` / `knowledge_base` / `structured`（`retrieval.py:45-51`）。 |
| `origin` | `str` | 必填 | 域名 / 知识库名 / 模板 id。 |
| `title` | `str \| None` | 可选（默认 `None`） | |
| `snippet` | `str` | 必填 | 检索到的文本片段——流入下游 LLM prompt（judgment / rewrite_loop）的"citation 片段"。 |
| `locator` | `str \| None` | 可选（默认 `None`） | 如 URL / `kb://...` / `db://...`。 |

## 5. 通道/合并语义

- `citations`：partial channel，`_merge_dict` reducer（`orchestrator.py:104-110`）——**按 key 求并集**（`{**left, **right}`，right 胜出；但单写者无 key 冲突）。
  语义=**按 key 替换**，非 append。
- key 空间 = `argument_id`（如 `n0001`、`bg-...`）**或** `hypothesis_id`（如 `h-...`）；
  两族 id 不冲突（`assembly.py:170-173`，STATE.md §1.2 line 69）。
- `Source` 是被每个下游消费者复用的承载类型：
  judgment `JudgmentFn` 形参 `citations: dict[str, list[Source]]`（`assembly.py:143`）；
  rewrite_loop `RewriteLoopFn` 形参同型（`assembly.py:205`）。

## 6. 接入真实后端的契约边界

- `RetrievalLayer.retrieve` 真实后端位于 `src/infra/retrieval.py`；
  接入时 **state 侧契约（上述输入 4 字段、输出 `citations` 单写者、`Source` schema、`_merge_dict` 按 key 替换语义）应保持不变**。
- 当前桩 `real=None`，故真实后端尚未联网；
  外部子代理开发人员据此 4 输入 / 1 输出 + `Source` schema 规划接入。
