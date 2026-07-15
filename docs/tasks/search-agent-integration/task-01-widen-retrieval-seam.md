# TASK-SA-1 — 拓宽 `RetrievalFn` seam 至 5 输入（+`paragraph_list`）

> 状态：已完成
> 阻塞：无（可立即开工；与 Slice 0 互不依赖、可并行）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Q2(α)、§User Story 6/20）
> 目标会话：任意新 session 读取本文件并执行。

## 任务概述

做 PRD §Q2 的"最小放宽"：retrieval 节点闭包增读 `state["paragraph_list"]`；`RetrievalFn` Protocol 增第 5 形参 `paragraph_list`。本切片只拓宽 seam、让桩与节点把 `paragraph_list` 穿下去，**不接真实后端**（真实适配器是 Slice 2）。桩仍返回空 citations，tracer bullet 不变。同步把契约文档与 STATE 速查从 4 输入改 5 输入（PRD：实现时改，勿提前描述未落地状态）。

决策形（PRD §Q2 原样）：

```python
def __call__(
    self,
    argument_tree: list[Argument],
    hypotheses: dict[str, list[Hypothesis]],
    query_time_range: TimeRange,
    session_context: SessionContext,
    paragraph_list: list[ParagraphRecord],   # 新增第 5 形参
) -> dict[str, list[Source]]: ...
```

## 验收标准

- [ ] `RetrievalFn` Protocol 增第 5 形参 `paragraph_list: list[ParagraphRecord]`（签名如上）。
- [ ] `_stub_retrieval` 增同名形参（读取/穿至 seam、不触发联网；docstring 仍述"产空 citations、真实后端后续切片接入"）。
- [ ] `_retrieval_node` build 闭包读 `state["paragraph_list"]` 并把它作为第 5 实参传给 `agents.retrieval(...)`（与 judgment 节点读 `paragraph_list` 同形）。
- [ ] 既有 4 形参的测试 fake（如 `tests/test_orchestrator_e2e.py` 的 `recording_retrieval`、`tests/test_orchestrator_fallback.py` 的 `throwing_retrieval`）改为 5 形参签名，断言桩能见到 `paragraph_list`。
- [ ] 新增/补一条断言：retrieval 节点把 `paragraph_list` 穿到 `RetrievalFn`（注入 recording fn 记录入参）。
- [ ] tracer bullet 不变：桩仍产空 citations、无触达段终稿逐字节等于原文（既有 e2e 测试守住）。
- [ ] `Source` schema / `_merge_dict` reducer / 拓扑 / citations 单写者契约 / `NodeFn` 同步签名 / manifest 装配范式 **全不动**。
- [ ] 契约文档 4→5 输入：`docs/contracts/retrieval-node.md` §2 输入表加 `paragraph_list` 行、签名行同步。
- [ ] STATE 速查 4→5 输入：`docs/STATE.md` §3.1 retrieval 行（约 L151）的输入列与签名列加 `paragraph_list`；`citations` channel 行（约 L69）的"当前伪代码桩"标签保留（真实后端仍未接）。
- [ ] 质量门全绿：`ruff check src tests` + `mypy --strict src` + `pytest -q`（conda env `HypoArgus`）。

## 实现指引（决策已锁定，勿偏离）

### 最小放宽边界（PRD §Q2）

- 只加 `paragraph_list` 一个形参。`forward target_text = ParagraphRecord.original_content` 的构造属 Slice 2 适配器，本切片不做。
- 适配器读 `state["original_doc"]` 做 `document_id` 指纹（PRD §Q9）属 Slice 2，本切片**不**改 `RetrievalFn` 再加形参（`original_doc` 在适配器侧自读 state，不走 Protocol 形参）。
- `Argument` 无文本字段是 ADR-0025 既成代价；`paragraph_list` 是补回段落原文的正典通道（judgment 节点已如此读）。

### 同形先例（对照，勿复制错形）

- judgment 的 `JudgmentLlmClient.judge(...)` 已是 6 输入、含 `paragraph_list`：`src/agents/judgment/contract.py:114-137`（`argument_tree, hypotheses, citations, paragraph_list, session_context, query_time_range`）。retrieval 拓宽后与其同 family。
- judgment 节点读 `paragraph_list` 的 build 闭包形态见 judgment 的 `_judgment_node`（同模块），retrieval 的 `_retrieval_node` 照搬"读 state channel、传第 5 实参"模式。

### 定位线索（来自探索，非 brittle 承诺）

- `RetrievalFn` Protocol：`src/agents/assembly.py:165-182`。
- `_stub_retrieval`：`src/agents/assembly.py:316-331`。
- `_retrieval_node`（当前读 4 channel、调 `agents.retrieval(...)` 4 实参、写 `citations` channel、`_guarded` 兜底空 citations）：`src/agents/assembly.py:625-655`。
- MANIFEST retrieval 条目 `real=None`：`src/agents/assembly.py:848-859`（本切片**不动** `real`，仍是 `None`）。
- `Agents.retrieval: RetrievalFn` 字段：`src/agents/assembly.py:239`。
- `PipelineState.paragraph_list`（reducer=`merge_paragraph_list`）：`src/runtime/orchestrator.py:139-162`。
- 既有 4 形参 fake：`tests/test_orchestrator_e2e.py:199-243`（`recording_retrieval`）；`tests/test_orchestrator_fallback.py:158-181`（`throwing_retrieval`）。
- `ParagraphRecord` 域模型（`original_content`/`summary`/`argument_tree_ids`）：与 judgment 侧同源，确认 import 路径与 judgment 一致。

### 文档"实现时改"（PRD §Further Notes）

- `docs/contracts/retrieval-node.md` §2 + `docs/STATE.md` §3.1 retrieval 行：4→5 输入。这两处 PRD 明确"实现时改"，本切片落地即改。
- `docs/DEVELOPMENT.md` §2/§3 的 retrieval 行（stub→seam 子包、`infra/` vendor 目录）属 Slice 4，本切片不改。

## 质量门

```bash
conda run -n HypoArgus ruff check src tests
conda run -n HypoArgus mypy --strict src
conda run -n HypoArgus pytest -q
```

## 状态追踪

| 日期 | 会话/执行者 | 状态变更 | 备注 |
|---|---|---|---|
| 2026-07-15 | Claude（TDD / dev/manifest-assembly） | 未开始 → 进行中 → 已完成 | Protocol/桩/节点 4→5 形参（`paragraph_list` 末位，PRD §Q2 锁定）；e2e recording_retrieval 5 形参 + 断言穿参；fallback throwing_retrieval 5 形参；契约 doc + STATE §3.1 4→5 输入；ruff/mypy/pytest 全绿。 |

## 完成检查

- [x] 验收标准全勾选。
- [x] 质量门全绿。
- [x] 契约文档与 STATE 速查已 4→5 输入。
- [x] 更新 `INDEX.md` 状态为 `已完成`。
