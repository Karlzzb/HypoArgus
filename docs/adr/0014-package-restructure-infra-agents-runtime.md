# ADR-0014：包重构为 src/ 扁平 + infra/agents/runtime 子包，manifest 驱动装配

## 状态

已接受（2026-07-12）

## 背景

原包布局为单一平铺 `src/hypoargus/*.py`，18 个模块挤在同一命名空间。
该布局带来三处摩擦：

1. **加 Agent 的仪式过重**：新增一个 Agent 需触动 7 处——新模块 + `Agents` dataclass 字段 + `create_stub_agents` 存根 + `create_real_agents` 分支 + `_X_node` + `StageSpec` 条目 + `__init__` re-export + 测试。
   装配逻辑散落，无 locality。
2. **Agent 物理不隔离**：每个 Agent 的 seam（Protocol + Fake）与其纯函数逻辑混在同一文件，无法作为自洽单元被独立 import 与调测。
3. **基础设施无处生根**：移植自 DeepTutor 的工具/历史等基础设施 seam 无归宿，只能继续堆在平铺根目录。

`src/hypoargus/` 两层命名（仓库 `HypoArgus` → `src/` → `hypoargus/`）被判定为冗余嵌套。
但去掉包名目录意味着顶层裸名导入（`import domain` / `from agents.parser import run`），存在与 PyPI 同名包碰撞的风险。

## 决策

1. **去掉 `hypoargus/` 目录，`src/` 直接作为包根**：`package-dir = {"": "src"}`，模块以裸名顶层导入。
   顶层名（`domain`、`partition`、`agents`、`runtime`、`infra` 等）碰撞风险**已知并接受**——本项目是隔离安装的应用而非库，碰撞不构成阻塞。
2. **domain 核心平铺留 `src/` 根**：`domain` / `partition` / `raw_store` / `status_machine` / `tree_invariants` 保持顶层模块。
   它们是被全部测试与全部 Agent 依赖的共享真相源，平铺避免给共享依赖套子包前缀。
3. **三子包承载非领域逻辑**：
   - `infra/`：基础设施 seam（工具协议、历史 seam、检索协议与适配器）。
   - `agents/`：每个 Agent 一个自洽单元。
     有 seam 的 Agent（`parser`/`verification`/`hypothesis`/`hitl1`/`hitl2`）拆为子包 `agents/<name>/{contract.py, agent.py, __init__.py}`——`contract.py` 放 Protocol + Fake，`agent.py` 放纯函数。
     纯函数 Agent（`merge`/`impact`/`consistency`/`writeback`）保持单模块 `agents/<name>.py`。
   - `runtime/`：`orchestrator.py`（主图 + `PipelineState` + `StageSpec` + `default_pipeline`）、`tool_registry.py`、agent manifest 装配。
4. **manifest 驱动装配，保 typed `Agents`**：`agents/assembly.py` 遍历 manifest 条目（stub 工厂 + real 工厂 + deps + stage build）构造 typed `Agents` dataclass 与 `default_pipeline`。
   加 Agent 的触点从 7 降至 3（新子包 + `Agents` 字段 + manifest 条目）。
   放弃动态 `dict[str, AgentEntry]` registry 方案——后者虽能把触点降到 2，但会令 `agents.parse` 失去 typed access，在 `mypy --strict` 项目中得不偿失。
5. **`__init__.py` 兼容 shim 弃用**：裸名导入下无 `hypoargus` 包可作 re-export 容器，测试 import 一次性重写为裸名。

## 权衡

- 顶层裸名换来一层嵌套的消除与导入路径的简洁，代价是命名空间不再受包名保护。
   选择接受该风险，理由是应用隔离安装、且 `domain`/`partition` 等名在本项目语境下语义自洽。
   记录此理由以免后续架构评审重复提议「回归 `hypoargus.` 命名空间」。
- 保留 typed `Agents` dataclass 而非全动态 registry：牺牲一个触点（3 vs 2）换取 `mypy --strict` 类型安全与 IDE 跳转。
   在本项目质量门（`mypy --strict`）下，类型安全优先于极致去仪式。
- Agent 子包仅对有 seam 的 Agent 拆分：纯函数 Agent 不强造子包，遵循 deletion test——1 文件纯函数套 3 文件子包是浅仪式。

## 影响

- 物理重构：纯文件搬移 + import 重写 + `pyproject` 调整，零逻辑变更，以字节级还原 e2e 为锚。
- manifest 装配与各 seam 直落终位，无需二次搬迁。
- 全部测试 import 由 `hypoargus.X` 重写为裸名 `X` / `agents.X` / `runtime.X` / `infra.X`，属机械改动。
- `ADR-0005`（两层存储）与 `ADR-0010`（HITL-2 硬闸门）不受影响——本 ADR 只动物理布局与装配机制，不动领域不变量。
