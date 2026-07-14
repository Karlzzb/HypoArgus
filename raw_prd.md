
---

# LangGraph 多智能体可视化服务 PRD V3.3
## 文档基础信息
- **文档名称**：LangGraph 多智能体可视化工作台服务需求文档
- **适用阶段**：第一期开发（无 Redis、单实例内存缓存、零侵入原有智能体）
- **受众**：Python 后端开发、前端开发、后续 Java 服务对接开发、测试
- **核心目标**
  1. 零侵入现有 LangGraph 智能体代码，实现前端完整可视化展示 CoT 思考、节点状态、中间产出、人机交互
  2. 彻底解决前端展示与真实智能体执行时序不同步、数据错乱问题
  3. 对外 API 永久兼容，当前前端直调、后期 Java 业务服务转发无需改动接口
  4. 采用页面嵌入式 HITL 交互卡片，摒弃弹窗，保证上下文完整可见
  5. 分层抽象存储，一期内存缓存，二期无缝切换 Redis 分布式持久化，无业务重构成本
- **核心约束**
  - 一期不引入 Redis，不修改 LangGraph State 序列化、持久化逻辑
  - 单 Python 服务实例部署，暂不支持分布式多实例扩容
  - 会话数据仅存活于当前浏览器页面会话，刷新/重启服务丢失上下文
  - WebSocket 数据流直连 Python，Java 上层不处理流式消息，规避转发延迟错乱
- **关键非功能指标（POC 目标）**
  - 并发会话数：≥ 50 个活跃会话同时可视化，无明显延迟
  - 事件推送延迟：WebSocket 消息从产生到前端渲染 ≤ 200ms（P50）
  - 内存占用：单会话平均内存 ≤ 5 MB，最大内存会话数保护阈值 100 个
  - 端到端对话延迟（从 HTTP 请求到首个可视化事件）：≤ 2s

---

# 一、整体架构与调用链路
## 1.1 分层架构
1. 前端展示层：智能体可视化工作台（单页面一体化布局）
2. 上层业务层（可选，二期接入）：Java 业务服务（仅鉴权、转发 HTTP，不处理 WebSocket）
3. Python 接入层
   - HTTP 统一接口：发起对话、HITL 人工回填、获取图结构
   - WebSocket 长连接：时序化实时事件推送（可视化唯一数据源），内置心跳保活与背压控制
4. 调度管理层：会话缓存抽象层、带 TTL 的内存锁、时序事件管理、闲置数据清理、会话数量上限控制
5. LangGraph 执行层：原生 Graph 执行、全局无侵入回调采集事件、interrupt/resume 原生 HITL 能力
6. 观测层：自建 Langfuse 全链路日志埋点 + 基础服务健康与指标端点

## 1.2 两条调用链路（接口完全统一，无差异化开发）
### 链路 A（当前一期）
前端工作台 → Nginx（强制 HTTPS/WSS） → Python HTTP/WebSocket
### 链路 B（二期接入 Java，接口零改动）
前端工作台 → Java 服务（纯透传请求头、入参） → Python HTTP
> 关键：WebSocket 前端直接连接 Python 服务，流式数据不经过 Java，从根源杜绝多层转发导致的时序错乱、展示不同步。

## 1.3 时序同步核心保障（解决展示与执行错位）
1. 每条 WebSocket 事件携带 `event_seq` 自增序列号，同一条 trace 链路严格递增，前端按序号过滤乱序、滞后消息
2. 单 session_id 同一时间仅允许一条执行链路运行，内存锁（带超时释放）拦截并发请求，无多流程事件混杂
3. 所有事件绑定 `session_id + trace_id` 双标识，前端自动过滤非当前活跃链路消息
4. 执行中断主动推送 `stream_abort` 事件，前端停止渲染，避免无限加载等待
5. 实时流仅做动态预览，历史回放以 Langfuse 持久化日志为可信基准，用于数据丢失兜底校验

---

# 二、全局唯一 ID 规范（全链路统一）
## 2.1 ID 定义
1. `user_id`：用户账号 ID，上层登录侧维护，存放请求头 `X-User-Id`，Python **强制校验归属**（见安全章节）
2. `session_id`：对话窗口唯一标识，前端生成存储 localStorage；一个浏览器标签页对应独立 session_id，多窗口数据完全隔离
3. `trace_id`：单次完整智能体执行链路 ID，Python 内部生成；HITL 断点续跑复用同一个 trace_id，上层无感知
4. `event_seq`：事件时序序号，单 trace 内从 0 自增，用于前端消息排序防乱序

## 2.2 ID 传递规则
1. HTTP 请求必填 `session_id`，其余 ID 由 Python 内部生成、关联、存储
2. WebSocket 连接地址必须携带 `session_id` 参数，连接成功绑定会话上下文
3. 内存缓存、WebSocket 事件、Langfuse 日志全部组合 `session_id + trace_id` 隔离数据

---

# 三、安全设计（工业级增强）
## 3.1 传输与信道安全
- 全站强制 HTTPS，WebSocket 使用 `wss://`，Nginx 层面完成 SSL 终结。
- 内部服务间通信若存在非信任网络，也应启用 mTLS 或内网隔离（一期可暂缓，但需预留配置）。

## 3.2 会话所有权校验
- Python 后端维护 session → user_id 的映射（一期内存字典 `session_owner: dict[str, str]`）。
- 所有 HTTP 请求及 WebSocket 连接建立时，必须校验 `X-User-Id` 与 `session_id` 对应关系，不允许跨用户访问。
- 若 `session_id` 不存在，则创建并绑定当前 `X-User-Id`。
- 若已存在但 `user_id` 不匹配，返回 `403 Forbidden`，WebSocket 连接直接关闭（状态码 4001）。
- 校验逻辑集中在中间件层，避免业务代码遗漏。

## 3.3 敏感信息保护
- 工具调用入参/返回、LLM 思考内容在推送前端和记录日志前，需经过可配置的脱敏过滤器（一期可提供简单正则替换如手机号、身份证，或默认不过滤但预留钩子）。
- 日志中禁止记录完整 `X-Token`，仅记录其哈希值（或前 8 位 + 哈希后缀）。

## 3.4 接口访问控制
- 所有 `/api/agent/*` 接口及 WebSocket 端点均需携带有效 `X-Token`，由 Python 调用上层用户中心进行验证（或本地 JWT 验签），鉴权失败返回 401。

---

# 四、存储层设计（一期内存缓存，抽象分层兼容二期 Redis）
## 4.1 分层设计原则
定义统一缓存抽象基类，一期仅实现内存缓存；二期新增 Redis 实现，上层业务代码完全不用修改，无重构成本。
### 抽象基类 SessionCacheBase
```python
class SessionCacheBase:
    def get_state(self, session_id: str): pass
    def save_state(self, session_id: str, state): pass
    def get_pause_meta(self, session_id: str): pass
    def save_pause_meta(self, session_id: str, meta): pass
    def lock_session(self, session_id: str, ttl: int = 30) -> bool: pass
    def unlock_session(self, session_id: str): pass
    def get_session_owner(self, session_id: str) -> str: pass
    def set_session_owner(self, session_id: str, user_id: str): pass
    def clean_idle(self): pass
```

## 4.2 一期实现：InMemorySessionCache（无 Redis，零智能体改造）
### 1. 内存会话 State 缓存
- 全局字典 `in_memory_session_state: dict[str, LangGraph原生State对象]`
- key：session_id
- value：直接存储 Graph 原生 State，**但强制要求 State 为可序列化对象（仅包含基本数据类型、列表、字典、Pydantic 模型等），禁止存储数据库连接、文件句柄、回调函数等不可序列化对象**。开发阶段需提供单元测试，通过 `pickle.dumps(state)` 验证。
- 清理规则：闲置 30 分钟未操作自动删除；同时会话总数达到阈值（可配置，默认 100）时，按 LRU 淘汰最早未使用的会话。

### 2. 内存 HITL 断点缓存
- 全局字典 `in_memory_pause_info: dict[str, PauseMeta]`
- PauseMeta 结构体
```python
@dataclass
class PauseMeta:
    trace_id: str
    last_event_seq: int
    human_question: str
    hint: str
    pause_time: int  # 时间戳
```
- 闲置 30 分钟自动清理，超时断点失效

### 3. 内存并发执行锁（带 TTL）
- 全局字典 `running_session_lock: dict[str, float]`，值为锁过期时间戳（当前时间 + TTL）。
- 加锁时先清理已过期的锁，然后尝试设置，若键已存在且未过期则返回加锁失败。
- 锁默认 TTL 为 300 秒（5 分钟），Graph 执行正常结束必须主动释放；另起后台线程每 30 秒扫描清理过期锁，防止死锁。

### 4. 会话所有权映射
- 全局字典 `session_owner: dict[str, str]`，记录 session_id → user_id。
- 通过 `set_session_owner` / `get_session_owner` 操作。

## 4.3 一期能力边界（需同步产品与测试）
### 支持能力
- 前端全量可视化：CoT 流式渲染、节点状态、中间产出、流程图动态展示
- 单页面不刷新前提下 HITL 人机交互、断点续跑，trace 链路连贯无断层
- WebSocket 时序防乱序、事件隔离、执行中断通知
- 单用户多对话窗口隔离，一套 API 兼容前端直调、Java 转发
- Langfuse 日志完整采集，支持历史回放校验真实执行流程
- 会话所有权强制校验，跨用户访问拒绝

### 一期不支持（二期 Redis 迭代补齐）
- 浏览器刷新、页面关闭重开，无法恢复历史对话与 HITL 断点（可前端草稿提示）
- Python 服务重启、容器发布，全部内存会话数据清空
- 多实例负载均衡部署，会话状态无法跨实例共享
- 长期会话记忆持久化（超过 30 分钟）

## 4.4 二期扩展规划（仅新增 Redis 实现，上层无改动）
新增 `RedisSessionCache` 实现 `SessionCacheBase` 抽象类，封装 State 序列化、Redis 读写、分布式锁逻辑；切换存储仅修改实例初始化配置，Graph、API、前端逻辑完全不变。

---

# 五、HTTP 统一接口规范（永久不变，兼容前后端调用）
## 5.1 基础信息
- **发起对话/续跑**：`POST /api/agent/run`
- **获取图结构**：`GET /api/agent/graph`
- 鉴权请求头：`X-Token`（统一身份凭证）、`X-User-Id`（用户账号 ID）
- 全局超时：15s（HTTP），Graph 内部执行超时可通过配置单独控制
- 接口用途：统一承载普通对话发起、HITL 人工回填续跑，不新增额外路由

## 5.2 `/api/agent/run` 请求入参
```json
{
  "session_id": "string 必填，会话窗口唯一标识",
  "query": "string 可选，用户原始提问，普通对话场景传入",
  "human_response": "string 可选，HITL 断点人工回复，续跑场景传入",
  "biz_trace_id": "string 可选，上层 Java 业务链路 ID，仅透传日志"
}
```
参数强约束：`query` 与 `human_response` 互斥，禁止同时传参，接口层强制校验拦截。

## 5.3 `/api/agent/run` 返回体固定结构
```json
{
  "code": "SUCCESS | NEED_HUMAN_INPUT | FAILED",
  "msg": "通用状态描述文本",
  "data": {
    "final_content": "流程最终回答，仅 code=SUCCESS 存在",
    "human_question": "人机交互提问文本，仅 code=NEED_HUMAN_INPUT 存在",
    "hint": "用户输入提示，仅 code=NEED_HUMAN_INPUT 存在"
  },
  "error_info": {
    "error_code": "细分错误枚举",
    "detail": "异常详情，仅 code=FAILED 展示"
  }
}
```
### 细分错误码枚举
| 错误码 | 含义 |
|--------|------|
| `LOCK_EXIST` | 会话正在执行/存在未处理 HITL 断点，禁止重复提交 |
| `PAUSE_EXPIRED` | HITL 断点闲置超时失效 |
| `GRAPH_TIMEOUT` | 智能体执行超时 |
| `CONNECTION_ABORT` | 执行中途用户断开 WebSocket |
| `PARAM_ERROR` | 入参格式非法、参数互斥冲突 |
| `FORBIDDEN` | 会话不属于当前用户 |
| `SESSION_LIMIT` | 活跃会话数达到上限，请关闭其他窗口后重试 |

## 5.4 `GET /api/agent/graph` 接口（新增）
- **用途**：返回 LangGraph 的静态节点和边拓扑，供前端初始时绘制完整流程图骨架。
- **鉴权**：同 `/api/agent/run`。
- **响应示例**：
```json
{
  "code": "SUCCESS",
  "data": {
    "nodes": [
      { "id": "start", "label": "开始", "type": "system", "color": "#909399" },
      { "id": "judge_node", "label": "合规评审节点", "type": "judge", "color": "#E6A23C" }
    ],
    "edges": [
      { "source": "start", "target": "judge_node" }
    ]
  }
}
```
- 前端可在连接 WebSocket 前调用，提前渲染完整节点拓扑，避免执行中动态添加造成的布局抖动。

---

# 六、WebSocket 实时事件规范（可视化核心，解决同步错乱）
## 6.1 连接地址与维持
- **连接 URL**：`wss://域名/ws/agent/stream?session_id={session_id}`
- **鉴权**：连接 Header 携带 `X-Token` 和 `X-User-Id`，建立后立即校验会话所有权，失败关闭连接（自定义 close code 4001）。
- **连接唯一性**：同一 session_id 仅允许一条 WebSocket 连接，新连接建立时旧连接被主动关闭（服务端推送 `stream_abort` 后关闭）。
- **心跳保活机制（新增）**：
  - 服务端每隔 30 秒检测一次：若在过去 30 秒内未向客户端发送任何数据帧，则主动发送一个 `heartbeat` 事件。
  - `heartbeat` 事件结构：
    ```json
    {
      "session_id": "...",
      "trace_id": null,
      "event_seq": -1,
      "event_type": "heartbeat",
      "payload": {}
    }
    ```
  - 前端收到 `heartbeat` 后直接丢弃，不做任何渲染与状态更新。
  - 此机制可防止 Nginx 或浏览器因静默期过长而断开 WebSocket 连接。

## 6.2 背压与流控机制（新增）
- 在每个 WebSocket 连接内部，使用 `asyncio.Queue`（最大容量可配置，默认 256）缓冲待发送事件。
- 事件生产者（LangGraph Callback 线程/协程）将事件放入队列：
  - 若队列未满，直接放入。
  - 若队列已满且当前事件为 `llm_thinking` 类型，则尝试将其 token 追加到队列中最后一个 `llm_thinking` 事件的 `token` 字段，合并为一个事件（减少队列条目）；若无法合并（如不存在前一个同类事件），则阻塞等待直到有空间。
  - 对于其他不可合并的关键事件（如 `node_start`、`human_pause`、`stream_abort`），若队列满则阻塞等待，保证事件不丢失。
- 一个独立的发送协程从队列中取出事件并发送到 WebSocket，自然实现对生产者的背压。
- 需在 `/metrics` 中暴露：
  - `ws_event_queue_size`：当前队列长度（实时）
  - `ws_event_queue_full_total`：队列满次数累计（可用于告警）

## 6.3 单条消息统一结构
```json
{
  "session_id": "会话 ID",
  "trace_id": "单次链路 ID",
  "event_seq": "数字，同 trace 内自增时序序号，heartbeat 事件为 -1",
  "event_type": "事件类型",
  "payload": "对应事件数据对象"
}
```

## 6.4 全生命周期事件定义
- `graph_static`：连接建立后首条推送（event_seq = -1），包含图静态结构，前端据此渲染骨架。
- `trace_start`：新 trace 开始，前端清空动态渲染状态，payload 空。
- `node_start`：节点开始执行，payload 含 node_id、label、type、color、desc、input。
- `llm_thinking`：CoT 增量文本，payload 含 node_id、token、full_thought。
- `tool_call`：工具调用事件，payload 含工具名、入参、返回结果。
- `node_output`：节点中间产出，payload 含 node_id、output。
- `node_end`：节点执行结束，payload 含 node_id。
- `human_pause`：触发 HITL，payload 含 node_id、question、hint。
- `stream_finish`：全链路正常结束，payload 空。
- `stream_abort`：链路异常终止，payload 含可选的 abort_reason。
- `heartbeat`：连接保活心跳，event_seq = -1，前端丢弃。

## 6.5 前端强制同步处理逻辑
1. 收到 `graph_static` 渲染静态流程图骨架。
2. 收到 `trace_start` 清空本地渲染动态数据，只处理当前 trace 事件。
3. 本地记录当前最大 `event_seq`，丢弃序号小于该值的滞后消息。
4. 收到 `stream_abort` 立即停止等待消息，展示「执行中断」。
5. 切换会话或刷新页面时，主动断开 WebSocket 并销毁本地缓存。
6. 忽略 `heartbeat` 事件。

---

# 七、前端工作台页面交互规范（无弹窗，单页面一体化）
## 7.1 页面名称：智能体可视化工作台
页面固定四大区域，无页面跳转、无悬浮弹窗，全部交互收敛在页面内。
### 区域 1：顶部会话管理栏（多窗口会话切换）
1. 新建对话按钮：前端生成全新随机 session_id，清空页面全部缓存、重连 WebSocket
2. 历史会话列表：展示当前用户所有内存存活 session；点击切换会话，重连对应 WebSocket 并加载回放日志
3. 状态栏：展示当前会话状态（空闲 / 执行中 / 待人工输入）
4. 风险提示浮层：常驻轻提示「刷新页面将丢失未完成交互记录」

### 区域 2：左侧 智能体流程图
1. 自动渲染 Graph 完整节点拓扑，初始骨架来自 `graph_static` 事件，节点状态由后续事件动态更新
2. 节点五种状态：未执行、运行中、已完成、待人工输入、执行中断
3. HITL 触发时对应节点高亮黄色「待用户输入」，直观展示流程卡点
4. 点击任意节点，中间面板自动加载该节点输入、完整 CoT、中间产出

### 区域 3：中间推理详情面板（双 Tab 切换）
#### Tab1 实时推理（默认激活）
1. 按节点分组展示流式 CoT 思考文本，增量 token 实时打字机渲染
2. 每个节点下方展示完整中间产出数据
3. HITL 暂停状态：面板底部弹出**嵌入式交互卡片**（内嵌页面，不遮挡流程图）
   - 卡片内容：机器提问文本、输入提示、文本输入框、提交按钮
   - 用户提交回复后卡片自动销毁，继续接收后续执行事件
#### Tab2 历史回放
1. 调用 Langfuse 查询接口，拉取当前 session 下全部 trace 完整日志
2. 100% 复用实时页面渲染组件，完整复现 CoT、节点流转、人机交互全流程
3. 回放数据为持久化基准，用于校验实时流丢失、展示错位问题

### 区域 4：底部对话输入区
1. 空闲状态：文本输入框+发送按钮，输入 query 调用 `/api/agent/run` 发起对话
2. 执行中状态：输入框置灰锁定，禁止重复提交
3. HITL 暂停状态：底部输入框锁定，仅可通过上方嵌入式卡片提交人工回复

## 7.2 HITL 完整交互流程（无弹窗，时序完全同步）
1. 用户发送提问，建立 WebSocket 连接，收到 `graph_static` 初始化骨架，Graph 开始执行，推送 `trace_start` 清空动态状态
2. Graph 执行至 interrupt 节点，推送 `human_pause` 事件；HTTP 接口同步返回 `NEED_HUMAN_INPUT`
3. 左侧流程图标记节点「待输入」，中间面板渲染嵌入式交互卡片
4. 用户填写内容提交，调用同一 HTTP 接口传入 `session_id + human_response`
5. Python 读取内存 PauseMeta，复用原始 trace_id，顺延 event_seq 接续执行 Graph
6. WebSocket 持续推送后续节点、CoT、中间产出，页面无缝接续渲染无断层
7. 全流程执行完毕推送 `stream_finish`，交互卡片永久销毁

---

# 八、全场景业务流程
## 8.1 普通对话流程（无 HITL）
1. 前端生成/读取 session_id，建立 WebSocket 连接，完成所有权校验，收到 `graph_static`
2. 用户输入 query，调用 `/api/agent/run` 接口
3. Python 内存缓存读取该 session 历史 State，无则新建空 State
4. 加内存执行锁（TTL 5 分钟），启动 LangGraph 执行，全局 Callback 实时推送 WebSocket 事件（经背压队列）
5. 执行完成，更新内存会话 State，释放执行锁，推送 `stream_finish`
6. HTTP 接口返回 code=SUCCESS 与最终回答

## 8.2 HITL 人机暂停 + 续跑流程
1. Graph 执行触发 interrupt 中断
2. 保存当前完整 State 至内存会话缓存，生成 PauseMeta 存入断点缓存
3. WebSocket 推送 `human_pause`，HTTP 返回 `NEED_HUMAN_INPUT`，释放执行线程（**但不释放锁**，锁过期时间重置为 30 分钟）
4. 用户填写回复提交接口，Python 校验内存存在有效 PauseMeta
5. 读取历史 State，使用 LangGraph 原生 `resume()` 传入用户回复续跑
6. 新事件序列号从断点记录的 last_event_seq+1 顺延，复用原有 trace_id
7. 执行完成删除内存 PauseMeta，更新会话 State，推送 `stream_finish`，释放执行锁

---

# 九、异常场景完整处理方案
## 9.1 执行中刷新/关闭浏览器（WebSocket 断开）
- Python 捕获连接断开信号，推送 `stream_abort` 事件，终止当前 Graph 执行并**立即释放执行锁**。
- 保留当前半截 State 在内存缓存，**不生成 HITL 断点，无法续跑本次流程**。
- 用户重新打开页面，原有 WebSocket 失效，复用 session_id 发起新对话时，读取历史 State，新建 trace 链路。

## 9.2 HITL 断点闲置 30 分钟超时
- 后台定时任务自动清理过期 PauseMeta 缓存，同时释放对应会话锁。
- 用户再次提交回复，接口返回 `PAUSE_EXPIRED` 错误码。
- 前端提示「交互已超时，请重新发起对话」。

## 9.3 同一窗口重复点击发送（并发提交）
- 内存锁检测会话存在未过期锁或有效断点。
- 接口直接返回 `LOCK_EXIST` 错误，前端提示「当前对话正在处理，请稍后」。

## 9.4 多标签页打开同一个 session_id
- 新 WebSocket 连接建立时，旧连接被推送 `stream_abort` 后断开，保证唯一流式连接。
- 前端规范：每个标签页独立生成全新 session_id，从源头规避。

## 9.5 Python 服务重启/发布
- 全部内存字典数据清空，所有会话、断点、执行锁全部销毁。
- 用户再次操作等同于全新对话，前端无额外改造。

## 9.6 执行锁超时
- 若 Graph 执行或人工等待导致锁超过 TTL 未释放（如代码死循环），后台扫描线程将强制清理过期锁，并中断对应 Graph 执行（通过 cancel token）。
- 前端收到 `stream_abort`，提示「执行超时，请重试」。

## 9.7 活跃会话数超限
- 当 `in_memory_session_state` 大小达到配置上限（默认 100）且无法淘汰更多过期会话时，新会话创建（`/api/agent/run`）返回 `SESSION_LIMIT` 错误。
- 前端提示「对话窗口已达上限，请关闭闲置窗口后重试」。

## 9.8 WebSocket 心跳丢失（新增）
- 若前端或中间网络代理在 90 秒内未收到任何消息（包括 heartbeat），应视为连接异常，主动关闭 WebSocket 并尝试重连。
- 重连后复用原有 session_id，前端重新调用 `/api/agent/run` 或手动续跑（需根据状态提示用户）。

## 9.9 背压队列溢出（新增）
- 若 `ws_event_queue_full_total` 指标持续增长，运维需考虑扩容或降低单会话生成速率。
- 极端情况下，若背压无法缓解导致内存压力，可临时丢弃 `llm_thinking` 事件（仅保留最终 `node_output`），并在前端给出「实时思考文本暂不可用」的提示，但必须确保关键控制事件不丢失。

---

# 十、底层 LangGraph 无侵入改造规范（零原有业务代码改动）
## 10.1 全局统一节点元数据配置
一处维护所有节点语义信息，自动注入 WebSocket 事件与 Langfuse 日志，同时支撑静态图接口。
```python
NODE_META = {
  "judge_node": {
    "label": "合规评审节点",
    "node_type": "judge",
    "color": "#E6A23C",
    "desc": "校验输出合规性"
  }
}
```

## 10.2 全局回调采集器（无侵入 Graph）
自定义全局 Callback 挂载至 Graph 全局配置，无需修改业务 Graph 代码，自动捕获全量执行事件：
1. LLM 增量输出 → 推送 llm_thinking 流式思考事件
2. 节点启动、输出、结束自动推送对应事件
3. 所有事件自动绑定 session_id、trace_id、event_seq
4. 事件数据同步写入 Langfuse Span 元数据

## 10.3 LangGraph 执行强制约束
1. Graph 服务启动全局单例初始化，禁止每次 HTTP 请求重复构建 Graph 实例
2. HITL 恢复仅使用原生 `resume` 参数传递人工输入，禁止直接修改 State 字段
3. 单次 trace 完整生命周期绑定独立 Langfuse Trace，续跑不新建日志链路
4. **State 可序列化约束**：所有 State 字段必须支持 `pickle`，由 CI 流水线中的冒烟测试保证

---

# 十一、可观测性与运维
## 11.1 健康检查与指标端点
- `GET /health`：返回服务状态，包含内存会话数、活跃锁数量等简要信息，供负载均衡/监控使用。
- `GET /metrics`：Prometheus 格式指标，至少包含：
  - `active_sessions`：当前存活会话数
  - `active_locks`：当前执行锁数量
  - `ws_connections`：活跃 WebSocket 连接数
  - `event_push_latency_seconds`：事件从产生到推送到前端的延迟分布
  - `graph_execution_duration_seconds`：单次 Graph 执行耗时
  - `ws_event_queue_size`：当前背压队列长度（按连接）
  - `ws_event_queue_full_total`：队列满次数累计
  - `langfuse_errors_total`：Langfuse 写入失败计数

## 11.2 日志与追踪
- 所有组件日志结构化为 JSON，包含 `session_id`、`trace_id`、`user_id`（脱敏后）。
- Langfuse 不可用时应降级处理：仅本地记录错误日志，不阻塞正常对话流程。

## 11.3 资源监控与告警
- 内存会话数超过阈值 80% 时，触发告警（如钉钉/邮件），通知运维提前扩容或排查。

---

# 十二、二期 Java 业务服务接入兼容方案（Python 接口零修改）
## 12.1 Java 服务职责
1. 用户登录后生成/下发 session_id，存储至前端 localStorage
2. 接收前端 HTTP 请求，**完整透传全部入参、请求头（X-Token、X-User-Id、biz_trace_id）** 转发至 Python `/api/agent/run`
3. 原样返回 Python 返回 JSON 给前端，仅根据 code 做业务侧日志、告警
4. **必须确保 Java 转发时不会修改或丢弃 `X-User-Id`，否则 Python 端会话所有权校验将失效**
5. 不处理 WebSocket 连接：前端直连 Python 流式服务，Java 不转发流式数据

## 12.2 兼容保障
- Python HTTP 接口入参、出参结构永久不变，Java 转发逻辑无需任何改造
- 可视化数据流全部直连 Python，Java 无需适配 CoT、节点、HITL 交互逻辑
- ID 体系、错误码、交互流程统一，Java 仅做简单透传

---

# 十三、开发验收 Checklist
## 13.1 存储层一期无 Redis 专项验收
- [ ] 未引入 Redis，原有 LangGraph 智能体代码无任何修改，State 可序列化且通过 pickle 测试
- [ ] 完成缓存抽象基类，内存缓存实现完整，预留 Redis 扩展入口
- [ ] 闲置会话、断点 30 分钟自动清理，会话数达上限时淘汰或拒绝新会话
- [ ] 页面刷新后丢失未完成 HITL 断点，新建对话正常读取历史 State
- [ ] 执行锁具有 TTL，死锁可被后台扫描解除

## 13.2 安全专项验收
- [ ] HTTP 与 WebSocket 全部使用 HTTPS/WSS
- [ ] 跨用户访问 session 被拦截，返回 403 或关闭连接
- [ ] 鉴权 token 缺失或无效时拒绝连接
- [ ] 敏感信息脱敏钩子存在并可配置

## 13.3 前端展示时序同步验收
- [ ] WebSocket 携带 event_seq，前端自动过滤乱序、滞后消息，无节点闪烁、文字跳变
- [ ] 收到 `graph_static` 提前绘制静态骨架
- [ ] trace_start 事件清空页面动态数据，无多轮流程数据叠加错乱
- [ ] 执行中断推送 stream_abort，前端停止渲染，无无限加载
- [ ] HITL 续跑事件序列号顺延，流程图、CoT 面板无数据断层、重复渲染
- [ ] 历史回放复用实时页面组件，Langfuse 日志与真实执行流程完全匹配
- [ ] 静默期超过 30 秒仍能收到 heartbeat，WebSocket 未断开

## 13.4 背压与流控验收（新增）
- [ ] 当 LLM 生成速度超过网络发送速度时，队列满触发 token 合并，无事件丢失
- [ ] 关键控制事件（human_pause、stream_abort）不会因队列满而被丢弃
- [ ] `/metrics` 中可观测队列深度与满次数

## 13.5 HITL 人机交互验收
- [ ] 不使用悬浮弹窗，页面嵌入式交互卡片，上下文完整可见
- [ ] 续跑复用原始 trace_id，Langfuse 整条日志链路无分裂
- [ ] 断点 30 分钟超时自动失效，提交返回明确过期错误提示
- [ ] 执行中/待输入状态拦截重复提交，返回 LOCK_EXIST

## 13.6 多会话隔离验收
- [ ] 不同 session_id 数据、事件、Trace 完全隔离，无串会话
- [ ] 新建对话生成全新 session_id，页面全部状态重置
- [ ] 切换会话自动重连 WebSocket，加载对应会话历史

## 13.7 Java 对接兼容验收
- [ ] 一套 HTTP 接口同时支持前端直调、Java 转发调用，无差异化路由
- [ ] WebSocket 前端直连 Python，Java 不参与流式数据传输
- [ ] 上层无需感知 trace_id、内存缓存、断点逻辑，仅维护 session_id

## 13.8 底层稳定性验收
- [ ] 全局 Callback 无侵入采集所有节点、CoT、工具事件，无数据丢失
- [ ] 执行中途页面关闭自动终止 Graph，不会残留僵尸执行进程
- [ ] 所有实时事件同步落地 Langfuse，日志可作为展示不一致问题排查基准
- [ ] `/health` 和 `/metrics` 端点返回正确数据

---

# 十四、风险与产品提示文案
## 14.1 一期方案风险
1. 单实例限制：无法多实例负载均衡，并发量大后需升级 Redis 分布式方案
2. 会话丢失风险：页面刷新、服务发布重启会清空对话上下文，前端增加提示文案：「刷新页面将丢失未完成交互记录」
3. 内存上限风险：在线会话过多占用服务内存，依靠 30 分钟闲置自动清理和会话总数上限控制

## 14.2 前端提示文案
1. 页面顶部常驻轻提示：「当前会话仅保存在页面，刷新后未完成交互将丢失」
2. HITL 断点超时提示：「交互已超时，请重新发起对话」
3. 重复提交拦截提示：「对话正在处理中，请稍后再试」
4. 会话数达上限提示：「同时打开的对话窗口过多，请关闭其他窗口后重试」
5. 连接被拒绝（权限）提示：「无权访问此会话」

---

**附录 A：HITL 交互关键时序示意**
```
前端                     Python服务                  LangGraph
 |                          |                          |
 |-- WS connect ----------->|                          |
 |<-- graph_static ---------|                          |
 |-- POST /run (query) ---->|                          |
 |                          |-- lock + start graph -->|
 |<-- WS trace_start -------|                          |
 |<-- WS node_start --------|                          |
 |<-- WS llm_thinking ------|                          |
 |                          |<-- interrupt (HITL) ----|
 |<-- WS human_pause -------|                          |
 |<-- HTTP NEED_HUMAN_INPUT |                          |
 |                          |                          |
 |-- POST /run (human_resp)>|                          |
 |                          |-- resume() ------------>|
 |<-- WS llm_thinking ------|                          |
 |<-- WS node_end ----------|                          |
 |<-- WS stream_finish -----|                          |
 |<-- HTTP SUCCESS ---------|                          |
```

---

