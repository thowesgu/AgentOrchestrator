import { useCallback, useEffect, useState } from "react";
import ReactFlow, { addEdge, Background, Controls, useEdgesState, useNodesState } from "reactflow";
import "reactflow/dist/style.css";
import { configFromGraph, graphFromConfig, NODE_TYPES, nodeStyle } from "./workflowGraph";
import "./WorkflowEditor.css";

export function WorkflowEditor({ configId, onClose, onSave }) {
  const [name, setName] = useState("");
  const [config, setConfig] = useState({ nodes: [], edges: [] });
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState(null);
  const [loading, setLoading] = useState(Boolean(configId));
  const [graphLoaded, setGraphLoaded] = useState(!configId);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!configId) return undefined;
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(`/api/workflow-configs/${encodeURIComponent(configId)}`, { signal: controller.signal });
        if (!response.ok) throw new Error("Unable to load workflow configuration.");
        const saved = await response.json();
        const parsed = typeof saved.config === "string" ? JSON.parse(saved.config) : saved.config;
        const graph = graphFromConfig(parsed);
        setName(saved.name);
        setConfig(parsed);
        setNodes(graph.nodes);
        setEdges(graph.edges);
        setGraphLoaded(true);
      } catch (failure) {
        if (failure.name !== "AbortError") {
          setLoadFailed(true);
          setError(failure.message);
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    load();
    return () => controller.abort();
  }, [configId, setNodes, setEdges]);

  const onConnect = useCallback((connection) => {
    setEdges((current) => addEdge({ ...connection, data: { workflowEdge: {} } }, current));
  }, [setEdges]);

  const onSelectionChange = useCallback(({ nodes: selectedNodes, edges: selectedEdges }) => {
    setSelectedNodeId(selectedNodes[0]?.id || null);
    setSelectedEdgeId(selectedEdges[0]?.id || null);
  }, []);

  function addNode(type) {
    let number = 1;
    while (nodes.some((node) => node.id === `${type}-${number}`)) number += 1;
    const id = `${type}-${number}`;
    const node = { id, type, label: NODE_TYPES[type].label };
    if (type === "agent") node.agent_id = "";
    if (type === "data_source") node.source_type = "manual";
    setNodes((current) => [...current.map((item) => ({ ...item, selected: false })), {
      id, position: { x: 80 + (current.length % 4) * 200, y: 80 + Math.floor(current.length / 4) * 120 },
      data: { label: node.label, workflowNode: node }, style: nodeStyle(type), selected: true,
    }]);
    setEdges((current) => current.map((edge) => ({ ...edge, selected: false })));
    setSelectedNodeId(id);
    setSelectedEdgeId(null);
  }

  const selectedNode = nodes.find((node) => node.id === selectedNodeId);
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId);

  function updateNode(field, value) {
    setNodes((current) => current.map((node) => {
      if (node.id !== selectedNodeId) return node;
      const workflowNode = { ...node.data.workflowNode, [field]: value };
      return { ...node, data: { ...node.data, workflowNode, ...(field === "label" ? { label: value } : {}) } };
    }));
  }

  function updateEdge(value) {
    setEdges((current) => current.map((edge) => edge.id !== selectedEdgeId ? edge : {
      ...edge, label: value,
      data: { ...edge.data, workflowEdge: { ...edge.data?.workflowEdge, data: value } },
    }));
  }

  function deleteSelection() {
    if (selectedNode) {
      setNodes((current) => current.filter((node) => node.id !== selectedNodeId));
      setEdges((current) => current.filter((edge) => edge.source !== selectedNodeId && edge.target !== selectedNodeId));
      setSelectedNodeId(null);
    } else if (selectedEdge) {
      setEdges((current) => current.filter((edge) => edge.id !== selectedEdgeId));
      setSelectedEdgeId(null);
    }
  }

  async function handleSave() {
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const response = await fetch(configId ? `/api/workflow-configs/${encodeURIComponent(configId)}` : "/api/workflow-configs", {
        method: configId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), config: configFromGraph(config, nodes, edges) }),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(typeof data.detail === "string" ? data.detail : "Save failed. Check the workflow and try again.");
      }
      onSave?.(await response.json());
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleDelete() {
    if (!configId || !window.confirm("Are you sure you want to delete this workflow?")) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`/api/workflow-configs/${encodeURIComponent(configId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("Delete failed.");
      onSave?.();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="workflow-editor" role="dialog" aria-modal="true" aria-labelledby="workflow-editor-title">
      <header className="workflow-editor-header">
        <h2 id="workflow-editor-title">{configId ? "Edit Workflow" : "New Workflow"}</h2>
        <label className="workflow-name">Name
          <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} placeholder="Workflow name" disabled={loading || loadFailed} />
        </label>
        <div className="workflow-editor-actions">
          {configId && <button className="delete-btn" onClick={handleDelete} disabled={loading}>Delete workflow</button>}
          <button onClick={onClose} disabled={loading}>Cancel</button>
          <button className="save-btn" onClick={handleSave} disabled={loading || loadFailed}>Save</button>
        </div>
      </header>
      {error && <div className="workflow-editor-error" role="alert">{error}</div>}
      <div className="workflow-editor-body">
        <aside className="workflow-editor-palette">
          <h3>Add node</h3>
          {Object.entries(NODE_TYPES).map(([type, definition]) => (
            <button key={type} onClick={() => addNode(type)} disabled={loading || loadFailed}>+ {definition.label}</button>
          ))}
          <p>Connect nodes by dragging between their handles. Select a node or edge to edit it.</p>
        </aside>
        <div className="workflow-editor-canvas">
          <ReactFlow key={graphLoaded ? "loaded" : "loading"} nodes={nodes} edges={edges} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
            onConnect={onConnect} nodesDraggable={!loading && !loadFailed} nodesConnectable={!loading && !loadFailed}
            elementsSelectable={!loading && !loadFailed} deleteKeyCode={loading || loadFailed ? null : ["Backspace", "Delete"]}
            onSelectionChange={onSelectionChange} fitView>
            <Background />
            <Controls />
          </ReactFlow>
        </div>
        <aside className="workflow-editor-details">
          <h3>Properties</h3>
          <fieldset disabled={loading || loadFailed}>
          {selectedNode ? (
            <>
              <p>{NODE_TYPES[selectedNode.data.workflowNode.type].label}</p>
              <label>Label<input value={selectedNode.data.label} onChange={(event) => updateNode("label", event.target.value)} /></label>
              {selectedNode.data.workflowNode.type === "agent" && (
                <label>Agent ID<input value={selectedNode.data.workflowNode.agent_id || ""} onChange={(event) => updateNode("agent_id", event.target.value)} /></label>
              )}
              {selectedNode.data.workflowNode.type === "data_source" && (
                <label>Source type<input value={selectedNode.data.workflowNode.source_type || ""} onChange={(event) => updateNode("source_type", event.target.value)} placeholder="e.g. manual, files, chat" /></label>
              )}
              <label>Output<input value={selectedNode.data.workflowNode.output || ""} onChange={(event) => updateNode("output", event.target.value)} /></label>
              {selectedNode.data.workflowNode.type === "agent" && (
                <label>Inputs (comma separated)<input value={(selectedNode.data.workflowNode.inputs || []).join(",")} onChange={(event) => updateNode("inputs", event.target.value.split(","))} onBlur={() => updateNode("inputs", (selectedNode.data.workflowNode.inputs || []).map((item) => item.trim()).filter(Boolean))} /></label>
              )}
              {selectedNode.data.workflowNode.type === "review" && (
                <label>Input<input value={selectedNode.data.workflowNode.input || ""} onChange={(event) => updateNode("input", event.target.value)} /></label>
              )}
              <button className="delete-btn" onClick={deleteSelection} disabled={loading}>Delete node</button>
            </>
          ) : selectedEdge ? (
            <>
              <label>Data<input value={typeof selectedEdge.data?.workflowEdge?.data === "string" ? selectedEdge.data.workflowEdge.data : ""} onChange={(event) => updateEdge(event.target.value)} /></label>
              <button className="delete-btn" onClick={deleteSelection} disabled={loading}>Delete edge</button>
            </>
          ) : <p>Select a node or edge to edit its properties.</p>}
          </fieldset>
        </aside>
      </div>
    </div>
  );
}
