import type { CotGroup } from "../ws/syncReduce";

interface CotViewProps {
  groups: CotGroup[];
  /** live Tab：末组未完成时显示打字机光标；replay Tab 不显示。 */
  live: boolean;
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/** CoT 流式视图——实时 Tab 与历史回放 Tab 共用此组件（PRD §13.3「与真实执行完全匹配」）。 */
export function CotView({ groups, live }: CotViewProps) {
  if (groups.length === 0) {
    return <div className="reasoning-empty">暂无推理流。</div>;
  }
  return (
    <>
      {groups.map((g, i) => {
        const isLast = i === groups.length - 1;
        const showCursor = live && isLast && !g.done;
        return (
          <div className="cot-group" key={g.key}>
            <header>
              <span>
                {g.nodeId} · 第 {g.instance + 1} 次
              </span>
              <span>{g.done ? "完成" : "进行中"}</span>
            </header>
            <div className="cot-text">
              {g.fullThought || g.tokens.join("")}
              {showCursor && <span className="cursor" />}
            </div>
            {g.toolCalls.length > 0 && (
              <div className="cot-outputs">
                {g.toolCalls.map((t, j) => (
                  <div className="out" key={`tc-${j}`}>
                    工具 {t.name ?? "?"}：{formatValue(t.args)}
                  </div>
                ))}
              </div>
            )}
            {g.outputs.length > 0 && (
              <div className="cot-outputs">
                {g.outputs.map((o, j) => (
                  <div className="out" key={`o-${j}`}>
                    {formatValue(o)}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}
