# TASK-SA-3 — 真实 V12 全链集成测试（`real_llm` 标记）

> 状态：已完成（代码交付 + 离线门全绿；真实凭证全链冒烟因本机缺凭证未跑，见下方「状态追踪」与「完成检查」）
> 阻塞：Slice 2（真实适配器已落地、可注入真 runtime）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Testing Decisions、§User Story 16/17）
> 目标会话：任意新 session 读取本文件并执行。

## 任务概述

为真实 V12 检索全链（Volcano/Bisheng 真实联网）加慢集成测试，遵循既有 `real_llm` 标记套件范式：带 `real_llm` 标记、模块级 `skipif` 凭证/网络缺失即跳、默认质量门不跑（CI 不被网络/凭证卡住）、真实链路有覆盖。测试驱动真实检索 → citations 非空 → judgment 据真实证据判终态。本切片只加测试，不改适配器（Slice 2 已落地）。

## 验收标准

- [x] 新增测试文件（如 `tests/test_real_retrieval_e2e.py`），模块级 `pytestmark` 带 `real_llm` 标记 + `skipif(not _HAS_KEYS, reason="needs VOLCANO_*/BISHENG_* + network")`。
- [x] 凭证探测键：`VOLCANO_SEARCH_API_KEY`（与 V12 `load_env` 读的 `VOLCANO_*` 一致）+ Bisheng 的 `BISHENG_TOKEN`/`BISHENG_BASE_URL`（按 V12 `evidence_retrieval/config.py` 实际读取的 env 名对齐）。
  - 落地说明：`BISHENG_TOKEN` 在 V12 `.env.example` 标注「自定义接口，无需认证」恒为空，故有效探测键取 `BISHENG_BASE_URL`；另加 `DASHSCOPE_API_KEY`（下游 judgment/解析/开药 LLM 必需，验收标准 §4 要求 judgment 据真实证据判终态）。探测键集 = `DASHSCOPE_API_KEY` + `VOLCANO_SEARCH_API_KEY` + `BISHENG_BASE_URL`。
- [x] 测试驱动真实 V12 全链：从 `markdown/` 真实论文取段（参考 `tests/real_papers.py` 的 `REAL_PAPER_CASES`），跑 retrieval 节点（真 runtime 注入），断言 `citations` channel 非空、`Source` 字段可溯源（`origin`/`locator` 非空）。
  - 落地：`test_real_retrieval_produces_nonempty_traceable_citations`，选最小论文 `paper_03_集成电路工程技术专业`（~6.6KB），经 `create_real_agents(retrieval_runtime=lazy_search_agent_runtime())` + `orch.graph.invoke` 跑全链，断言 citations 非空 + 至少一条 `origin`/`locator` 双非空（宽松「至少一条」容 Bisheng 内网降级）。
- [x] 断言下游 judgment 据真实 `citations` 判终态（非空裁决、非永远 KEEP），即"真实后端使 judgment 见真实素材"成立。
  - 落地：`test_real_retrieval_drives_nontrivial_judgment`，断言 `state["hypotheses"]` 至少一条假设脱离 `pending`（落 `supported/doubtful/refuted`）。两用例共享模块级 fixture 跑一次全链（控成本，PRD §Q4 reuse）。
- [x] tracer bullet 守护：真实后端未配置/未触达的对照路径终稿逐字节等于原文（PRD story 17）。
  - 落地说明：由既有 `tests/test_real_llm_pipeline_e2e.py`（Slice 2 已把 `lazy_search_agent_runtime` 接入 `run_real_pipeline`，凭证齐时该测试亦 exercising 真实检索；未触达/未配置 → 终稿逐字节等于原文）与 `tests/test_retrieval_adapter.py::test_tracer_bullet_real_adapter_empty_output_keeps_byte_identity` 共守，本文件模块 docstring 显式声明不重复。
- [x] 离线默认质量门不被卡：`pytest -m "not real_llm" -q` 全绿，新测试默认 deselected。
  - 落地：661 passed / 4 skipped / **13 deselected**（原 11 + 新 2）。
- [x] 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`（离线门）全绿。
  - 落地：ruff `All checks passed!`；mypy `Success: no issues found in 57 source files`；新文件 `pytest -q` → 2 skipped（凭证缺）。

## 实现指引（决策已锁定，勿偏离）

### `real_llm` 范式（既有先例，照搬形态）

- 标记注册：`pyproject.toml:62-67` 的 `[tool.pytest.ini_options] markers` 已含 `real_llm`；V12 全链测试复用同一标记（不新增 marker）。
- 模块级闸门范式：`tests/test_real_llm_parse.py:24-28` 与 `tests/test_real_llm_pipeline_e2e.py:25-28` 的 `_HAS_KEY = bool(os.environ.get("..."))` + `pytestmark = [pytest.mark.real_llm, pytest.mark.skipif(not _HAS_KEY, ...)]`。V12 测试改凭证键即可同形。
- 真实论文驱动：`tests/real_papers.py` 的 `REAL_PAPER_CASES`（`markdown/*.md`，9 篇，缺目录自然零用例不报错）；`conftest.py:42-52` 暴露 parametrized `real_paper` fixture。
- 真实 chat model fixture 范式：`tests/test_real_llm_parse.py` 的 `@pytest.fixture(scope="module") real_chat_model`（`build_qwen_chat_model(timeout=120.0, max_tokens=8192)`）+ resilient retry across DashScope 瞬态。
- 已知瞬态坑：既有真实套件 ~20–40min、DashScope 瞬态需 retry；V12 全链叠加 Volcano/Bisheng 网络瞬态，测试要对网络抖动容错（重试/宽松断言），并在本文件记录已知瞬态坑与修复（如既有 memory `real-llm-test-suite`）。

### 取样范围

- 全链跑 9 篇太贵；参考既有 E2E 套件选最小 + 一篇中等的做法（`tests/test_real_llm_pipeline_e2e.py` 的 `_E2E_PAPERS` 选 2 篇）。V12 全链建议同样选 1–2 篇代表性论文，避免 CI 时长爆炸。
- 真实检索断言聚焦"非空 + 可溯源 + judgment 非空裁决"，不对具体 citation 内容做脆弱断言（网络结果不定）。

### 注入真 runtime

- Slice 2 的 `create_real_agents(..., retrieval_runtime=...)` 注入路径即测试入口；真实 runtime 由 `SearchAgentRuntime.from_env(with_llm=False)` 在 fixture 建（scope=module 复用、跨用例不每次重建，PRD §Q4 reuse）。
- V12 `load_env()` 在 import 时跑（读自己 `.env`）；测试前确认 conda env `HypoArgus` 内 `VOLCANO_*`/`BISHENG_*`/`LLM_*`（仅 `with_llm=False` 时 LLM_* 可缺，judge 用确定性不调 LLM）已配。

### 定位线索（来自探索，非 brittle 承诺）

- 标记 config：`pyproject.toml:62-67`。
- 既有真实套件：`tests/test_real_llm_parse.py`（9 篇 × 8 契约）、`tests/test_real_llm_pipeline_e2e.py`（2 篇 E2E、`run_real_pipeline` 驱动）、`tests/test_real_llm_wiring.py`（wiring + provider-factory 错误路径）。
- 论文加载：`tests/real_papers.py` + `tests/conftest.py:42-52`。
- 离线门文档：`docs/TESTING.md:108-126`（`pytest -m "not real_llm" -q`）。

## 质量门

```bash
conda run -n HypoArgus ruff check src tests
conda run -n HypoArgus mypy --strict src
conda run -n HypoArgus pytest -q                                  # 离线门；新测试默认 deselected
# 凭证齐时按需跑：
conda run -n HypoArgus pytest -m real_llm -q tests/test_real_retrieval_e2e.py
```

## 状态追踪

| 日期 | 会话/执行者 | 状态变更 | 备注 |
|---|---|---|---|
| 2026-07-16 | Claude（dev/manifest-assembly） | → 已完成（代码 + 离线门） | 新增 `tests/test_real_retrieval_e2e.py`：2 用例（citations 非空可溯源 + judgment 非空裁决），共享模块级全链 fixture；ruff/mypy/离线 pytest 全绿、新 2 用例 deselected。本机缺 `DASHSCOPE_API_KEY`/`VOLCANO_SEARCH_API_KEY`/`BISHENG_BASE_URL` 且 vendor `.env` 不在仓内 → 真实全链冒烟未跑（skipif 闸门放行 skip）；凭证齐时按需 `pytest -m real_llm -q tests/test_real_retrieval_e2e.py`。 |

## 完成检查

- [x] 验收标准全勾选。
- [x] 离线质量门全绿、新测试默认 deselected（661 passed / 4 skipped / 13 deselected）。
- [ ] 凭证齐时真实全链至少跑通一次（在本文件记录结果与已知瞬态坑）。
  - **未完成**：本会话环境无 `DASHSCOPE_API_KEY` + `VOLCANO_SEARCH_API_KEY` + `BISHENG_BASE_URL`（vendor `.env` 未入仓、shell env 亦无），skipif 闸门放行 skip、无法触网。已知瞬态坑预判（待凭证齐时实证并回填本节）：
    1. DashScope 结构化输出瞬态（既有 `real_llm` 套件 ~20–40min、需 retry）——本测试复用 `real_chat_model(timeout=120, max_tokens=8192)`，但未挂 `_parse_resilient` 式重试（全链经 orchestrator `_guarded` 逐节点兜底，单 stage 瞬态会被吞入 `errors` 而非硬崩；若 fixture 整体抛错，两用例将 error 而非 fail，属可接受降级）。
    2. Volcano/Bisheng 网络瞬态——Bisheng 为内网地址（`10.30.186.171`），公网 CI 不可达时 V12 KB 路径超时降级（config 内 `EVIDENCE_RETRIEVAL_PARALLEL_KB_TIMEOUT_MS=12000`），Volcano 全网检索仍产网络类 citations；故 citations 断言取「至少一条可溯源」宽松形。
    3. judgment 非空裁决断言对 LLM 非确定性敏感——若 DashScope 某次返空裁决（全 `pending`），`test_real_retrieval_drives_nontrivial_judgment` 会 fail；真实环境下若反复 fail，考虑放宽为「citations 非空 ∨ judgment 非空裁决」或加 retry（记录待实证）。
- [x] 更新 `INDEX.md` 状态为 `已完成`。
