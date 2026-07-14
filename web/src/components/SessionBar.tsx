import { useWorkbench } from "../ws/workbenchStore";

const PHASE_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "执行中",
  awaiting_input: "待人工输入",
  done: "已完成",
  aborted: "执行中断",
};

/** 顶部会话管理栏——新建对话 / 历史会话列表 / 状态栏 / 风险提示浮层（Toasts）。 */
export function SessionBar() {
  const { state, actions } = useWorkbench();
  return (
    <header className="sessionbar app-sessionbar">
      <span className="brand">HypoArgus</span>
      <button className="btn primary" onClick={actions.newSession}>
        新建对话
      </button>
      <div className="session-list">
        {state.sessions.length === 0 && (
          <span className="reasoning-empty">近 30 min 无本地会话</span>
        )}
        {state.sessions.map((s) => (
          <span
            key={s.id}
            className={`session-chip ${s.id === state.currentSessionId ? "active" : ""}`}
            onClick={() => actions.switchSession(s.id)}
            title={s.id}
          >
            <span className={`dot ${s.lastPhase ?? ""}`} />
            {s.id.slice(0, 8)}
          </span>
        ))}
      </div>
      <span className={`status-pill ${state.phase}`}>
        <span className="dot" />
        {PHASE_LABEL[state.phase] ?? state.phase}
      </span>
      <span className="status-pill">{state.connected ? "已连接" : "未连接"}</span>
    </header>
  );
}
