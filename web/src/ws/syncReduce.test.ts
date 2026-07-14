import { describe, it, expect } from "vitest";
import { initialDisplayState, syncReduce } from "./syncReduce";
import type { GraphStaticPayload, WsMessage } from "../types";

function msg(
  event_type: WsMessage["event_type"],
  payload: Record<string, unknown>,
  overrides: Partial<WsMessage> = {},
): WsMessage {
  return {
    session_id: "s1",
    trace_id: "t1",
    event_seq: 0,
    event_type,
    payload,
    ...overrides,
  };
}

const GRAPH_STATIC: GraphStaticPayload = {
  nodes: [
    { id: "__start__", label: "Start", type: "system", color: null, visible: true, interrupt: false },
    { id: "hitl1", label: "HITL1", type: "human", color: "#f59e0b", visible: true, interrupt: true },
    { id: "hitl2", label: "HITL2", type: "human", color: "#ef4444", visible: true, interrupt: true },
    { id: "__end__", label: "End", type: "system", color: null, visible: true, interrupt: false },
  ],
  edges: [
    { source: "__start__", target: "hitl1", cond: null, max: null },
    { source: "hitl1", target: "hitl2", cond: null, max: null },
    { source: "hitl1", target: "parse+partition", cond: "replay", max: 3 },
    { source: "hitl2", target: "__end__", cond: null, max: null },
  ],
  warnings: [],
};

describe("syncReduce: graph_static", () => {
  it("renders the static skeleton from graph_static (visible nodes + edges + warnings)", () => {
    const next = syncReduce(
      initialDisplayState(),
      msg("graph_static", GRAPH_STATIC as unknown as Record<string, unknown>, {
        trace_id: "",
        event_seq: -1,
      }),
    );
    expect(next.graph).not.toBeNull();
    expect(next.graph!.nodes).toHaveLength(4);
    expect(next.graph!.nodes.map((n) => n.id)).toEqual([
      "__start__",
      "hitl1",
      "hitl2",
      "__end__",
    ]);
    expect(next.graph!.edges).toHaveLength(4);
    expect(next.graph!.edges[2]).toMatchObject({
      source: "hitl1",
      target: "parse+partition",
      cond: "replay",
      max: 3,
    });
    expect(next.graph!.warnings).toEqual([]);
    // graph_static 不改变运行态 / phase。
    expect(next.phase).toBe("idle");
    expect(next.currentTraceId).toBeNull();
  });
});

describe("syncReduce: trace_start", () => {
  it("clears dynamic data, sets current trace, and resets max event_seq", () => {
    const prior = syncReduce(
      initialDisplayState(),
      msg("graph_static", GRAPH_STATIC as unknown as Record<string, unknown>, {
        trace_id: "",
        event_seq: -1,
      }),
    );
    // 预置一些动态态，验证 trace_start 会清空。
    const withDynamics: typeof prior = {
      ...prior,
      phase: "running",
      currentTraceId: "t0",
      maxEventSeq: 5,
      nodes: { hitl1: { status: "done", instance: 0, outputs: [], triggerCount: 1 } },
      cotGroups: [
        { key: "hitl1#0", nodeId: "hitl1", instance: 0, tokens: ["x"], fullThought: "x", outputs: [], toolCalls: [], done: true },
      ],
      hitl: { nodeId: "hitl1", question: "q", hint: "h", detail: {} },
      abortReason: "GRAPH_TIMEOUT",
    };

    const next = syncReduce(withDynamics, msg("trace_start", {}));
    expect(next.phase).toBe("running");
    expect(next.currentTraceId).toBe("t1");
    // trace_start 是当前 trace 的第一条（event_seq=0），故 maxEventSeq 推进到 0。
    expect(next.maxEventSeq).toBe(0);
    // 动态态被清空。
    expect(next.nodes).toEqual({});
    expect(next.cotGroups).toEqual([]);
    expect(next.hitl).toBeNull();
    expect(next.abortReason).toBeNull();
    // 静态骨架保留。
    expect(next.graph).not.toBeNull();
  });
});

/** 已进入 trace（trace_start 后）的初始态，便于后续规则测试。 */
function inTrace() {
  return syncReduce(initialDisplayState(), msg("trace_start", {}, { trace_id: "t1", event_seq: 0 }));
}
// 上面 inTrace 返回态的 currentTraceId/maxEventSeq，供构造消息对齐。
const T1 = { trace_id: "t1" };

describe("syncReduce: node_start / node_output / node_end", () => {
  it("node_start marks the node running with instance + label + input", () => {
    const next = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "hitl1",
        node_instance: 0,
        label: "HITL1",
        type: "human",
        color: "#f59e0b",
        input: { doc: "x" },
      }, { ...T1, event_seq: 1 }),
    );
    expect(next.nodes["hitl1"]).toMatchObject({
      status: "running",
      instance: 0,
      label: "HITL1",
      type: "human",
      color: "#f59e0b",
      triggerCount: 1,
    });
    expect(next.nodes["hitl1"].input).toEqual({ doc: "x" });
    expect(next.maxEventSeq).toBe(1);
  });

  it("node_output appends intermediate output to the node + CoT group", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "judgment", node_instance: 0, label: "Judgment", type: "llm", color: null, input: null,
      }, { ...T1, event_seq: 1 }),
    );
    const next = syncReduce(
      started,
      msg("node_output", { node_id: "judgment", node_instance: 0, output: { verdict: "credible" } }, { ...T1, event_seq: 2 }),
    );
    expect(next.nodes["judgment"].outputs).toEqual([{ verdict: "credible" }]);
    const grp = next.cotGroups.find((g) => g.key === "judgment#0");
    expect(grp).toBeDefined();
    expect(grp!.outputs).toEqual([{ verdict: "credible" }]);
  });

  it("node_end marks the node done, stores final output, closes the CoT group", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "hitl2", node_instance: 0, label: "HITL2", type: "human", color: null, input: null,
      }, { ...T1, event_seq: 1 }),
    );
    const next = syncReduce(
      started,
      msg("node_end", { node_id: "hitl2", node_instance: 0, output: "final" }, { ...T1, event_seq: 2 }),
    );
    expect(next.nodes["hitl2"].status).toBe("done");
    expect(next.nodes["hitl2"].finalOutput).toBe("final");
    expect(next.cotGroups.find((g) => g.key === "hitl2#0")!.done).toBe(true);
  });

  it("replay loop: a second node_start for the same node increments triggerCount + opens a new CoT group", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
      }, { ...T1, event_seq: 1 }),
    );
    const next = syncReduce(
      started,
      msg("node_start", {
        node_id: "hitl1", node_instance: 1, label: "HITL1", type: "human", color: null, input: null,
      }, { ...T1, event_seq: 2 }),
    );
    expect(next.nodes["hitl1"].instance).toBe(1);
    expect(next.nodes["hitl1"].triggerCount).toBe(2);
    expect(next.cotGroups.filter((g) => g.nodeId === "hitl1")).toHaveLength(2);
    expect(next.cotGroups.map((g) => g.instance)).toEqual([0, 1]);
  });
});

describe("syncReduce: laggard / out-of-order discard", () => {
  it("discards a same-trace message whose event_seq < current max", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
      }, { ...T1, event_seq: 5 }),
    );
    expect(started.maxEventSeq).toBe(5);
    // 滞后消息（seq=3 < 5）：丢弃，状态不变。
    const next = syncReduce(
      started,
      msg("node_output", { node_id: "hitl1", node_instance: 0, output: "stale" }, { ...T1, event_seq: 3 }),
    );
    expect(next).toBe(started);
    expect(next.maxEventSeq).toBe(5);
  });

  it("discards messages whose trace_id != current trace", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
      }, { ...T1, event_seq: 1 }),
    );
    const next = syncReduce(
      started,
      msg("node_output", { node_id: "hitl1", node_instance: 0, output: "other-trace" }, { trace_id: "t-other", event_seq: 2 }),
    );
    expect(next).toBe(started);
  });
});

describe("syncReduce: llm_thinking", () => {
  it("appends tokens to the CoT group of the node's current instance (typewriter)", () => {
    const started = syncReduce(
      inTrace(),
      msg("node_start", {
        node_id: "judgment", node_instance: 0, label: "Judgment", type: "llm", color: null, input: null,
      }, { ...T1, event_seq: 1 }),
    );
    const a = syncReduce(started, msg("llm_thinking", { node_id: "judgment", token: "Hel", full_thought: "Hel" }, { ...T1, event_seq: 2 }));
    const b = syncReduce(a, msg("llm_thinking", { node_id: "judgment", token: "lo", full_thought: "Hello" }, { ...T1, event_seq: 3 }));
    const grp = b.cotGroups.find((g) => g.key === "judgment#0")!;
    expect(grp.tokens).toEqual(["Hel", "lo"]);
    expect(grp.fullThought).toBe("Hello");
  });

  it("groups replay-loop tokens under the new instance after a second node_start", () => {
    let s = syncReduce(inTrace(), msg("node_start", {
      node_id: "judgment", node_instance: 0, label: "J", type: "llm", color: null, input: null,
    }, { ...T1, event_seq: 1 }));
    s = syncReduce(s, msg("llm_thinking", { node_id: "judgment", token: "first", full_thought: "first" }, { ...T1, event_seq: 2 }));
    s = syncReduce(s, msg("node_start", {
      node_id: "judgment", node_instance: 1, label: "J", type: "llm", color: null, input: null,
    }, { ...T1, event_seq: 3 }));
    s = syncReduce(s, msg("llm_thinking", { node_id: "judgment", token: "second", full_thought: "second" }, { ...T1, event_seq: 4 }));
    const g0 = s.cotGroups.find((g) => g.key === "judgment#0")!;
    const g1 = s.cotGroups.find((g) => g.key === "judgment#1")!;
    expect(g0.tokens).toEqual(["first"]);
    expect(g1.tokens).toEqual(["second"]);
  });
});

describe("syncReduce: human_pause / stream_finish / stream_abort / heartbeat", () => {
  it("human_pause sets the embedded HITL card + phase awaiting_input", () => {
    const next = syncReduce(
      inTrace(),
      msg("human_pause", {
        node_id: "hitl1",
        question: "请确认段落切分是否合理。",
        hint: "skip/accept/edit/replay",
        detail: { argument_tree: [] },
      }, { ...T1, event_seq: 2 }),
    );
    expect(next.phase).toBe("awaiting_input");
    expect(next.hitl).toEqual({
      nodeId: "hitl1",
      question: "请确认段落切分是否合理。",
      hint: "skip/accept/edit/replay",
      detail: { argument_tree: [] },
    });
  });

  it("stream_finish ends the run (phase done, card cleared)", () => {
    const paused = syncReduce(inTrace(), msg("human_pause", {
      node_id: "hitl1", question: "q", hint: "h", detail: {},
    }, { ...T1, event_seq: 2 }));
    const next = syncReduce(paused, msg("stream_finish", {}, { ...T1, event_seq: 9 }));
    expect(next.phase).toBe("done");
    expect(next.hitl).toBeNull();
  });

  it("stream_abort stops waiting (phase aborted, reason stored)", () => {
    const next = syncReduce(inTrace(), msg("stream_abort", { abort_reason: "GRAPH_TIMEOUT" }, { ...T1, event_seq: 9 }));
    expect(next.phase).toBe("aborted");
    expect(next.abortReason).toBe("GRAPH_TIMEOUT");
  });

  it("heartbeat is ignored (state unchanged, no maxEventSeq effect)", () => {
    const started = syncReduce(inTrace(), msg("node_start", {
      node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
    }, { ...T1, event_seq: 1 }));
    const next = syncReduce(started, msg("heartbeat", {}, { trace_id: "", event_seq: -1 }));
    expect(next).toBe(started);
  });
});

describe("syncReduce: reconnect replay (refresh / switch session)", () => {
  it("a second trace_start clears dynamic data so replayed events rebuild from scratch", () => {
    // 第一段：建一些动态态。
    let s = syncReduce(initialDisplayState(), msg("graph_static", GRAPH_STATIC as unknown as Record<string, unknown>, { trace_id: "", event_seq: -1 }));
    s = syncReduce(s, msg("trace_start", {}, { trace_id: "t1", event_seq: 0 }));
    s = syncReduce(s, msg("node_start", {
      node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
    }, { trace_id: "t1", event_seq: 1 }));
    s = syncReduce(s, msg("llm_thinking", { node_id: "hitl1", token: "x", full_thought: "x" }, { trace_id: "t1", event_seq: 2 }));
    expect(s.cotGroups).toHaveLength(1);

    // 重连：后端先发 graph_static（骨架保留），再重放 trace_start + 全量事件。
    s = syncReduce(s, msg("graph_static", GRAPH_STATIC as unknown as Record<string, unknown>, { trace_id: "", event_seq: -1 }));
    const cleared = syncReduce(s, msg("trace_start", {}, { trace_id: "t1", event_seq: 0 }));
    expect(cleared.phase).toBe("running");
    expect(cleared.cotGroups).toEqual([]);
    expect(cleared.nodes).toEqual({});
    expect(cleared.maxEventSeq).toBe(0);

    // 重放事件按 event_seq 复现（复用同一 reducer 路径）。
    const rebuilt = syncReduce(cleared, msg("node_start", {
      node_id: "hitl1", node_instance: 0, label: "HITL1", type: "human", color: null, input: null,
    }, { trace_id: "t1", event_seq: 1 }));
    expect(rebuilt.cotGroups).toHaveLength(1);
    expect(rebuilt.nodes["hitl1"].status).toBe("running");
  });
});
