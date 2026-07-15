# Context / 术语表

本文件是论证驱动型文档修订多智能体系统的**领域术语表（Ubiquitous Language）**，只收录概念定义，不含实现细节。

## 核心实体

- **论证树 (Argumentation Tree)**：全链路唯一数据主干。将文档解构为具备父子从属关系的逻辑节点树。
- **论证节点 (Argument)**：树的基本单元。分为核心逻辑节点与影子节点两类。
- **核心逻辑节点**：`main_claim`（主论点）、`sub_claim`（分论点）、`evidence`（论据）、`qualification`（限定条件）。参与逻辑传导与事实校验。
- **影子节点 (Shadow Node)**：`background`（背景叙述）、`evaluation`（主观评价）。只读，不参与校验与传导，但提供上下文并参与最终文本拼接。

## 映射与定位

- **段落 (Paragraph / `paragraph_id`)**：回写的**唯一原子单位**。见 ADR-0001。
- **text_span**：节点在原文中的起止偏移量，**仅作段内辅助定位**，回写逻辑不依赖它。
- **基数约束**：一个段落可含多个节点；一个节点不可跨段落。
- **只读原文段落表 (Original Paragraphs)**：`{ paragraph_id → 原始 bytes }` 的不可变副本。字节级还原、HITL-2 对比左栏、回写拷贝的共同真相源，**永不整篇进 Agent 上下文**。见 ADR-0005。
- **段落聚合根 (ParagraphRecord / `paragraph_list`)**：每段一条的聚合记录，正向拥有与论证节点的一对多关系（`argument_tree_ids`）。
  `Argument` 是纯推理结构（不再持 `paragraph_id` / `content`）；段落原文每段一份存于 `ParagraphRecord.original_content`（取代原节点级存原句），摘要存于 `ParagraphRecord.summary`。见 ADR-0025。
- **节点权重 (`argument_weight`)**：0-100 整数，解析器建树时按明文 rubric 赋值——带数据/引源的论据高分、泛泛断言低分，影子节点恒 0。
  供影响传导计算上层论点的剩余支撑率（`surviving_weight / total_weight`），是 `invalid` / `weakening` 判定的依据。见 ADR-0013。

## 论证树结构原则（解析器默认形态·软启发式）

- 每个**实质段落**对应树上**至少一个节点**（论点/论据/限定）；**影子段落**对应影子节点。
- 段落可含多个节点（ADR-0001），故「每段一论点」是默认，不是上限，也不是恰好一个。
- 段落的主节点通过 `parent_id` 归属到某个上层论点；上层论点通常独立成段（领起段/标题段），「多段服务于一个上层论点」即父子指针。
- 这是**软启发式**，不是校验硬约束——**解析器不得为无论点的段落硬造论点**（无论点段应归为影子节点）。

## 状态

- **节点状态机**：`unverified` → `pending_verification` → (`credible` | `doubtful` | `error`) → `adopted`（HITL-2 采纳·待回写）→ `corrected`（回写成功）。回写失败停留 `adopted` 可重试。`invalid` = 影响传导判上层论点失去支撑。见 ADR-0011。
  **注（Slice 6）**：新流程（rewrite_loop + hitl2 终稿闸门）按段落 / 文本工作，与 argument 的 `status` 解耦——`adopted`/`corrected`/`adopted_hypothesis_id` 在新流程不再被写（domain 字段保留不删）；终稿文本确认只经 hitl2 逐段确认 / 编辑 / 驳回 `proposed_rewrites`。
- **error vs invalid**：`error` 是事实验证判叶子论据自证其伪；`invalid` 是影响传导判上层论点被拖垮。
- **adopted**：已采纳待回写的中间态，持久记录「采纳了哪条假说」，是回写幂等重试的依据。

## 智能体角色

- **全局调度 Agent**：中枢编排、状态管理、HITL 调度、单向流控制。
- **论证结构解析 Agent**：唯一语义解析入口，产出论证树 + `paragraph_list` + `query_time_range` 桩。
- **假设生成 Agent**：在原文边界内为节点**仅 propose** 产 pending 候选修订假说（取证移至 judgment）。
- **裁决 Agent (judgment)**：检索之后的单一判断节点，五合一——吃 `citations` 判 per-argument / per-hypothesis 终态、再按序串联 merge / impact / consistency 纯函数（ADR-0017）。
- **重写循环 Agent (rewrite_loop)**：judgment 之后逐段提议重写文本；对被触达段产 `proposed_rewrites`、未触达段省略（ADR-0017）。
- **终稿确认 Agent (hitl2)**：逐段确认 / 编辑 / 驳回 `proposed_rewrites` 后拼装终稿（ADR-0017 重定位）。原独立「修订回写 Agent」已裁撤，终稿在 hitl2 落地。

## 关键算子 / 机制

- **原文侧终态（judgment 取证）**：对 claim & evidence 据 `citations` 判终态，产出原文状态 `credible / doubtful / error`（原 verification ReAct 体检职责，Slice 5 并入 judgment）。
- **线路 2 / 开药**：对节点**仅 propose** 产 pending 假说（「无假说」= 空数组）；取证（吃 citations 判终态 `supported / doubtful / refuted`）由 judgment 节点完成（Slice 5）。与原文 `credible/doubtful/error` 对称。见 ADR-0008。
- **双轨合并算子 (Merge Operator)**：按 12 格矩阵裁决 `原文.status × 假说.status`（judgment 内串联调用，非独立 stage）。见 ADR-0006。
- **裁决动作 (MergeAction)**：合并算子对单节点的六种裁决：`keep`（保留原文）、`replace` / `rewrite` / `supplement`（成立假说按语义关系分流，见回写三操作）、`conflict`（原文 credible 且对立假说成立，贴签交人判）、`freeze`（原文 credible 且递进/扩展假说成立，严格冻结原文不动）。见 ADR-0006。
- **回写三操作**：`替换 (replace)` / `改写 (rewrite)` / `补充 (supplement)`。由**假说与原文的语义关系**（`relation: oppose/advance/expand`）决定：**对立→替换、递进→改写、扩展→补充**。关系由假设生成 Agent 标注，一假说一关系。见 ADR-0006、ADR-0007。
- **conflict**：原文 `credible` 且对立假说亦成立时贴的标签，交 HITL-2 人判，系统不自动裁决。
- **HITL**：节点 1、节点 2。重构后语义见下「重构方向术语」：hitl1=partition 确认闸门（有界打回）、hitl2=终稿文本确认闸门。

## 重构方向术语（ADR-0017·已落地）

下列术语为流水线重构（ADR-0017）所接受的方向，Slice 1–6 已全部落地。
术语一旦定义即作为后续代码切片的契约语言；字段流向以 `docs/STATE.md` §1 为唯一描述点。

- **会话上下文 (session_context)**：贯穿全链的运行上下文，单一嵌套对象，含 `session_id` / `user_id` / `current_time` / `user_prompt`。
  单写者=入口注入（与 `original_doc` 同入 START），全链只读；供 LLM 检索与生成 seam 携带一致运行背景。
  业务字段改走 `PipelineState` channel，取代原 ADR-0016「业务字段仅走 `RunnableConfig`」的做法（`RunnableConfig` 仍承载 langgraph 原生 metadata / callbacks / checkpointer，见 ADR-0017 §5/§6）。
- **查询时间范围 (query_time_range)**：本文所需的数据查询时间范围（`start` / `end` / `rationale`）。
  单写者=`parse+partition`（当前伪代码桩，默认 2025–2026，真实 LLM 时间识别待后续切片）；读者=retrieval / rewrite / judgment。
  供下游检索限定在正确时间窗、供 LLM 决策有时间上下文。见 ADR-0017。
- **段落摘要 (paragraph_summary)**：每段的摘要文本，由 `parse+partition` 两阶段顺产（树调用产 proposals + 摘要分块调用产 `list[ParagraphSummary]`）。
  现承载于段落聚合根 `ParagraphRecord.summary`（摘要单一定义点；原 `paragraph_summaries` state channel 已退役，见 ADR-0025）。
  供 hypothesis_propose / rewrite_loop 读取（judgment 取段 `original_content`、不读摘要），避免一次性 / 逐点喂入时上下文爆炸；**不并入 `OriginalParagraphs`**（保其字节级无损只读表身份）。见 ADR-0017 / ADR-0025 / STATE.md §1。
- **judgment 节点**：检索之后的单一判断节点，五合一（verification 取证 + hypothesis 取证 + merge 裁决 + impact 传导 + consistency 批注）。
  控制流合并为 1，但 merge / impact / consistency 的**纯函数逻辑保留、不交 LLM 裁决**；取证由新 LLM seam 吃 `citations` 判终态，不再 ReAct 逐段逐点检索。
  重构 ADR-0002（乐观并行）/ ADR-0006（12 格矩阵合流）的双线路并行设计。见 ADR-0017。
- **重写循环 (rewrite_loop)**：逐段提议重写文本的节点。
  对**被触达段**（有 supported 假说 / 相关 citations）由 LLM 提议重写；**未触达段逐字节拷回**。
  产出 `proposed_rewrites: dict[paragraph_id, str]`（仅被触达段），供 HITL-2 确认。
  重写阶段放弃「终稿逐字节一致 / content 不被 LLM 改写 / 幂等纯函数回写」三条承诺（仅被触达段；未触达段仍逐字节忠实；HITL-2 仍为唯一决策闸门）。见 ADR-0017。
- **resolved_rewrites**：HITL-2 决策应用后的段文本表（`paragraph_id → 终稿文本`，仅含被确认 / 编辑段；驳回 / 未触达段省略）。
  由 hitl2 的 `resolve_rewrites` 产、`assemble_final_document` 据之按规范顺序拼 `final_document`（确认 / 编辑段用其文本、驳回 / 未触达段逐字节原文）。
  也是崩溃恢复续跑入口 `Orchestrator.resume_rewrite(resolved_rewrites, original_paragraphs)` 的入参——按段文本幂等重推导，不再依赖 `adopted_hypothesis_id`。
- **hitl1（语义变更）**：从「结构确认·可跳过」重定义为 **partition 确认闸门**：人确认段落切分是否合理，不合理则按用户 prompt 重跑 `parse+partition`。
  打回**打破「绝不打回」**（PRD §13 / DEVELOPMENT §1），须**有界**（max retries 默认 3）；超限向前推进 + 贴 `partition_retry_exhausted`。
  partition「按 prompt 重切」当前为伪代码桩。见 ADR-0017 §2 / §4。
- **hitl2（语义变更）**：重定位为**终稿文本确认闸门**：逐段确认 / 编辑 / 驳回 `proposed_rewrites`，拼成 `final_document`。
  被确认段用提议文本、被编辑段用编辑文本、被驳回段回退原文、未触达段逐字节原文。
  仍不可跳过、`Hitl2GateError` 原样上抛、绝不替人拍板（ADR-0010 不动）；原 `writeback` 节点裁撤，终稿在此落地。`adopted`/`corrected`/`adopted_hypothesis_id` 在新流程不再被写（domain 字段保留不删）。见 ADR-0017。

## 运行时身份（可视化服务·ADR-0022 / ADR-0023）

- **会话 (session_id)**：一个浏览器标签页对应的工作台身份，**外部**（一期前端、二期 Java 登录后）生成、Python 仅登记与校验、不生成。
  作 checkpointer `thread_id`、PauseMeta / session_owner / 锁 / 注册表的内部主键；跨刷新稳定。
  _避免_: 把 `session_id` 当 Python 维护的对象、或与 [[session_context]] 混用。
- **链路 (trace_id)**：工作台内**一次修订执行链路** ID，**Python 内部 mint**（收到无活跃 `pause_meta` 的 `query` 时生成）；HITL 断点续跑复用同一 `trace_id`。
  一个 session 含多个 trace（多轮修订）。
  _避免_: 把 `trace_id` 与 `session_id` 混为一谈、或交由前端生成（fresh-run vs resume 仅 Python 能判）。
- **会话上下文 (session_context)**：单次运行的只读上下文袋（`session_id` / `user_id` / `current_time` / `user_prompt`），与上述 `session_id` 不同概念——前者随每次 run 注入、后者跨多 run 稳定。
- **event_seq**：单 trace 内事件时序序号，翻译层 mint、`trace_events.event_seq` 落库，前端按序过滤乱序；`heartbeat` 为 -1。

## 实现映射

本文件只定义概念；具体实现见下列文档，二者分层维护、避免漂移。

- 状态树字段流向（主/子智能体 state、字段来源、LLM seam 输入形式）：`docs/STATE.md`。
- 模块边界、seam、装配与扩展点：`docs/DEVELOPMENT.md`。
- 架构决策记录：`docs/adr/`。
- 新增子智能体接入指南：`docs/adding-an-agent.md`。
