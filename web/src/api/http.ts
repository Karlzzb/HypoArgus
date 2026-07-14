// HTTP 控制面客户端（相对路径：开发经 Vite 代理、生产经 Nginx）。
// 端点：GET /api/agent/graph、POST /api/agent/run（fresh/resume 二态）。
// 错误响应统一 {error, message}（app.py:_api_error_handler），映射 HTTP 状态。

import type { ApiErrorBody, GraphView, RunRequest, RunResponse } from "../types";

/** 一期错误码（errors.py:ErrorCode），前端据 code 渲染 §14.2 文案。 */
export type ErrorCode =
  | "PARAM_ERROR"
  | "FORBIDDEN"
  | "LOCK_EXIST"
  | "PAUSE_EXPIRED"
  | "SESSION_LIMIT"
  | "GRAPH_TIMEOUT"
  | "UNKNOWN";

export class ApiError extends Error {
  readonly code: ErrorCode;
  constructor(code: ErrorCode, message: string) {
    super(message);
    this.name = "ApiError";
    this.code = code;
  }
}

async function toApiError(res: Response): Promise<ApiError> {
  let body: ApiErrorBody;
  try {
    body = (await res.json()) as ApiErrorBody;
  } catch {
    return new ApiError("UNKNOWN", res.statusText);
  }
  return new ApiError((body.error as ErrorCode) ?? "UNKNOWN", body.message);
}

export async function getGraph(signal?: AbortSignal): Promise<GraphView> {
  const res = await fetch("/api/agent/graph", { signal });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as GraphView;
}

export async function postRun(req: RunRequest, signal?: AbortSignal): Promise<RunResponse> {
  const res = await fetch("/api/agent/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as RunResponse;
}
