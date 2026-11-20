import { NODE_TYPES, nodeStyle } from "./workflowGraph.js";

export function visualizationGraph(workflow) {
  const nodes = workflow.nodes.map((node, index) => {
    const type = Object.hasOwn(NODE_TYPES, node.type) ? node.type : "agent";
    const status = workflow.node_statuses?.[node.id];
    const activeNodes = workflow.active_node_ids || [];
    const active = activeNodes.includes(node.id) || workflow.active_node_id === node.id;
    const conversation = workflow.agent_conversations?.find((item) => item.node_id === node.id);
    const interactiveWaiting = activeNodes.includes(node.id)
      && node.type === "agent"
      && conversation?.interaction_mode === "interactive"
      && conversation?.status === "waiting";
    const label = node.label || node.data?.label || NODE_TYPES[type].label;
    return {
      id: node.id,
      data: {
        label: active ? `${label} · Active` : status === "completed" ? `${label} · Completed` : label,
        nodeId: node.id,
      },
      position: node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
        ? node.position : { x: (index % 4) * 220, y: Math.floor(index / 4) * 130 },
      style: {
        ...nodeStyle(type),
        borderColor: interactiveWaiting ? "#c94b4b" : active ? "#d09541" : status === "completed" ? "#71917a" : NODE_TYPES[type].color,
        borderWidth: active || status === "completed" ? 3 : 2,
        boxShadow: interactiveWaiting
          ? "0 0 0 4px rgba(201, 75, 75, .2)"
          : active ? "0 0 0 4px rgba(208, 149, 65, .2)" : undefined,
        fontSize: 12,
        fontWeight: 500,
        textAlign: "center",
      },
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
