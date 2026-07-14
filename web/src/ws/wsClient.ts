// WebSocket 客户端——相对 URL（开发经 Vite 代理注入 X-User-Id、生产经 Nginx）。
// 职责：连接 / 断线指数退避重连 / 把每帧 JSON 解析为 WsMessage 投递给上层。
// 重连后由后端重放 trace_events（graph_static → trace_start → 全量事件），
// 上层 syncReduce 据 trace_start 自动清动态态并按 event_seq 复现——故客户端无需另写回放逻辑。

import type { WsMessage } from "../types";

export interface WsClientCallbacks {
  onMessage: (m: WsMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  /** 重连耗尽（背压极端 / 服务不可达）→ 上层提示「实时思考暂不可用」。 */
  onGiveUp?: () => void;
}

const MAX_RECONNECT = 5;

function wsUrl(sessionId: string): string {
  const proto = globalThis.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${globalThis.location.host}/ws/agent/stream?session_id=${encodeURIComponent(sessionId)}`;
}

export class WsClient {
  private ws: WebSocket | null = null;
  private attempts = 0;
  private closed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly sessionId: string,
    private readonly cb: WsClientCallbacks,
  ) {}

  connect(): void {
    if (this.closed) return;
    this.ws = new WebSocket(wsUrl(this.sessionId));
    this.ws.onopen = () => {
      this.attempts = 0;
      this.cb.onOpen?.();
    };
    this.ws.onmessage = (e: MessageEvent) => {
      try {
        this.cb.onMessage(JSON.parse(e.data as string) as WsMessage);
      } catch {
        /* 丢弃畸形帧（心跳文本等）。 */
      }
    };
    this.ws.onclose = () => {
      if (this.closed) return;
      this.cb.onClose?.();
      this.scheduleReconnect();
    };
    this.ws.onerror = () => {
      // error 后必有 close，统一在 close 处重连。
      this.ws?.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.attempts >= MAX_RECONNECT) {
      this.cb.onGiveUp?.();
      return;
    }
    const delay = Math.min(1000 * 2 ** this.attempts, 8000);
    this.attempts += 1;
    this.timer = setTimeout(() => this.connect(), delay);
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.close();
  }
}
