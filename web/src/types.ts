// T-07 前端契约类型——与后端（src/api_layer）代码现状对齐，单一真相见代码：
//   - HTTP:  src/api_layer/run.py (RunRequest/RunResponse/HumanResponse)
//   - HTTP:  src/api_layer/app.py  (GraphResponse/GraphNodeOut/GraphEdgeOut)
//   - WS:    src/api_layer/ws.py (_message 信封) + trace_store.py (EventType)
//   - 图:    src/api_layer/graph_view.py (GraphNode/GraphEdge/GraphView)
//   - HITL:  src/agents/hitl1/contract.py、src/agents/hitl2/contract.py（detail 形状）
//
// 前端只感知 session_id（localStorage 生成）；trace_id / 轮次由 Python 判。

// --------------------------------------------------------------------------- //
// 图结构（graph_static / GET /api/agent/graph 同形）
// --------------------------------------------------------------------------- //

export interface GraphNode {
  /** 节点 id（不透明，可含 '+'，如 "parse+partition"，禁止拆分）。 */
  id: string;
  label: string;
  type: string;
  color: string | null;
  visible: boolean;
  /** 是否为 HITL 中断节点（hitl1 / hitl2）。 */
  interrupt: boolean;
}

export interface GraphEdge {
  source: string;
  target: string;
  /** 条件边名（如 "replay"），无条件边为 null。 */
  cond: string | null;
  /** 回放边上限（如 hitl1→parse+partition 的 max=3），普通边为 null。 */
  max: number | null;
}

export interface GraphView {
  nodes: GraphNode[];
  edges: GraphEdge[];
  warnings: string[];
}

// --------------------------------------------------------------------------- //
// WS 消息（信封 + event_type + payload）
// --------------------------------------------------------------------------- //

export type EventType =
  | "graph_static"
  | "trace_start"
  | "node_start"
  | "node_output"
  | "node_end"
  | "llm_thinking"
  | "tool_call"
  | "human_pause"
  | "stream_finish"
  | "stream_abort"
  | "heartbeat";

/** WS 信封（ws.py:_message）。graph_static 与 heartbeat 的 trace_id=""、event_seq=-1。 */
export interface WsMessage {
  session_id: string;
  trace_id: string;
  event_seq: number;
  event_type: EventType;
  payload: Record<string, unknown>;
}

// --- payload 形状（按 trace_store / translator 代码现状） --- //

export interface GraphStaticPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  warnings: string[];
}

export interface NodeStartPayload {
  node_id: string;
  node_instance: number;
  label: string;
  type: string;
  color: string | null;
  input: unknown;
}

export interface NodeOutputPayload {
  node_id: string;
  node_instance: number;
  output: unknown;
}

export interface NodeEndPayload {
  node_id: string;
  node_instance: number;
  output: unknown;
}

export interface LlmThinkingPayload {
  node_id: string;
  token: string;
  full_thought: string;
}

export interface ToolCallPayload {
  node_id: string;
  name: string | null;
  args: unknown;
}

export interface HumanPausePayload {
  node_id: string;
  question: string;
  hint: string;
  /** interrupt payload model_dump：hitl1=Hitl1Question、hitl2=Hitl2Question。 */
  detail: Record<string, unknown>;
}

export interface StreamAbortPayload {
  abort_reason: string;
}

// --------------------------------------------------------------------------- //
// HITL detail 形状（human_pause.payload.detail / RunResponse.detail）
// --------------------------------------------------------------------------- //

/** hitl1 中断 payload（Hitl1Question）。 */
export interface Hitl1QuestionDetail {
  argument_tree: unknown[];
}

/** hitl2 中断 payload（Hitl2Question）。 */
export interface Hitl2QuestionDetail {
  review: {
    paragraphs: Array<{
      paragraph_id: string;
      original_text: string;
      proposed_text: string;
    }>;
    has_pending: boolean;
  };
}

// --------------------------------------------------------------------------- //
// HTTP（/api/agent/run）
// --------------------------------------------------------------------------- //

export type RunStatus = "NEED_HUMAN_INPUT" | "SUCCESS" | "FAILED";

/** hitl1 合法 action：skip/accept/edit/replay；hitl2：pass/decide。 */
export interface HumanResponse {
  action: string;
  text?: string;
}

export interface RunRequest {
  session_id: string;
  query?: string | null;
  human_response?: HumanResponse | null;
  document?: string | null;
  biz_trace_id?: string | null;
}

export interface RunResponse {
  status: RunStatus;
  session_id: string;
  trace_id: string;
  /** NEED_HUMAN_INPUT 时的中断节点名（hitl1/hitl2）。 */
  node_id: string | null;
  human_question: string | null;
  hint: string | null;
  /** interrupt payload model_dump（与 human_pause.payload.detail 同源）。 */
  detail: Record<string, unknown> | null;
  final_document: string | null;
  errors: string[];
  biz_trace_id: string | null;
}

/** 统一错误响应体（app.py:_api_error_handler）。 */
export interface ApiErrorBody {
  error: string;
  message: string;
}

// --------------------------------------------------------------------------- //
// 前端派生：节点显示状态机
// --------------------------------------------------------------------------- //

export type NodeStatus =
  | "idle" // 未执行
  | "running" // 运行中
  | "done" // 已完成
  | "awaiting_input" // 待人工输入
  | "aborted"; // 执行中断

/** §14.2 提示文案 key（前端据状态 / 错误码渲染）。 */
export type PromptKey =
  | "unsaved_input_closed" // 执行中关闭未保存输入
  | "hitl_timeout" // HITL 超时（PAUSE_EXPIRED 410）
  | "duplicate_submit" // 重复提交（LOCK_EXIST 409）
  | "session_limit" // 会话数达上限（SESSION_LIMIT 429）
  | "permission_denied" // 权限拒绝（FORBIDDEN 403）
  | "live_thinking_unavailable" // 实时思考暂不可用（背压极端）
  | "stream_aborted" // 执行中断（stream_abort）
  | "param_error"; // 参数错误（PARAM_ERROR 400）
