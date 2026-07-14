// 会话列表（localStorage）——一期前端自管。
// 注：服务端「近 30 min 存活 session」过滤属 T-08（无 list 端点），
// 前端展示本地创建过的会话；点击切换即重连 WS（后端按 session_id 重放 trace_events）。

export interface SessionSummary {
  id: string;
  createdAt: number;
  lastSeen: number;
  /** 最近一次终态标签（成功 / 失败 / 待输入 / 中断），便于历史列表展示。 */
  lastPhase?: string;
}

const KEY = "hypoargus.sessions";
const ACTIVE_WINDOW_MS = 30 * 60 * 1000;

export function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `s-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function loadSessions(): SessionSummary[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as SessionSummary[];
    if (!Array.isArray(parsed)) return [];
    return parsed;
  } catch {
    return [];
  }
}

export function saveSessions(sessions: SessionSummary[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(sessions));
  } catch {
    /* localStorage 不可用时静默降级（隐私模式等）。 */
  }
}

export function upsertSession(
  sessions: SessionSummary[],
  id: string,
  patch?: Partial<SessionSummary>,
): SessionSummary[] {
  const now = Date.now();
  const idx = sessions.findIndex((s) => s.id === id);
  if (idx >= 0) {
    const updated = { ...sessions[idx], lastSeen: now, ...patch };
    const next = [...sessions];
    next[idx] = updated;
    return next;
  }
  return [{ id, createdAt: now, lastSeen: now, ...patch }, ...sessions];
}

/** 仅保留近 30 min 存活 + 至多 50 条，避免无限增长。 */
export function pruneSessions(sessions: SessionSummary[]): SessionSummary[] {
  const now = Date.now();
  return sessions
    .filter((s) => now - s.lastSeen < ACTIVE_WINDOW_MS)
    .sort((a, b) => b.lastSeen - a.lastSeen)
    .slice(0, 50);
}
