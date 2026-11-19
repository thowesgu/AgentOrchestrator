import test from "node:test";
import assert from "node:assert/strict";
import { configFromGraph, graphFromConfig } from "./workflowGraph.js";

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
  assert.deepEqual(result.nodes[1].position, { x: 120, y: 80 });
  assert.deepEqual(result.edges, config.edges);
  assert.deepEqual(graphFromConfig(result).nodes[1].position, { x: 120, y: 80 });
});

test("unsupported graphs fail without being replaced", () => {
  assert.throws(() => graphFromConfig({ steps: [] }), /nodes and edges/);
  assert.throws(() => graphFromConfig({ nodes: [{ id: "x", type: "other" }], edges: [] }), /unsupported node/);
  assert.throws(() => graphFromConfig({ nodes: [], edges: [{ source: "missing", target: "missing" }] }), /invalid edge/);
});
