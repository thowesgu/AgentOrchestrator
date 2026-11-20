import test from "node:test";
import assert from "node:assert/strict";
import { visualizationGraph } from "./visualizationGraph.js";

test("visualization uses saved positions, types, labels and connections", () => {
  const graph = visualizationGraph({
    nodes: [
      { id: "input", type: "data_source", label: "Request", position: { x: 800, y: 450 } },
      { id: "agent", type: "agent", label: "Developer", position: { x: 1200, y: 450 } },
      { id: "review", type: "review", label: "Review", position: { x: 1500, y: 450 } },
    ],
    edges: [
      { source: "input", target: "agent", data: "request" },
      { source: "agent", target: "review" },
    ],
  });
  assert.deepEqual(graph.nodes.map((node) => node.position), [
    { x: 800, y: 450 }, { x: 1200, y: 450 }, { x: 1500, y: 450 },
  ]);
  assert.deepEqual(graph.nodes.map((node) => node.data.label), ["Request", "Developer", "Review"]);
  assert.equal(new Set(graph.nodes.map((node) => node.style.background)).size, 3);
  assert.equal(graph.edges[0].label, "request");
  assert.equal(graph.edges[1].target, "review");
});

test("legacy nodes without positions do not overlap at the origin", () => {
  const graph = visualizationGraph({
    nodes: [
      { id: "a", type: "agent" },
      { id: "b", type: "review" },
      { id: "c", type: "data_source" },
    ],
    edges: [],
  });
  assert.equal(new Set(graph.nodes.map((node) => JSON.stringify(node.position))).size, 3);
});

test("visualization marks the active and completed workflow steps", () => {
  const graph = visualizationGraph({
    nodes: [
      { id: "agent-1", type: "agent", label: "Research" },
      { id: "agent-2", type: "agent", label: "Summarize" },
    ],
    edges: [{ source: "agent-1", target: "agent-2" }],
    active_node_id: "agent-2",
    node_statuses: { "agent-1": "completed", "agent-2": "active" },
  });

  assert.equal(graph.nodes[0].data.label, "Research · Completed");
  assert.equal(graph.nodes[1].data.label, "Summarize · Active");
  assert.equal(graph.nodes[1].style.borderColor, "#d09541");
});
