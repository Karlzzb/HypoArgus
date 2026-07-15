# TASK-SA-3 — 真实 V12 全链集成测试（`real_llm` 标记）

> 状态：未开始
> 阻塞：Slice 2（真实适配器已落地、可注入真 runtime）
> 母 PRD：`docs/prd-search-agent-integration.md`（§Testing Decisions、§User Story 16/17）
> 目标会话：任意新 session 读取本文件并执行。

## 任务概述

为真实 V12 检索全链（Volcano/Bisheng 真实联网）加慢集成测试，遵循既有 `real_llm` 标记套件范式：带 `real_llm` 标记、模块级 `skipif` 凭证/网络缺失即跳、默认质量门不跑（CI 不被网络/凭证卡住）、真实链路有覆盖。测试驱动真实检索 → citations 非空 → judgment 据真实证据判终态。本切片只加测试，不改适配器（Slice 2 已落地）。

## 验收标准

- [ ] 新增测试文件（如 `tests/test_real_retrieval_e2e.py`），模块级 `pytestmark` 带 `real_llm` 标记 + `skipif(not _HAS_KEYS, reason="needs VOLCANO_*/BISHENG_* + network")`。
- [ ] 凭证探测键：`VOLCANO_SEARCH_API_KEY`（与 V12 `load_env` 读的 `VOLCANO_*` 一致）+ Bisheng 的 `BISHENG_TOKEN`/`BISHENG_BASE_URL`（按 V12 `evidence_retrieval/config.py` 实际读取的 env 名对齐）。
- [ ] 测试驱动真实 V12 全链：从 `markdown/` 真实论文取段（参考 `tests/real_papers.py` 的 `REAL_PAPER_CASES`），跑 retrieval 节点（真 runtime 注入），断言 `citations` channel 非空、`Source` 字段可溯源（`origin`/`locator` 非空）。
- [ ] 断言下游 judgment 据真实 `citations` 判终态（非空裁决、非永远 KEEP），即"真实后端使 judgment 见真实素材"成立。
- [ ] tracer bullet 守护：真实后端未配置/未触达的对照路径终稿逐字节等于原文（PRD story 17）。
- [ ] 离线默认质量门不被卡：`pytest -m "not real_llm" -q` 全绿，新测试默认 deselected。
- [ ] 质量门：`ruff check src tests` + `mypy --strict src` + `pytest -q`（离线门）全绿。

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
| _ | _ | → 进行中 | _ |

## 完成检查

- [ ] 验收标准全勾选。
- [ ] 离线质量门全绿、新测试默认 deselected。
- [ ] 凭证齐时真实全链至少跑通一次（在本文件记录结果与已知瞬态坑）。
- [ ] 更新 `INDEX.md` 状态为 `已完成`。
