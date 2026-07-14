# HANDOFF — Slice 6（rewrite_loop + hitl2 终稿确认闸门 + final_document 拼装）

接续 `/tdd` 任务：执行 `docs/pipeline-restructure-tasks.md` 的 Slice 6，TDD 纵切，完成后同步任务文档状态。
批准的计划：`/home/karl/.claude/plans/snappy-sauteeing-galaxy.md`（先读它，再读本文件）。

## 当前进度（TDD 周期）

任务列表见 TaskList（Task #10–#17）。周期状态：

- ✅ Cycle 2（rewrite_loop propose_rewrites 纯函数 + LLM seam）— GREEN。
  - 新建 `src/agents/rewrite_loop/`（`contract.py` / `agent.py` / `__init__.py`）：`RewriteLlmClient` Protocol、`FakeRewriteLlmClient`、`RewriteLoopOutcome`、`propose_rewrites`。
  - `tests/test_writeback.py`（保留文件名、内容改写为「提议→确认→拼接」纯函数子缝）的 propose 段 6 测试 GREEN。
- ✅ Cycle 3+4（hitl2 契约级重写 argument→段落级 + assemble_final_document 纯函数）— GREEN。
  - `src/agents/hitl2/contract.py` 全量重写：删 `AdoptOp/RejectOp/EditContentOp/CandidateView/ArgumentReview`；新增 `ConfirmRewriteOp/EditRewriteOp/RejectRewriteOp`（discriminator=`action`）、`ParagraphRewriteReview`、`Hitl2Review(paragraphs, has_pending)`。保留 `Hitl2GateError/Hitl2Action(PASS,DECIDE)/Hitl2Decision/Hitl2Gate/FakeHitl2Gate/ConservativeHitl2Gate`。
  - `src/agents/hitl2/agent.py` 全量重写：`build_review(original_paragraphs, proposed_rewrites)`、`resolve_rewrites(proposed_rewrites, ops)`、`assemble_final_document(original_paragraphs, resolved_rewrites)`、`confirm(...)→Hitl2Confirmation`、`Hitl2Confirmation(@dataclass: final_document: bytes, resolved_rewrites: dict[str,str])`。
  - `tests/test_hitl2.py` 全量重写（段落级三态）+ `tests/test_writeback.py` assemble 段 — 31 测试 GREEN。
- 🟨 Cycle 1（拓扑 tracer + 装配/orchestrator 接线）— **进行中、未 GREEN**。
  - 已完成：`src/agents/assembly.py`（`Agents.writeback`→`rewrite_loop` 字段、`RewriteLoopFn`/`Hitl2Fn` Protocol 重签名、`_stub_writeback`→`_stub_rewrite_loop`、`_stub_hitl2` 重签名、`_writeback_node`→`_rewrite_loop_node`、`_hitl2_node` 重写产 `final_document`、MANIFEST 删 writeback 加 rewrite_loop、hitl2.deps→`("rewrite_loop",)`、`RealDeps.rewrite_llm`、`create_real_agents` 透传）。
  - 已完成：`src/runtime/orchestrator.py`（`PipelineState` 加 `proposed_rewrites: Annotated[dict[str,str], _merge_dict]`、删 `from agents.writeback import WritebackResult`、加 `from agents.hitl2 import assemble_final_document`、`resume_writeback`→`resume_rewrite(resolved_rewrites, original_paragraphs)→bytes`、拓扑 docstring 更新）。
  - 已完成：`git rm src/agents/writeback.py`（模块裁撤）。
  - 已完成：`tests/test_orchestrator_topology.py` 全量重写（断言新 7 拓扑 `parse+partition, hitl1, hypothesis_propose, retrieval, judgment, rewrite_loop, hitl2`、rewrite_loop.deps==("judgment",)、hitl2.deps==("rewrite_loop",)、writeback 缺席）— **4 测试 GREEN**。
  - ⚠️ **唯一未 GREEN 的卡点**：`src/runtime/cli_gates.py` 的 `CliHitl2Gate` 类体未重写（仅 import 已更新为新类型）。mypy 9 errors 全在此文件：`Hitl2Review.arguments`（新叫 `paragraphs`）、`ArgumentReview/AdoptOp/RejectOp/EditContentOp` 未定义。

## 下一步（按顺序）

1. **重写 `src/runtime/cli_gates.py` 的 `CliHitl2Gate` 类体**（Cycle 1 收尾）。
   - import 已改好（`ConfirmRewriteOp/EditRewriteOp/RejectRewriteOp/ParagraphRewriteReview/Hitl2Review`）。
   - 旧 `review(review)` 遍历 `review.arguments`（`ArgumentReview`）产 `AdoptOp/RejectOp/EditContentOp`。新契约：遍历 `review.paragraphs`（`ParagraphRewriteReview{paragraph_id, original_text, proposed_text}`），每段产一个 `ConfirmRewriteOp(paragraph_id)` / `EditRewriteOp(paragraph_id, text)` / `RejectRewriteOp(paragraph_id)`。
   - 命令设计（每段一决策）：`[c]onfirm` / `edit <text...>` / `[r]eject`（默认 reject=省略 op 或显式 RejectRewriteOp）。非交互（无 tty）→ 有 has_pending 时 `DECIDE`+空 ops（全驳回→原文）；无 has_pending → `PASS`。保留 `_is_interactive`/`input_fn`/`out_fn` 注入 seam 与软校验风格（笔误重 prompt、绝不产出会触发 `Hitl2GateError` 的决策）。
   - 同步改 `tests/test_real_llm_wiring.py` 的两个 HITL-2 CLI 测试（`test_cli_hitl2_gate_pass_when_no_pending` / `test_cli_hitl2_gate_noninteractive_decides_empty`）：`Hitl2Review(arguments=[], has_pending=...)` → `Hitl2Review(paragraphs=[], has_pending=...)`。
   - 跑 `ruff check src`（有 5 个 fixable，含 assembly.py organize-imports；用 `ruff check --fix src` 或手改）+ `mypy --strict src`（须 0 errors）+ `pytest -q tests/test_orchestrator_topology.py tests/test_hitl2.py tests/test_writeback.py`。Cycle 1 GREEN。

2. **Cycle 5+6（rewrite_loop/hitl2 节点兜底 + _guarded）** — Task #14。
   - `tests/test_orchestrator_fallback.py`：加 `test_rewrite_loop_exception_falls_back_to_empty_proposed_rewrites_and_logs`（whole-node 异常→空 proposed_rewrites + errors + 终稿原文）；`Hitl2GateError` 原样上抛不兜底（已有 `test_hitl2_gate_error_is_hard_stop_not_swallowed` 需适配新 hitl2 签名：throwing hitl2 现签名 `(original_paragraphs, proposed_rewrites)`，可对 `proposed_rewrites` 传非空触发 `Hitl2GateError`，或直接 raise）；hitl2 普通异常→原文 bytes fallback。
   - 删旧 `test_writeback_stage_exception_falls_back_to_original_bytes`（writeback 已裁撤）。
   - `_rewrite_loop_node`/`_hitl2_node` 的 `_guarded` 已在 assembly.py 写好，本周期主要是测试。

3. **Cycle 7（e2e 触达+确认路径）** — Task #15。
   - `tests/test_orchestrator_e2e.py`：把 `calls` 字典与 `Agents(...)`/`replace(base, ...)` 的 `writeback` 键/字段改为 `rewrite_loop`（行 58-66、75-83 附近）。
   - 加 `test_e2e_touched_confirmed_rewrite_lands_in_final_document`：注入 `judgment_llm=FakeJudgmentLlmClient(judge_factory=...)` 产 supported 假说 + `rewrite_llm=FakeRewriteLlmClient(propose_factory=...)` 产提议文本 + `hitl2_gate=FakeHitl2Gate(Hitl2Decision(DECIDE, [ConfirmRewriteOp(...)]))` → 断言终稿含确认文本、未触达段逐字节原文。
   - 既有触达路径测试（judgment verdicts → merge action）的 `out==doc` 由「ConservativeHitl2Gate 全驳回 + rewrite_loop 桩不提议」保字节一致（应仍通过）。

4. **Cycle 8（resume_rewrite 适配）** — Task #16。
   - `tests/test_orchestrator_resume.py` 全量重写：`Orchestrator().resume_rewrite(resolved_rewrites, original_paragraphs)` → 调 `assemble_final_document`。测试：确认段→提议文本、省略段→原文、幂等重跑同 bytes。删旧 `resume_writeback`/adopted/corrected/writeback_error 断言。

5. **Cycle 9+10（真实 adapter + run_real + 文档 + 全量质量门）** — Task #17。
   - `src/infra/llm_adapters.py` 加 `QwenRewriteLlmClient`（镜像 `QwenJudgmentLlmClient`，扁平信封或直 str 输出）。
   - `src/runtime/run_real.py`：`create_real_agents(..., rewrite_llm=QwenRewriteLlmClient(chat), ...)`。
   - `tests/test_real_llm_wiring.py`：离线装配断言 `agents.rewrite_loop` 字段存在/可注入（参考既有 judgment/hypothesis 装配测试模式）。
   - 文档同步：`docs/STATE.md`（§1 主表加 `proposed_rewrites` 行从 §1.2 迁入、`final_document` 写者改 hitl2；§1.2 标 Slice 6 落地；§3.1 节点速查表删 writeback 行加 rewrite_loop 行改 hitl2 行；§3.2 边界；§2 argument 字段表 status/issue_tags/adopted_hypothesis_id 写者去除 writeback，注 adopted/corrected 在新流程不再被写、domain 字段保留不删）；`docs/DEVELOPMENT.md` §1 拓扑+§2 模块表；`CONTEXT.md` 查漏 `resolved_rewrites`。
   - 全量：`ruff check src tests` + `mypy --strict src` + `pytest -q` 全过（基线曾 399 passed/6 skipped；当前因 writeback 测试已删 + 新测试已加，预期 ~380+ passed）。
   - 同步 `docs/pipeline-restructure-tasks.md` Slice 6 状态 ⬜→🟩 + 验收勾选（行 24 状态表 + 行 243-255 验收 checkbox）。

## 关键设计决定（已与用户确认，不可回退）

- **rewrite_loop 失败不碰 `argument_tree`**：新流程按段/文本工作，与 argument 的 status/merge_decision 解耦；`argument_tree` 在 judgment 之后仍是 judgment 唯一写者。失败段记 `[rewrite_loop] {pid}: ExcType: msg` 到 `errors` channel + 省略出 `proposed_rewrites` → 终稿回退原文 bytes。验收 #7「贴 writeback_error」按此等价实现（信号在 errors 日志 + 段回退原文，不写 argument_tree）。
- **hitl2 契约级重写**（非兼容）：argument 级 AdoptOp/RejectOp/EditContentOp → 段落级 ConfirmRewriteOp/EditRewriteOp/RejectRewriteOp。旧 `adopted`/`corrected`/`adopted_hypothesis_id`/`HYPOTHESIS_RELATION_TO_MERGE_ACTION` 机制在 domain.py 保留不删（新流程不写，minimizing domain churn）。
- **test_writeback.py 保留文件名**（验收 #8 字面「改/扩」），内容改写为 propose→confirm→assemble 纯函数子缝；不重命名为 test_final_assembly.py。
- **新拓扑**：`parse+partition → hitl1 → hypothesis_propose → retrieval → judgment → rewrite_loop → hitl2 → END`（7 stage；writeback 裁撤）。

## 已改文件清单（本会话）

新增：`src/agents/rewrite_loop/{__init__,contract,agent}.py`。
重写：`src/agents/hitl2/{__init__,contract,agent}.py`、`tests/test_hitl2.py`、`tests/test_writeback.py`、`tests/test_orchestrator_topology.py`。
修改：`src/agents/assembly.py`、`src/runtime/orchestrator.py`、`src/runtime/cli_gates.py`（仅 import 改好，类体待重写）。
删除：`src/agents/writeback.py`（`git rm`）。
未动：`tests/test_orchestrator_e2e.py`、`tests/test_orchestrator_fallback.py`、`tests/test_orchestrator_resume.py`、`tests/test_real_llm_wiring.py`、`src/infra/llm_adapters.py`、`src/runtime/run_real.py`（这些是后续周期的 RED/GREEN 目标）。

## 质量门基线

`ruff check src tests` + `mypy --strict src` + `pytest -q`。基线（本会话起点）：399 passed/6 skipped。当前因 writeback 测试删除 + 新测试加入，passed 数已降；全量 GREEN 在 Cycle 9+10 收尾。**不强制 `ruff format`、不重排既有文件**（既有缩进保持，PRD §29）。`ruff check --fix` 可用（5 个 fixable 当前是 organize-imports 类）。

## 建议技能

- `tdd`：本任务即 `/tdd` 纵切，继续 RED→GREEN 一周一实现。
- `codebase-design`：若需复核 rewrite_loop / hitl2 接口深度。
- `domain-modeling`：若文档同步时需校准 `CONTEXT.md` 统一语言。
- `code-review` 或 `simplify`：Cycle 9+10 全量 GREEN 后做一次收尾评审。
