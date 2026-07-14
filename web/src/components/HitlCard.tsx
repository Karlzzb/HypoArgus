import { useState } from "react";
import type { HitlCard as HitlCardData } from "../ws/syncReduce";
import type { Hitl2QuestionDetail } from "../types";

interface HitlCardProps {
  hitl: HitlCardData;
  submitting: boolean;
  onSubmit: (action: string, text: string) => void;
}

const HITL1_ACTIONS = ["skip", "accept", "edit", "replay"] as const;
const HITL2_ACTIONS = ["pass", "decide"] as const;

const ACTION_LABEL: Record<string, string> = {
  skip: "跳过",
  accept: "采纳",
  edit: "编辑",
  replay: "重切",
  pass: "通过",
  decide: "全驳回",
};

/** 嵌入式 HITL 交互卡片——不遮挡流程图，提交后由 store 销毁（phase→running、hitl→null）。 */
export function HitlCard({ hitl, submitting, onSubmit }: HitlCardProps) {
  const [text, setText] = useState("");
  const isHitl1 = hitl.nodeId === "hitl1";
  const actions = isHitl1 ? HITL1_ACTIONS : HITL2_ACTIONS;
  const review = isHitl1
    ? null
    : (hitl.detail as unknown as Hitl2QuestionDetail | undefined)?.review;

  return (
    <div className="hitl-card">
      <div className="q">{hitl.question}</div>
      <div className="hint">可选动作：{hitl.hint}</div>
      {review && review.paragraphs.length > 0 && (
        <div className="cot-outputs">
          {review.paragraphs.map((p) => (
            <div className="out" key={p.paragraph_id}>
              <strong>{p.paragraph_id}</strong>
              {"\n— 原文："}
              {p.original_text}
              {"\n— 提议："}
              {p.proposed_text}
            </div>
          ))}
        </div>
      )}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="（一期自由文本，可不填）"
        disabled={submitting}
      />
      <div className="hitl-actions">
        {actions.map((a) => (
          <button
            key={a}
            className="btn primary"
            disabled={submitting}
            onClick={() => onSubmit(a, text)}
          >
            {ACTION_LABEL[a] ?? a}
          </button>
        ))}
      </div>
    </div>
  );
}
