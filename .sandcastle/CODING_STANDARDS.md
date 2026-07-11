# Coding Standards

<!-- Project-specific standards for HypoArgus (LangGraph / Python).
     The reviewer agent loads this during code review via @.sandcastle/CODING_STANDARDS.md,
     so these are enforced during review without costing tokens during implementation. -->

## Style

- Python 3.11+。类型注解齐全：公共函数签名、Pydantic 模型字段。
- `snake_case` for variables and functions; `PascalCase` for classes and Pydantic models。
- 模块单一职责；优先组合而非继承。
- 不留被注释代码或 TODO 注释。
- 命名表意清晰，显式优于紧凑。

## Testing

- TDD：先写失败的 `pytest`，再写实现使其通过，再重构。
- 黑盒外部行为验证：断言流水线输入输出（纯文本 → 论证树流转 → 局部缝合终稿），绝不断言 Agent 内部提示词结构、迭代次数或私有变量。
- 字节级对齐：未变更段落逐字节等于原文（含空行、缩进、换行、末尾空格）。
- 三个纯函数子缝优先复用而非新增：段落切分、双轨合并算子、段落回写。

## Architecture (LangGraph)

- 控制流 = 图拓扑 + 条件边；不靠 prompt 散文约束规则。
- 共享状态用带 reducer 的 `Annotated[..., reducer]` state channel（按 id upsert / 字典并集），几乎不写 `asyncio.Lock`。
- 产出大输出的原文分流进 evidence key / `BaseStore`；`messages` 只留摘要。
- 结构化决策用 `with_structured_output` + Pydantic，拿到即合法对象。
- 有界收敛：单 worker 迭代上限、`safety_cap`、队列 `max_length` + 模糊去重、`recursion_limit` 全部物化在 state / 图配置里。
- partial 兜底：失败回填 + `emit` 警告 + `status`/`errors` 标注，绝不静默吞或伪装成功。
- HITL `interrupt()` 必配 `checkpointer`；`interrupt` 不放在有副作用的代码之后。
- `checkpointer` 负责单次运行断点续跑；`BaseStore` 负责跨会话长期记忆，固化走 update / audit / dedup 三模式。

## Domain invariants (HypoArgus)

- 原文 bytes 永不整篇进任何 Agent 上下文；论证树节点只携带自身那小段原文加 `paragraph_id` 指针。
- 分区不变式：段落切分后按序拼接逐字节等于原文。
- 回写以段落为唯一原子单位：未命中段逐字节拷回，命中段按 `relation` 分流 —— `oppose→replace`、`advance→rewrite`、`expand→supplement`（段尾追加带审计标识）。
- 节点状态机非法变更一律拦截；`adopted → corrected` 幂等可重试，按 `adopted` 且未 `corrected` 的节点续跑。
- 双轨合并按 12 格矩阵裁决；`credible` 原文遇成立的对立假设贴 `conflict` 交人判，系统不自动裁决。
