import { useWorkbench, promptText } from "../ws/workbenchStore";

/** §14.2 风险提示浮层——不阻塞、可逐条关闭。 */
export function Toasts() {
  const { state, actions } = useWorkbench();
  if (state.toasts.length === 0) return null;
  return (
    <div className="toasts">
      {state.toasts.map((t) => (
        <div className="toast" key={t.id}>
          <span>{promptText(t.key)}</span>
          <button onClick={() => actions.dismissToast(t.id)} aria-label="关闭">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
