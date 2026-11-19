export const NODE_TYPES = {
  agent: { label: "Agent", color: "#354e43", text: "#fff" },
  data_source: { label: "Data source", color: "#e2e6df", text: "#354e43" },
  review: { label: "Review", color: "#f4e3b2", text: "#354e43" },
};

export function graphFromConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)
      || !Array.isArray(config.nodes) || !Array.isArray(config.edges)) {
    throw new Error("This workflow does not contain a nodes and edges graph and cannot be edited in the builder.");
  }
  const ids = new Set();
  const nodes = config.nodes.map((node, index) => {
    if (!node || !Object.hasOwn(NODE_TYPES, node.type) || typeof node.id !== "string" || !node.id || ids.has(node.id)
        || (node.type === "agent" && node.inputs != null
          && (!Array.isArray(node.inputs) || node.inputs.some((input) => typeof input !== "string")))) {
      throw new Error("This workflow contains an unsupported node or duplicate node ID and cannot be edited in the builder.");
    }
    ids.add(node.id);
    return {
      id: node.id,
      position: node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)
        ? node.position : { x: (index % 4) * 220, y: Math.floor(index / 4) * 130 },
      data: { label: node.label || node.data?.label || NODE_TYPES[node.type].label, workflowNode: node },
      style: nodeStyle(node.type),
    };
  });
  const edges = config.edges.map((edge, index) => {
    if (!edge || !ids.has(edge.source) || !ids.has(edge.target)) {
      throw new Error("This workflow contains an invalid edge and cannot be edited in the builder.");
    }
    return {
      id: `edge-${index}`,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle,
      targetHandle: edge.targetHandle,
      label: typeof edge.data === "string" ? edge.data : undefined,
      data: { workflowEdge: edge },
    };
  });
  return { nodes, edges };
}

export function nodeStyle(type) {
  const colors = NODE_TYPES[type];
  return {
    background: colors.color, color: colors.text, border: `2px solid ${colors.color}`,
    borderRadius: 8, padding: "10px 15px", minWidth: 130,
  };
}

export function configFromGraph(config, nodes, edges) {
  return {
    ...config,
    nodes: nodes.map((node) => ({
      ...node.data.workflowNode,
      id: node.id,
      label: node.data.label,
      position: node.position,
    })),
    edges: edges.map((edge) => ({
      ...edge.data?.workflowEdge,
      source: edge.source,
      target: edge.target,
      ...(edge.sourceHandle ? { sourceHandle: edge.sourceHandle } : {}),
      ...(edge.targetHandle ? { targetHandle: edge.targetHandle } : {}),
    })),
  };
}
