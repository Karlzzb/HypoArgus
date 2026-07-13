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
- **节点权重 (`argument_weight`)**：0-100 整数，解析器建树时按明文 rubric 赋值——带数据/引源的论据高分、泛泛断言低分，影子节点恒 0。
  供影响传导计算上层论点的剩余支撑率（`surviving_weight / total_weight`），是 `invalid` / `weakening` 判定的依据。见 ADR-0013。

## 论证树结构原则（解析器默认形态·软启发式）

- 每个**实质段落**对应树上**至少一个节点**（论点/论据/限定）；**影子段落**对应影子节点。
- 段落可含多个节点（ADR-0001），故「每段一论点」是默认，不是上限，也不是恰好一个。
- 段落的主节点通过 `parent_id` 归属到某个上层论点；上层论点通常独立成段（领起段/标题段），「多段服务于一个上层论点」即父子指针。
- 这是**软启发式**，不是校验硬约束——**解析器不得为无论点的段落硬造论点**（无论点段应归为影子节点）。

## 状态

- **节点状态机**：`unverified` → `pending_verification` → (`credible` | `doubtful` | `error`) → `adopted`（HITL-2 采纳·待回写）→ `corrected`（回写成功）。回写失败停留 `adopted` 可重试。`invalid` = 影响传导判上层论点失去支撑。见 ADR-0011。
- **error vs invalid**：`error` 是事实验证判叶子论据自证其伪；`invalid` 是影响传导判上层论点被拖垮。
- **adopted**：已采纳待回写的中间态，持久记录「采纳了哪条假说」，是回写幂等重试的依据。

## 智能体角色

- **全局调度 Agent**：中枢编排、状态管理、HITL 调度、单向流控制。
- **论证结构解析 Agent**：唯一语义解析入口，产出论证树。
- **假设生成 Agent**：在原文边界内为节点生成可证伪的候选修订假设。
- **事实验证 Agent**：ReAct 机制多源检索校验论据真伪，写回节点状态。
- **影响传导 Agent**：校验结果沿树向上传导，评估对上层论点的影响。
- **一致性校验 Agent**：中立质检，仅贴批注标签（`issue_tags`），无打回权。
- **修订回写 Agent**：按段落原子缝合，产出终稿。

## 关键算子 / 机制

- **线路 1 / 体检**：对 claim & evidence 正向查询与纠错，产出原文状态 `credible / doubtful / error`。
- **线路 2 / 开药**：对节点生成假说并对假说取证，产出假说状态 `supported / doubtful / refuted`（「无假说」= 空数组）。与原文 `credible/doubtful/error` 对称。见 ADR-0008。
- **双轨合并算子 (Merge Operator)**：合并两线路结果，按 12 格矩阵裁决。见 ADR-0006。
- **裁决动作 (MergeAction)**：合并算子对单节点的六种裁决：`keep`（保留原文）、`replace` / `rewrite` / `supplement`（成立假说按语义关系分流，见回写三操作）、`conflict`（原文 credible 且对立假说成立，贴签交人判）、`freeze`（原文 credible 且递进/扩展假说成立，严格冻结原文不动）。见 ADR-0006。
- **回写三操作**：`替换 (replace)` / `改写 (rewrite)` / `补充 (supplement)`。由**假说与原文的语义关系**（`relation: oppose/advance/expand`）决定：**对立→替换、递进→改写、扩展→补充**。关系由假设生成 Agent 标注，一假说一关系。见 ADR-0006、ADR-0007。
- **conflict**：原文 `credible` 且对立假说亦成立时贴的标签，交 HITL-2 人判，系统不自动裁决。
- **HITL**：节点 1（结构确认，可跳过）、节点 2（修订内容确认，双轨决策大闸）。

## 实现映射

本文件只定义概念；具体实现见下列文档，二者分层维护、避免漂移。

- 状态树字段流向（主/子智能体 state、字段来源、LLM seam 输入形式）：`docs/STATE.md`。
- 模块边界、seam、装配与扩展点：`docs/DEVELOPMENT.md`。
- 架构决策记录：`docs/adr/`。
- 新增子智能体接入指南：`docs/adding-an-agent.md`。
