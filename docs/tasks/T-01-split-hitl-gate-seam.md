---
id: T-01
title: 拆分 HITL gate seam（review → formulate_question + parse_reply）
status: done
assignee: "Karlzzb"
blocked_by: []
covers_adr: ["0022"]
covers_prd: ["§10.4", "§7.2"]
layer: [graph, tests]
type: prefactor
---

# T-01 — 拆分 HITL gate seam

## Source

- PRD §10.4（CLI 与服务共用一套机制·两个驱动者）：`gate seam` 的 `review()` 拆为 `formulate_question()` + `parse_reply()`，服务注入 InterruptDrivenGate、CLI 注入 TerminalGate，均实现拆分后 Protocol。
- ADR-0022（侵入面 = gate seam + orchestrator 装配 + MANIFEST；业务纯函数不动）。
- 基线：`Hitl1Gate.review(self, argument_tree) -> Hitl1Decision`（`src/agents/hitl1/contract.py:169-177`）、`Hitl2Gate.review(self, review) -> Hitl2Decision`（`src/agents/hitl2/contract.py:149-158`）当前是**单一阻塞方法**，未拆分。

## What to build

把 HITL 闸门的单一 `review()` 调用拆成两段语义：

1. `formulate_question(...)`：据当前视图**构造问题**（hitl1 的分段结构、hitl2 的 `proposed_rewrites` 逐段待确认表）。
2. `parse_reply(...)`：把人工回复**解析**成 `Hitl*Decision`（`action` + `ops`，一期 `ops` 仍可空）。

拆分后 Protocol 形状为 `interrupt()` 暂停（服务，`formulate_question` 后挂起、`parse_reply` 在 resume 时喂回）与终端同步阻塞（CLI，`formulate_question` 后立刻 `input()` 再 `parse_reply`）提供**共同契约**。
本切片**只改 seam 形状与实现**：业务纯函数 `confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document` **不动**——它们仍只消费 `Hitl*Decision`。

实现要点（决策性边界，非逐文件实现指令）：

- `CliHitl1Gate` / `CliHitl2Gate`（`src/runtime/cli_gates.py:62` / `:166`）与 `FakeHitl1Gate` / `FakeHitl2Gate`（`src/agents/hitl1/contract.py:180-187`、`src/agents/hitl2/contract.py:161-168`）、`ConservativeHitl2Gate`（`src/agents/hitl2/contract.py:171-184`）全部改实现拆分后 Protocol。
- `confirm` / `confirm_partition` 内部对 gate 的单次调用改为「先 `formulate_question` 再 `parse_reply`」两步；一期 CLI / Fake 实现里两步可同步紧贴（`formulate_question` 后立即阻塞取回复再 `parse_reply`），行为与现状等价。
- 一期 `parse_reply` 输入仍是自由文本 / 假决策，产出 `action`-only `Hitl*Decision`（空 `ops`），结构化 `ops` 编辑推后（PRD §7.2 注）。
- `review()` 旧方法可保留为「`formulate_question` + `parse_reply` 组合」的兼容便捷方法（仅 CLI/Fake 同步场景用），但 Protocol 契约以拆分后两方法为准。

## Acceptance criteria

- [x] `Hitl1Gate` / `Hitl2Gate` Protocol 以 `formulate_question(...)` + `parse_reply(...)` 为契约（`review()` 保留为同步便捷包装——默认实现抛 `NotImplementedError`、仅同步 gate 覆写，不作为新代码依赖点）。
- [x] `CliHitl1Gate` / `CliHitl2Gate` / `FakeHitl1Gate` / `FakeHitl2Gate` / `ConservativeHitl2Gate` 全部实现拆分后 Protocol。
- [x] 业务纯函数 `confirm` / `confirm_partition` / `resolve_rewrites` / `assemble_final_document` 未改（`src/agents/hitl1/agent.py` 与 `src/agents/hitl2/agent.py` git diff 为空）。
- [x] 现有 e2e 测试（`tests/test_orchestrator_e2e.py`、`tests/test_hitl1.py`、`tests/test_hitl2.py`、`tests/test_real_llm_wiring.py` 等）改用拆分后 seam 仍全绿，行为等价（`review()` 全保真含 ops 路径不动）。
- [x] 质量门通过：`conda run -n HypoArgus ruff check` + `conda run -n HypoArgus mypy --strict` + `conda run -n HypoArgus pytest`。

## Verification（真实输出）

```
$ conda run -n HypoArgus ruff check .
All checks passed!

$ conda run -n HypoArgus mypy --strict src
Success: no issues found in 37 source files

$ conda run -n HypoArgus pytest -q
410 passed, 3 skipped in 20.30s
  # skipped：1× needs DASHSCOPE_API_KEY + network + tokens；2× test_writeback 样例不足两段（均 env/数据门控，非失败）
```

## 实现纪要（与 ADR-0022 对齐）

- **契约形状**：`formulate_question(view) -> Hitl*Question`（interrupt payload = 纯数据快照，不渲染、不阻塞）；`parse_reply(reply: Hitl*Reply) -> Hitl*Decision`（resume value → 决策）。`Hitl1Reply` / `Hitl2Reply` 一期承载 `action + text`，`parse_reply` 产 **action-only** 决策（空 `ops`），结构化 ops 编辑推后（PRD §7.2 注）——与 ADR-0022「一期 human_response 仅承载 action + 自由文本」一致，T-03 `InterruptDrivenGate` 可直接落地此 seam 形状。
- **`review()` 重定位**：从「单一契约方法」降级为**同步便捷包装**——Protocol 默认实现抛 `NotImplementedError`（异步 gate 经 `formulate_question` + `parse_reply` 驱动、不覆写 `review`），同步 gate（CLI/Fake/Conservative）覆写它、保持**全保真含 ops**（如 `FakeHitl2Gate` 仍返回注入的 `DECIDE + ConfirmRewriteOp` 决策，e2e `test_e2e_touched_confirmed_rewrite_lands_in_final_document` 由此仍绿）。业务纯函数仍只调 `gate.review()`，行为与现状等价。
- **`docs/graph_utils.py` 预存 lint 修复**：13 处 `UP006/UP035/UP045`（`List`→`list`、`Optional`→`X | None`、`typing`→`collections.abc`）经 `ruff check --fix` 修复（类型标注现代化、无功能改动、未跑 `ruff format` 不重排既有文件），使全仓 `ruff check .` 转绿。

## Blocked by

None — 可立即开始（无 Postgres / 无 web 依赖）。

## Notes

- 本切片是「make the change easy, then make the easy change」的前置：T-03 的 `InterruptDrivenGate`（服务）与 `TerminalGate`（CLI）都需要这个拆分 seam 才能落地。
- 拆分后两方法的入参 / 出参应与 ADR-0022 的「interrupt payload = `formulate_question` 产出、resume value = `parse_reply` 输入」对齐，避免 T-03 再改 seam 形状。
