// T-07 WS 强制同步逻辑（PRD §6.5）——纯函数，行为真相的唯一载体。
//
// 设计为「深模块」：接口极小（ingest 一条 WS 消息 → 产出一个显示态），
// 实现内部承载全部强制同步规则。纯函数、无 IO、无 React 依赖，便于 Vitest 逐规则覆盖。
//
// 规则（PRD §6.5）：
//   - graph_static：渲染静态骨架（仅可见节点）。
//   - trace_start：清空动态数据、只处理当前 trace；记录当前最大 event_seq。
//   - 丢弃 event_seq 小于当前最大值的滞后消息（同 trace 乱序）。
//   - node_start/node_output/node_end：更新节点状态 + CoT 分组。
//   - llm_thinking：按 node_id+node_instance 分组增量 token（打字机）。
//   - human_pause：置嵌入式交互卡片、phase=awaiting_input。
//   - stream_finish：phase=done。
//   - stream_abort：停止等待、phase=aborted、展示「执行中断」。
//   - heartbeat：忽略。
//   - 切换会话 / 刷新：由 store 层断连重连后，重连回放仍走本 reducer（trace_start 重置）。

import type {
  GraphView,
  GraphStaticPayload,
  HumanPausePayload,
  LlmThinkingPayload,
  NodeEndPayload,
  NodeOutputPayload,
  NodeStartPayload,
  NodeStatus,
  StreamAbortPayload,
  ToolCallPayload,
  WsMessage,
} from "../types";

// --------------------------------------------------------------------------- //
// 显示态
// --------------------------------------------------------------------------- //

export interface NodeRuntime {
  status: NodeStatus;
  /** 该节点当前显示的 node_instance（最新一次触发）。 */
  instance: number;
  label?: string;
  type?: string;
  color?: string | null;
  input?: unknown;
  /** 中间产出（node_output，按到达顺序）。 */
  outputs: unknown[];
  /** 节点终态产出（node_end）。 */
  finalOutput?: unknown;
  /** 该节点的触发次数（≥ node_instance+1），用于回放环角标「×N」。 */
  triggerCount: number;
}

export interface CotGroup {
  /** `${node_id}#${node_instance}`。 */
  key: string;
  nodeId: string;
  instance: number;
  /** 增量 token（llm_thinking，按到达顺序）。 */
  tokens: string[];
  /** 累积思考全文（与后端 full_thought 对齐）。 */
  fullThought: string;
  /** 该 node_instance 的中间产出。 */
  outputs: unknown[];
  /** 该 node_instance 的工具调用。 */
  toolCalls: { name: string | null; args: unknown }[];
  done: boolean;
}

export interface HitlCard {
  nodeId: string;
  question: string;
  hint: string;
  /** interrupt payload model_dump（hitl1=Hitl1Question、hitl2=Hitl2Question）。 */
  detail: Record<string, unknown>;
}

export type Phase =
  | "idle"
  | "running"
  | "awaiting_input"
  | "aborted"
  | "done";

export interface DisplayState {
  phase: Phase;
  /** 静态骨架（graph_static）。 */
  graph: GraphView | null;
  /** 当前 trace；trace_start 切换；非当前 trace 的滞后消息丢弃。 */
  currentTraceId: string | null;
  /** 当前 trace 已处理的最大 event_seq；小于此值的同 trace 消息丢弃。 */
  maxEventSeq: number;
  /** 节点运行态（按 node_id）。 */
  nodes: Record<string, NodeRuntime>;
  /** CoT 分组（按到达顺序）。 */
  cotGroups: CotGroup[];
  /** 当前嵌入式 HITL 卡片（无则 null）。 */
  hitl: HitlCard | null;
  /** stream_abort 原因（无则 null）。 */
  abortReason: string | null;
  /** 终稿文本（SUCCESS 时由 HTTP RunResponse 落，reducer 不写）。 */
  finalDocument: string | null;
  /** 失败原因列表（FAILED 时由 HTTP RunResponse 落，reducer 不写）。 */
  errors: string[];
}

export function initialDisplayState(): DisplayState {
  return {
    phase: "idle",
    graph: null,
    currentTraceId: null,
    maxEventSeq: -1,
    nodes: {},
    cotGroups: [],
    hitl: null,
    abortReason: null,
    finalDocument: null,
    errors: [],
  };
}

// --------------------------------------------------------------------------- //
// reducer（逐规则实现，见 syncReduce.test.ts 的 TDD 增量）
// --------------------------------------------------------------------------- //

// --------------------------------------------------------------------------- //
// 内部辅助
// --------------------------------------------------------------------------- //

function cotKey(nodeId: string, instance: number): string {
  return `${nodeId}#${instance}`;
}

/** 确保 CoT 分组存在（按 key），返回新数组与命中分组；不修改原数组。 */
function ensureGroup(
  cotGroups: CotGroup[],
  nodeId: string,
  instance: number,
): { cotGroups: CotGroup[]; group: CotGroup } {
  const key = cotKey(nodeId, instance);
  const found = cotGroups.find((g) => g.key === key);
  if (found) return { cotGroups, group: found };
  const created: CotGroup = {
    key,
    nodeId,
    instance,
    tokens: [],
    fullThought: "",
    outputs: [],
    toolCalls: [],
    done: false,
  };
  return { cotGroups: [...cotGroups, created], group: created };
}

function emptyNode(instance: number, triggerCount: number): NodeRuntime {
  return { status: "running", instance, outputs: [], triggerCount };
}

// --------------------------------------------------------------------------- //
// reducer
// --------------------------------------------------------------------------- //

export function syncReduce(state: DisplayState, msg: WsMessage): DisplayState {
  // graph_static / heartbeat 不属任何 trace（trace_id=""、event_seq=-1），直接处理。
  switch (msg.event_type) {
    case "graph_static": {
      const p = msg.payload as unknown as GraphStaticPayload;
      return { ...state, graph: { nodes: p.nodes, edges: p.edges, warnings: p.warnings } };
    }
    case "heartbeat":
      // 忽略心跳。
      return state;
    case "trace_start": {
      // 清空动态数据、只处理当前 trace；记录当前最大 event_seq。
      return {
        ...state,
        phase: "running",
        currentTraceId: msg.trace_id,
        maxEventSeq: msg.event_seq,
        nodes: {},
        cotGroups: [],
        hitl: null,
        abortReason: null,
        finalDocument: null,
        errors: [],
      };
    }
  }

  // 其余为 trace-scoped 事件：只处理当前 trace；丢弃序号小于当前最大值的滞后消息。
  if (msg.trace_id !== state.currentTraceId) return state;
  if (msg.event_seq < state.maxEventSeq) return state;
  const s: DisplayState = {
    ...state,
    maxEventSeq: Math.max(state.maxEventSeq, msg.event_seq),
  };

  switch (msg.event_type) {
    case "node_start": {
      const p = msg.payload as unknown as NodeStartPayload;
      const prev = s.nodes[p.node_id];
      const triggerCount = (prev?.triggerCount ?? 0) + 1;
      const nodes: Record<string, NodeRuntime> = {
        ...s.nodes,
        [p.node_id]: {
          status: "running",
          instance: p.node_instance,
          label: p.label,
          type: p.type,
          color: p.color,
          input: p.input,
          outputs: [],
          finalOutput: undefined,
          triggerCount,
        },
      };
      const { cotGroups } = ensureGroup(s.cotGroups, p.node_id, p.node_instance);
      return { ...s, nodes, cotGroups };
    }
    case "node_output": {
      const p = msg.payload as unknown as NodeOutputPayload;
      const { cotGroups, group } = ensureGroup(s.cotGroups, p.node_id, p.node_instance);
      const cotGroupsNext = cotGroups.map((g) =>
        g.key === group.key ? { ...g, outputs: [...g.outputs, p.output] } : g,
      );
      const prev = s.nodes[p.node_id];
      const nodes: Record<string, NodeRuntime> = prev
        ? { ...s.nodes, [p.node_id]: { ...prev, outputs: [...prev.outputs, p.output] } }
        : { ...s.nodes, [p.node_id]: { ...emptyNode(p.node_instance, 1), outputs: [p.output] } };
      return { ...s, nodes, cotGroups: cotGroupsNext };
    }
    case "node_end": {
      const p = msg.payload as unknown as NodeEndPayload;
      const { cotGroups, group } = ensureGroup(s.cotGroups, p.node_id, p.node_instance);
      const cotGroupsNext = cotGroups.map((g) =>
        g.key === group.key ? { ...g, done: true } : g,
      );
      const prev = s.nodes[p.node_id];
      const nodes: Record<string, NodeRuntime> = prev
        ? { ...s.nodes, [p.node_id]: { ...prev, status: "done", finalOutput: p.output } }
        : { ...s.nodes, [p.node_id]: { ...emptyNode(p.node_instance, 1), status: "done", finalOutput: p.output } };
      return { ...s, nodes, cotGroups: cotGroupsNext };
    }
    case "llm_thinking": {
      // llm_thinking payload 仅含 node_id（无 node_instance）——按该节点最新 node_start
      // 的 instance 归组（token 总落在该节点 node_start..node_end 之间）。
      const p = msg.payload as unknown as LlmThinkingPayload;
      const instance = s.nodes[p.node_id]?.instance ?? 0;
      const { cotGroups, group } = ensureGroup(s.cotGroups, p.node_id, instance);
      const cotGroupsNext = cotGroups.map((g) =>
        g.key === group.key
          ? { ...g, tokens: [...g.tokens, p.token], fullThought: p.full_thought }
          : g,
      );
      return { ...s, cotGroups: cotGroupsNext };
    }
    case "tool_call": {
      const p = msg.payload as unknown as ToolCallPayload;
      const instance = s.nodes[p.node_id]?.instance ?? 0;
      const { cotGroups, group } = ensureGroup(s.cotGroups, p.node_id, instance);
      const cotGroupsNext = cotGroups.map((g) =>
        g.key === group.key
          ? { ...g, toolCalls: [...g.toolCalls, { name: p.name, args: p.args }] }
          : g,
      );
      return { ...s, cotGroups: cotGroupsNext };
    }
    case "human_pause": {
      const p = msg.payload as unknown as HumanPausePayload;
      return {
        ...s,
        phase: "awaiting_input",
        hitl: { nodeId: p.node_id, question: p.question, hint: p.hint, detail: p.detail },
      };
    }
    case "stream_finish": {
      return { ...s, phase: "done", hitl: null };
    }
    case "stream_abort": {
      const p = msg.payload as unknown as StreamAbortPayload;
      return { ...s, phase: "aborted", abortReason: p.abort_reason, hitl: null };
    }
    default:
      return s;
  }
}
