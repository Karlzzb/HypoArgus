# TASK-SA-4 — 文档：ADR-0026 + CONTEXT 术语 + DEVELOPMENT B1

> 状态：已完成
> 阻塞：Slice 2（真实适配器已落地，文档才描述既成状态而非未落地状态）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Further Notes、§User Story 18/19/21）
> 目标会话：任意新 session 读取本文件并执行。

## 任务概述

迁移决议全定后一次性写齐文档（PRD：勿写半成品）。写 ADR-0026 捕获本次迁移全部架构决议（含 PRD §6 白名单偏差）；`CONTEXT.md` 增"检索子智能体 (SearchAgent)"glossary 条目；`docs/DEVELOPMENT.md` §2/§3/§4/§8.1 从 inline 桩描述改为 seam 子包 + `infra/` vendor 目录（B1）。`docs/contracts/retrieval-node.md` 与 `docs/STATE.md` 的 4→5 输入已在 Slice 1 改，本切片复核并补真实后端落地后的描述。

## 验收标准

- [x] **ADR-0026**（编号续 0025，`docs/adr/0026-*.md`，标题自拟如 `real-retrieval-backend-via-searchagent-v12`）写齐，覆盖：
  - Q1（挂 retrieval seam、否 replace-judgment 拓扑、`with_llm=False` 丢 verdict、judgment 重判）。
  - Q2（retrieval 读 `paragraph_list` 的最小放宽、forward `target_text=original_content` 的 ADR-0025 代价、`RetrievalFn` 5 输入）。
  - Q3/B1（vendor 决定 + B1 落地路径：seam 子包全量 strict、vendor carve-out 排除、真实后端放 `infra/` 层）。
  - Q4（daemon worker loop、零框架改动、loop-affine 纠偏、进程级单例、`atexit` 尽力 `aclose`）。
  - Q5（domain whitelist 作废的已记录 PRD §6 偏差 + PII 脱敏/可溯源审计重承载 + 结构化靠 V12 自承 + KB 靠 Bisheng 服务端鉴权）。
  - Q6（`CitationRecord→Source` 映射表、`snippet←content`、全 ACCEPTED+DEGRADED 映射）。
  - Q7（`required_slots` 传空）/ Q8（`argument_context` 传空）/ Q9（`request_id`/`document_id`(blake2b)/`user_id` 映射）。
  - 满足三准则（难逆 / 无上下文会困惑 / 真实权衡）。
- [x] ADR-0026 格式遵循 `domain-modeling` skill 的 ADR-FORMAT（参照 `docs/adr/0025-...`：`# ADR-NNNN：…` / `## 状态`（日期、取代关系、配套 PRD + 任务链接）/ `## 背景` / `## 决策` / `## 权衡` / `## 影响`）。
- [x] **`CONTEXT.md`**：`## 智能体角色`（约 L37）下增"检索子智能体 (SearchAgent)"条目：真实检索后端、挂 retrieval `real`、`with_llm=False`（确定性 judge 免费）、verdict 丢弃、judgment 重判、domain whitelist 作废（PRD §6 已记录偏差）、脱敏/审计由 seam 重承载。
- [x] **`docs/DEVELOPMENT.md` §2**：retrieval 从"inline 桩、真实后端 Out of Scope"改为 seam 子包（`src/agents/retrieval/`）+ `infra/` vendor 目录（B1）；infra 表 retrieval 行去掉"Out of Scope"标签、指向新 adapter；agents 表加 `agents/retrieval/{contract,agent}.py` 子包行。
- [x] **`docs/DEVELOPMENT.md` §3**：seam 一览表"检索层"行的"真实后端"列从"待接"改为已落地；新增 retrieval `real=` 矩阵行（§4 装配矩阵补 retrieval 行，与 judgment 同形）。
- [x] **`docs/DEVELOPMENT.md` §8.1**：去掉"retrieval 节点仍为桩"措辞，改为真实后端已接、citations 非空、judgment 据之判终态。
- [x] **`docs/contracts/retrieval-node.md` + `docs/STATE.md` §3.1**：复核 Slice 1 已改的 5 输入，补"真实后端已落地"描述（`citations` channel 行的"当前伪代码桩"标签改为真实后端）。
- [x] 所有内部交叉链接有效（ADR↔PRD↔任务、ADR↔STATE↔CONTEXT）。
- [x] 质量门全绿；文档改动不引入 lint/type/test 回归。

## 实现指引（决策已锁定，勿偏离）

### "实现时改"原则（PRD §Further Notes）

- `docs/contracts/retrieval-node.md` 与 `docs/STATE.md` 的 4→5 输入：Slice 1 已改；本切片只复核 + 把"桩"标签翻为"真实后端"。
- `docs/DEVELOPMENT.md` §2/§3：PRD 明确"实现时改"，Slice 2 落地后本切片改。
- ADR-0026：PRD 明确"决议全定后一次性写，勿写半成品"——Slice 2 落地后决议已成既成事实，本切片写。

### PRD §6 偏差记录（Q5）

"PRD §6"是 `src/infra/retrieval.py` docstring 对未纳入仓库的外部 PRD 的引用（仓内无 PRD 文件；`PRD §X` 代码引用指向外部 PRD，doc-sync 时勿 scrub——见 memory `prd-section-refs-convention`）。domain whitelist 作废作为"已记录的 PRD §6 偏差"写进 ADR-0026 + CONTEXT。

### ADR 三准则自检

- **难逆**：vendor 入仓 + carve-out + daemon worker loop 是难逆结构决议。
- **无上下文会困惑**：未来读者会问"为何不 route-through 框架合规层""为何不用 `asyncio.run` 桥接""为何 domain whitelist 作废"——ADR 显式记录否掉理由。
- **真实权衡**：`with_llm=False` 丢 verdict 换零成本但损失子智能体数值层（Q7/Q8 传空降级）；daemon loop 零框架改动但进程退出无正经 drain（与既有先例同形，(ii) 留作未来 ops 升级位）。

### 定位线索（来自探索，非 brittle 承诺）

- ADR 目录：`docs/adr/`（`0001`–`0025`，`0026` 空闲）；格式参照 `docs/adr/0025-paragraph-as-aggregate-root.md`。
- CONTEXT glossary：`CONTEXT.md` `## 智能体角色`（约 L37）。
- DEVELOPMENT §2 模块地图（含 retrieval inline 桩描述）：`docs/DEVELOPMENT.md:165-207`；§3 seam 一览：`:219-233`；§4 装配矩阵：`:235-256`；§8.1 接入真实 LLM provider：`:292-303`。
- 契约文档：`docs/contracts/retrieval-node.md` §2/§3；STATE：`docs/STATE.md` §3.1 retrieval 行（约 L151）+ `citations` channel 行（约 L69）。
- 母 PRD：`docs/prd-search-agent-integration.md`；任务索引：`docs/tasks/search-agent-integration/INDEX.md`。

## 质量门

```bash
conda run -n HypoArgus ruff check src tests
conda run -n HypoArgus mypy --strict src
conda run -n HypoArgus pytest -q
```

## 状态追踪

| 日期 | 会话/执行者 | 状态变更 | 备注 |
|---|---|---|---|
| 2026-07-16 | Claude (TDD / Slice 4) | 未开始 → 进行中 | 开工；探索 landed 实现（commits a6d0dc1/4d5bd51/c5a5d3d/184d39b）。 |
| 2026-07-16 | Claude (TDD / Slice 4) | 进行中 → 已完成 | ADR-0026 + CONTEXT 术语 + DEVELOPMENT §2/§3/§4/§8.1 + contracts/retrieval-node + STATE §3.1 翻桩→真实后端；ADR README 索引移位；INDEX 标已完成。 |

## 完成检查

- [x] 验收标准全勾选。
- [x] ADR-0026 覆盖 Q1–Q9 + B1 + PRD §6 偏差、过三准则自检。
- [x] CONTEXT / DEVELOPMENT / contracts / STATE 全部描述既成状态、无"未落地"残留。
- [x] 交叉链接有效。
- [x] 更新 `INDEX.md` 状态为 `已完成`（整条迁移收尾）。
