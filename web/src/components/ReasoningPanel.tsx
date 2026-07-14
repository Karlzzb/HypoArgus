import { useWorkbench, promptText } from "../ws/workbenchStore";
import { CotView } from "./CotView";
import { HitlCard } from "./HitlCard";

function LiveTab() {
  const { state, actions } = useWorkbench();
  return (
    <>
      <CotView groups={state.cotGroups} live />
      {state.hitl && (
        <HitlCard
          hitl={state.hitl}
          submitting={state.submitting}
          onSubmit={actions.submitHumanResponse}
        />
      )}
      {state.phase === "done" && state.finalDocument != null && (
        <div className="final-doc">
          <div className="reasoning-empty" style={{ marginBottom: 8 }}>
            终稿
          </div>
          {state.finalDocument}
        </div>
      )}
      {state.phase === "done" && state.errors.length > 0 && (
        <div className="final-doc" style={{ borderColor: "var(--aborted)" }}>
          {state.errors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}
      {state.phase === "aborted" && (
        <div className="final-doc" style={{ borderColor: "var(--aborted)" }}>
          {promptText("stream_aborted")}
          {state.abortReason ? `（${state.abortReason}）` : ""}
        </div>
      )}
    </>
  );
}

function ReplayTab() {
  const { state, actions } = useWorkbench();
  return (
    <>
      <div className="reasoning-empty" style={{ marginBottom: 8 }}>
        历史回放——按 event_seq 复现，复用同一渲染组件。点击下方按钮可对当前 trace 重新回放。
      </div>
      <button
        className="btn"
        style={{ marginBottom: 12 }}
        onClick={actions.replay}
        disabled={!state.currentSessionId}
      >
        重新回放当前 trace
      </button>
      <CotView groups={state.cotGroups} live={false} />
    </>
  );
}

export function ReasoningPanel() {
  const { state, actions } = useWorkbench();
  return (
    <section className="app-reasoning">
      <div className="reasoning-tabs">
        <button
          className={`reasoning-tab ${state.tab === "live" ? "active" : ""}`}
          onClick={() => actions.switchTab("live")}
        >
          实时推理
        </button>
        <button
          className={`reasoning-tab ${state.tab === "replay" ? "active" : ""}`}
          onClick={() => actions.switchTab("replay")}
        >
          历史回放
        </button>
      </div>
      <div className="reasoning-body">
        {state.tab === "live" ? <LiveTab /> : <ReplayTab />}
      </div>
    </section>
  );
}
