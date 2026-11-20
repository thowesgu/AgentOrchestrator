export const NODE_TYPES = {
  start: { label: "Start", color: "#b8d7c3", text: "#263d33" },
  agent: { label: "Agent", color: "#354e43", text: "#fff" },
  data_source: { label: "Data source", color: "#e2e6df", text: "#354e43" },
  review: { label: "Review", color: "#f4e3b2", text: "#354e43" },
};

export function flowNodeType(type) {
  return type === "start" || type === "data_source" ? "workflowSource" : undefined;
}

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
      ...(flowNodeType(node.type) ? { type: flowNodeType(node.type) } : {}),
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
    borderRadius: type === "start" ? 24 : 8, padding: "10px 15px", minWidth: 130,
  };
}

export function validateWorkflowGraph(nodes, edges) {
  const errors = [];
  const starts = nodes.filter((node) => node.data?.workflowNode?.type === "start");
  if (starts.length === 0) {
    errors.push("Add a Start node.");
  } else if (starts.length > 1) {
    errors.push("A workflow can contain only one Start node.");
  }

  const nodeIds = new Set(nodes.map((node) => node.id));
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const adjacency = new Map(nodes.map((node) => [node.id, []]));
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  const incomingSources = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) {
      errors.push("Every connection must join two existing nodes.");
      continue;
    }
    adjacency.get(edge.source).push(edge.target);
    incoming.set(edge.target, incoming.get(edge.target) + 1);
    incomingSources.get(edge.target).push(nodesById.get(edge.source));
    const target = nodesById.get(edge.target);
    if (["start", "data_source"].includes(target.data?.workflowNode?.type)) {
      errors.push(`${target.data.label || target.id} does not accept incoming connections.`);
    }
  }
  for (const node of nodes) {
    if (node.data?.workflowNode?.type !== "review") continue;
    const sources = incomingSources.get(node.id);
    if (sources.length !== 1) {
      errors.push(`${node.data.label || node.id} must have exactly one incoming connection.`);
    } else if (sources[0]?.data?.workflowNode?.type !== "agent") {
      errors.push(`${node.data.label || node.id} must receive its input from an agent.`);
    }
  }

  if (starts.length === 1) {
    const startId = starts[0].id;
    if (incoming.get(startId) > 0) {
      errors.push("The Start node cannot have an incoming connection.");
    }
    if (adjacency.get(startId).length === 0) {
      errors.push("Connect the Start node to at least one step.");
    }

    const dataSourceIds = nodes
      .filter((node) => node.data?.workflowNode?.type === "data_source")
      .map((node) => node.id);
    const reachable = new Set([startId, ...dataSourceIds]);
    const pending = [...reachable];
    while (pending.length) {
      for (const target of adjacency.get(pending.pop())) {
        if (!reachable.has(target)) {
          reachable.add(target);
          pending.push(target);
        }
      }
    }
    const disconnected = nodes.filter((node) => !reachable.has(node.id));
    if (disconnected.length) {
      errors.push(`Connect every node to Start. Unreachable: ${disconnected.map((node) => node.data?.label || node.id).join(", ")}.`);
    }
  }

  const remainingIncoming = new Map(incoming);
  const ready = nodes.filter((node) => remainingIncoming.get(node.id) === 0).map((node) => node.id);
  let visited = 0;
  while (ready.length) {
    const source = ready.pop();
    visited += 1;
    for (const target of adjacency.get(source)) {
      remainingIncoming.set(target, remainingIncoming.get(target) - 1);
      if (remainingIncoming.get(target) === 0) ready.push(target);
    }
  }
  if (visited !== nodes.length) {
    errors.push("Connections cannot contain a cycle.");
  }

  return errors;
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
