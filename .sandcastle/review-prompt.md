# TASK

Review the latest commit on the shared branch `sandcastle/dev` and improve code clarity, consistency, and maintainability while preserving exact functionality.

This is a **LangGraph / Python** project (HypoArgus). Review against the project's actual invariants, not generic style. Before reviewing, read the relevant section of `prd_v2.0.md` and `docs/langgraph-dev-guide.md` so you know what "correct" looks like for the stage this issue touches.

> **你在非交互沙箱里、AFK（无人值守）状态下运行，禁止调用 `EnterPlanMode` / `ExitPlanMode`，禁止等待 plan 审批，禁止提问等待人类回复。**
> 你在 `--print` 模式下无人批准 plan——进 plan 模式即本次 review 空转、0 commit。要改就直接改、要提交就直接提交，遇到歧义自行基于文档与 commit 作最合理判断后继续，绝不阻塞等待。

# CONTEXT

## Latest commit (the issue just implemented) — stat

!`git show --stat HEAD`

## Full diff of the latest commit

!`git show HEAD`

## Commit log accumulated on this branch (main..HEAD)

!`git log main..HEAD --oneline`

# REVIEW PROCESS

1. **Understand the change** — read the diff above and the PRD section for the pipeline stage it touches.

2. **LangGraph-specific checks** (from `docs/langgraph-dev-guide.md`):
   - 控制流是否落在图的边 / 条件边上，而非靠 prompt 散文约束？（铁律）
   - 共享可变状态是否用了带 reducer 的 `Annotated[..., reducer]` state channel，而不是 `asyncio.Lock`？
   - 产出大输出的原文是否分流进 evidence key / `BaseStore`，`messages` 里只留摘要？
   - 结构化决策是否用 `with_structured_output` + Pydantic，而非手解析？
   - 有界收敛是否物化：单 worker 迭代上限、`safety_cap`、队列 `max_length` + 去重、`recursion_limit`？
   - partial 兜底：失败是否回填 + `emit` 警告 + `status`/`errors` 标注，绝不静默吞或伪装成功？
   - HITL `interrupt()` 是否配了 `checkpointer`、是否放在有副作用的代码之前？

3. **Project invariants (correctness hardline)**:
   - 分区不变式：段落切分后按序拼接逐字节等于原文（含空行、缩进、换行、末尾空格）。
   - 未被采纳改动命中的段落按 `paragraph_id` 从只读段落表逐字节拷回；只有命中段才重写 / 段尾追加。
   - 原文 bytes 永不整篇进任何 Agent 上下文；节点只带自身小段 + `paragraph_id` 指针。
   - 节点状态机非法变更一律拦截；`adopted → corrected` 回写幂等可重试。
   - 双轨合并按 12 格矩阵裁决，`credible + 对立 supported` 贴 `conflict` 交人判、不自动裁决。

4. **Python quality**:
   - Reduce unnecessary complexity and nesting; eliminate redundant code and abstractions.
   - Improve readability through clear variable and function names; consolidate related logic.
   - No commented-out code or TODO comments.
   - 类型注解齐全；避免滥用 `Any`、未校验的强转；避免假设 unchecked。
   - Are new/changed behaviours covered by `pytest`?
   - Choose clarity over brevity — explicit code is often better than overly compact code.

5. **Apply project standards** — follow @.sandcastle/CODING_STANDARDS.md.

6. **Preserve functionality** — never change what the code does, only how it does it. All original features, outputs, and behaviours must remain intact. The byte-level原文保护 invariants are functional correctness, not style — do not relax them.

# EXECUTION

If you find improvements to make:

1. Make the changes directly on this branch (`sandcastle/dev`).
2. Run `pytest` (and `ruff check .` / `mypy` if configured) to ensure nothing is broken.
3. Commit describing the refinements.

If the code is already clean and well-structured, do nothing.

Once complete, output <promise>COMPLETE</promise>.
