// 工作台控制器 + React hook——把纯 syncReduce（WS）与 HTTP 控制面、会话、Tab、提示
// 装配为一个单一状态对象（WorkbenchState）+ 动作集（WorkbenchActions）。
//
// 设计要点：
//   - 显示态真相 = syncReduce（纯函数）；控制器只在 HTTP 响应落 final_document / errors
//     及在断线 / 错误码时推 §14.2 提示。
//   - WS 重连回放由后端负责（graph_static → trace_start → 全量事件），syncReduce 据
//     trace_start 自动清动态态并按 event_seq 复现——控制器不另写回放。
//   - 前端只感知 session_id；fresh-run vs resume 由 Python 判（CONTEXT.md「运行时身份」）。

import { useSyncExternalStore } from "react";
import { ApiError, postRun, type ErrorCode } from "../api/http";
import { PROMPTS } from "../prompts";
import {
  loadSessions,
  newSessionId,
  pruneSessions,
  saveSessions,
  upsertSession,
  type SessionSummary,
} from "../sessions";
import type { GraphView, PromptKey, RunResponse } from "../types";
import { WsClient } from "./wsClient";
import {
  type CotGroup,
  type HitlCard,
  type NodeRuntime,
  type Phase,
  initialDisplayState,
  syncReduce,
} from "./syncReduce";

export interface Toast {
  id: string;
  key: PromptKey;
}

export interface WorkbenchState {
  phase: Phase;
  graph: GraphView | null;
  currentTraceId: string | null;
  nodes: Record<string, NodeRuntime>;
  cotGroups: CotGroup[];
  hitl: HitlCard | null;
  abortReason: string | null;
  finalDocument: string | null;
  errors: string[];
  currentSessionId: string | null;
  sessions: SessionSummary[];
  tab: "live" | "replay";
  toasts: Toast[];
  connected: boolean;
  submitting: boolean;
}

export interface WorkbenchActions {
  newSession: () => void;
  switchSession: (id: string) => void;
  submitQuery: (query: string, document: string) => void;
  submitHumanResponse: (action: string, text: string) => void;
  switchTab: (tab: "live" | "replay") => void;
  /** 历史回放：断开并重连 WS → 后端按 event_seq 重放 trace_events（复用同一 reducer 路径）。 */
  replay: () => void;
  dismissToast: (id: string) => void;
}

export interface Workbench {
  state: WorkbenchState;
  actions: WorkbenchActions;
}

const ERROR_TO_PROMPT: Record<ErrorCode, PromptKey> = {
  PARAM_ERROR: "param_error",
  FORBIDDEN: "permission_denied",
  LOCK_EXIST: "duplicate_submit",
  PAUSE_EXPIRED: "hitl_timeout",
  SESSION_LIMIT: "session_limit",
  GRAPH_TIMEOUT: "stream_aborted",
  UNKNOWN: "live_thinking_unavailable",
};

/** §14.2 文案直接查询（供组件渲染）。 */
export function promptText(key: PromptKey): string {
  return PROMPTS[key];
}

function newToastId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

class WorkbenchController {
  private display = initialDisplayState();
  private currentSessionId: string | null = null;
  private sessions: SessionSummary[] = pruneSessions(loadSessions());
  private tab: "live" | "replay" = "live";
  private toasts: Toast[] = [];
  private connected = false;
  private submitting = false;
  private ws: WsClient | null = null;
  private readonly listeners = new Set<() => void>();
  private snapshot: WorkbenchState;

  constructor() {
    saveSessions(this.sessions);
    this.snapshot = this.buildSnapshot();
    this.actions = {
      newSession: this.newSession,
      switchSession: this.switchSession,
      submitQuery: this.submitQuery,
      submitHumanResponse: this.submitHumanResponse,
      switchTab: this.switchTab,
      replay: this.replay,
      dismissToast: this.dismissToast,
    };
  }

  // ---- 外部存储订阅（useSyncExternalStore） ---- //

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getSnapshot = (): WorkbenchState => this.snapshot;

  private emit(): void {
    this.snapshot = this.buildSnapshot();
    this.listeners.forEach((f) => f());
  }

  private buildSnapshot(): WorkbenchState {
    return {
      phase: this.display.phase,
      graph: this.display.graph,
      currentTraceId: this.display.currentTraceId,
      nodes: this.display.nodes,
      cotGroups: this.display.cotGroups,
      hitl: this.display.hitl,
      abortReason: this.display.abortReason,
      finalDocument: this.display.finalDocument,
      errors: this.display.errors,
      currentSessionId: this.currentSessionId,
      sessions: this.sessions,
      tab: this.tab,
      toasts: this.toasts,
      connected: this.connected,
      submitting: this.submitting,
    };
  }

  // ---- 提示 ---- //

  private pushToast(key: PromptKey): void {
    this.toasts = [...this.toasts, { id: newToastId(), key }];
  }

  dismissToast = (id: string): void => {
    this.toasts = this.toasts.filter((t) => t.id !== id);
    this.emit();
  };

  // ---- 会话 ---- //

  newSession = (): void => {
    const id = newSessionId();
    this.activateSession(id);
  };

  switchSession = (id: string): void => {
    this.activateSession(id);
  };

  private activateSession(id: string): void {
    this.currentSessionId = id;
    this.sessions = pruneSessions(upsertSession(this.sessions, id));
    saveSessions(this.sessions);
    this.display = initialDisplayState();
    this.tab = "live";
    this.toasts = [];
    this.connected = false;
    this.connectWs(id);
    this.emit();
  }

  switchTab = (tab: "live" | "replay"): void => {
    this.tab = tab;
    this.emit();
  };

  replay = (): void => {
    // 断开重连：后端重放 graph_static → trace_start → 全量事件，syncReduce 据 trace_start
    // 清动态态并按 event_seq 复现——与实时流同源同表、复用同一渲染组件。
    if (!this.currentSessionId) return;
    this.display = initialDisplayState();
    this.toasts = [];
    this.connectWs(this.currentSessionId);
    this.emit();
  };

  // ---- WS ---- //

  private connectWs(id: string): void {
    this.ws?.close();
    this.ws = new WsClient(id, {
      onMessage: (m) => {
        this.display = syncReduce(this.display, m);
        this.emit();
      },
      onOpen: () => {
        this.connected = true;
        this.emit();
      },
      onClose: () => {
        const wasRunning = this.display.phase === "running";
        this.connected = false;
        if (wasRunning) this.pushToast("live_thinking_unavailable");
        this.emit();
      },
      onGiveUp: () => {
        this.pushToast("live_thinking_unavailable");
        this.emit();
      },
    });
    this.ws.connect();
  }

  // ---- HTTP ---- //

  submitQuery = async (query: string, document: string): Promise<void> => {
    if (!this.currentSessionId) return;
    // 空闲 / 已完成 / 中断才可发；执行中 / 待输入锁定。
    if (this.display.phase === "running" || this.display.phase === "awaiting_input") return;
    this.submitting = true;
    this.display = {
      ...this.display,
      phase: "running",
      hitl: null,
      abortReason: null,
      finalDocument: null,
      errors: [],
    };
    this.emit();
    try {
      const res = await postRun({
        session_id: this.currentSessionId,
        query,
        document,
      });
      this.applyRunResponse(res);
    } catch (e) {
      this.handleApiError(e);
    } finally {
      this.submitting = false;
      this.emit();
    }
  };

  submitHumanResponse = async (action: string, text: string): Promise<void> => {
    if (!this.currentSessionId || !this.display.hitl) return;
    this.submitting = true;
    this.display = { ...this.display, phase: "running", hitl: null };
    this.emit();
    try {
      const res = await postRun({
        session_id: this.currentSessionId,
        human_response: { action, text },
      });
      this.applyRunResponse(res);
    } catch (e) {
      this.handleApiError(e);
    } finally {
      this.submitting = false;
      this.emit();
    }
  };

  private applyRunResponse(res: RunResponse): void {
    // WS 已流式投递事件；HTTP 响应确认终态并落 final_document / errors（WS 不携带二者）。
    if (res.status === "NEED_HUMAN_INPUT") {
      const hitl: HitlCard | null = res.node_id
        ? {
            nodeId: res.node_id,
            question: res.human_question ?? "",
            hint: res.hint ?? "",
            detail: res.detail ?? {},
          }
        : this.display.hitl;
      this.display = { ...this.display, phase: "awaiting_input", hitl };
    } else if (res.status === "SUCCESS") {
      this.display = {
        ...this.display,
        phase: "done",
        finalDocument: res.final_document,
        hitl: null,
      };
      this.markSessionPhase("done");
    } else {
      this.display = {
        ...this.display,
        phase: "done",
        errors: res.errors,
        hitl: null,
      };
      this.markSessionPhase("failed");
    }
  }

  private markSessionPhase(phase: string): void {
    if (!this.currentSessionId) return;
    this.sessions = pruneSessions(
      upsertSession(this.sessions, this.currentSessionId, { lastPhase: phase }),
    );
    saveSessions(this.sessions);
  }

  private handleApiError(e: unknown): void {
    if (e instanceof ApiError) {
      this.pushToast(ERROR_TO_PROMPT[e.code] ?? "param_error");
    } else {
      this.pushToast("live_thinking_unavailable");
    }
    // 提交失败不应卡在 running；回到可输入态。
    this.display = { ...this.display, phase: "idle" };
  }

  // 供测试与卸载重置单例。
  reset(): void {
    this.ws?.close();
    this.display = initialDisplayState();
    this.currentSessionId = null;
    this.tab = "live";
    this.toasts = [];
    this.connected = false;
    this.submitting = false;
    this.emit();
  }

  actions: WorkbenchActions;
}

let controller: WorkbenchController | null = null;

function getController(): WorkbenchController {
  if (!controller) controller = new WorkbenchController();
  return controller;
}

/** 仅供测试：重置单例控制器（断开 WS、清态）。 */
export function resetWorkbench(): void {
  getController().reset();
}

export function useWorkbench(): Workbench {
  const ctrl = getController();
  const state = useSyncExternalStore(ctrl.subscribe, ctrl.getSnapshot, ctrl.getSnapshot);
  return { state, actions: ctrl.actions };
}
