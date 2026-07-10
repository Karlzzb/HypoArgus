# LangGraph 多阶段 Agent 开发指南

一份自包含的 LangGraph 侧开发清单：怎么把"深度研究 / 多阶段 Agent"落成一张可执行的状态图。关键代码全部内嵌（源码在 `agentkit/`，文末有文件对照表）。

---

## 0. 心智模型：Agent 是以 LLM 为决策核的状态机

不要把 Agent 想成"一个 while 循环里的自由对话"，它是一张**状态图**：orchestrator 分解任务 → 隔离的并行 worker 舰队 → 汇总器成稿。
LLM 不掌管控制流，它每一步只做一个极窄的**结构化决策**（要不要继续、调哪个工具、拆不拆新子课题）；"接下来执行什么"由**图的拓扑 + 条件边**决定，不由模型的散文决定。

贯穿全文的铁律：凡是能靠 prompt "请你务必…"被模型忽略的规则，都要落成图里可执行的边或校验。

---

## 1. 骨架：四相 = 节点 + 边

```
rephrase → decompose →〔HITL 确认闸门〕→ research（动态并行）→ report → END
```

- `rephrase`：精炼课题（可选，可反问用户）；`decompose`：拆成子课题大纲 `outline[]`，用 `with_structured_output` 保证结构（§6.3）。
- `research`：supervisor 用 `Send` 动态 fan-out 多个 worker，队列会自我生长（§2）；`report`：遍历所有 block 的证据 + 引用成稿。

装配（节点注册 + 条件边）就是一段 `StateGraph`：

```python
graph = StateGraph(DeepResearchState)
graph.add_node("supervisor", supervisor_node)
graph.add_node("research_worker", research_worker_node)
graph.add_node("aggregate", aggregate_node)
graph.add_node("report", report_node)

graph.add_edge(START, "supervisor")
graph.add_conditional_edges("supervisor", dispatch,
    {"research_worker": "research_worker", "report": "report"})
graph.add_edge("research_worker", "aggregate")          # Send join barrier
graph.add_conditional_edges("aggregate", loop_or_report,
    {"supervisor": "supervisor", "report": "report"})
graph.add_edge("report", END)
return graph.compile(checkpointer=checkpointer)
```

---

## 2. 动态并行：Send fan-out + reducer 合并（本范式的核心）

### 2.1 用 Send 分发，而不是共享可变队列

supervisor 每轮取一批 pending block，用 `Send` fan-out 到多个 worker 实例。每个 worker 拿到的是 `Send` 投递的 **scoped payload**（那一个 block + 调度上下文），彼此隔离、互不通信：

```python
def dispatch(state):
    pending = pending_blocks(state.get("blocks", []))
    if not pending or state.get("rounds", 0) > _effective_cap(state):
        return "report"
    batch = pending[: state.get("max_parallel_topics", 3)]   # 有界批次
    common = {"topic": state["topic"], "known_titles": [b["title"] for b in state["blocks"]],
              "block_count": len(state["blocks"]), "queue_max_length": state["queue_max_length"]}
    return [Send("research_worker", {"block": b, **common}) for b in batch]
```

### 2.2 worker 返回 partial state，交给 reducer 无锁合并

worker **绝不**手动改共享队列或加锁，只 `return` 一份 partial state（自己那个 block 翻成 `completed` + 新发现的子课题 + 引用）：

```python
async def research_worker_node(state, config):
    block = state["block"]                       # Send 注入的那一个 block
    output = WorkerOutput.model_validate(await llm_json(...))   # 结构化输出
    # 铸造全局唯一引用 id：CIT-{block}-{seq}
    citations = {f"CIT-{block['block_id']}-{i:02d}": {...} for i, c in enumerate(output.citations, 1)}
    completed = TopicBlock(block_id=block["block_id"], status="completed",
                           knowledge=output.knowledge, ...).model_dump()
    children = [TopicBlock(block_id=block_id_for(s.title), title=s.title, parent=block["block_id"]).model_dump()
                for s in output.append
                if find_similar(s.title, known_titles) is None and projected_count < queue_max_length]
    return {"blocks": [completed, *children], "citations": citations}
```

State 用 reducer 声明合并规则，LangGraph 自动折叠并行 worker 的返回：

```python
class DeepResearchState(BaseState, total=False):
    blocks:    Annotated[list[dict], merge_blocks]         # 工作清单 + 证据
    citations: Annotated[dict[str, dict], merge_citations] # id → 引用

def merge_blocks(left, right):                 # 按 block_id upsert，保持首见顺序
    merged = list(left or [])
    index = {b["block_id"]: i for i, b in enumerate(merged)}
    for b in right or []:
        pos = index.get(b["block_id"])
        if pos is None: index[b["block_id"]] = len(merged); merged.append(b)  # 新子课题追加
        else: merged[pos] = b                                                 # pending→completed 覆盖
    return merged

def merge_citations(left, right):              # 两个 id→citation 字典求并集
    return {**(left or {}), **(right or {})}
```

- `merge_blocks`：`pending→completed` 覆盖旧副本，新子课题追加到尾部——**队列自我生长 = 一次值合并**，下一轮 supervisor 自然调度到它。`block_id_for(title)` 是标题的确定性哈希，两个 worker 同轮发现同一子课题 → 同一 id → 折叠成一个。
- `merge_citations`：并发 worker 各铸唯一 `CIT-{block}-{seq}`，字典并集永不撞号。

**迁移铁律：LangGraph 里几乎不该手写 `asyncio.Lock`，共享可变状态一律换成带 reducer 的 state channel。** citations 只在每个 super-step 随 checkpoint 落盘一次，去掉了旧实现里 O(N²) 的落盘和锁。

### 2.3 join barrier + 显式循环

```python
def loop_or_report(state):                     # aggregate 之后：还有 pending 且没超 cap 就再跑一轮
    if state.get("rounds", 0) > _effective_cap(state): return "report"
    return "supervisor" if pending_blocks(state.get("blocks", [])) else "report"
```

- `research_worker → aggregate` 是 `Send` 的 join barrier：reducer 合并完 `aggregate` 才看到全量。
- `aggregate → supervisor` 是一条**图里可见的环**，回环由 `rounds` vs `safety_cap` 把关（§5），动态生长不藏在节点内部。

---

## 3. 状态即记忆：三层记忆，各司其职

| 层 | 存什么 | 寿命 / 读者 | LangGraph 落点 |
| --- | --- | --- | --- |
| 层1 对话内 | `[CIT-x] 摘要`、推理旁白、tool 轮 | 一个 worker 一相；模型每轮都看 | `Annotated[list, add_messages]` |
| 层2 block 内证据 | 每次工具调用的 query+原文+摘要+citation | 活到写报告；**不进对话** | worker 局部 state / 带 reducer 的另一 key |
| 层3 跨 block | 全部 block、状态、跨 worker 共享引用 | 整个任务；可落盘 | `blocks` / `citations` channel + `BaseStore` |

**关键纪律：产生大输出的原文永远不放进 `messages`。** 原文塞进独立的 evidence key 或 `BaseStore`，`messages` 里只留摘要（§4）。`BaseStore` 负责跨线程/跨会话的长期记忆，`checkpointer` 负责单次运行的快照续跑，分工不同（§7）。

> 长期记忆固化：把一次运行的产出沉淀进 `BaseStore` 时，别简单 append，走 **update（增量抽事实）/ audit（对照原文审校）/ dedup（全文去重）** 三模式（参考 `deeptutor/services/memory/consolidator/`）。

---

## 4. 源头压缩，而非事后裁剪（防 context 爆炸的治本招）

"事后裁剪"（消息太长就砍最早几条）只是止损。更强的一招是**在源头压缩**：每条会产生大输出的工具结果，在写回 state 之前先过一个压缩步骤，只把摘要放进 `messages`，原文分流进 evidence key。两种落点：
- `pre_model_hook`：进模型前对 `ToolMessage` 压缩。
- 独立压缩 node：worker 内一次专门的压缩 LLM 调用（配 `add_messages` 用同 id 覆盖，或 `RemoveMessage` 删原文那条）。

三层输入/输出闸，每处都不许无限膨胀（数字按需调整）：

| 闸 | 位置 | 作用 |
| --- | --- | --- |
| 摘要器输入截断（如前 8000 字符） | 压缩 node 入口 | 喂给摘要器的原文先砍短 |
| 摘要器输出上限（如 1500 token） | 压缩 node 出口 | 进对话的摘要恒短 |
| evidence 原文硬上限（如 50KB） | 写 evidence key 时 | 旁存的原文也有天花板 |

这样对话天然小而密，你几乎永远不需要 `trim_messages`。

> 区分：这里用的是 **summarization（压缩省 context）**，不是 **Reflexion（自我批判改进）**；摘要器只压缩、不评判，质量控制交给 §6 的确定性校验（比让模型自省更可控、可测、便宜）。

---

## 5. 有界收敛：让"自主"不变成"跑飞"

worker 能自己 APPEND 新子课题，自主就必须有界，否则队列无限生长、循环永不停。四道界全部物化在 state / 图配置里：

| 界 | 防的失控 | LangGraph 落点 |
| --- | --- | --- |
| 单 worker 迭代上限 | 一个 block 无限调工具 | worker 子循环 `iteration` 计数 + 到顶路由 `finalize` |
| 调度轮数 `safety_cap` | supervisor 回环失控 | state 里 `rounds` vs `safety_cap = max(20, queue_max×4)` |
| 队列 `max_length` + 模糊去重 | 重复 / 超量 APPEND | worker 里 `find_similar` 去重 + 容量上限，超了丢弃 |
| `recursion_limit` | 整图兜底 | 编译/调用时设，作为硬 backstop |

轮数闸在 supervisor 里判定并**如实标注降级**（不静默截断）：

```python
def safety_cap_for(queue_max_length): return max(20, queue_max_length * 4)

# supervisor_node 内：
over_cap = rounds > _effective_cap(state)
if over_cap and pending_blocks(current_blocks):
    updates["status"] = "capped"                 # 标注，供 report 层如实反映
    emit("progress", stage="supervising", trace_kind="warning", safety_cap_reached=True)
```

去重是纯函数，并发 worker 各自独立计算、reducer 折叠，两层协作：

```python
def block_id_for(title):                 # 确定性 id：同标题 → 同 id → reducer 折叠成一个
    norm = re.sub(r"\s+", " ", title.strip().lower())
    return f"block_{hashlib.blake2b(norm.encode(), digest_size=6).hexdigest()}"

# find_similar(title, existing, threshold=0.85)：模糊层（SequenceMatcher + token Jaccard），
#   拦"换个说法再提一遍"——归一化后逐个算相似度，≥threshold 的最高分即命中，否则 None。
```

---

## 6. 输出质量：四道确定性防线

保证质量不是"让模型更聪明"，而是**用代码把模型钉在质量约束里**——凡是 prompt 里靠不住的规则，都改写成图里可执行的闸门。

### 6.1 证据可引用、可溯源

每条工具结果压成带 `[CIT-x]` 的摘要，原文旁存并挂 citation_id，让每个进入报告的论点都能追回原始检索原文——"有据可查"而非模型编造。落点：worker 返回 `citations` channel（§2.2），report 层遍历取用。

### 6.2 语义校验：evaluator-optimizer（拦"没查就答"）

失败模式：模型偷懒，一次工具都没调就想收尾编答案。这类**业务语义规则**在 LangGraph 里不会自动消失，要写成 conditional_edge：检查"这个 block 有没有至少一次证据"，没有就把边指回 worker 重做——一个评审判据，不合格打回。

### 6.3 格式合法：结构化输出 + 校验重试

失败模式：模型输出结构不合法（该出 JSON 却夹带散文）。用 `with_structured_output` 让 SDK 层保证结构，拿到即合法对象；校验失败在 node 里 catch，把错误拼进下次 prompt 重调或用条件边路由回同一 node。

```python
class Outline(BaseModel):
    sub_topics: list[SubTopic]
outline = llm.with_structured_output(Outline).invoke(prompt)   # 拿到即合法
```

> 迁移红利：用"首行标签 + 正则解析 + 纠错 prompt"表达动作的那套软协议脚手架，在 LangGraph 里**整体塌缩成读结构化字段 + 条件边**。
> 消失的只是格式类违规；语义类校验（§6.2）不会消失，换成边。

### 6.4 partial 兜底：部分失败也产出，但绝不假装成功

失败模式：某个 block 崩了 / 耗尽轮数没收尾。既不能整任务失败（用户一无所获），也不能假装成功（骗用户），三个动作缺一不可：**回填**（崩掉的 block 用空知识回填，让幸存 block 仍成稿）、**可见**（`emit` 警告给前端）、**标注**（state 里打 `status="capped"` / `partial=True` + 失败清单）。

单循环的强制收尾闸就是一个 `route_after_llm`：到顶不再 loop，改路由到 `finalize` 兜底：

```python
def route_after_llm(state):
    if state.get("finalize_reason"): return "finalize"          # 供应商耗尽后有活儿→兜底
    if state.get("status") == "succeeded" or not state.get("pending_tool_calls"):
        return "final"
    if state.get("iteration", 0) >= state.get("max_iterations", MAX): return "finalize"  # 迭代闸
    return "tools"
```

> 最该抄进你项目的一条：工业级 Agent 的诚实性 = **失败要么被修复，要么被如实标注，绝不静默吞掉、更不伪装成功**。
> 你的图该有一个 `errors` / `status` 的 state key，让下游和前端看得见降级。

### 防线速查

| 防线 | 挡住的失败 | 手段 |
| --- | --- | --- |
| 摘要 + 引用 | 报告无据、context 爆 | 压缩 node + citations channel |
| evaluator-optimizer | 没查就编答案 | 语义 conditional_edge 打回 |
| 结构化输出 + 重试 | 输出格式非法 | `with_structured_output` + Pydantic |
| partial 兜底 | 部分失败 → 全崩 / 假成功 | 回填 + 可见警告 + `status` 标注 |

---

## 7. 持久化与续跑：checkpointer + BaseStore

- **`checkpointer`**（Postgres/SQLite）：按 super-step 存快照，任意节点崩了都能从最近点**断点续跑**，是 HITL interrupt 的前提（§8）。
- **`BaseStore`**：跨线程/跨会话的长期记忆，与单次运行的 checkpoint 分工不同。

编译时接上即可（`graph.compile(checkpointer=checkpointer)`），别自己写落盘/续跑逻辑。

---

## 8. HITL 闸门：interrupt + Command(resume)

大纲确认这类"停下来等人点头"的闸门用 `interrupt()`：

```python
def decompose_node(state):
    outline = make_outline(...)
    confirmed = interrupt(outline)         # 暂停，把大纲交给用户
    return {"confirmed_outline": confirmed}
```

用户确认后用 `Command(resume=值)` 恢复。**三个必须记住的坑：**

1. 必须配 `checkpointer`，否则无法冻结 state。
2. 恢复时**整个 node 从头重跑**（不是从 `interrupt()` 那行继续）——别把 `interrupt` 放在有副作用的代码后面。
3. 多个 interrupt 按顺序索引匹配 resume 值；别在节点里条件跳过 interrupt。

昂贵的并行研究（token 成本可达普通对话的 15×）应等确认后再启动。见 [LangGraph Interrupts 官方文档](https://docs.langchain.com/oss/python/langgraph/interrupts)。

---

## 9. 决策：什么时候上图，什么时候手写软协议

| 情形 | 倾向 | 为什么 |
| --- | --- | --- |
| 控制流是"几个明确阶段 + 明确转移" | **LangGraph 图** | 拓扑即控制流，省掉整个解析/纠错层 |
| 需要结构化决策 / 工具路由 | **LangGraph 图** | 读 `tool_calls` / state 字段天然可靠 |
| 要断点续跑 / 崩溃恢复 | **LangGraph 图** | `checkpointer` 现成 |
| 一套循环复用于很多形态各异的流程 | 手写软协议尚可 | 换 prompt/合同即可，不必逐个画图 |
| 要标签级流式子轨（实时前端） | 手写软协议有优势 | 图里要自己在节点处理流式路由 |
| 不想绑定框架 / 极致定制 | 手写软协议 | 纯手写，无框架约束 |

一句话：**控制流越"确定、分阶段、要恢复"，越该上 LangGraph；越"高度动态、一套循环复用多种流程、要标签级流式"，手写才划算。** 四相骨架用图表达，只有 worker 内部那个 ReAct 小循环可能仍值得手写。

