# 开发文档（DEVELOPMENT）

HypoArgus 是论证驱动型文档修订多智能体系统：纯文本 → 段落切分 → 论证树 → 开药 → 批量检索 → 裁决（取证 + 合并 + 影响传导 + 一致性）→ 逐段重写提议 → HITL-2 终稿确认 → 终稿。
本文面向贡献者，描述模块边界、seam、装配与扩展点；术语见 `CONTEXT.md`，架构决策见 `docs/adr/`，状态树字段流向见 `docs/STATE.md`。
状态树字段流向（主/子智能体 state、字段来源、LLM seam 输入形式）见 `docs/STATE.md`。

## 1. 架构总览

整条流水线是一张 LangGraph `StateGraph`，控制流落边（代码）而非 prompt 散文。
数据在 stage 间经 **state channel** 路由，**无跨模块直接调用**；流水线严格单向——唯 `hitl1` 经条件边有**有界打回**（`hitl1 → parse+partition`，max retries 默认 3，超限向前 + 贴 `partition_retry_exhausted`；ADR-0017），其余 stage 绝不打回。

速记（拓扑骨架·Slice 6 后）：

```
START → parse+partition → hitl1 → hypothesis_propose → retrieval
      → judgment → rewrite_loop → hitl2 → END
```

> **重构方向（ADR-0017·已落地）**：Slice 1–6 全部落地（parse+partition 合并、hitl1 重定义、hypothesis_propose、retrieval 桩、judgment 五合一、rewrite_loop 新增 + hitl2 重定位为终稿闸门 + writeback 裁撤）。
> 术语见 `CONTEXT.md`「重构方向术语」；字段流向见 `docs/STATE.md` §1。

目标单向图（Slice 6 落地后）：

```
START → parse+partition
          产 argument_tree + paragraph_list + query_time_range
        → hitl1                  （partition 确认闸门；可打回重跑 parse+partition，有界·ADR-0017）
        → hypothesis_propose     （逐 argument 产候选假说，status=pending）
        → retrieval              （批量检索，统一返回 citations·当前桩）
        → judgment               （LLM 取证 + 纯函数 merge/impact/consistency·ADR-0017）
        → rewrite_loop           （逐段 LLM 提议重写；未触达段逐字节拷回·ADR-0017·Slice 6）
        → hitl2                  （人确认终稿文本·终稿文本确认闸门·Slice 6 重定位）
        → final_document
END
贯穿 state：session_context(session/user/current_time/user_prompt) + query_time_range（ADR-0017）
```

节点增删（重构后）：

| 操作 | 节点 | 说明 |
|---|---|---|
| 合并 | `partition` + `parse` → `parse+partition` | 单一 `AgentEntry`，`deps=()` 接 START；partition 切分 + 字节自检、parse 建树主逻辑不动（`Argument` 不再存 `content` / `paragraph_id`，原句收到 `paragraph_list`），新增两阶段 LLM 调用多吐 `query_time_range` / `paragraph_list`（摘要折叠进 `paragraph_list.summary`，P-01：树 + 摘要分块拆关注点）。ADR-0017 / ADR-0025 |
| 重定义 | `hitl1` | partition 确认闸门 + 有界打回（ADR-0017）；动作集对齐「确认继续 / 打回重跑」 |
| 新增 | `hypothesis_propose` | 逐 argument 调 `propose`（不取证），产 pending 假说；读 `paragraph_list`（取该段 `original_content` + `summary`）。ADR-0017 |
| 新增 | `retrieval` | 批量检索，统一返回 `citations`；当前伪代码桩（空 citations，不联网）。ADR-0017 |
| 五合一 | `verification` + `hypothesis`（取证）+ `merge` + `impact` + `consistency` → `judgment` | 控制流合并为 1，merge/impact/consistency 纯函数逻辑不动、由 judgment 按序串联调用。ADR-0017 |
| 新增 | `rewrite_loop` | 逐段 LLM 提议重写；产 `proposed_rewrites`。ADR-0017·Slice 6 |
| 重定位 | `hitl2` | 终稿文本确认闸门：逐段确认 / 编辑 / 驳回 `proposed_rewrites`，拼 `final_document`。ADR-0017·Slice 6 |
| 裁撤 | `writeback` | 终稿在 hitl2 落地，不再有独立回写节点。ADR-0017·Slice 6 |

详细控制流（标注每个 stage 是 **单次** 还是 **循环**，以及 channel 路由、兜底边界）：

```
START
  │  original_doc: bytes（原始输入，单次写入）
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ① parse+partition                                【单次·有 _guarded·有 LLM】 │
│    partition：段落切分 + 字节级自检 assert（失败即正确性 bug → 硬停、不兜底）│
│    parse：LlmClient.parse(original_paragraphs)（唯一读段落文本的环节）        │
│    失败降级 → argument_tree=[]（空树向前）+ 桩 query_time_range + 空 summaries│
│    writes: original_paragraphs, argument_tree, query_time_range,            │
│            paragraph_list                                                    │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ② hitl1                          【partition 确认闸门·同步·有 _guarded】  │
│    gate.review(argument_tree) → SKIP / ACCEPT / EDIT / REPLAY（ADR-0017）   │
│    SKIP/ACCEPT/EDIT = 确认继续（EDIT 逐 op validate_tree；改树形不改文本）  │
│    REPLAY = 打回重跑 parse+partition（按 user prompt·当前伪代码桩 ADR-0017）│
│      预算内（retry_count < max_retries，默认 3）→ route=REPLAY、计数 +1     │
│      超限 → route=CONTINUE + 贴 partition_retry_exhausted（受控分支、       │
│              非异常降级）                                                   │
│    writes: argument_tree, hitl1_route, partition_retry_count[, errors]     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  │  hitl1 出条件边（ADR-0017）：route=CONTINUE → 默认下游（hypothesis_propose）；
  │  route=REPLAY → 回 parse+partition（受控打回、有界）
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ③ hypothesis_propose          【单次·逐 argument 调 LLM·有 _guarded】       │
│    for argument in argument_tree (EVIDENCE/SUB):                            │
│      proposals = llm.propose(argument, paragraph_summary)                   │
│      → 铸 Hypothesis(status=pending)（hypothesis_id 幂等派生）                │
│    失败降级 → 该节点无假设（空列表向前、不卡死）                             │
│    writes: hypotheses（dict[str, list[Hypothesis]]，partial channel）     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ④ retrieval           【单次·伪代码桩·无 LLM·有 _guarded·真实后端 Out of Scope】│
│    读 argument_tree + hypotheses + query_time_range + session_context      │
│    当前桩不联网、产空 citations（infra.retrieval 接口层不变）                 │
│    失败降级 → 空 citations 向前                                              │
│    writes: citations（dict[str, list[Source]]，partial channel）           │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑤ judgment             【单次·有 _guarded·有 LLM·五合一·ADR-0017】          │
│    reads: argument_tree + hypotheses + citations + session_context + qtr   │
│    result = llm.judge(...)  → per-argument / per-hypothesis 终态裁决         │
│    局部 argument_credibility（verdict→ArgumentStatus）+ 终态化 hypotheses   │
│      （pending→supported/doubtful/refuted）                                  │
│    按序串联纯函数（逻辑不动）：                                                │
│      merge_with_partials（字段级合流 + 12 格矩阵裁决）→ impact → consistency   │
│    整树写回 argument_tree（单写者，故裁撤 argument_credibility partial）      │
│    失败降级 → 覆盖范围内未判决节点置 error + 贴 orchestrator_error:judgment  │
│    writes: argument_tree（整树）, hypotheses（终态化）                       │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑥ rewrite_loop       【单次·逐段调 LLM·有 _guarded·ADR-0017·Slice 6】       │
│    reads: argument_tree + citations + paragraph_list +                      │
│           session_context + qtr                                              │
│    触达判定（逐段）：段内有 supported 假说 / 命中 citations → 触达              │
│    for 触达段: text = llm.propose_rewrite(...)                               │
│      非空 → 入 proposed_rewrites；None/空 → 省略；抛错 → 省略 + 记 errors     │
│    失败降级（whole-node 异常） → 空 proposed_rewrites + 日志向前                │
│    ⚠ 不碰 argument_tree（按段/文本工作、与 argument 状态解耦）                 │
│    writes: proposed_rewrites（dict[str,str]，partial channel）[, errors]     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑦ hitl2              【单次·同步硬闸门·不可跳过（ADR-0010）·有 _guarded】    │
│    reads: original_paragraphs + proposed_rewrites                            │
│    review = build_review(...)  → 被触达段原文 × 提议重写呈现                 │
│    decision = gate.review(review)                                            │
│    ConservativeHitl2Gate：无待决 → PASS / 有待决 → DECIDE+空 ops（全驳回）   │
│    DECIDE: resolve_rewrites（confirm→提议文本 / edit→编辑文本 / reject→省略） │
│    assemble_final_document → 按规范顺序拼 final_document                      │
│    ⚠ Hitl2GateError 原样上抛、不兜底（正确性硬停，与其它 stage 降级语义不同）  │
│    其余异常兜底 → 回退原文 bytes 拼接（保护原文底线）                         │
│    writes: final_document                                                    │
│    崩溃恢复（离图入口，非 HITL resume）：                                     │
│      Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)     │
│      （按段文本幂等重推导，复用 assemble_final_document）                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
END

旁路通道：
  errors（reducer _append_errors） ← 各 _guarded 降级时 _log_error_patch 追加
                                      "[stage] ExcType: msg"，汇总到 RunResult.errors

图例：
  【单次】= 图层级节点一次调用返回一个 patch（无图层级循环）。
  judgment 内部按序串联 merge/impact/consistency 三个纯函数（对 LangGraph 不可见的内部步骤）。
  _guarded 覆盖 7 个 stage（除 partition 字节自检外都兜底）；Hitl2GateError 不兜底。
```

核心承诺：**无任何采纳改动时，终稿与原始输入逐字节完全一致**（tracer bullet 承诺，贯穿全部 stage）。

两层 seam（区分清楚）：

- **adapter seam（实现可换）**：每个智能体是一个 `Protocol` 契约 + 一组可注入的 `Agents` dataclass 字段。
  桩与真实实现满足同一契约，逐个替换（`create_stub_agents` → `create_real_agents`）。
- **拓扑 seam（参与可换）**：流水线由数据驱动的 `StageSpec` 序列描述（`default_pipeline()`）。
  传入另一种 spec 即另一种拓扑（如省略 `hypothesis_propose` / `judgment`）——拓扑的「第二种 adapter」使该 seam 为真 seam。

## 2. 模块地图

源码在 `src/`（扁平布局，ADR-0014：`package-dir={""="src"}`、`mypy_path=src` 解析顶层裸名 `domain`/`agents`/`infra`/`runtime`）。

### 领域核心（`src/` 根，共享不变语言；所有 agent 依赖、不反向依赖 agent）

| 模块 | 职责 |
|---|---|
| `domain.py` | `Argument` / `ArgumentType` / `ArgumentStatus` / `Hypothesis` / `HypothesisStatus` / `MergeAction` / `MergeDecision` / `ParagraphRecord`。节点 / 段落聚合根形状为决策、非最终代码；字段由各 agent 分阶段补全。 |
| `original_paragraphs.py` | `OriginalParagraphs`：只读、确定性、字节级无损的原文段落表（ADR-0005/0009）。 |
| `partition.py` | 纯代码段落切分（零 LLM，ADR-0009）+ 分区不变式自检。 |
| `tree_invariants.py` | 论证树结构不变式（`validate_tree` / `rebuild_children`，ADR-0001）。 |
| `status_machine.py` | 集中状态机：`validate_transition` / `transition_argument` / `mark_argument_error` + `ALLOWED_TRANSITIONS`（ADR-0011）。 |

### 公共契约与 provider adapter（`src/infra/`）

| 模块 | 职责 |
|---|---|
| `infra/retrieval.py` | `RetrievalLayer` Protocol + Mock + 合规校验（白名单 / 权限 / 模板在接口层强制）。retrieval 节点消费（真实后端 Out of Scope，当前桩产空 citations）。 |
| `infra/llm_provider.py` | `build_qwen_chat_model()`：`ChatOpenAI` 指向 DashScope 端点；API key 只读 `DASHSCOPE_API_KEY`。 |
| `infra/llm_adapters.py` | `QwenParseLlmClient` / `QwenHypothesisLlmClient` / `QwenJudgmentLlmClient` / `QwenRewriteLlmClient`：经 `with_structured_output` 绑各 seam 契约（扁平 schema、无判别联合）。 |

> Slice 5 删除了原 ReAct 独占 infra：`infra/retrieval_tool.py` / `infra/tool_protocol.py` / `infra/history.py`（RetrievalTool / ToolRegistry / HistoryStore）——五合一后 judgment 吃预取 citations、不再有 ReAct 循环与步间历史。

### 智能体（`src/agents/`）

每个 **seam agent** 是子包 `agents/<name>/{contract,agent}.py`（ADR-0014）：`contract.py` 放 Protocol + Fake 桩 + 结构化 I/O 模型，`agent.py` 放纯函数，`__init__` re-export 保 `from agents.<name> import ...` 路径不变。
**纯函数 agent** 为 `agents/<name>.py` 扁平单文件（无 seam、确定性、桩 = 真实）。
每个 agent 都是**纯函数 + 注入 seam + provider-free Fake**：可独立 import、独立调用、独立单测（见 `docs/adding-an-agent.md`）。

| 模块 | 接口 | seam | 覆盖节点 |
|---|---|---|---|
| `agents/parser/{contract,agent}.py` | `parse(original_paragraphs, llm) -> ParseOutput` | `LlmClient` / `FakeLlmClient` | 全段（唯一读段落文本的环节） |
| `agents/hitl1/{contract,agent}.py` | `confirm_partition(argument_tree, retry_count, *, gate, max_retries) -> Hitl1Outcome`（partition 闸门 + 有界打回·ADR-0017） | `Hitl1Gate` / `FakeHitl1Gate` | partition 确认（确认继续 / 打回重跑） |
| `agents/hypothesis/{contract,agent}.py` | `propose_hypotheses(argument_tree, paragraph_list, llm) -> dict[str, list[Hypothesis]]`（仅 propose、产 pending） | `HypothesisLlmClient` / `FakeHypothesisLlmClient` | evidence / sub_claim（不读检索，ADR-0002） |
| `agents/judgment/{contract,agent}.py` | `judge_and_adjudicate(argument_tree, hypotheses, citations, sc, qtr, llm) -> JudgmentOutcome`（五合一：取证 + merge/impact/consistency 串联·ADR-0017） | `JudgmentLlmClient` / `FakeJudgmentLlmClient` | main_claim / sub_claim / evidence |
| `agents/rewrite_loop/{contract,agent}.py` | `propose_rewrites(argument_tree, citations, paragraph_list, sc, qtr, llm) -> RewriteLoopOutcome`（逐段提议重写·ADR-0017·Slice 6） | `RewriteLlmClient` / `FakeRewriteLlmClient` | 被触达段（supported 假说 / 命中 citations） |
| `agents/hitl2/{contract,agent}.py` | `confirm(original_paragraphs, proposed_rewrites, gate) -> Hitl2Confirmation` / `build_review(...)` / `assemble_final_document(...)` / `resolve_rewrites(...)` | `Hitl2Gate` / `FakeHitl2Gate` / `ConservativeHitl2Gate` | 被触达段（终稿文本确认硬闸门，ADR-0010/0017） |
| `agents/merge.py` | `merge` / `merge_with_partials` / `apply_partial_updates` | 无（确定性 12 格矩阵纯函数·judgment 串联调用） | 全节点标注 |
| `agents/impact.py` | `impact(argument_tree) -> list[Argument]` | 无（串行·不产文本·剩余支撑率纯函数·judgment 串联调用） | 上层论点 invalid/weakening |
| `agents/consistency.py` | `consistency(argument_tree) -> list[Argument]` | 无（单次扫描·只贴 `issue_tags`·judgment 串联调用） | 段落级 / 全局一致性 |

> **重构方向节点增删**（ADR-0017）：Slice 1–6 全部落地（parse+partition 合并、hitl1 重定义、hypothesis_propose、retrieval 桩、judgment 五合一 + 删除 verification ReAct 模块与独占 infra、rewrite_loop 新增、hitl2 重定位为终稿闸门、writeback 裁撤）。
> 装配仍由 `MANIFEST` 驱动（§4），新增 / 合并节点各加 / 改一条 `AgentEntry`，`default_pipeline()` 自动派生新拓扑，不改 `runtime/orchestrator.py` 图装配。

### 装配与调度（`src/agents/assembly.py` + `src/runtime/`）

| 模块 | 职责 |
|---|---|
| `agents/assembly.py` | `Agents` dataclass（7 契约）+ `MANIFEST`（单一 manifest：每 stage 的 `stub`/`real`/`deps`/`build`）+ `create_stub_agents` / `create_real_agents` + `_guarded` 兜底 + 各 `_X_node` build 闭包。 |
| `runtime/orchestrator.py` | `Orchestrator` 调度中枢 + `StageSpec` / `default_pipeline`（遍历 `MANIFEST` 派生）+ `PipelineState` / `RunResult` / `NodeFn` / `merge_argument_tree` reducer + `resume_rewrite`。 |
| `runtime/cli_gates.py` | `CliHitl1Gate` / `CliHitl2Gate`：交互式同步闸门（终端 `input()`），非 tty 退化为保守决策。 |
| `runtime/run_real.py` | `run_real_pipeline(original_doc)` + `python -m runtime.run_real` 入口。 |

## 3. seam 一览

| seam | 类型 | 两个 adapter（→ 真 seam） | 位置 |
|---|---|---|---|
| LLM 解析 | Protocol | `FakeLlmClient` ↔ `QwenParseLlmClient` | `agents/parser/contract.py`（真 adapter `infra/llm_adapters.py`） |
| LLM 开药 | Protocol | `FakeHypothesisLlmClient` ↔ `QwenHypothesisLlmClient` | `agents/hypothesis/contract.py`（真 adapter `infra/llm_adapters.py`） |
| LLM 裁决 | Protocol | `FakeJudgmentLlmClient` ↔ `QwenJudgmentLlmClient` | `agents/judgment/contract.py`（真 adapter `infra/llm_adapters.py`） |
| LLM 重写提议 | Protocol | `FakeRewriteLlmClient` ↔ `QwenRewriteLlmClient` | `agents/rewrite_loop/contract.py`（真 adapter `infra/llm_adapters.py`） |
| HITL-1 闸门 | Protocol | `FakeHitl1Gate` ↔ `CliHitl1Gate` | `agents/hitl1/contract.py`（真 adapter `runtime/cli_gates.py`） |
| HITL-2 闸门 | Protocol | `FakeHitl2Gate` / `ConservativeHitl2Gate` ↔ `CliHitl2Gate` | `agents/hitl2/contract.py`（真 adapter `runtime/cli_gates.py`） |
| 检索层 | Protocol | `create_mock_retrieval_layer` ↔ 真实后端 | `infra/retrieval.py` |
| agent 实现 | dataclass 字段 | stub ↔ real（`MANIFEST` 驱动 `dataclasses.replace` 逐条换） | `agents/assembly.py: Agents` |
| **拓扑** | `StageSpec` 序列 | `default_pipeline()`（遍历 `MANIFEST` 派生）↔ 自定义 spec（省略 stage） | `runtime/orchestrator.py: default_pipeline` |

每个 seam 都有「第二个 adapter」——按 deep-module 原则（two adapters means a real seam）均为真 seam，无假 seam。

## 4. 装配：从桩到真实（manifest 驱动）

`MANIFEST`（`agents/assembly.py`）是单一装配真相源：每条 `AgentEntry` 含 `name`/`field`/`stub`/`real`/`deps`/`build`。
它同时驱动两件事——typed `Agents` dataclass 构造与 `default_pipeline()` 拓扑（`StageSpec(name, build, deps)` per entry）。

- `create_stub_agents()`：遍历 `MANIFEST`，按 `field` 把 `stub` 装入 `Agents`。返回全套桩——tracer bullet 端到端回路（无采纳改动 → 终稿逐字节等于原文）。
- `create_real_agents(...)`：在桩基础上遍历 `MANIFEST`，对有 `real` 工厂的条目调 `real(RealDeps)`，返回非 `None` 者替换对应 `field`（`dataclasses.replace`）。纯函数 agent 与 retrieval 的 `real=None`，不替换（桩 = 真实 / 真实后端 Out of Scope）。

条件替换矩阵（`real` 工厂返回 `None` 即保留桩）：

| 入参 | 替换 |
|---|---|
| `llm`（必填） | 解析桩 → 真实解析 |
| `hitl1_gate`（必填） | HITL-1 桩 → 真实闸门 |
| `hypothesis_llm`（可选） | 开药桩 → 真实「投机生成」（产 pending 假说） |
| `judgment_llm`（可选） | 裁决桩 → 真实裁判（吃 citations 判终态 + 串联 merge/impact/consistency） |
| `rewrite_llm`（可选） | 重写桩 → 真实逐段提议重写（产 `proposed_rewrites`） |
| `hitl2_gate`（可选，缺省 `ConservativeHitl2Gate`） | HITL-2 桩 → 真实 confirm（终稿文本确认） |

merge / impact / consistency 均为确定性纯函数、无 LLM / 检索依赖——桩路径与真实装配共用同一实现（由 judgment 串联调用），故字节级承诺在任一替换组合下都成立。
`real` 工厂用 `functools.partial` 绑定依赖（恢复 IDE 跳转与签名可见性，比 lambda 闭包可读）。
每条 `AgentEntry` 的 `stub`/`real`/`build`/`deps` 四字段逐项含义与新增 agent 的三触点见 `docs/adding-an-agent.md`。

## 5. 异常兜底与单向流控（issue #11 · PRD §13）

每个下游 stage 经 `_guarded(stage, body, fallback)` 统一兜底：

- `body()` 正常返回 patch；
- `Hitl2GateError`（硬闸门正确性硬停，ADR-0010）**原样上抛、不兜底**——绝不无人拍板自动采纳；
- 其余异常 → `fallback()` 降级 patch + `_log_error_patch` 日志、单向向前推进。

各 stage 只声明「正常返回」与「降级 patch」两件本质之事，样板收口于一处（locality）。
逐 stage 降级语义见 §1 大框每 stage 的 `失败降级 →` 标注（图细节，不再于此重复表）。

终稿拼装幂等续跑入口：`Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)`（崩溃恢复，复用 `assemble_final_document` 纯函数按段文本幂等再推导）。

## 6. judgment 的 partial 合流（ADR-0017·Slice 5 五合一）

Slice 5 后检索之后的五节点（verification ReAct 取证 / hypothesis 取证 / merge / impact / consistency）并入单一 `judgment` 节点。
hypothesis_propose 写 `hypotheses` partial channel（候选假设列表、`status=pending`）、retrieval 写 `citations` partial channel（`list[Source]`）。
judgment 读两 partial + `argument_tree`，经 `llm.judge(...)` 取 per-argument / per-hypothesis 终态后：

- 构造局部 `argument_credibility`（verdict → `ArgumentStatus`）+ 终态化 `hypotheses`（`pending`→终态）；
- 按序串联纯函数 `merge_with_partials`（字段级合流 + 12 格矩阵裁决）→ `impact` → `consistency`；
- **整树写回** `argument_tree`（单写者）。

因 judgment 是检索之后唯一的整树写者、直接把终态写回树，故裁撤原 `argument_credibility` partial channel（ADR-0017 终局）——避免多线路整节点 upsert 互相覆盖丢字段（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）。
`argument_tree` channel 的 `merge_argument_tree` reducer 负责整树 upsert（首见顺序、同 id 覆盖）。

## 7. 状态机集中化（ADR-0011）

`status_machine.py` 是唯一的状态迁移裁判：`ALLOWED_TRANSITIONS` 表 + `validate_transition`。
HITL-2 采纳（`_apply_adopt`）与 orchestrator 兜底（`mark_argument_error`）都经此一处，杜绝规则漂移。
非法迁移一律拦截（`IllegalStatusTransitionError` / `Hitl2GateError`）。

## 8. 扩展点

### 8.1 接入真实 LLM provider

四条 LLM seam（解析 / 开药 / 裁决 / 重写提议）的第二 adapter 已落地——provider 工厂 + 四个 adapter + CLI 闸门 + 入口：

| 模块 | 角色 |
|---|---|
| `infra/llm_provider.py` | `build_qwen_chat_model()`：把 `ChatOpenAI` 指向 DashScope OpenAI-compatible 端点（`base_url` + `qwen-max`）。API key **只**读环境变量 `DASHSCOPE_API_KEY`，绝不硬编码。 |
| `infra/llm_adapters.py` | `QwenParseLlmClient` / `QwenHypothesisLlmClient` / `QwenJudgmentLlmClient` / `QwenRewriteLlmClient`：经 `with_structured_output(<schema>)` 满足各 seam 契约（dev-guide §6.3）。四条 seam 的 contract schema 均为扁平 BaseModel（`ParseResult` / `_ProposalsEnvelope` / `JudgmentResult` / `_RewriteEnvelope`、无判别联合 `oneOf`）；hypothesis / judgment / rewrite 直接绑 contract schema，**parse 拆两阶段**——绑内部信封 `_ParseTreeEnvelope`（proposals-only）+ `_SummariesEnvelope`（按 8 段分块的 `ParagraphSummary`），再折成 `ParseResult`（P-01：单绑定下大论文摘要被系统性少填）；结构化链**懒构建**，构造期不触碰 provider 特性。 |
| `runtime/cli_gates.py` | `CliHitl1Gate` / `CliHitl2Gate`：交互式同步闸门（终端 `input()` 收决策）；非 tty 退化为保守决策（HITL-1 `SKIP`、HITL-2 有待决则全驳回、原文逐字节保留），守住「绝不替人拍板自动采纳」。 |
| `runtime/run_real.py` | `run_real_pipeline(original_doc)` 组装上述全部 + `Orchestrator`；`python -m runtime.run_real [input] [-o output]`。retrieval 节点仍为桩（产空 citations），故 judgment 经 FakeJudgmentLlmClient 默认空裁决 → 全 KEEP → rewrite_loop 无触达段 → 终稿逐字节等于原文。 |

检索当前用 `create_mock_retrieval_layer()`（合规、确定、假素材）——真实检索后端待接（白名单/权限/模板在 `infra/retrieval.py` 接口层强制）；接入后 citations 非空、judgment 据之判终态，拓扑不动。

API key 配置：复制 `.env.example` 为 `.env`（已被 `.gitignore` 忽略、不会提交）并填入 `DASHSCOPE_API_KEY`；`build_qwen_chat_model()` 启动时自动从 cwd 下 `.env` 加载（无依赖极简加载器，亦可改用环境变量）。`.env` 可选 `DASHSCOPE_MODEL` 覆盖默认 `qwen-max`。

最小跑法：

```bash
cp .env.example .env          # 填入 DASHSCOPE_API_KEY
python -m runtime.run_real input.txt -o final.md
```

注入其它 `BaseChatModel` 即指向别的网关；实现自定义 `Protocol`（如 `LlmClient`）用 `with_structured_output(Schema)` 保证结构合法，注入 `create_real_agents(...)` 即可，核心管线不变。

真实联网冒烟（需 key + 网络 + token，默认 skip）：`pytest -rsv tests/test_real_llm_wiring.py -k dashscope_smoke`。

### 8.2 自定义拓扑

`default_pipeline()` 返回 `tuple[StageSpec, ...]`。
传入 `Orchestrator(spec=...)` 即可表达不同拓扑——例如省略 `judgment`：删除该 stage 并把 `hitl2` 的 `deps` 改为 `("retrieval",)`；或省略 `hypothesis_propose`：删除该 stage 并把 `retrieval` 的 `deps` 改为 `("hitl1",)`。
约束：`deps` 引用的 stage 必须在 spec 中存在（否则 LangGraph 报错）；被省略 stage 的下游须相应重连 `deps`。

### 8.3 新增 agent

manifest 驱动下触点为 **3**（ADR-0014，原 7 触点收口于 `MANIFEST`）：

1. **新子包** `agents/<name>/{contract,agent,__init__}.py`（seam agent）或扁平 `agents/<name>.py`（纯函数 agent）：`Protocol` seam + provider-free `Fake*` + 纯函数主逻辑（`argument_tree -> patch`，`model_copy` 不改输入）。
2. **`agents/assembly.py`**：加 `Agents` 字段（typed）+ 一条 `MANIFEST` 条目（`stub` / `real` 工厂 / `deps` / `build` 闭包，后者用 `_guarded` 兜底）。
3. **测试**：`tests/test_<name>.py`（纯函数单测）+ `test_orchestrator_e2e.py` 加集成断言。

`default_pipeline()` 自动纳入新 stage（遍历 `MANIFEST`），无需改 `runtime/orchestrator.py`。
完整走读（judgment 真例 + 可填空骨架模板 + 两层调测）见 **`docs/adding-an-agent.md`**。

## 9. 质量门

```bash
ruff check src tests          # E/F/I/UP/B，line-length 99，不强制 ruff format
mypy --strict src             # 扁平 src 布局（ADR-0014），mypy_path=src 解析顶层裸名
pytest -q                     # Slice 6 后基线（删 writeback 测试 + 加 rewrite_loop/hitl2 测试）
```

不强制 `ruff format`——不重排既有文件（既有缩进风格保持）。
lint / 类型 / 测试失败一律修，即使非本次改动引入（见 `CLAUDE.md`）。

## 10. 关键约束速查

- **一段一节点**：`paragraph_id` 单数，不跨段（ADR-0001）。
- **段落原文永不被 LLM 改写**：段落原文 `original_content`（存于 `ParagraphRecord`，ADR-0025）只来自只读字节表解码、`Argument` 不再存原句字段（by construction）。Slice 6 的 rewrite_loop 对**被触达段**产**提议重写文本**（写入 `proposed_rewrites`、不回写 `ParagraphRecord.original_content`），最终是否落地由 hitl2 人确认——这是 ADR-0017 对「终稿逐字节一致 / 段落原文不被 LLM 改写」承诺的受控放宽（仅被触达段、需人拍板；未触达段仍逐字节忠实）。
- **绝不替人拍板**：judgment（含 merge/impact/consistency）不置 `adopted`、不改 `content`。Slice 6 后 `adopted`/`corrected`/`adopted_hypothesis_id` 在新流程不再被写（domain 字段保留不删）；终稿文本确认只经 hitl2 逐段确认 / 编辑 / 驳回 `proposed_rewrites`。
- **HITL-2 不可跳过**：硬闸门，无待决→一键通过、有待决→绝不可 PASS（ADR-0010）。
- **单向流控**：异常即记日志、就地降级、继续向前；无复杂分布式重试（PRD §13）。唯 `hitl1` 有**有界打回**（条件边回 `parse+partition`，max retries 默认 3；超限改向前 + 贴 `partition_retry_exhausted`，受控分支、非异常降级，ADR-0017）——其余 stage 绝不打回。
- **幂等终稿拼装**：`assemble_final_document` 从只读原文表 + `resolved_rewrites` 按规范顺序重推导 `final_document`，重跑得同一份 bytes（ADR-0017）；崩溃恢复续跑入口 `Orchestrator.resume_rewrite`。
