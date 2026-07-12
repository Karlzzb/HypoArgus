# 开发文档（DEVELOPMENT）

HypoArgus 是论证驱动型文档修订多智能体系统：纯文本 → 段落切分 → 论证树 → 双线路（体检 ∥ 开药）→ 合并 → 影响传导 → 一致性校验 → HITL-2 确认 → 段落原子回写 → 终稿。
本文面向贡献者，描述模块边界、seam、装配与扩展点；术语见 `CONTEXT.md`，架构决策见 `docs/adr/`，完整需求见 `prd_v2.0.md`。
状态树字段流向（主/子智能体 state、字段来源、LLM seam 输入形式）见 `docs/STATE.md`。

## 1. 架构总览

整条流水线是一张 LangGraph `StateGraph`，控制流落边（代码）而非 prompt 散文。
数据在 stage 间经 **state channel** 路由，**无跨模块直接调用**；流水线严格单向、绝不打回。

速记（拓扑骨架）：

```
START → partition → parse → hitl1 → (verification ∥ hypothesis) → merge
      → impact → consistency → hitl2 → writeback → END
```

详细控制流（标注每个 stage 是 **单次** 还是 **循环**，以及 channel 路由、兜底边界）：

```
START
  │  original_doc: bytes（原始输入，单次写入）
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ① partition                                          【单次·纯代码·无 LLM】 │
│    段落切分 + 字节级自检 assert（失败即正确性 bug → 硬停；不包 _guarded）    │
│    writes: original_paragraphs(OriginalParagraphs 只读表), argument_tree=[] │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ② parse                                        【单次·有 _guarded·有 LLM】 │
│    LlmClient.parse(original_paragraphs)（唯一读段落文本的环节）              │
│    失败降级 → argument_tree=[]（空树向前）                                    │
│    writes: argument_tree（merge_argument_tree reducer，整节点 upsert）        │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ③ hitl1                          【单次·同步闸门，非 interrupt·有 _guarded】 │
│    gate.review(argument_tree) → SKIP / ACCEPT / EDIT                        │
│    EDIT 在深拷贝上逐 op 跑 validate_tree                                    │
│    writes: argument_tree                                                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ├───────────────────────────┐  ← hitl1 出两条并行边（固定，非动态 fan-out）
  ▼                            ▼
┌─────────────────────────┐  ┌─────────────────────────┐
│ ④ verification（体检）   │  │ ⑤ hypothesis（开药）     │   并行双线路 ∥
│ 图层级 = 单次节点        │  │ 图层级 = 单次节点         │   互不见对方写入
│ ── 内部 ReAct 循环 ──    │  │ ── 内部循环 ──           │
│ for argument in argument_tree: │  │ for argument in argument_tree: │
│   (MAIN/SUB/EVIDENCE)    │  │   (EVIDENCE/SUB)          │
│   for _ in range(8):  ◀──有界循环   proposals = propose()  ◀──单次生成
│     step = next_step     │  │   for p in proposals:     │  ◀──有界 ReAct 取证
│     Conclude → 判决返回  │  │     for _ in range(8):    │
│     Search  → retrieve   │  │       step = next_verify_step
│             → observation│  │       Conclude → 判决      │
│ 退出：判决/异常/         │  │       Search  → retrieve    │
│       max_iter 耗尽 → ERROR  │             → observation │
│                          │  │ 退出：判决/异常/          │
│                          │  │       max_iter 耗尽 → DOUBTFUL│
│ writes:                  │  │ writes:                   │
│   argument_credibility   │  │   hypotheses              │
│   (dict[str,ArgumentStat])│  │   (dict[str,list[Hypoth]])│
└─────────────────────────┘  └─────────────────────────┘
                 │                        │
                 └───────────┬────────────┘  ← join（LangGraph 等齐两条分支）
                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑥ merge                             【单次·无 LLM·纯函数·有 _guarded】      │
│    reads: argument_tree + argument_credibility + hypotheses                │
│    apply_partial_updates（字段级合流：status ∥ candidate_hypotheses）       │
│    → merge_fn（12 格矩阵裁决；绝不置 adopted、不改 content/status）          │
│    writes: argument_tree                                                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑦ impact                       【单次·串行·无 LLM·纯函数·有 _guarded】      │
│    上层论点 invalid / weakening（剩余支撑率）                                │
│    writes: argument_tree                                                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑧ consistency                 【单次扫描·无 LLM·只贴 issue_tags·有 _guarded】│
│    writes: argument_tree                                                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑨ hitl2              【单次·同步硬闸门·不可跳过（ADR-0010）·有 _guarded】    │
│    gate.review(review) → Hitl2Decision                                      │
│    ConservativeHitl2Gate：无待决 → PASS / 有待决 → 拒（绝不替人拍板）        │
│    ⚠ Hitl2GateError 原样上抛、不兜底（正确性硬停，与其它 stage 降级语义不同）  │
│    adopted / corrected 只由此处决定                                          │
│    writes: argument_tree                                                     │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑩ writeback                【单次·幂等纯函数·有 _guarded】                   │
│    从原始 bytes 重新推导整篇终稿（supplement 永不累积，ADR-0011）            │
│    失败降级 → 回退原文 bytes（保护原文底线）；adopted 节点不动、待续跑      │
│    writes: final_document                                                    │
│    崩溃恢复（离图入口，非 HITL resume）：Orchestrator.resume_writeback(...)  │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
END

旁路通道：
  errors（reducer _append_errors） ← 各 _guarded 降级时 _log_error_patch 追加
                                      "[stage] ExcType: msg"，汇总到 RunResult.errors

图例：
  【单次】= 图层级节点一次调用返回一个 patch（无图层级循环）。
  【循环】= 循环藏在节点函数内部，对 LangGraph 不可见；verification/hypothesis 在图层级
           仍是单次节点，但其内部对树中每个节点（hypothesis 还对每条 proposal）跑有界 ReAct。
  max_iterations 默认 8（assembly RealDeps.max_iterations）。
  _guarded 覆盖 10 个 stage 中的 9 个（除 partition）；Hitl2GateError 不兜底。
```

核心承诺：**无任何采纳改动时，终稿与原始输入逐字节完全一致**（tracer bullet 承诺，贯穿全部 stage）。

两层 seam（区分清楚）：

- **adapter seam（实现可换）**：每个智能体是一个 `Protocol` 契约 + 一组可注入的 `Agents` dataclass 字段。
  桩与真实实现满足同一契约，逐个替换（`create_stub_agents` → `create_real_agents`）。
- **拓扑 seam（参与可换）**：流水线由数据驱动的 `StageSpec` 序列描述（`default_pipeline()`）。
  传入另一种 spec 即另一种拓扑（如省略 `hypothesis` / `consistency`）——拓扑的「第二种 adapter」使该 seam 为真 seam。

## 2. 模块地图

源码在 `src/`（扁平布局，ADR-0014：`package-dir={""="src"}`、`mypy_path=src` 解析顶层裸名 `domain`/`agents`/`infra`/`runtime`），共 37 个模块、约 5700 行。

### 领域核心（`src/` 根，共享不变语言；所有 agent 依赖、不反向依赖 agent）

| 模块 | 职责 |
|---|---|
| `domain.py` | `Argument` / `ArgumentType` / `ArgumentStatus` / `Hypothesis` / `MergeAction` / `MergeDecision`。节点形状为决策、非最终代码；字段由各 agent 分阶段补全。 |
| `original_paragraphs.py` | `OriginalParagraphs`：只读、确定性、字节级无损的原文段落表（ADR-0005/0009）。 |
| `partition.py` | 纯代码段落切分（零 LLM，ADR-0009）+ 分区不变式自检。 |
| `tree_invariants.py` | 论证树结构不变式（`validate_tree` / `rebuild_children`，ADR-0001）。 |
| `status_machine.py` | 集中状态机：`validate_transition` / `transition_argument` / `mark_argument_error` + `ALLOWED_TRANSITIONS`（ADR-0011）。 |

### 公共契约与 provider adapter（`src/infra/`）

| 模块 | 职责 |
|---|---|
| `retrieval.py` | `RetrievalLayer` Protocol + Mock + 合规校验（白名单 / 权限 / 模板在接口层强制）。体检 / 开药共用。 |
| `retrieval_tool.py` | `RetrievalTool` 包 `RetrievalLayer`，独占 `SearchStep → RetrievalRequest` 翻译（ADR-0015）。 |
| `tool_protocol.py` | 工具框架 seam 骨架（`ToolRegistry` / `BaseTool` / `ToolResult`，ADR-0015）。 |
| `history.py` | `HistoryStore`：ReAct 步间检索观察记忆 + 压缩（ADR-0016）。 |
| `llm_provider.py` | `build_qwen_chat_model()`：`ChatOpenAI` 指向 DashScope 端点；API key 只读 `DASHSCOPE_API_KEY`。 |
| `llm_adapters.py` | `QwenParseLlmClient` / `QwenVerifyLlmClient` / `QwenHypothesisLlmClient`：经 `with_structured_output` 绑各 seam 契约。 |

### 智能体（`src/agents/`）

每个 **seam agent** 是子包 `agents/<name>/{contract,agent}.py`（ADR-0014）：`contract.py` 放 Protocol + Fake 桩 + 结构化 I/O 模型，`agent.py` 放纯函数，`__init__` re-export 保 `from agents.<name> import ...` 路径不变。
**纯函数 agent** 为扁平单文件（无 seam、确定性、桩 = 真实）。
每个 agent 都是**纯函数 + 注入 seam + provider-free Fake**：可独立 import、独立调用、独立单测（见 `docs/adding-an-agent.md`）。

| 模块 | 接口 | seam | 覆盖节点 |
|---|---|---|---|
| `parser/{contract,agent}.py` | `parse(original_paragraphs, llm) -> list[Argument]` | `LlmClient` / `FakeLlmClient` | 全段（唯一读段落文本的环节） |
| `hitl1/{contract,agent}.py` | `confirm(argument_tree, gate) -> list[Argument]` | `Hitl1Gate` / `FakeHitl1Gate` | 结构编辑（可跳过） |
| `verification/{contract,agent}.py` | `verify(argument_tree, llm, retrieval, *, max_iterations) -> dict[str, ArgumentStatus]` | `VerifyLlmClient` / `FakeVerifyLlmClient` | main_claim / sub_claim / evidence |
| `hypothesis/{contract,agent}.py` | `hypothesize(argument_tree, llm, retrieval, *, max_iterations) -> dict[str, list[Hypothesis]]` | `HypothesisLlmClient` / `FakeHypothesisLlmClient` | evidence / sub_claim（不读体检结论，ADR-0002） |
| `hitl2/{contract,agent}.py` | `confirm(argument_tree, original_paragraphs, gate)` / `build_review(...)` | `Hitl2Gate` / `FakeHitl2Gate` / `ConservativeHitl2Gate` | 待决节点（不可跳过硬闸门，ADR-0010） |
| `merge.py` | `merge` / `merge_with_partials` / `apply_partial_updates` | 无（确定性 12 格矩阵纯函数） | 全节点标注 |
| `impact.py` | `impact(argument_tree) -> list[Argument]` | 无（串行·不产文本·剩余支撑率纯函数） | 上层论点 invalid/weakening |
| `consistency.py` | `consistency(argument_tree) -> list[Argument]` | 无（单次扫描·只贴 `issue_tags`） | 段落级 / 全局一致性 |
| `writeback.py` | `writeback(argument_tree, original_paragraphs) -> WritebackResult` | 无（段落原子缝合·幂等纯函数） | 被采纳节点 |

### 装配与调度（`src/agents/assembly.py` + `src/runtime/`）

| 模块 | 职责 |
|---|---|
| `agents/assembly.py` | `Agents` dataclass（9 契约）+ `MANIFEST`（单一 manifest：每 stage 的 `stub`/`real`/`deps`/`build`）+ `create_stub_agents` / `create_real_agents` + `_guarded` 兜底 + 各 `_X_node` build 闭包。 |
| `runtime/orchestrator.py` | `Orchestrator` 调度中枢 + `StageSpec` / `default_pipeline`（遍历 `MANIFEST` 派生）+ `PipelineState` / `RunResult` / `NodeFn` / `merge_argument_tree` reducer + `resume_writeback`。 |
| `runtime/cli_gates.py` | `CliHitl1Gate` / `CliHitl2Gate`：交互式同步闸门（终端 `input()`），非 tty 退化为保守决策。 |
| `runtime/run_real.py` | `run_real_pipeline(original_doc)` + `python -m runtime.run_real` 入口。 |

## 3. seam 一览

| seam | 类型 | 两个 adapter（→ 真 seam） | 位置 |
|---|---|---|---|
| LLM 解析 | Protocol | `FakeLlmClient` ↔ `QwenParseLlmClient` | `agents/parser/contract.py`（真 adapter `infra/llm_adapters.py`） |
| LLM 体检 | Protocol | `FakeVerifyLlmClient` ↔ `QwenVerifyLlmClient` | `agents/verification/contract.py`（真 adapter `infra/llm_adapters.py`） |
| LLM 开药 | Protocol | `FakeHypothesisLlmClient` ↔ `QwenHypothesisLlmClient` | `agents/hypothesis/contract.py`（真 adapter `infra/llm_adapters.py`） |
| HITL-1 闸门 | Protocol | `FakeHitl1Gate` ↔ `CliHitl1Gate` | `agents/hitl1/contract.py`（真 adapter `runtime/cli_gates.py`） |
| HITL-2 闸门 | Protocol | `FakeHitl2Gate` / `ConservativeHitl2Gate` ↔ `CliHitl2Gate` | `agents/hitl2/contract.py`（真 adapter `runtime/cli_gates.py`） |
| 检索层 | Protocol | `create_mock_retrieval_layer` ↔ 真实后端 | `infra/retrieval.py` |
| 工具框架 | 骨架 | `ToolRegistry` + `RetrievalTool` ↔ 未来 `BaseTool` 实现 | `infra/tool_protocol.py` / `infra/retrieval_tool.py` |
| 历史 seam | Protocol | `HistoryStore` ↔ 未来消息轮次融合 | `infra/history.py`（ADR-0016） |
| agent 实现 | dataclass 字段 | stub ↔ real（`MANIFEST` 驱动 `dataclasses.replace` 逐条换） | `agents/assembly.py: Agents` |
| **拓扑** | `StageSpec` 序列 | `default_pipeline()`（遍历 `MANIFEST` 派生）↔ 自定义 spec（省略 stage） | `runtime/orchestrator.py: default_pipeline` |

每个 seam 都有「第二个 adapter」——按 deep-module 原则（two adapters means a real seam）均为真 seam，无假 seam。

## 4. 装配：从桩到真实（manifest 驱动）

`MANIFEST`（`agents/assembly.py`）是单一装配真相源：每条 `AgentEntry` 含 `name`/`field`/`stub`/`real`/`deps`/`build`。
它同时驱动两件事——typed `Agents` dataclass 构造与 `default_pipeline()` 拓扑（`StageSpec(name, build, deps)` per entry）。

- `create_stub_agents()`：遍历 `MANIFEST`，按 `field` 把 `stub` 装入 `Agents`（`partition` 无 field 跳过）。返回全套桩——tracer bullet 端到端回路（无采纳改动 → 终稿逐字节等于原文）。
- `create_real_agents(...)`：在桩基础上遍历 `MANIFEST`，对有 `real` 工厂的条目调 `real(RealDeps)`，返回非 `None` 者替换对应 `field`（`dataclasses.replace`）。纯函数 agent 与 `partition` 的 `real=None`，不替换（桩 = 真实）。

条件替换矩阵（`real` 工厂返回 `None` 即保留桩）：

| 入参 | 替换 |
|---|---|
| `llm`（必填） | 解析桩 → 真实解析 |
| `hitl1_gate`（必填） | HITL-1 桩 → 真实闸门 |
| `verify_llm + retrieval`（同时给出） | 体检桩 → 真实 ReAct |
| `hypothesis_llm + retrieval`（同时给出） | 开药桩 → 真实「投机生成 + 逐条取证」 |
| `hitl2_gate`（可选，缺省 `ConservativeHitl2Gate`） | HITL-2 桩 → 真实 confirm |

合并 / 影响传导 / 一致性 / 回写均为确定性纯函数、无 LLM / 检索依赖——桩路径与真实装配共用同一实现，故字节级承诺在任一替换组合下都成立。
`real` 工厂用 `functools.partial` 绑定依赖（恢复 IDE 跳转与签名可见性，比 lambda 闭包可读）。
每条 `AgentEntry` 的 `stub`/`real`/`build`/`deps` 四字段逐项含义与新增 agent 的三触点见 `docs/adding-an-agent.md`。

## 5. 异常兜底与单向流控（issue #11 · PRD §13）

每个下游 stage 经 `_guarded(stage, body, fallback)` 统一兜底：

- `body()` 正常返回 patch；
- `Hitl2GateError`（硬闸门正确性硬停，ADR-0010）**原样上抛、不兜底**——绝不无人拍板自动采纳；
- 其余异常 → `fallback()` 降级 patch + `_log_error_patch` 日志、单向向前推进。

各 stage 只声明「正常返回」与「降级 patch」两件本质之事，样板收口于一处（locality）。
逐 stage 降级语义见 §1 大框每 stage 的 `失败降级 →` 标注（图细节，不再于此重复表）。

回写幂等续跑入口：`Orchestrator.resume_writeback(argument_tree, original_paragraphs)`（崩溃恢复，复用纯函数幂等再推导）。

## 6. 并行双线路的 channel 合流

体检 ∥ 开药是两条固定并行边，在 `merge` 处 join。
两线路各从同一棵 HITL-1 输出树出发、互不见对方写入。
若让二者直接写同一 `argument_tree` channel，则后写者整节点覆盖先写者——`status` 与 `candidate_hypotheses` 无法在同节点共存。

解法（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）：

- 体检写 `argument_credibility` channel（只产 `ArgumentStatus`）；
- 开药写 `hypotheses` channel（只产 `list[Hypothesis]`）；
- `merge_node` 经 `merge_with_partials` 字段级合流（`apply_partial_updates`）到同一棵树、再跑矩阵裁决。
- `argument_tree` channel 的 `merge_argument_tree` reducer 负责整树 upsert（首见顺序、同 id 覆盖）。

## 7. 状态机集中化（ADR-0011）

`status_machine.py` 是唯一的状态迁移裁判：`ALLOWED_TRANSITIONS` 表 + `validate_transition`。
HITL-2 采纳（`_apply_adopt`）与 orchestrator 兜底（`mark_argument_error`）都经此一处，杜绝规则漂移。
非法迁移一律拦截（`IllegalStatusTransitionError` / `Hitl2GateError`）。

## 8. 扩展点

### 8.1 接入真实 LLM provider

三条 LLM seam（解析 / 体检 / 开药）的第二 adapter 已落地——provider 工厂 + 三个 adapter + CLI 闸门 + 入口：

| 模块 | 角色 |
|---|---|
| `infra/llm_provider.py` | `build_qwen_chat_model()`：把 `ChatOpenAI` 指向 DashScope OpenAI-compatible 端点（`base_url` + `qwen-max`）。API key **只**读环境变量 `DASHSCOPE_API_KEY`，绝不硬编码。 |
| `infra/llm_adapters.py` | `QwenParseLlmClient` / `QwenVerifyLlmClient` / `QwenHypothesisLlmClient`：经 `with_structured_output(<schema>)` 满足各 seam 契约（dev-guide §6.3）。判别联合走**扁平信封 schema**（`action` + 各分支可选字段 → `to_step()` 映射回 contract 判别类），最大化 OpenAI-compatible 网关兼容；结构化链**懒构建**，构造期不触碰 provider 特性。 |
| `runtime/cli_gates.py` | `CliHitl1Gate` / `CliHitl2Gate`：交互式同步闸门（终端 `input()` 收决策）；非 tty 退化为保守决策（HITL-1 `SKIP`、HITL-2 有待决则全驳回），守住「绝不替人拍板自动采纳」。 |
| `runtime/run_real.py` | `run_real_pipeline(original_doc)` 组装上述全部 + Mock 检索 + `Orchestrator`；`python -m runtime.run_real [input] [-o output]`。 |

检索当前用 `create_mock_retrieval_layer()`（合规、确定、假素材）——ReAct 循环跑通，evidence 为 mock；真实检索后端待接（白名单/权限/模板在 `infra/retrieval.py` 接口层强制）。

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
传入 `Orchestrator(spec=...)` 即可表达不同拓扑——例如省略 `hypothesis`：删除该 stage 并把 `merge` 的 `deps` 改为 `("verification",)`。
约束：`deps` 引用的 stage 必须在 spec 中存在（否则 LangGraph 报错）；`merge` 依赖的并行线路可按需裁剪。

### 8.3 新增 agent

manifest 驱动下触点为 **3**（ADR-0014，原 7 触点收口于 `MANIFEST`）：

1. **新子包** `agents/<name>/{contract,agent,__init__}.py`（seam agent）或扁平 `agents/<name>.py`（纯函数 agent）：`Protocol` seam + provider-free `Fake*` + 纯函数主逻辑（`argument_tree -> patch`，`model_copy` 不改输入）。
2. **`agents/assembly.py`**：加 `Agents` 字段（typed）+ 一条 `MANIFEST` 条目（`stub` / `real` 工厂 / `deps` / `build` 闭包，后者用 `_guarded` 兜底）。
3. **测试**：`tests/test_<name>.py`（纯函数单测）+ `test_orchestrator_e2e.py` 加集成断言。

`default_pipeline()` 自动纳入新 stage（遍历 `MANIFEST`），无需改 `orchestrator.py`。
完整走读（`verification` 真例 + 可填空骨架模板 + 两层调测）见 **`docs/adding-an-agent.md`**。

## 9. 质量门

```bash
ruff check src tests          # E/F/I/UP/B，line-length 99，不强制 ruff format
mypy --strict src             # 扁平 src 布局（ADR-0014），mypy_path=src 解析顶层裸名
pytest -q                     # 416 测试 + 6 skip
```

不强制 `ruff format`——不重排既有文件（既有缩进风格保持）。
lint / 类型 / 测试失败一律修，即使非本次改动引入（见 `CLAUDE.md`）。

## 10. 关键约束速查

- **一段一节点**：`paragraph_id` 单数，不跨段（ADR-0001）。
- **content 永不被 LLM 改写**：节点文本只来自只读表逐字节拷回（`parser.py` 先例，by construction）。
- **绝不替人拍板**：合并不置 `adopted`、不改 `content`/`status`；`adopted`/`corrected` 只由 HITL-2 + 回写负责。
- **HITL-2 不可跳过**：硬闸门，无待决→一键通过、有待决→绝不可 PASS（ADR-0010）。
- **单向流控**：异常即记日志、就地降级、继续向前；无复杂分布式重试（PRD §13）。
- **幂等回写**：始终从原始 bytes 重新推导整篇终稿，supplement 永不累积（ADR-0011）。
