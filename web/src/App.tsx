import { SessionBar } from "./components/SessionBar";
import { FlowGraph } from "./components/FlowGraph";
import { ReasoningPanel } from "./components/ReasoningPanel";
import { InputBar } from "./components/InputBar";
import { Toasts } from "./components/Toasts";
import { useWorkbench } from "./ws/workbenchStore";

export default function App() {
  const { state } = useWorkbench();
  return (
    <>
      <SessionBar />
      <section className="app-graph">
        <FlowGraph
          graph={state.graph}
          nodes={state.nodes}
          phase={state.phase}
          hitlNodeId={state.hitl?.nodeId ?? null}
        />
      </section>
      <ReasoningPanel />
      <InputBar />
      <Toasts />
    </>
  );
}
