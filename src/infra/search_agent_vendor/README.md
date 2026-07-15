# Vendored SearchAgent V12（检索子智能体）

本目录是 SearchAgent V12 子智能体的 vendored third-party 源码，作真实检索后端的物理落点
（PRD §Q3/B1：真实后端放 `infra/` 层）。

## 来源

源码从 `standby_agents/SearchAgent_V12_Delivery_v2/dist/search_agent-0.3.0-py3-none-any.whl`
解出（standby 源目录是 *不完整* 的抽取——缺 `base.py` / `retrieval.py` /
`evidence_retrieval/semantic_validation.py` 等，故以 wheel 为准正典完整源码）。
wheel 是纯 Python（`py3-none-any`），解出的 `.py` 即源码，可审计。

## 导入结构

V12 内部全用相对导入（`.` / `..` / `...`），无绝对 `from evidence_retrieval...`。
故只需 `search_agent` 为顶层可导入包；`evidence_retrieval` / `providers` / `edre` 是其
子包，随相对导入解析。`pyproject.toml` 的 `[tool.setuptools.packages.find]` 用
`where = ["src", "src/infra/search_agent_vendor"]` 让 `search_agent` 在本目录下被发现为
顶层包。

## 质量门 carve-out

vendored 源码**排除出** `ruff check` / `mypy --strict`（见 `pyproject.toml` 的
`[tool.ruff] exclude` 与 `[tool.mypy] exclude`）。新增适配器代码（Slice 2，
`src/agents/retrieval/`）才全量进严格门。

## 运行时契约要点（供 Slice 2 适配器）

- `SearchAgentRuntime.from_env(with_llm=False)` → `EvidenceRetrievalDependencies.defaults`：
  构造 4 个 httpx client（loop 在首请求时才绑，loop-affine）+ 确定性 judge，不调 LLM、不发请求、
  不需 VOLCANO/BISHENG 凭证。凭据校验在请求期才触发。
- `ainvoke` 是 async；`aclose` 是 async。
- `load_env()` 在 `search_agent/__init__.py` 导入期执行（读包内 `.env`；本仓不提交 `.env`，
  故为 no-op；进程环境变量优先）。
- 环境变量见 `search_agent.env.example`（`LLM_*` / `VOLCANO_*` / `BISHENG_*` / `LANGFUSE_*` /
  `EVIDENCE_RETRIEVAL_*` 调参），与主仓 `DASHSCOPE_API_KEY` 不冲突。

## 不改 V12 源码

PRD 硬约束：vendor 源码原样，不改 V12 内部逻辑。脱敏 / 审计 / 映射在 Slice 2 适配器侧承载。
