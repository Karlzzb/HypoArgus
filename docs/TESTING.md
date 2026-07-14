# 测试文档（TESTING）

HypoArgus 的测试遵循 PRD «Testing Decisions»：**黑盒外部行为验证**为主、**纯函数子缝单测**为基。
核心断言贯穿全部 stage：**无采纳改动时，终稿与原始输入逐字节完全一致**（含空行/缩进/换行/末尾空格）。

## 1. 测试分层

| 层 | 文件 | 验证 | 数量 |
|---|---|---|---:|
| 领域核心 | `test_partition.py` / `test_original_paragraphs.py` / `test_tree_invariants.py` / `test_status_machine.py` | 切分字节级还原 / 只读表 / 树不变式 / 状态机迁移 | 28 / 16 / 8 / 41 |
| infra seam | `test_retrieval.py` | 检索契约+合规（白名单 / 权限 / 模板） | 19 |
| 纯函数子缝 | `test_parser.py` / `test_hypothesis.py` / `test_merge.py` / `test_impact.py` / `test_consistency.py` / `test_writeback.py` | 各 agent 纯函数（不经 Orchestrator）+ Fake seam | 25 / 11 / 36 / 34 / 24 / 20 |
| HITL | `test_hitl1.py` / `test_hitl2.py` | 闸门契约 / 硬闸门拦截 / 段级三态确认 | 27 / 13 |
| 端到端 | `test_orchestrator_e2e.py` | 全链字节级承诺 + 各 issue 集成（逐个换桩→真实）+ judgment 五合一 + rewrite_loop/hitl2 终稿确认 | 59 |
| 异常兜底 | `test_orchestrator_fallback.py` | 任一 stage 异常 → 兜底 + 单向推进 + 日志（PRD §13）；`_guarded` 放行 `GraphBubbleUp` | 12 |
| 崩溃恢复 | `test_orchestrator_resume.py` | 终稿拼装幂等续跑（issue #11） | 4 |
| 持久化 HITL spine | `test_checkpoint.py` / `test_gates.py` / `test_orchestrator_interrupt.py` | checkpointer 编解码 + PG 往返 / 中断驱动闸门 seam / resume 循环 + 跨进程续跑（T-03） | 5 / 6 / 4 |
| 拓扑 seam | `test_orchestrator_topology.py` | `PipelineSpec` 数据驱动 + 自定义拓扑 | 4 |
| 真实 LLM 联网 | `test_real_llm_wiring.py` | DashScope provider 适配（需 key+网络，默认 skip） | 11 |

合计 **441 passed, 3 skipped**（444 collected；skip：`test_writeback.py:330` 样例不足两段 ×2；`test_real_llm_wiring.py:293` dashscope_smoke 需 key+网络 ×1）。
T-03 后新增持久化异步 HITL spine 测试：`test_checkpoint.py`（codec + PG 往返）、`test_gates.py`（中断驱动闸门 seam）、`test_orchestrator_interrupt.py`（resume 循环 + 跨进程续跑）、`test_orchestrator_fallback.py` 增 `_guarded` 放行 `GraphBubbleUp`。故基线较 T-01（410 passed）上升至 441 passed。

## 2. 测试约定

### 2.1 provider-free Fake

每个 LLM / 闸门 seam 配一个 `Fake*`（`FakeLlmClient` / `FakeHypothesisLlmClient` / `FakeJudgmentLlmClient` / `FakeHitl1Gate` / `FakeHitl2Gate`）。
Fake 支持「factory（据输入动态决策）」注入——离线、确定、可断言，单测不触真实 provider。
`FakeJudgmentLlmClient` 默认返回空 `JudgmentResult`（无裁决 → 全 KEEP → 逐字节忠实，tracer bullet 承诺）。

### 2.2 样例文档矩阵

`tests/conftest.py` 的 `SAMPLE_DOCS` 覆盖边界形态（空行 / 缩进 / 列表 / 代码围栏 / 无末尾换行 / 末尾空格 / 混合 / 仅空白 / 单行）。
`sample_doc` fixture 参数化驱动——每个字节级断言跑遍全部样例。

### 2.3 字节级承诺

`assert orch.run(doc) == doc` 是贯穿 e2e 的核心断言：
无采纳改动时终稿逐字节等于原文。
任何 stage 的改动若破坏此承诺，会被 `sample_doc` 参数化立即捕获。

## 3. 各测试文件覆盖契约

### `test_orchestrator_e2e.py`（59）

- `test_e2e_byte_identical_no_adoptions`：默认桩全链字节级承诺（遍历样例）。
- `test_e2e_pipeline_single_direction_no_reschedule`：每个桩恰好调用一次（7 stage 单向推进、无打回）。
- `test_e2e_default/injected_session_context_*` / `test_e2e_retrieval_*`：贯穿 state（session_context / query_time_range）+ retrieval 桩产空 citations 仍逐字节还原。
- `test_real_parse_*`：真实解析接入（空 LLM / 成环提议 / 真实提议 / HITL-1 编辑）仍逐字节还原。
- `test_real_hypothesis_wired_*`：真实开药接入（产 pending 假说、propose 异常兜底）仍逐字节还原。
- `test_real_judgment_argument_verdicts_land_in_tree_*`：judgment 据 citations 判 per-argument 终态、merge 全 KEEP、逐字节还原。
- `test_real_judgment_hypothesis_verdicts_trigger_merge_action_*`：judgment 落假说终态 → merge 矩阵命中 REPLACE、保守 HITL-2 驳回、逐字节还原。
- `test_real_judgment_impact_propagates_*`：judgment 产 evidence error → impact 后序上推 sub_claim/main_claim invalid。
- `test_real_judgment_consistency_tags_*`：judgment 调 consistency 贴 `multi_main_claim` 批注、不影响终稿。
- `test_e2e_touched_confirmed_rewrite_lands_in_final_document`：judgment 落 supported 假说 → rewrite_loop 对触达段产提议 → hitl2 (FakeHitl2Gate) 确认 → 终稿含确认文本、未触达段逐字节原文（Slice 6 终稿确认路径）。

### `test_orchestrator_fallback.py`（12）

- `test_judgment_wholesale_exception_marks_in_scope_arguments_error`：judgment 整体异常 → 覆盖范围内未判决节点置 `error` + `orchestrator_error:judgment` 标签 + 逐字节还原。
- `test_tree_stage_exception_keeps_pipeline_alive_and_logs`（参数化 parse/hitl1/hitl2）：任一主干 stage 异常 → stale 树向前 + 日志（hitl2 普通异常回退原文 bytes）。
- `test_rewrite_loop_exception_falls_back_to_empty_proposed_rewrites_and_logs`：rewrite_loop 整体异常 → 空 `proposed_rewrites` + 日志、逐字节还原（不碰 `argument_tree`）。
- `test_hitl2_gate_error_is_hard_stop_not_swallowed`：`Hitl2GateError` 原样上抛、不兜底（ADR-0010）。
- `test_guarded_re_raises_graph_bubbleup_not_swallowed`（T-03）：`_guarded` 不吞 `GraphBubbleUp`（`GraphInterrupt` 基类）——hitl1/hitl2 节点的 `interrupt` 不被静默兜底、图正常暂停。
- `test_guarded_still_swallows_plain_runtime_error`（T-03）：普通异常仍兜底（`GraphBubbleUp` 放行不影响既有降级语义）。
- `test_retrieval_exception_falls_back_to_empty_citations_and_logs`：retrieval 异常 → 空 citations + 日志、逐字节还原。
- `test_hitl1_replay_*` / `test_hitl1_replay_exhaustion_*`：partition 打回一次后继续 / 超限贴 `partition_retry_exhausted` 向前。

### `test_orchestrator_resume.py`（4）

终稿拼装幂等续跑：`resume_rewrite(resolved_rewrites, original_paragraphs)` 据 `resolved_rewrites` 按段文本重推导 `final_document`、幂等重跑同 bytes（确认段用 resolved 文本、省略段逐字节原文、空 resolved 逐字节等于原文）。

### `test_checkpoint.py`（5）+ `test_gates.py`（6）

- `test_checkpoint.py`：`HypoArgusSerializer`（`JsonPlusSerializer` 子类 + `OriginalParagraphs`
  信封编解码）纯函数往返（段落序 + bytes 等价、空文档、委托 pydantic/bytes/dict 不变、
  普通同名 dict 不误还原）+ 真实 `AsyncPostgresSaver` 落库往返（ADR-0022：`OriginalParagraphs`
  经 checkpointer 写读等价）。
- `test_gates.py`：`InterruptHitl1Gate` / `InterruptHitl2Gate` 的 `formulate_question` /
  `parse_reply` 纯数据 seam（action-only 决策、空 ops、快照解耦）。`review()` 的
  `formulate_question → interrupt → parse_reply` 组合由集成测试覆盖。

### `test_orchestrator_interrupt.py`（4）

持久化异步 HITL spine（ADR-0022）集成：图注入 `InterruptHitl*Gate` + `AsyncPostgresSaver`、
由 `runtime.run_real.run_resume_loop` 驱动 `ainvoke → aget_state → Command(resume=...)`。
- `test_resume_loop_drives_two_interrupts_to_terminal_byte_identical`：fresh run 经 hitl1
  暂停（SKIP resume）+ hitl2 暂停（无待决自动 PASS）续跑至终态、逐字节原文。
- `test_resume_loop_hitl1_replay_then_skip_reruns_parse`：hitl1 REPLAY（有界打回）经 interrupt
  resume 路径重跑 parse+partition、再 SKIP 续跑。
- `test_cross_process_resume_persists_interrupt_across_savers`：进程 1 跑至 hitl1 暂停（退出），
  新 saver + 同 `session_id` `aget_state` 见 interrupt、resume 续跑至完成（ADR-0022 核心承诺）。
- `test_interrupt_state_carries_original_paragraphs_through_checkpoint`：interrupt 暂停点的
  `state.values` 含 `original_paragraphs`、跨 saver 读回仍 `OriginalParagraphs` 等价。

### `test_orchestrator_topology.py`（4）

- `test_default_spec_replicates_fixed_topology_byte_identity`：默认 spec 复刻原拓扑（行为零变化）。
- `test_default_pipeline_is_immutable_tuple_of_frozen_specs`：spec 不可变、7 stage、`judgment.deps=("retrieval",)` / `rewrite_loop.deps=("judgment",)` / `hitl2.deps=("rewrite_loop",)`。
- `test_custom_spec_dropping_hypothesis_propose_skips_it`：省略 hypothesis_propose → retrieval 接 hitl1、开药绝不被调用、仍逐字节还原。
- `test_custom_spec_dropping_judgment_skips_it`：省略 judgment → hitl2 接 retrieval、裁决绝不被调用、仍逐字节还原。

## 4. 如何运行

```bash
pytest -q                         # 全量（~30s：含 Postgres checkpointer 集成）
pytest tests/test_merge.py -q     # 单文件
pytest -k "byte_identical" -q     # 按名筛选
pytest --tb=short                 # 失败时短回溯
```

`pytest-asyncio`（`asyncio_mode = "auto"`，见 `pyproject.toml`）：async 测试函数自动收集，
无需 `@pytest.mark.asyncio`。

质量门（CI 应跑）：

```bash
ruff check src tests
mypy --strict src
pytest -q
```

### 4.1 Postgres checkpointer 集成测试

`tests/conftest.py` 的 `pg_checkpointer` async 夹具产一个已 `setup` 的
`AsyncPostgresSaver`（装配 `HypoArgusSerializer`，见 `runtime.checkpoint`）。连接串读
`.env` 的 `HYPOARGUS_PG_DSN`（`conftest` 顶部 `load_dotenv()` 注入；CLI 侧 `main()` 同样
加载）。PG 不可达 / 未配置时该夹具 `pytest.skip`——不阻塞离线纯函数测试。

每个集成测试用唯一 `thread_id` 避免共享 PG 实例的碰撞；跨进程续跑测试另开一个 saver
（新 PG 连接）模拟「进程 2」。CI / 本地均跑 conda `HypoArgus`，需可达的 Postgres
（一期选 shared PG；testcontainer 亦可，见 `docs/adr/0022` Considered Options）。

## 5. 如何新增测试

### 5.1 纯函数子缝单测

直接 import 纯函数 + 构造 `Fake*` seam + 手建 `Argument` 树：

```python
from agents.judgment import judge_and_adjudicate, FakeJudgmentLlmClient
from agents.judgment import ArgumentVerdictEntry, JudgmentArgumentVerdict
from domain import Argument, ArgumentType, ArgumentStatus

argument_tree = [Argument(argument_id="n0", argument_type=ArgumentType.MAIN_CLAIM,
                          paragraph_id="p0001", content="x")]
llm = FakeJudgmentLlmClient(
    judge_factory=lambda tree, hyps, cites, sc, qtr: JudgmentResult(
        argument_verdicts=[ArgumentVerdictEntry(argument_id="n0",
                                                verdict=JudgmentArgumentVerdict.CREDIBLE)]
    )
)
outcome = judge_and_adjudicate(argument_tree, {}, {}, sc, qtr, llm)
assert outcome.argument_tree[0].status is ArgumentStatus.CREDIBLE
```

约定：

- **断言纯函数返回新实例**（`model_copy`），输入树不变；
- 用 Fake 的 `factory` 做多假设断言；
- 检索用 `create_mock_retrieval_layer()`，不触网络。

### 5.2 端到端集成断言

在 `test_orchestrator_e2e.py` 加：用 `create_real_agents(...)` 装配、`replace(agents, X=wrapped_X)` 包一层捕获中间态、断言字节级承诺 + 该 stage 的语义落位。

### 5.3 异常兜底断言

在 `test_orchestrator_fallback.py` 加：`replace(base, X=lambda *a: raise RuntimeError(...))`，断言 `report.final_document == _DOC` + `any("X" in e for e in report.errors)`。
若该 stage 有特殊降级语义（如 judgment 标 scope error），补对应断言。

### 5.4 拓扑变体断言

构造自定义 spec（`replace(stage, deps=...)` 删除 / 重连），`Orchestrator(spec=...)`，断言被省略 agent 调用次数为 0 + 字节级承诺。

## 6. 测试不覆盖项（已知边界）

- 真实 LLM provider 集成：Fake 覆盖契约，provider 适配器属生产装配、无单测（除默认 skip 的 dashscope_smoke）。
- `test_writeback.py:330` skip：样例不足两段、无法验证多段拼装的中间段（需 ≥2 段样例）。
- 真实检索后端：retrieval 节点为伪代码桩（产空 citations），真实后端 Out of Scope。
- HITL 一期 `action-only`：`InterruptHitl*Gate.parse_reply` 产 action-only 决策（空 ops），
  故触达段经 interrupt 路径恒被驳回（终稿逐字节原文）；结构化逐段 confirm/edit 推后
  （PRD §7.2 注 / ADR-0022 二期）。全保真含 ops 路径仍由同步 `FakeHitl*Gate.review` 覆盖
  （`test_e2e_touched_confirmed_rewrite_lands_in_final_document`）。
