---
id: T-07
title: React 工作台（单页 live + 回放 + 嵌入式 HITL 卡片）
status: done
assignee: "Karlzzb"
blocked_by: ["T-06", "T-04", "T-02"]
covers_adr: []
covers_prd: ["§7", "§6.5", "§10.1", "§14.2"]
layer: [web, tests]
type: feature
---

# T-07 — React 工作台

## Source

- PRD §7（前端工作台页面交互规范·无弹窗单页一体化）、§6.5（前端强制同步处理逻辑）、§10.1（MANIFEST 单一源驱动骨架）、§14.2（前端提示文案）。
- 基线：仓库根 `web/` **不存在**；前端是全新 React + Vite 项目，与 conda `HypoArgus` 解耦（不混入 `src/`，避免污染 setuptools/ruff/mypy 边界，PRD §1.5）。

## What to build

落地单页面一体化智能体可视化工作台，消费 T-04 HTTP 与 T-06 WS，渲染骨架来自 T-02 `build_graph_view`。
四大区域无跳转、无弹窗；HITL 用**嵌入式交互卡片**（不遮挡流程图）。

决策性要点（区域，PRD §7.1）：

- **顶部会话管理栏**：新建对话（前端生成新 `session_id`、清缓存、重连 WS）；历史会话列表（当前用户 `session_owner` 近 30 min 存活 session，点击切换重连、按 `trace_id` 列轮次）；状态栏（空闲 / 执行中 / 待人工输入）；风险提示浮层。
- **左侧智能体流程图**：骨架来自 `graph_static`（`build_graph_view`）；节点状态由事件动态更新（未执行 / 运行中 / 已完成 / 待人工输入 / 执行中断）；HITL 节点高亮「待输入」；回放环 `node_instance` 角标「×N」或执行栈展开；点击节点 → 中间面板加载该 `node_instance` 输入 / 完整 CoT / 中间产出。
- **中间推理详情面板（双 Tab）**：
  - Tab1 实时推理（默认）：按 `node_id`+`node_instance` 分组流式 CoT、增量 token 打字机渲染；节点下展示中间产出；HITL 暂停时面板底部嵌入式交互卡片（机器提问 / 输入提示 / 文本框 / 提交按钮，提交后销毁）。
  - Tab2 历史回放：查 `trace_events`（按 `session_id`+`trace_id`+`event_seq`）拉该 trace 全部事件，**100% 复用实时渲染组件**按 `event_seq` 顺序复现。
- **底部对话输入区**：空闲文本框 + 发送（`query` 调 `/api/agent/run`）；执行中置灰锁定；HITL 暂停底部锁定、仅上方嵌入式卡片提交 `human_response`（一期自由文本）。

WS 客户端强制同步逻辑（PRD §6.5）：

- 收 `graph_static` 渲染静态骨架（仅可见节点）；收 `trace_start` 清空动态数据、只处理当前 trace；记录当前最大 `event_seq`、丢弃序号小于该值的滞后消息；收 `stream_abort` 停止等待、展示「执行中断」；切换会话 / 刷新断开 WS、销毁本地缓存、重连后按 `event_seq` 从 `trace_events` 回放到最新再接 live；忽略 `heartbeat`。

可见性：前端据 `visible` 渲染骨架；`visible=False` 节点不出现在 `graph_static`（T-02 已产），前端不另判。

提示文案（PRD §14.2）：执行中关闭未保存输入 / HITL 超时 / 重复提交 / 会话数达上限 / 权限拒绝 / 实时思考暂不可用（背压极端）。

## Acceptance criteria

- [x] `web/` React + Vite 项目落地，独立 node 工具链，与 conda `HypoArgus` 解耦，不混入 `src/`。
- [x] 四大区域单页一体化、无跳转、无弹窗；HITL 用嵌入式交互卡片（不遮挡流程图），提交后销毁。
- [x] 流程图骨架来自 `graph_static`；节点状态随事件动态更新；HITL 节点高亮「待输入」；回放环 `node_instance` 角标 / 栈展开。
- [x] 实时推理 Tab：`node_id`+`node_instance` 分组流式 CoT、打字机渲染、节点中间产出展示。
- [x] 历史回放 Tab：查 `trace_events` 全量事件，**复用实时渲染组件**按 `event_seq` 复现，与实时流同源同表。
- [x] WS 强制同步逻辑齐全：`graph_static` 渲骨架、`trace_start` 清动态、`event_seq` 滤乱序、`stream_abort` 停等待、切换 / 刷新断连重连回放、忽略 `heartbeat`。
- [x] 底部输入区状态切换正确（空闲可发、执行中置灰、HITL 暂停锁定仅卡片提交）。
- [x] §14.2 提示文案齐全（含背压极端「实时思考暂不可用」）。
- [x] E2E（Playwright，按用户全局规范「真实用户交互」）：浏览器驱动完整一次修订（发起 → 实时 CoT → HITL 卡片提交 → 续跑 → 终态），刷新 / 切会话重连回放正确，UI 像素级无错位（发现任何视觉瑕疵一并修）。
- [x] 前端质量门通过（lint + typecheck + build）。

## 实现备注 / 发现的出入（先报后改）

- **`translator.py` 的 `llm_thinking` 事件名 bug（已修，TDD）**：翻译层只匹配 `on_llm_stream`，
  而 LangChain chat 模型（含真 Qwen `ChatOpenAI`）发的是 `on_chat_model_stream` ——故 `llm_thinking`
  在生产永不产，前端实时 CoT 无数据源。此为本任务核对「文档引用行号与代码现状一致」时发现的
  真实出入：T-05/§6.4 声称产 `llm_thinking`，但代码不产。修法为在 `translator.py` 把
  `on_chat_model_{start,stream,end}` 与 `on_llm_*` 等价处理（保留两套词汇），属修 bug、非 ADR 偏离
  （`llm_thinking` 事件本即 ADR-0023 事件词汇一部分），未起 ADR-0024+。新增 `tests/test_translator.py`
  两条用例 + `tests/test_streaming_fake_llm.py` 一条端到端用例佐证。
- **确定性 E2E 后端（`e2e/`）**：浏览器无法在 WebSocket 上设 `X-User-Id` 头，故前端用相对 URL
  （开发经 Vite 代理注入头、生产经 Nginx 注入）。为避免 E2E 依赖真实 Qwen（网络 / token / 抖动），
  新增 `e2e/_fakes.py` 的 `StreamingFakeChat`（逐字符流式吐 schema 默认 JSON → `astream_events` 产
  `on_chat_model_stream` → 翻译层产 `llm_thinking`）+ `e2e/dev_server.py`（镜像 `server.serve`，四 seam
  全换 fake + InMemory side metadata + 真实 Postgres checkpointer + 全可见可见性）。语义结果与离线
  `Fake*` 同（空 proposals → 全 background → 终稿逐字节原文），但带流式 token，确定性、无网络消耗。
  生产仍用仓根 `config/visibility.yaml`（隐藏 `parse+partition`，CoT 来自可见下游节点）。
- **历史回放复用实时组件**：`trace_events` 无 HTTP 端点（T-08 未到），回放仅经 WS 重连——后端重放
  `graph_static → trace_start → 全量事件`，`syncReduce` 据 `trace_start` 自动清动态态并按 `event_seq`
  复现，故实时 Tab 与回放 Tab 共用同一 `CotView` 组件（PRD §13.3「与真实执行完全匹配」）。
- **会话列表**：服务端「近 30 min 存活 session」过滤属 T-08（无 list 端点）；前端展示 localStorage
  自管会话，点击切换即重连 WS 重放。

## Blocked by

- T-06（WS 事件流 + `graph_static`）。
- T-04（HTTP `/api/agent/run` + `/api/agent/graph`）。
- T-02（`build_graph_view` / `visible` 元数据驱动骨架）。

## Notes

- 前端只感知 `session_id`（localStorage 生成），不生成 `trace_id` / 不判轮次——fresh-run vs resume 由 Python 判（CONTEXT.md「运行时身份」）。
- 历史回放复用实时组件是 PRD §13.3「与真实执行完全匹配」的关键；不得为回放另写一套渲染。
- 按 CLAUDE.md 全局规范，前端 E2E 用 Playwright、像素级标准；任何视觉错位（即便与本任务无关）一并提修。
