# 测试文档（TESTING）

HypoArgus 的测试遵循 PRD «Testing Decisions»：**黑盒外部行为验证**为主、**纯函数子缝单测**为基。
核心断言贯穿全部 stage：**无采纳改动时，终稿与原始输入逐字节完全一致**（含空行/缩进/换行/末尾空格）。

## 1. 测试分层

| 层 | 文件 | 验证 | 数量 |
|---|---|---|---:|
| 领域核心 | `test_partition.py` / `test_original_paragraphs.py` / `test_tree_invariants.py` / `test_status_machine.py` | 切分字节级还原 / 只读表 / 树不变式 / 状态机迁移 | 28 / 16 / 8 / 41 |
| infra seam | `test_retrieval.py` / `test_retrieval_tool.py` / `test_tool_registry.py` / `test_history.py` | 检索契约+合规 / SearchStep 翻译 / 工具调度 / 历史记忆+压缩 | 19 / 8 / 5 / 10 |
| 纯函数子缝 | `test_parser.py` / `test_verification.py` / `test_hypothesis.py` / `test_merge.py` / `test_impact.py` / `test_consistency.py` / `test_writeback.py` | 各 agent 纯函数（不经 Orchestrator）+ Fake seam | 17 / 12 / 16 / 36 / 34 / 24 / 26 |
| HITL | `test_hitl1.py` / `test_hitl2.py` | 闸门契约 / 硬闸门拦截 / 采纳链 | 19 / 20 |
| 端到端 | `test_orchestrator_e2e.py` | 全链字节级承诺 + 各 issue 集成（逐个换桩→真实） | 52 |
| 异常兜底 | `test_orchestrator_fallback.py` | 任一 stage 异常 → 兜底 + 单向推进 + 日志（PRD §13） | 9 |
| 崩溃恢复 | `test_orchestrator_resume.py` | 回写幂等续跑（issue #11） | 3 |
| 拓扑 seam | `test_orchestrator_topology.py` | `PipelineSpec` 数据驱动 + 自定义拓扑（B 重构） | 4 |
| 真实 LLM 联网 | `test_real_llm_wiring.py` | DashScope provider 适配（需 key+网络，默认 skip） | 9 +1 skip |

合计 **416 passed, 6 skipped**（skip：`test_writeback.py:239` 样例不足三段无法改中间段 ×5；`test_real_llm_wiring.py:162` dashscope_smoke 需 key+网络 ×1）。

## 2. 测试约定

### 2.1 provider-free Fake

每个 LLM / 闸门 seam 配一个 `Fake*`（`FakeLlmClient` / `FakeVerifyLlmClient` / `FakeHypothesisLlmClient` / `FakeHitl1Gate` / `FakeHitl2Gate`）。
Fake 支持「script（按序）」与「factory（据输入动态决策）」两种注入——离线、确定、可断言，单测不触真实 provider。

### 2.2 样例文档矩阵

`tests/conftest.py` 的 `SAMPLE_DOCS` 覆盖边界形态（空行 / 缩进 / 列表 / 代码围栏 / 无末尾换行 / 末尾空格 / 混合 / 仅空白 / 单行）。
`sample_doc` fixture 参数化驱动——每个字节级断言跑遍全部样例。

### 2.3 字节级承诺

`assert orch.run(doc) == doc` 是贯穿 e2e 的核心断言：
无采纳改动时终稿逐字节等于原文。
任何 stage 的改动若破坏此承诺，会被 `sample_doc` 参数化立即捕获。

## 3. 各测试文件覆盖契约

### `test_orchestrator_e2e.py`（52）

- `test_e2e_byte_identical_no_adoptions`：默认桩全链字节级承诺（遍历样例）。
- `test_e2e_pipeline_single_direction_no_reschedule`：每个桩恰好调用一次（单向推进、无打回）。
- `test_real_parse_*`：真实解析接入（空 LLM / 成环提议 / 真实提议 / HITL-1 编辑）仍逐字节还原。
- `test_real_verify_*` / `test_real_hypothesis_*`：真实体检 / 开药接入（含全程异常兜底）仍逐字节还原。
- `test_real_merge_wired_*`：双轨合并矩阵裁决（conflict / replace）落位、不改文本。
- `test_real_impact_*`：影响传导上推 invalid、全 credible 不动。
- `test_real_consistency_*`：一致性贴批注（multi_main_claim）不影响回写。
- `test_real_hitl2_*`：保守闸门全驳回 / 全 credible PASS / 越权 PASS 硬停 / 采纳链持久化 + 回写翻正。

### `test_orchestrator_fallback.py`（9）

- `test_merge_stage_exception_does_not_hang_and_logs`：合并异常 → 不卡死 + 逐字节还原 + 日志。
- `test_verification_wholesale_exception_marks_in_scope_nodes_error`：体检整体异常 → 覆盖范围内未判决节点置 `error` + `orchestrator_error:verify` 标签。
- `test_tree_stage_exception_keeps_pipeline_alive_and_logs`（参数化 parse/hitl1/impact/consistency/hitl2）：任一树形 stage 异常 → stale 树向前 + 日志。
- `test_hitl2_gate_error_is_hard_stop_not_swallowed`：`Hitl2GateError` 原样上抛、不兜底（ADR-0010）。
- `test_writeback_stage_exception_falls_back_to_original_bytes`：回写异常 → 回退原文 bytes（分区不变式）+ 日志。

### `test_orchestrator_resume.py`（3）

回写幂等续跑：`resume_writeback` 据持久化 `adopted_hypothesis_id` 重做、不重复注入、状态收敛 `corrected`。

### `test_orchestrator_topology.py`（4，B 重构新增）

- `test_default_spec_replicates_fixed_topology_byte_identity`：默认 spec 复刻原拓扑（行为零变化）。
- `test_default_pipeline_is_immutable_tuple_of_frozen_specs`：spec 不可变、`merge` 双依赖。
- `test_custom_spec_dropping_hypothesis_skips_it`：省略 hypothesis → 开药绝不被调用、merge 仅依赖 verification、仍逐字节还原。
- `test_custom_spec_dropping_consistency_skips_it`：省略 consistency → 一致性绝不被调用、hitl2 接 impact、仍逐字节还原。

## 4. 如何运行

```bash
pytest -q                         # 全量（~1s）
pytest tests/test_merge.py -q     # 单文件
pytest -k "byte_identical" -q     # 按名筛选
pytest --tb=short                 # 失败时短回溯
```

质量门（CI 应跑）：

```bash
ruff check src tests
mypy --strict src
pytest -q
```

## 5. 如何新增测试

### 5.1 纯函数子缝单测

直接 import 纯函数 + 构造 `Fake*` seam + 手建 `Argument` 树：

```python
from agents.verification import verify, FakeVerifyLlmClient, ConcludeStep, VerifyVerdict
from infra.retrieval import create_mock_retrieval_layer
from domain import Argument, ArgumentType, ArgumentStatus

argument_tree = [Argument(argument_id="n0", argument_type=ArgumentType.MAIN_CLAIM,
                 paragraph_id="p0001", content="x")]
llm = FakeVerifyLlmClient(factory=lambda argument, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE))
updates = verify(argument_tree, llm, create_mock_retrieval_layer())
assert updates["n0"] is ArgumentStatus.CREDIBLE
```

约定：

- **断言纯函数返回新实例**（`model_copy`），输入树不变；
- 用 Fake 的 `factory` 做多假设断言、`script` 做按序断言；
- 检索用 `create_mock_retrieval_layer()`，不触网络。

### 5.2 端到端集成断言

在 `test_orchestrator_e2e.py` 加：用 `create_real_agents(...)` 装配、`replace(agents, X=wrapped_X)` 包一层捕获中间态、断言字节级承诺 + 该 stage 的语义落位。

### 5.3 异常兜底断言

在 `test_orchestrator_fallback.py` 加：`replace(base, X=lambda *a: raise RuntimeError(...))`，断言 `report.final_document == _DOC` + `any("X" in e for e in report.errors)`。
若该 stage 有特殊降级语义（如 verification 标 scope error），补对应断言。

### 5.4 拓扑变体断言

构造自定义 spec（`replace(stage, deps=...)` 删除 / 重连），`Orchestrator(spec=...)`，断言被省略 agent 调用次数为 0 + 字节级承诺。

## 6. 测试不覆盖项（已知边界）

- 真实 LLM provider 集成：Fake 覆盖契约，provider 适配器属生产装配、无单测。
- HITL `interrupt` + checkpointer：本切片为同步注入闸门；真实打断属后续切片（dev-guide §7/§8）。
- `test_writeback.py:239` skip：样例不足三段、无法验证改中间段（需 ≥3 段样例）。
