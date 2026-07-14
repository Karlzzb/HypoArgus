import { useState } from "react";
import { useWorkbench, promptText } from "../ws/workbenchStore";

const SAMPLE_DOC = "主论点。\n\n分论点。\n\n论据。\n";

/** 底部对话输入区——空闲可发、执行中置灰、HITL 暂停锁定（仅上方卡片提交）。 */
export function InputBar() {
  const { state, actions } = useWorkbench();
  const [query, setQuery] = useState("");
  const [doc, setDoc] = useState(SAMPLE_DOC);
  const locked = state.phase === "running" || state.phase === "awaiting_input";

  const submit = () => {
    const q = query.trim();
    if (!q || locked) return;
    void actions.submitQuery(q, doc);
    setQuery("");
  };

  return (
    <footer className="inputbar app-input">
      <textarea
        className="doc-field"
        value={doc}
        onChange={(e) => setDoc(e.target.value)}
        disabled={locked}
        placeholder="待修订文档（多段，段以空行分隔）…"
      />
      <textarea
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        disabled={locked}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={locked ? promptText("unsaved_input_closed") : "输入修订指令…（Ctrl+Enter 发送）"}
      />
      <button
        className="btn primary"
        disabled={locked || !query.trim() || state.submitting}
        onClick={submit}
      >
        发送
      </button>
    </footer>
  );
}
