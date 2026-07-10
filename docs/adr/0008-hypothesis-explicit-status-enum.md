# ADR-0008：假说使用显式状态枚举，与原文状态机对称

## 状态

已接受（2026-07-10）

## 背景

合并矩阵（ADR-0006）里假说有 4 种情况：成立/存疑/被推翻/无。
但 `candidate_hypotheses` 原结构只有 `confidence`（0~1 数字），无法区分「存疑（无支撑也无反证，低分）」与「被推翻（查到反证）」——二者动作截然不同：存疑弱呈现供参考，被推翻直接丢弃不给用户看。
且原文节点状态用显式枚举 `credible/doubtful/error`，假说不应降级为裸数字，两线路应对称。

## 决策

给假说上显式状态枚举，`confidence` 降为排序辅助：

```
candidate_hypotheses: {
  text,
  relation: 'oppose' | 'advance' | 'expand',
  status: 'supported' | 'doubtful' | 'refuted',
  confidence: number
}
```

1. **`status` 是矩阵判定的唯一依据**。`confidence` 不参与「成不成立」，仅用于同一节点内多条 `supported` 假说的排序/展示。
2. **「无假说」由数组为空表达**，不占枚举。
3. 两线路状态机对称：原文 `credible/doubtful/error` ↔ 假说 `supported/doubtful/refuted`。Merge 读两枚举叉乘。

## 权衡

- 显式枚举比阈值判定更防误导（避免把 refuted 当 doubtful 弱呈现给用户）。

## 影响

- `candidate_hypotheses` 新增 `status` 字段。
- Merge 矩阵实现读 `原文.status × 假说.status`，不读 confidence 阈值。
