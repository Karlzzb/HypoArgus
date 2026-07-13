# 新增子智能体接入指南（ADDING AN AGENT）

把一个新子智能体挂进 HypoArgus 流水线的**最小路径**。
manifest 驱动下触点为 **3**（ADR-0014），无需改 `runtime/orchestrator.py`——`default_pipeline()` 遍历 `MANIFEST` 自动纳入新 stage。
术语见 `CONTEXT.md`；模块边界与装配总览见 `docs/DEVELOPMENT.md` §2/§4；测试约定见 `docs/TESTING.md` §5。

## 0. 心智模型：一条 `AgentEntry` = 一个 stage

每条 `agents/assembly.py:AgentEntry`（`agents/assembly.py:542`）四字段，把「agent 实现」与「图拓扑」收口为一行：

| 字段 | 含义 | 举例（`verification`） |
|---|---|---|
| `name` | 图节点名 / stage 名（拓扑标识） | `"verification"` |
| `field` | `Agents` dataclass 字段名（`partition` 无 agent → `None`） | `"verification"` |
| `stub` | 桩 fn（tracer bullet 用；纯函数 agent 此处即真实实现） | `_stub_verification` |
| `real` | 条件替换工厂 `RealDeps → fn | None`（返 `None` 保留桩；纯函数 agent 为 `None`） | 见 `agents/assembly.py:591` |
| `deps` | 上游 stage 名（`()` 接 START） | `("hitl1",)` |
| `build` | `Agents → NodeFn` 闭包（含 `_guarded` 兜底） | `_verification_node`（`agents/assembly.py:382`） |

`MANIFEST`（`agents/assembly.py:562`）是单一装配真相源，同时驱动 typed `Agents` 构造（`create_stub_agents`/`create_real_agents`，`agents/assembly.py:666`/`679`）与 `default_pipeline()` 拓扑派生（`runtime/orchestrator.py:158`：`StageSpec(name, build, deps)` per entry）。
故新增 agent = **新子包/模块 + `Agents` 字段 + 一条 `MANIFEST` 条目**，三触点。

## 1. 两种 agent 形态

| 形态 | 位置 | seam | `real` 工厂 | 例子 |
|---|---|---|---|---|
| **seam agent** | 子包 `agents/<name>/{contract,agent,__init__}.py` | 有（`Protocol` + `Fake*`） | 非 `None`（条件替换桩） | `parser`/`verification`/`hypothesis`/`hitl1`/`hitl2` |
| **纯函数 agent** | 扁平 `agents/<name>.py` | 无（确定性、桩 = 真实） | `None`（不替换） | `merge`/`impact`/`consistency`/`writeback` |

seam agent 拆子包（ADR-0014）：`contract.py` 放 `Protocol` + `Fake*` 桩 + 结构化 I/O 模型（provider-free），`agent.py` 放纯函数，`__init__` re-export 保 `from agents.<name> import ...` 路径不变。
下文以 seam agent 为主线走读真例 `verification`，§4 给纯函数变体侧边栏。

## 2. 走读真例：`verification`（seam agent）

### 2.1 契约面 `agents/verification/contract.py`

provider-free，可被 `agent.py` 与外部测试独立 import。

- **结构化 I/O 模型**：判别联合步 `SearchStep`（`contract.py:43`）/ `ConcludeStep`（`contract.py:60`），按 `action` 字段判别，合并为 `VerifyStep = Annotated[SearchStep | ConcludeStep, Field(discriminator="action")]`（`contract.py:68`）。
- **LLM seam**：`VerifyLlmClient` Protocol（`contract.py:77`），唯一方法 `next_step(argument, observations) -> SearchStep | ConcludeStep`。真实适配器用 `with_structured_output(VerifyStep)` 保证结构合法（见 `infra/llm_adapters.py`）。
- **离线桩**：`FakeVerifyLlmClient`（`contract.py:89`），支持 `script`（按序）与 `factory`（据输入动态决策）两种注入——离线、确定、可断言。

### 2.2 纯函数 `agents/verification/agent.py`

- **入口**：`verify(argument_tree, llm, retrieval, *, max_iterations=8) -> dict[str, ArgumentStatus]`（`agent.py:93`）。
- 返回 partial（by `argument_id`），**不改输入树**（`model_copy`）；`content` 永不动。
- 内部 ReAct 藏在 `_verify_argument`（`agent.py:61`）的 `for _ in range(max_iterations):` 有界循环里——图层级仍是单次节点。
- 体检覆盖 `main_claim / sub_claim / evidence`；`qualification` 与影子节点不在 dict 中（保持 `unverified`）。

### 2.3 装配 `agents/assembly.py`

三个触点落于此文件：

1. **`Agents` 字段**（`agents/assembly.py:157`）：`verification: VerifyFn`（typed，保 `agents.verification: VerifyFn` 字段访问类型安全）。
2. **`MANIFEST` 条目**（`agents/assembly.py:587-603`）：

   ```python
   AgentEntry(
       name="verification",
       field="verification",
       stub=_stub_verification,
       real=lambda d: (
           partial(verify_fn, llm=d.verify_llm, retrieval=d.retrieval,
                   max_iterations=d.max_iterations)
           if d.verify_llm is not None and d.retrieval is not None
           else None
       ),
       deps=("hitl1",),
       build=_verification_node,
   )
   ```

   `real` 工厂读 `RealDeps`（`agents/assembly.py:529`）：仅当 `verify_llm + retrieval` 同时给出才返非 `None`，否则保留桩。
3. **build 闭包** `_verification_node`（`agents/assembly.py:382-399`）：经 `_guarded("verification", body, fallback)` 兜底——body 产 `argument_credibility` partial，异常降级为 `_mark_verify_scope_error`（覆盖范围内未判决节点置 `error`）。

`__init__.py`（`agents/verification/__init__.py`）re-export `verify`/`VerifyLlmClient`/`FakeVerifyLlmClient`/`VerifyStep` 等，保外部 import 路径。

## 3. 骨架模板（新 seam agent，照填）

设新 agent 名 `bias_check`（段级偏见检查，贴 `issue_tags`、不改 `content`/`status`——属批注型 seam）。

`agents/bias_check/contract.py`：

```python
from __future__ import annotations
from typing import Protocol
from pydantic import BaseModel
from domain import Argument

class BiasCheckStep(BaseModel):       # 结构化输出模型
    has_bias: bool
    tag: str                           # 命中时贴的 issue_tag
    reasoning: str = ""

class BiasCheckLlmClient(Protocol):    # LLM seam
    def check(self, argument: Argument) -> BiasCheckStep: ...

class FakeBiasCheckLlmClient:         # 离线桩（provider-free）
    def __init__(self, script=None, *, factory=None) -> None: ...
    def check(self, argument: Argument) -> BiasCheckStep: ...
```

`agents/bias_check/agent.py`：

```python
from __future__ import annotations
from domain import Argument

def bias_check(
    argument_tree: list[Argument],
    llm: BiasCheckLlmClient,            # 注入 seam
) -> dict[str, list[str]]:              # partial: argument_id → issue_tags
    """纯函数：返回 partial 更新，model_copy 不改输入树，content/status 不动。"""
    updates: dict[str, list[str]] = {}
    for a in argument_tree:
        step = llm.check(a)
        if step.has_bias:
            updates[a.argument_id] = [step.tag]
    return updates
```

`agents/bias_check/__init__.py`：re-export `bias_check`/`BiasCheckLlmClient`/`FakeBiasCheckLlmClient`/`BiasCheckStep`。

`agents/assembly.py` 三触点：

```python
# 1. Agents 字段
class Agents:
    ...
    bias_check: BiasCheckFn

# 2. MANIFEST 条目（插入到 hitl2 之前、impact 之后为宜）
AgentEntry(
    name="bias_check",
    field="bias_check",
    stub=_stub_bias_check,              # 返回 {} 的桩
    real=lambda d: partial(bias_check_fn, llm=d.bias_check_llm)
        if d.bias_check_llm is not None else None,
    deps=("impact",),                   # 接 impact 之后
    build=_bias_check_node,             # _guarded 兜底闭包
)

# RealDeps 加可选字段：bias_check_llm: BiasCheckLlmClient | None = None
# create_real_agents(...) 形参同步加 bias_check_llm=... 透传给 RealDeps
```

`_bias_check_node` 闭包照 `_verification_node` 形状写：`_guarded("bias_check", lambda: {"issue_tags_partial": agents.bias_check(tree)}, lambda: {})`，下游 merge/hitl2 据此合流（若需写回 `issue_tags`，仿 `merge_with_partials` 加字段级合流）。

`runtime/orchestrator.py` **无需改**——`default_pipeline()` 遍历 `MANIFEST` 自动纳入。

## 4. 纯函数变体侧边栏（`real=None`）

无 LLM/检索依赖的确定性 agent（如 `merge`/`impact`/`consistency`/`writeback`）更简：

- 扁平单文件 `agents/<name>.py`（无 `contract.py`/`__init__` 子包）。
- `MANIFEST` 条目 `stub` 即真实实现、`real=None`（`create_real_agents` 不替换）：
  ```python
  AgentEntry(name="impact", field="impact", stub=_impact, real=None,
             deps=("merge",), build=_impact_node)
  ```
- `build` 闭包仍用 `_guarded` 兜底（`agents/assembly.py:444` 为 `_impact_node` 范例）。
- 无 seam、无 Fake、无 provider 适配——单测直接 `from agents.impact import impact; impact(tree)`。

## 5. 单独调用与调测

### 5.1 Tier 1 — 纯函数 unit（直接 import + Fake seam）

最快回路，不触 Orchestrator、不触网络。照 `tests/test_verification.py:131`：

```python
from agents.verification import verify, FakeVerifyLlmClient, ConcludeStep, VerifyVerdict
from agents.parser import ...                       # 若需建树
from infra.retrieval import create_mock_retrieval_layer
from domain import Argument, ArgumentType

tree = [Argument(argument_id="n0", argument_type=ArgumentType.MAIN_CLAIM,
                 paragraph_id="p0001", content="x")]
llm = FakeVerifyLlmClient(factory=lambda a, obs: ConcludeStep(verdict=VerifyVerdict.CREDIBLE))
updates = verify(tree, llm, create_mock_retrieval_layer())   # 检索用 Mock，不触网
assert updates["n0"] is ArgumentStatus.CREDIBLE
```

约定（见 `TESTING.md` §5.1）：断言纯函数返回新实例（`model_copy`，输入树不变）；`factory` 做多分支断言、`script` 做按序断言；检索恒用 `create_mock_retrieval_layer()`。

### 5.2 Tier 2 — 单真实-agent · 钉桩管线（**调测头条**）

把**你正开发的 agent** 接真实 seam、其余 stage 留桩，在真实 `Orchestrator` 上跑一篇样例文档——
既见你的 agent 在真管线里的语义落位，又因其余为桩而保字节级承诺可断言。
这是 TESTING.md §5.1（unit）与全真实 e2e 之间的**当前缺口**，最贴合「调测子智能体」。

照 `tests/test_orchestrator_e2e.py:267-279`（体检真接入、解析/HITL-1 真、其余桩）：

```python
from dataclasses import replace
from agents.assembly import create_real_agents
from agents.verification import FakeVerifyLlmClient
from runtime.orchestrator import Orchestrator

# 仅给「必填 + 你的 seam」的 deps；未给的 seam 留桩（hypothesis 桩返回 {}）
agents = create_real_agents(
    llm=<parse_llm>,                       # 必填（解析）
    hitl1_gate=<hitl1_gate>,               # 必填（HITL-1）
    verify_llm=FakeVerifyLlmClient(factory=_search_then_credible_factory()),
    retrieval=create_mock_retrieval_layer(),
)

# 包一层捕获中间态（replace 单字段，不改其余）：
calls = []
def wrapped(tree):
    out = agents.verification(tree)
    calls.append(out); return out
orch = Orchestrator(agents=replace(agents, verification=wrapped))

report = orch.run(b"原文 bytes...")
assert report.final_document == b"原文 bytes..."   # 无采纳 → 逐字节还原（字节级承诺）
assert any(...) for c in calls                     # 你的 agent 语义落位
```

要点：

- `create_real_agents` 形参即「替换矩阵」（见 `DEVELOPMENT.md` §4）：只传你 seam 的 deps，其余自动留桩。
- `replace(agents, <field>=wrapped)` 单字段包一层，捕获 partial / 注入故障——不改其它 stage。
- 断言两条：**字节级承诺**（`final_document == 原文`）保底 + 你的 agent 的语义落位。
- 故障注入调测：把 `wrapped` 改成 `lambda tree: (_ for _ in ()).throw(RuntimeError)`，断言 `_guarded` 降级 patch + 日志 + 单向推进（见 `tests/test_orchestrator_fallback.py`）。

### 5.3 CLI 一脚本（fold under Tier 1）

不跑 Orchestrator、只调纯函数：写一个 `scripts/probe_<name>.py`，`from agents.<name> import <fn>`，stdin 喂样例树、print partial。本质是 Tier 1 的脚本化包装，用于交互式探查。
真实现 provider 接入与 `.env`/`DASHSCOPE_API_KEY` 见 `DEVELOPMENT.md` §8.1。

## 6. 质量门（新增 agent 后必跑）

```bash
ruff check src tests
mypy --strict src            # 新 Agents 字段 + MANIFEST 条目须 typed 过
pytest -q                    # 新 test_<name>.py + e2e 集成断言
```

约定：新增 `tests/test_<name>.py`（纯函数单测，仿 `test_verification.py`）+ 在 `test_orchestrator_e2e.py` 加「换桩→真」集成断言（仿 `test_real_verify_*`，断言字节级承诺 + 语义落位）。
若 agent 有特殊降级语义，补 `test_orchestrator_fallback.py` 故障注入断言。
