import { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { GraphView } from "../types";
import type { NodeRuntime, Phase } from "../ws/syncReduce";

interface FlowGraphProps {
  graph: GraphView | null;
  nodes: Record<string, NodeRuntime>;
  phase: Phase;
  hitlNodeId: string | null;
}

interface GraphNodeData {
  label: string;
  nodeType: string;
  status: string;
  triggerCount: number;
  awaiting: boolean;
}

const STATUS_CLASS: Record<string, string> = {
  idle: "",
  running: "running",
  done: "done",
  awaiting_input: "awaiting",
  aborted: "aborted",
};

/** 拓扑分层布局：BFS 自 __start__（忽略 cond=replay 回放边），按深度横向排布。 */
function layout(graph: GraphView): Map<string, { x: number; y: number }> {
  const pos = new Map<string, { x: number; y: number }>();
  const forward = new Map<string, string[]>();
  for (const n of graph.nodes) forward.set(n.id, []);
  for (const e of graph.edges) {
    if (e.cond === "replay") continue; // 回放边不参与分层
    if (!forward.has(e.source)) forward.set(e.source, []);
    forward.get(e.source)!.push(e.target);
  }
  const depth = new Map<string, number>();
  depth.set("__start__", 0);
  const queue = ["__start__"];
  while (queue.length > 0) {
    const cur = queue.shift()!;
    const d = depth.get(cur)!;
    for (const nxt of forward.get(cur) ?? []) {
      if (!depth.has(nxt)) {
        depth.set(nxt, d + 1);
        queue.push(nxt);
      }
    }
  }
  let maxDepth = 0;
  for (const d of depth.values()) maxDepth = Math.max(maxDepth, d);
  for (const n of graph.nodes) {
    if (!depth.has(n.id)) depth.set(n.id, (maxDepth += 1));
  }
  const layers = new Map<number, string[]>();
  for (const n of graph.nodes) {
    const d = depth.get(n.id)!;
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d)!.push(n.id);
  }
  for (const [d, ids] of layers) {
    ids.forEach((id, i) => {
      const y = (i - (ids.length - 1) / 2) * 110;
      pos.set(id, { x: d * 240, y });
    });
  }
  return pos;
}

function GraphNode({ data }: NodeProps) {
  const d = data as unknown as GraphNodeData;
  const statusClass = STATUS_CLASS[d.status] ?? "";
  const typeClass = d.nodeType;
  return (
    <div
      className={`flow-node ${typeClass} ${statusClass} ${d.awaiting ? "awaiting" : ""}`}
    >
      <Handle type="target" position={Position.Left} />
      {d.label}
      {d.triggerCount > 1 && (
        <span className="badge">×{d.triggerCount}</span>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { custom: GraphNode };

export function FlowGraph({ graph, nodes, phase, hitlNodeId }: FlowGraphProps) {
  const { rfNodes, rfEdges } = useMemo(() => {
    if (!graph) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] };
    const pos = layout(graph);
    const rfNodes: Node[] = graph.nodes.map((n) => {
      const rt = nodes[n.id];
      const status = rt?.status ?? "idle";
      const awaiting =
        phase === "awaiting_input" && hitlNodeId === n.id;
      const data: GraphNodeData = {
        label: n.label,
        nodeType: n.type,
        status,
        triggerCount: rt?.triggerCount ?? 1,
        awaiting,
      };
      return {
        id: n.id,
        type: "custom",
        position: pos.get(n.id) ?? { x: 0, y: 0 },
        data: data as unknown as Record<string, unknown>,
      };
    });
    const rfEdges: Edge[] = graph.edges.map((e, i) => ({
      id: `e${i}-${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      label: e.cond ?? undefined,
      animated: e.cond === "replay",
    }));
    return { rfNodes, rfEdges };
  }, [graph, nodes, phase, hitlNodeId]);

  if (!graph) {
    return (
      <div className="graph-empty">等待图骨架（graph_static）…</div>
    );
  }
  return (
    <div className="graph-wrap" style={{ height: "100%" }}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={NODE_TYPES}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
