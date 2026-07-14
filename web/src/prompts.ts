// §14.2 提示文案——前端据状态 / 错误码渲染。

import type { PromptKey } from "./types";

export const PROMPTS: Record<PromptKey, string> = {
  unsaved_input_closed: "执行中已关闭未保存的输入。",
  hitl_timeout: "人工确认已超时（暂停超过 30 分钟），请重新发起。",
  duplicate_submit: "检测到重复提交，同一会话正在执行中。",
  session_limit: "活跃会话数已达上限，请先结束其他会话。",
  permission_denied: "权限拒绝：会话不属于当前用户。",
  live_thinking_unavailable: "实时思考暂不可用（服务端背压），请等待或刷新重连。",
  stream_aborted: "执行中断：本次修订已停止。",
  param_error: "请求参数有误，请检查输入。",
};
