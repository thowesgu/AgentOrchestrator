import { NODE_TYPES, nodeStyle } from "./workflowGraph.js";

export function visualizationGraph(workflow) {
  const nodes = workflow.nodes.map((node, index) => {
    const type = Object.hasOwn(NODE_TYPES, node.type) ? node.type : "agent";
    return {
      id: node.id,
      data: { label: node.label || node.data?.label || NODE_TYPES[type].label },
      position: node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
        ? node.position : { x: (index % 4) * 220, y: Math.floor(index / 4) * 130 },
      style: { ...nodeStyle(type), fontSize: 12, fontWeight: 500, textAlign: "center" },
    };
  });
  const edges = workflow.edges.map((edge, index) => ({
    id: `edge-${index}`,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.sourceHandle,
    targetHandle: edge.targetHandle,
    label: typeof edge.data === "string" ? edge.data : undefined,
    style: { stroke: "#a8b2a7", strokeWidth: 2 },
  }));
  return { nodes, edges };
}
