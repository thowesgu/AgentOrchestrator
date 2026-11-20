import test from "node:test";
import assert from "node:assert/strict";
import {
  configFromGraph, graphFromConfig, validateWorkflowGraph,
} from "./workflowGraph.js";

test("graph round trip preserves workflow metadata, node fields and edge data", () => {
  const config = {
    name: "Example",
    nodes: [
      { id: "source", type: "data_source", source_type: "chat", output: "request", config: { description: "Input" } },
      { id: "agent", type: "agent", label: "Agent", agent_id: "developer", inputs: ["request"] },
      { id: "review", type: "review", input: "result" },
    ],
    edges: [{ source: "source", target: "agent", data: "request", custom: true }],
  };
  const graph = graphFromConfig(config);
  graph.nodes[1].position = { x: 120, y: 80 };
  const result = configFromGraph(config, graph.nodes, graph.edges);
  assert.equal(result.name, "Example");
  assert.deepEqual(result.nodes[0].config, { description: "Input" });
  assert.equal(result.nodes[1].agent_id, "developer");
  assert.equal(graph.nodes[0].type, "workflowSource");
  assert.deepEqual(result.nodes[1].position, { x: 120, y: 80 });
  assert.deepEqual(result.edges, config.edges);
  assert.deepEqual(graphFromConfig(result).nodes[1].position, { x: 120, y: 80 });
});

test("unsupported graphs fail without being replaced", () => {
  assert.throws(() => graphFromConfig({ steps: [] }), /nodes and edges/);
  assert.throws(() => graphFromConfig({ nodes: [{ id: "x", type: "other" }], edges: [] }), /unsupported node/);
  assert.throws(() => graphFromConfig({ nodes: [], edges: [{ source: "missing", target: "missing" }] }), /invalid edge/);
});

test("workflow validation accepts a connected acyclic graph from one Start", () => {
  const config = {
    nodes: [
      { id: "start", type: "start" },
      { id: "agent", type: "agent" },
      { id: "review", type: "review" },
    ],
    edges: [
      { source: "start", target: "agent" },
      { source: "agent", target: "review" },
    ],
  };
  const graph = graphFromConfig(config);
  assert.deepEqual(validateWorkflowGraph(graph.nodes, graph.edges), []);
  const saved = configFromGraph(config, graph.nodes, graph.edges);
  const reopened = graphFromConfig(saved);
  assert.equal(reopened.nodes[0].data.workflowNode.type, "start");
  assert.deepEqual(saved.edges, config.edges);
});

test("workflow validation reports missing Start, disconnected nodes, duplicate Starts and cycles", () => {
  const withoutStart = graphFromConfig({
    nodes: [{ id: "agent", type: "agent" }],
    edges: [],
  });
  assert.match(validateWorkflowGraph(withoutStart.nodes, withoutStart.edges).join(" "), /Add a Start node/);

  const disconnected = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "agent", type: "agent" },
      { id: "review", type: "review", label: "Unconnected review" },
    ],
    edges: [{ source: "start", target: "agent" }],
  });
  assert.match(validateWorkflowGraph(disconnected.nodes, disconnected.edges).join(" "), /Unconnected review/);

  const duplicateStart = graphFromConfig({
    nodes: [{ id: "start-1", type: "start" }, { id: "start-2", type: "start" }],
    edges: [{ source: "start-1", target: "start-2" }],
  });
  assert.match(validateWorkflowGraph(duplicateStart.nodes, duplicateStart.edges).join(" "), /only one Start/);

  const cycle = graphFromConfig({
    nodes: [{ id: "start", type: "start" }, { id: "agent", type: "agent" }],
    edges: [{ source: "start", target: "agent" }, { source: "agent", target: "start" }],
  });
  assert.match(validateWorkflowGraph(cycle.nodes, cycle.edges).join(" "), /cycle/);
});

test("Start and data source nodes reject incoming connections", () => {
  const graph = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "source", type: "data_source", label: "Input file" },
      { id: "agent", type: "agent" },
    ],
    edges: [
      { source: "start", target: "source" },
      { source: "source", target: "agent" },
    ],
  });
  assert.match(validateWorkflowGraph(graph.nodes, graph.edges).join(" "), /Input file does not accept incoming/);
});

test("data sources are independent roots and can connect directly to agents", () => {
  const graph = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "source", type: "data_source", label: "Input file" },
      { id: "agent", type: "agent" },
    ],
    edges: [
      { source: "start", target: "agent" },
      { source: "source", target: "agent" },
    ],
  });
  assert.deepEqual(validateWorkflowGraph(graph.nodes, graph.edges), []);
});

test("review nodes require exactly one input from an agent", () => {
  const graphWithSource = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "source", type: "data_source" },
      { id: "review", type: "review", label: "Approval" },
    ],
    edges: [
      { source: "start", target: "source" },
      { source: "source", target: "review" },
    ],
  });
  assert.match(validateWorkflowGraph(graphWithSource.nodes, graphWithSource.edges).join(" "), /must receive its input from an agent/);

  const graphWithMultipleInputs = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "agent-1", type: "agent" },
      { id: "agent-2", type: "agent" },
      { id: "review", type: "review" },
    ],
    edges: [
      { source: "start", target: "agent-1" },
      { source: "start", target: "agent-2" },
      { source: "agent-1", target: "review" },
      { source: "agent-2", target: "review" },
    ],
  });
  assert.match(validateWorkflowGraph(graphWithMultipleInputs.nodes, graphWithMultipleInputs.edges).join(" "), /exactly one incoming connection/);

  const valid = graphFromConfig({
    nodes: [
      { id: "start", type: "start" },
      { id: "agent", type: "agent" },
      { id: "review", type: "review" },
    ],
    edges: [
      { source: "start", target: "agent" },
      { source: "agent", target: "review" },
    ],
  });
  assert.deepEqual(validateWorkflowGraph(valid.nodes, valid.edges), []);
});
