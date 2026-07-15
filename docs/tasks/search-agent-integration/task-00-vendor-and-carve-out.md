# TASK-SA-0 — vendor SearchAgent V12 + carve-out + deps + 可导入

> 状态：已完成
> 阻塞：无（可立即开工；与 Slice 1 互不依赖、可并行）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Q3/B1、§质量门与环境）
> 目标会话：任意新 session 读取本文件并执行。

## 任务概述

把暂存于 `standby_agents/SearchAgent_V12_Delivery_v2/` 的子智能体源码 vendoring 入仓，作为真实检索后端的物理落点（PRD B1：真实后端侧放 `infra/` 层）。vendored 源码作 vendored third-party 排除出 `mypy --strict` / `ruff`；新增适配器代码（Slice 2）才进严格门。子智能体自带依赖加进 `pyproject.toml`，使 vendor 后能真实安装与运行。本切片不写任何适配器逻辑，只把"能 import、能构造、质量门不炸"这三件事做实。

## 验收标准

- [x] `standby_agents/SearchAgent_V12_Delivery_v2/` 的源码已 vendoring 入仓（standby 区 `.gitignore` 忽略；vendored 树入 `src/infra/search_agent_vendor/search_agent/` 纳入版本控制）。
- [x] vendored 源码以 `search_agent` 顶层包可导入；`evidence_retrieval` 为其子包 `search_agent.evidence_retrieval`（**实现纠偏**：V12 内部全用相对导入 `.`/`..`/`...`，无绝对 `from evidence_retrieval...`；`top_level.txt` 仅 `search_agent`。故只需 `search_agent` 顶层，任务原述"两个顶层包+绝对导入"系误读，已按代码实际落地）。
- [x] `conda run -n HypoArgus python -c "from search_agent.api import SearchAgentRuntime; SearchAgentRuntime.from_env(with_llm=False); print('ok')"` 不抛异常（已验证；凭据校验在请求期才触发，`defaults()` 构造 4 个 httpx client 不发请求）。
- [x] `pyproject.toml` 已加齐 V12 依赖，无破坏性冲突（`langfuse` 3.0→`>=4.14` 实升 4.13.2→4.14.0、`httpx` 升为 core、`langchain>=1.3`/`langchain-anthropic>=1.4`/`sentence-transformers>=3,<4`(torch 已在 env)/`pypdf>=4`/`jinja2`/`requests`/`ddgs`/`python-dotenv` 新增；langfuse 调用方 `infra.observability`/`api_layer.langfuse_wrap` 导入验证不破）。
- [x] ruff/mypy 已加 carve-out 排除（`[tool.ruff] exclude = ["src/infra/search_agent_vendor"]`、`[tool.mypy] exclude = ["src/infra/search_agent_vendor/"]`），vendored 91 文件不进严格门（mypy 报 54 源文件全绿）。
- [x] 质量门全绿：`ruff check src tests`（All checks passed）+ `mypy --strict src`（no issues, 54 files）+ `pytest -q`（651 collected，详见落地记录）。
- [x] 既有的、非 vendored 的 `src/` 代码未被重排（`ruff format` 未强制；未改既有文件格式）。

## 实现指引（决策已锁定，勿偏离）

### 物理落点（PRD §Q3/B1）

- 真实后端放 `src/infra/` 层，与既有 `QwenJudgmentLlmClient`（`src/infra/llm_adapters.py`，在 infra、不在 agents）同层，填 `src/infra/retrieval.py` 注释里预留的"真实后端后续切片接入"槽。
- seam 侧（`src/agents/retrieval/` 子包）属 Slice 2，本切片不动。

### 导入结构约束（关键，易踩坑）

V12 是自洽包，内部绝对导入 `evidence_retrieval.*` 与相对导入 `.tracing`/`.env` 并存。standby 目录的 `pyproject.toml` 用 `"search_agent" = "."` 把目录本身装成 `search_agent` 包，`evidence_retrieval/` 作为顶层包一并安装。vendoring 入 `src/infra/` 时不能破坏这两条导入路径。推荐方案（任选其一，落地后在本任务记录实际选择）：

1. 在 `src/infra/` 下建一个 vendoring 子目录（如 `src/infra/search_agent_vendor/`），把 standby 的全部源码（`api.py`/`tracing.py`/`env.py`/`config.py`/`__init__.py` + `evidence_retrieval/`）原样放进去；在 `pyproject.toml` 的 `[tool.setuptools]`/`[tool.mypy]` 里把这个 vendoring 目录加到 `package-dir`/`mypy_path`/`pythonpath`，使 `search_agent` 与 `evidence_retrieval` 仍为顶层可导入；ruff/mypy exclude 这个 vendoring 子树。
2. 直接装 standby 目录打的 wheel（`standby_agents/.../dist/search_agent-0.3.0-py3-none-any.whl`）到 conda env，源码仍 vendoring 入仓供审计但运行时走已装包。

PRD 明确要 vendoring 源码入仓（不只装 wheel），故方案 1 为正典；方案 2 的 wheel 仅作 fallback。

### carve-out 排除（当前无先例，需新增）

仓库现状：`[tool.ruff]`/`[tool.mypy]` 均无 `exclude`；质量门靠命令行路径 `ruff check src tests` / `mypy --strict src` 圈定范围。vendoring 进 `src/` 后会被扫到，故必须显式 exclude。参考形态（最终目录名以方案 1 落地为准）：

```toml
[tool.ruff]
exclude = ["src/infra/search_agent_vendor"]   # 实际目录名对齐
[tool.mypy]
exclude = ["src/infra/search_agent_vendor/"]
```

### 依赖合并（PRD §质量门与环境、§子智能体自带依赖）

standby `pyproject.toml` 声明（version `0.3.0`，`requires-python>=3.11`）：

- `langgraph>=1.2,<1.3` — 主仓已有 `langgraph>=1.2`，相容，保留主仓上界或就宽。
- `langchain>=1.3,<1.4` / `langchain-openai>=1.3,<1.4` / `langchain-anthropic>=1.4,<1.5` — 主仓有 `langchain-core>=0.3`、`langchain-openai>=0.2`；需对齐到 V12 要求的较新版本。
- `langfuse>=4.14,<5` — **冲突点**：主仓 core 依赖 `langfuse>=3.0`。升到 `>=4.14`，并跑现有 langfuse 调用方确认不破（`api_layer/server.py` 的 `wrap_langfuse_handler(build_langfuse_callback(), ...)` 等）。
- `jinja2` / `requests` / `ddgs` / `python-dotenv` — 新增。
- `sentence-transformers>=3,<4` — 新增（重型依赖，确认 conda env 装得动）。
- `httpx>=0.27,<0.29` — 主仓当前仅在 dev 依赖有 `httpx>=0.27`；**升为 core 依赖**（V12 四个 httpx client 都要）。
- `pydantic>=2.7,<3` — 主仓 `pydantic>=2.0`，收紧到 `>=2.7` 一般无碍。
- `pypdf>=4` — 新增。

落地后 `conda run -n HypoArgus pip install -e .` 能装齐，且 `conda run -n HypoArgus python -c "import langfuse, langgraph, sentence_transformers, httpx, pypdf, ddgs, jinja2"` 全绿。

### 定位线索（来自探索，非 brittle 承诺）

- standby 源：`standby_agents/SearchAgent_V12_Delivery_v2/`（仓内 `.gitignore` 已忽略 `standby_agents/`，故移走即纳入版本控制）。
- 主仓 `pyproject.toml`：依赖 `pyproject.toml:9-21`、dev extras `:24-43`、pytest `:62-67`、ruff `:68-74`、mypy `:76-81`。
- `infra/retrieval.py` "真实后端后续切片接入"槽：`src/infra/retrieval.py:11-12`（module docstring）。
- 既有 langfuse 调用方：`src/api_layer/server.py:113-160`（`serve` 里 `wrap_langfuse_handler(build_langfuse_callback(), ...)`）。

## 质量门

```bash
conda run -n HypoArgus pip install -e .                          # 装齐 vendored 依赖
conda run -n HypoArgus ruff check src tests
conda run -n HypoArgus mypy --strict src
conda run -n HypoArgus pytest -q
conda run -n HypoArgus python -c "from search_agent.api import SearchAgentRuntime; SearchAgentRuntime.from_env(with_llm=False); print('ok')"
```

## 落地记录（实际选择，供 Slice 2 引用）

### vendoring 源：以 wheel 为正典（standby 源不完整）

探索发现 `standby_agents/SearchAgent_V12_Delivery_v2/` 源目录是**不完整抽取**——缺
`base.py` / `retrieval.py` / `evidence_retrieval/semantic_validation.py` 等，而
`evidence_retrieval/__init__.py:32` `from .semantic_validation import ...` 会炸，导致
`from search_agent.api import SearchAgentRuntime` 根本无法导入。故改以
`standby_agents/.../dist/search_agent-0.3.0-py3-none-any.whl`（纯 Python `py3-none-any`，
解出的 `.py` 即源码、可审计）为正典完整源，解出其 `search_agent/` 包树入仓。

### 导入方案：方案 1 变体——`find` 多 `where`，非 `package-dir` 显式映射

落地 `src/infra/search_agent_vendor/search_agent/`（保留 wheel 的 `search_agent/` 顶层子目录，
非 standby 的 `package-dir "." = 根` 形态）。`pyproject.toml` 用：

```toml
[tool.setuptools.packages.find]
where = ["src", "src/infra/search_agent_vendor"]
```

第二 `where` 让 `search_agent`（+ `search_agent.evidence_retrieval` / `.providers` / `.edre`
等子包）在该目录下被发现为顶层包；`src/` 仍发现主仓裸包。容器目录
`src/infra/search_agent_vendor/` **无 `__init__.py`**（非包），故 `src/` find 不下钻、无
`infra.search_agent_vendor` 幻影包、无重复发现。V12 内部全相对导入，故 `search_agent` 顶层
即可，无需 `evidence_retrieval` 顶层。

附 `src/infra/search_agent_vendor/search_agent.env.example`（operator 凭据参考）+
`README.md`（来源/导入/契约/carve-out 说明）。

### 依赖合并落地值

`pyproject.toml` core deps 实际写入：`langgraph>=1.2`、`pydantic>=2.7`、
`langchain-core>=1.3`、`langchain>=1.3`、`langchain-openai>=1.3`、`langchain-anthropic>=1.4`、
`langfuse>=4.14`、`httpx>=0.27`、`jinja2`、`requests`、`ddgs`、`python-dotenv`、
`sentence-transformers>=3,<4`、`pypdf>=4`、`pyyaml>=6.0` + 既有 T-04 web/PG 依赖。dev extras
同步。env 实装：langfuse 4.13.2→4.14.0、langchain-anthropic 1.4.8、
sentence-transformers 3.4.1（torch 2.11.0 已在 env，无额外重型下载）。**不加上界**（就宽，
与主仓既有无上界风格一致；当前实装版本满足 V12 下界）。

### carve-out

`[tool.ruff] exclude = ["src/infra/search_agent_vendor"]`；
`[tool.mypy] exclude = ["src/infra/search_agent_vendor/"]`。验证：mypy `--strict src` 报
"54 source files"（vendored 91 文件已排除）；ruff `check src tests` 全绿。

### 烟雾测试

新增 `tests/test_search_agent_vendor_smoke.py`（默认质量门跑，非 `real_llm`）：断言
`search_agent` 顶层可导入 + `evidence_retrieval` 子包 public_contracts 可取，且
`SearchAgentRuntime.from_env(with_llm=False)` 无凭证构造。2 passed。

### 附带修复（pre-existing，非本切片引入）

`tests/test_streaming_fake_llm.py`（T-07）引用的 `e2e/_fakes.py` + `e2e/dev_server.py` 在
`44bc155 基础测试完成` 被删但测试未删，致 pytest 收集报错、整门变红（早于本切片即红）。
经用户确认，从 `c8be31c` 恢复 `e2e/_fakes.py` + `e2e/dev_server.py`，收集恢复（651 collected）、
该测试 7.25s 通过（用 `InMemorySessionCache`，非 PG）。

## 状态追踪

| 日期 | 会话/执行者 | 状态变更 | 备注 |
|---|---|---|---|
| 2026-07-15 | claude (TDD SA-0) | → 进行中 | 开工：vendoring 入仓 + carve-out + deps |
| 2026-07-15 | claude (TDD SA-0) | → 已完成 | wheel 为正典源、find 多 where、carve-out、deps、smoke 测试；附带恢复 e2e/ 修 pre-existing 收集阻塞 |

## 完成检查

- [x] 验收标准全勾选。
- [x] 质量门全绿。
- [x] vendoring 落点与导入方案已在本文件记录（供 Slice 2 引用）。
- [x] 更新 `INDEX.md` 状态为 `已完成`。
