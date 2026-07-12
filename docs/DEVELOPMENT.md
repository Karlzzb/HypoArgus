# 开发文档（DEVELOPMENT）

HypoArgus 是论证驱动型文档修订多智能体系统：纯文本 → 段落切分 → 论证树 → 双线路（体检 ∥ 开药）→ 合并 → 影响传导 → 一致性校验 → HITL-2 确认 → 段落原子回写 → 终稿。
本文面向贡献者，描述模块边界、seam、装配与扩展点；术语见 `CONTEXT.md`，架构决策见 `docs/adr/`，完整需求见 `prd_v2.0.md`。

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
  │  raw_text: bytes（原始输入，单次写入）
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ① partition                                          【单次·纯代码·无 LLM】 │
│    段落切分 + 字节级自检 assert（失败即正确性 bug → 硬停；不包 _guarded）    │
│    writes: store(RawParagraphStore 只读表), tree=[]                         │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ② parse                                        【单次·有 _guarded·有 LLM】 │
│    LlmClient.parse(store)（唯一读段落文本的环节）                            │
│    失败降级 → tree=[]（空树向前）                                            │
│    writes: tree（merge_tree reducer，整节点 upsert）                         │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ③ hitl1                          【单次·同步闸门，非 interrupt·有 _guarded】 │
│    gate.review(tree) → SKIP / ACCEPT / EDIT                                 │
│    EDIT 在深拷贝上逐 op 跑 validate_tree                                    │
│    writes: tree                                                             │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ├───────────────────────────┐  ← hitl1 出两条并行边（固定，非动态 fan-out）
  ▼                            ▼
┌────────────────────────┐  ┌────────────────────────┐
│ ④ verification（体检）   │  │ ⑤ hypothesis（开药）    │   并行双线路 ∥
│ 图层级 = 单次节点        │  │ 图层级 = 单次节点        │   互不见对方写入
│ ── 内部 ReAct 循环 ──    │  │ ── 内部循环 ──          │
│ for node in tree:        │  │ for node in tree:        │
│   (MAIN/SUB/EVIDENCE)    │  │   (EVIDENCE/SUB)         │
│   for _ in range(8):  ◀──有界循环        proposals = propose()  ◀──单次生成
│     step = next_step     │  │   for p in proposals:    │  ◀──有界 ReAct 取证
│     Conclude → 判决返回  │  │     for _ in range(8):   │
│     Search  → retrieve   │  │       step = next_verify_step
│             → observation│  │       Conclude → 判决     │
│ 退出：判决/异常/         │  │       Search  → retrieve   │
│       max_iter 耗尽 → ERROR  │             → observation │
│                          │  │ 退出：判决/异常/         │
│                          │  │       max_iter 耗尽 → DOUBTFUL│
│ writes:                  │  │ writes:                  │
│   verification_updates   │  │   hypothesis_updates     │
└────────────────────────┘  └────────────────────────┘
                 │                        │
                 └───────────┬────────────┘  ← join（LangGraph 等齐两条分支）
                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑥ merge                             【单次·无 LLM·纯函数·有 _guarded】      │
│    reads: tree + verification_updates + hypothesis_updates                 │
│    apply_partial_updates（字段级合流：status ∥ candidate_hypotheses）        │
│    → merge_fn（12 格矩阵裁决；绝不置 adopted、不改 content/status）          │
│    writes: tree                                                            │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑦ impact                       【单次·串行·无 LLM·纯函数·有 _guarded】      │
│    上层论点 invalid / weakening（剩余支撑率）                                │
│    writes: tree                                                            │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑧ consistency                 【单次扫描·无 LLM·只贴 issue_tags·有 _guarded】│
│    writes: tree                                                            │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑨ hitl2              【单次·同步硬闸门·不可跳过（ADR-0010）·有 _guarded】    │
│    gate.review(review) → Hitl2Decision                                      │
│    ConservativeHitl2Gate：无待决 → PASS / 有待决 → 拒（绝不替人拍板）        │
│    ⚠ Hitl2GateError 原样上抛、不兜底（正确性硬停，与其它 stage 降级语义不同）  │
│    adopted / corrected 只由此处决定                                          │
│    writes: tree                                                            │
└────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ ⑩ writeback                【单次·幂等纯函数·有 _guarded】                   │
│    从原始 bytes 重新推导整篇终稿（supplement 永不累积，ADR-0011）            │
│    失败降级 → 回退原文 bytes（保护原文底线）；adopted 节点不动、待续跑      │
│    writes: final_doc                                                       │
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

源码在 `src/hypoargus/`，共 18 个模块、约 4500 行。

### 领域核心（共享不变语言，所有 agent 依赖、不反向依赖 agent）

| 模块 | 职责 |
|---|---|
| `domain.py` | `ArgumentationNode` / `NodeType` / `NodeStatus` / `Hypothesis` / `MergeAction` / `MergeDecision`。节点形状为决策、非最终代码；字段由各 agent 分阶段补全。 |
| `raw_store.py` | `RawParagraphStore`：只读、确定性、字节级无损的原文段落表（ADR-0005/0009）。 |
| `partition.py` | 纯代码段落切分（零 LLM）。 |
| `tree_invariants.py` | 论证树结构不变式（`validate_tree` / `rebuild_children`）。 |
| `status_machine.py` | 集中状态机：`validate_transition` / `transition_node` / `mark_node_error` + `ALLOWED_TRANSITIONS`（ADR-0011）。 |

### 公共契约层（provider seam）

| 模块 | 职责 |
|---|---|
| `retrieval.py` | `RetrievalLayer` Protocol + Mock（白名单/权限/模板在接口层强制）。体检 / 开药共用。 |

### 智能体（每个一个深模块：小接口 + 大实现）

每个智能体都是**纯函数 + 注入 seam + provider-free Fake**：可独立 import、独立调用、独立单测。

| 模块 | 接口 | seam | 覆盖节点 |
|---|---|---|---|
| `parser.py` | `parse(store, llm) -> list[ArgumentationNode]` | `LlmClient` / `FakeLlmClient` | 全段（唯一读段落文本的环节） |
| `hitl1.py` | `confirm(tree, gate) -> list[ArgumentationNode]` | `Hitl1Gate` / `FakeHitl1Gate` | 结构编辑（可跳过） |
| `verification.py` | `verify(tree, llm, retrieval, *, max_iterations) -> dict` | `VerifyLlmClient` / `FakeVerifyLlmClient` | main_claim / sub_claim / evidence |
| `hypothesis.py` | `hypothesize(tree, llm, retrieval, *, max_iterations) -> dict` | `HypothesisLlmClient` / `FakeHypothesisLlmClient` | evidence / sub_claim（不读体检结论，ADR-0002） |
| `merge.py` | `merge(tree)` / `merge_with_partials(tree, v, h, merge_fn)` / `apply_partial_updates(...)` | 无（确定性 12 格矩阵纯函数） | 全节点标注 |
| `impact.py` | `impact(tree) -> list[ArgumentationNode]` | 无（串行·不产文本·剩余支撑率纯函数） | 上层论点 invalid/weakening |
| `consistency.py` | `consistency(tree) -> list[ArgumentationNode]` | 无（单次扫描·只贴 `issue_tags`） | 段落级 / 全局一致性 |
| `hitl2.py` | `confirm(tree, store, gate) -> list[ArgumentationNode]` / `build_review(...)` | `Hitl2Gate` / `FakeHitl2Gate` / `ConservativeHitl2Gate` | 待决节点（不可跳过硬闸门，ADR-0010） |
| `writeback.py` | `writeback(tree, store) -> WritebackResult` | 无（段落原子缝合·幂等纯函数） | 被采纳节点 |

### 装配与调度

| 模块 | 职责 |
|---|---|
| `agents.py` | `Agents` dataclass（9 个契约）+ `create_stub_agents` / `create_real_agents`（`functools.partial` 绑定依赖、逐个条件替换桩）。 |
| `orchestrator.py` | `Orchestrator` 调度中枢 + `StageSpec` / `default_pipeline` / `_guarded` 兜底。 |

## 3. seam 一览

| seam | 类型 | 两个 adapter（→ 真 seam） | 位置 |
|---|---|---|---|
| LLM 解析 | Protocol | `FakeLlmClient` ↔ 真实 provider | `parser.LlmClient` |
| LLM 体检 | Protocol | `FakeVerifyLlmClient` ↔ 真实 ReAct | `verification.VerifyLlmClient` |
| LLM 开药 | Protocol | `FakeHypothesisLlmClient` ↔ 真实 | `hypothesis.HypothesisLlmClient` |
| HITL-1 闸门 | Protocol | `FakeHitl1Gate` ↔ 真实 interrupt | `hitl1.Hitl1Gate` |
| HITL-2 闸门 | Protocol | `FakeHitl2Gate` / `ConservativeHitl2Gate` ↔ 真实 interrupt | `hitl2.Hitl2Gate` |
| 检索层 | Protocol | `create_mock_retrieval_layer` ↔ 真实 | `retrieval.RetrievalLayer` |
| agent 实现 | dataclass 字段 | stub ↔ real（`dataclasses.replace` 逐个换） | `agents.Agents` |
| **拓扑** | `StageSpec` 序列 | `default_pipeline()` ↔ 自定义 spec（省略 stage） | `orchestrator.default_pipeline` |

每个 seam 都有「第二个 adapter」——按 deep-module 原则（two adapters means a real seam）均为真 seam，无假 seam。

## 4. 装配：从桩到真实

`create_stub_agents()` 返回全套桩（tracer bullet 端到端回路：无采纳改动 → 终稿逐字节等于原文）。
`create_real_agents(...)` 在桩基础上**逐个、条件地**替换：

- `llm`（必填）→ 解析桩换真实解析；
- `hitl1_gate`（必填）→ HITL-1 桩换真实闸门；
- `verify_llm + retrieval`（同时给出）→ 体检桩换真实 ReAct；
- `hypothesis_llm + retrieval`（同时给出）→ 开药桩换真实「投机生成 + 逐条取证」；
- `hitl2_gate`（可选，缺省 `ConservativeHitl2Gate`）→ HITL-2 真实 confirm。

合并 / 影响传导 / 一致性 / 回写均为**确定性纯函数、无 LLM / 检索依赖**——桩路径与真实装配共用同一实现，故字节级承诺在任一替换组合下都成立。

绑定依赖用 `functools.partial`（如 `partial(verify_fn, llm=verify_llm, retrieval=retrieval, max_iterations=...)`）——恢复 IDE 跳转与签名可见性，比 lambda 闭包更可读。

## 5. 异常兜底与单向流控（issue #11 · PRD §13）

每个下游 stage 经 `_guarded(stage, body, fallback)` 统一兜底：

- `body()` 正常返回 patch；
- `Hitl2GateError`（硬闸门正确性硬停，ADR-0010）**原样上抛、不兜底**——绝不无人拍板自动采纳；
- 其余异常 → `fallback()` 降级 patch + `_log_error_patch` 日志、单向向前推进。

各 stage 只声明「正常返回」与「降级 patch」两件本质之事，样板收口于一处（locality）。
降级语义一览：

| stage | 降级 patch |
|---|---|
| parse | `tree=[]`（空树向前） |
| hitl1 / impact / consistency / hitl2 | 保留 stale 树 |
| verification | 覆盖范围内未判决节点置 `error`（`_mark_verify_scope_error`） |
| hypothesis | 空 partial（不置节点 error，避免覆盖体检判决） |
| merge | 已合流未裁决的 `combined`（`apply_partial_updates`） |
| writeback | 回退原文 bytes（保护原文底线）；`adopted` 节点不动、待续跑 |

回写幂等续跑入口：`Orchestrator.resume_writeback(tree, store)`（崩溃恢复，复用纯函数幂等再推导）。

## 6. 并行双线路的 channel 合流

体检 ∥ 开药是两条固定并行边，在 `merge` 处 join。
两线路各从同一棵 HITL-1 输出树出发、互不见对方写入。
若让二者直接写同一 `tree` channel，则后写者整节点覆盖先写者——`status` 与 `candidate_hypotheses` 无法在同节点共存。

解法（dev-guide §2.2 铁律：共享可变状态换成带 reducer 的 channel）：

- 体检写 `verification_updates` channel（只改 `status`）；
- 开药写 `hypothesis_updates` channel（只改 `candidate_hypotheses`）；
- `merge_node` 经 `merge_with_partials` 字段级合流（`apply_partial_updates`）到同一棵树、再跑矩阵裁决。
- `tree` channel 的 `merge_tree` reducer 负责整树 upsert（首见顺序、同 id 覆盖）。

## 7. 状态机集中化（ADR-0011）

`status_machine.py` 是唯一的状态迁移裁判：`ALLOWED_TRANSITIONS` 表 + `validate_transition`。
HITL-2 采纳（`_apply_adopt`）与 orchestrator 兜底（`mark_node_error`）都经此一处，杜绝规则漂移。
非法迁移一律拦截（`IllegalStatusTransitionError` / `Hitl2GateError`）。

## 8. 扩展点

### 8.1 接入真实 LLM provider

实现对应 `Protocol`（如 `LlmClient` / `VerifyLlmClient`），用 `with_structured_output(Schema)` 保证结构合法（dev-guide §6.3）。
注入 `create_real_agents(...)` 即可，其余不变。

### 8.2 自定义拓扑

`default_pipeline()` 返回 `tuple[StageSpec, ...]`。
传入 `Orchestrator(spec=...)` 即可表达不同拓扑——例如省略 `hypothesis`：删除该 stage 并把 `merge` 的 `deps` 改为 `("verification",)`。
约束：`deps` 引用的 stage 必须在 spec 中存在（否则 LangGraph 报错）；`merge` 依赖的并行线路可按需裁剪。

### 8.3 新增 agent

1. 新模块：`Protocol` seam + provider-free `Fake*` + 纯函数主逻辑（`tree -> patch`，`model_copy` 不改输入）。
2. `agents.py`：加 `XFn` Protocol + `Agents` 字段 + `_stub_X`（桩）+ 在 `create_real_agents` 条件替换。
3. `orchestrator.py`：加 `_X_node(agents) -> NodeFn`（用 `_guarded`）+ 在 `default_pipeline()` 插入 `StageSpec` 与 `deps`。
4. `__init__.py`：re-export。
5. 测试：`tests/test_X.py`（纯函数单测）+ 在 `test_orchestrator_e2e.py` 加集成断言。

## 9. 质量门

```bash
ruff check src tests          # E/F/I/UP/B，line-length 99，不强制 ruff format
mypy --strict src             # 扁平 src 布局（ADR-0014），mypy_path=src 解析顶层裸名
pytest -q                     # ~384 测试
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
