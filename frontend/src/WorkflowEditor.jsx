import { useCallback, useEffect, useState } from "react";
import ReactFlow, {
  addEdge, Background, Controls, Handle, Position, useEdgesState, useNodesState,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  configFromGraph, flowNodeType, graphFromConfig, NODE_TYPES, nodeStyle, validateWorkflowGraph,
} from "./workflowGraph";
import "./WorkflowEditor.css";

const MAX_FILE_BYTES = 1_048_576;
const FILE_ACCEPT = ".txt,.md,.csv,.json,.yaml,.yml,.xml,.log";

function WorkflowSourceNode({ data }) {
  return (
    <>
      <Handle type="source" position={Position.Right} />
      <span>{data.label}</span>
    </>
  );
}

const flowNodeTypes = { workflowSource: WorkflowSourceNode };

async function readResponse(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message = typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail.map((item) => item.msg).filter(Boolean).join(" ")
        : "The request failed. Check the file and try again.";
    throw new Error(message);
  }
  return payload;
}

async function encodeFile(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return btoa(binary);
}

export function WorkflowEditor({ configId, onClose, onSave }) {
  const [savedConfigId, setSavedConfigId] = useState(configId);
  const [name, setName] = useState("");
  const [config, setConfig] = useState({ nodes: [], edges: [] });
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [agentConfigs, setAgentConfigs] = useState([]);
  const [constantFiles, setConstantFiles] = useState([]);
  const [previewFile, setPreviewFile] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState(null);
  const [loading, setLoading] = useState(Boolean(configId));
  const [fileBusy, setFileBusy] = useState(false);
  const [graphLoaded, setGraphLoaded] = useState(!configId);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState("");
  const [saveMessage, setSaveMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const [agents, saved] = await Promise.all([
          fetch("/api/agent-configs", { signal: controller.signal }).then(readResponse),
          configId
            ? fetch(`/api/workflow-configs/${encodeURIComponent(configId)}`, { signal: controller.signal }).then(readResponse)
            : Promise.resolve(null),
        ]);
        if (controller.signal.aborted) return;
        setAgentConfigs(agents);
        if (saved) {
          const parsed = typeof saved.config === "string" ? JSON.parse(saved.config) : saved.config;
          const graph = graphFromConfig(parsed);
          setName(saved.name);
          setConfig(parsed);
          setNodes(graph.nodes);
          setEdges(graph.edges);
          setGraphLoaded(true);
        }
      } catch (failure) {
        if (failure.name !== "AbortError" && !controller.signal.aborted) {
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

  useEffect(() => {
    if (!savedConfigId) return undefined;
    const controller = new AbortController();
    fetch(`/api/workflow-configs/${encodeURIComponent(savedConfigId)}/constant-files`, { signal: controller.signal })
      .then(readResponse)
      .then(setConstantFiles)
      .catch((failure) => {
        if (failure.name !== "AbortError" && !controller.signal.aborted) setError(failure.message);
      });
    return () => controller.abort();
  }, [savedConfigId]);

  const onConnect = useCallback((connection) => {
    setEdges((current) => addEdge({ ...connection, data: { workflowEdge: {} } }, current));
  }, [setEdges]);

  const onSelectionChange = useCallback(({ nodes: selectedNodes, edges: selectedEdges }) => {
    setSelectedNodeId(selectedNodes[0]?.id || null);
    setSelectedEdgeId(selectedEdges[0]?.id || null);
  }, []);

  function addNode(type) {
    if (type === "start" && nodes.some((node) => node.data.workflowNode.type === "start")) return;
    let number = 1;
    while (nodes.some((node) => node.id === `${type}-${number}`)) number += 1;
    const id = `${type}-${number}`;
    const node = { id, type, label: NODE_TYPES[type].label };
    if (type === "agent") node.agent_id = "";
    if (type === "data_source") node.source_type = "constant_file";
    setNodes((current) => [...current.map((item) => ({ ...item, selected: false })), {
      id, type: flowNodeType(type), position: { x: 80 + (current.length % 4) * 200, y: 80 + Math.floor(current.length / 4) * 120 },
      data: { label: node.label, workflowNode: node }, style: nodeStyle(type), selected: true,
    }]);
    setEdges((current) => current.map((edge) => ({ ...edge, selected: false })));
    setSelectedNodeId(id);
    setSelectedEdgeId(null);
  }

  const selectedNode = nodes.find((node) => node.id === selectedNodeId);
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId);
  const selectedNodeInputs = selectedNode
    ? edges
      .filter((edge) => edge.target === selectedNode.id)
      .map((edge) => {
        const source = nodes.find((node) => node.id === edge.source);
        return source && {
          id: source.id,
          label: source.data.label || source.id,
          type: source.data.workflowNode.type,
        };
      })
      .filter(Boolean)
    : [];

  function updateNode(field, value) {
    const selectedFile = field === "file_id" ? constantFiles.find((file) => file.id === value) : null;
    setNodes((current) => current.map((node) => {
      if (node.id !== selectedNodeId) return node;
      const workflowNode = { ...node.data.workflowNode, [field]: value };
      return {
        ...node,
        data: {
          ...node.data,
          workflowNode,
          ...(field === "label" ? { label: value } : {}),
          ...(selectedFile ? { label: selectedFile.filename } : {}),
        },
      };
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
    setSaveMessage("");
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    const graph = configFromGraph(config, nodes, edges);
    const validationErrors = validateWorkflowGraph(nodes, edges);
    if (validationErrors.length) {
      setError(`This workflow is incomplete:\n${validationErrors.map((message) => `• ${message}`).join("\n")}`);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const response = await fetch(savedConfigId ? `/api/workflow-configs/${encodeURIComponent(savedConfigId)}` : "/api/workflow-configs", {
        method: savedConfigId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), config: graph }),
      });
      const saved = await readResponse(response);
      setSavedConfigId(saved.id);
      setConfig(typeof saved.config === "string" ? JSON.parse(saved.config) : saved.config);
      setSaveMessage("Workflow saved successfully.");
      onSave?.(saved, { keepOpen: true });
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleDelete() {
    if (!savedConfigId || !window.confirm("Are you sure you want to delete this workflow?")) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`/api/workflow-configs/${encodeURIComponent(savedConfigId)}`, { method: "DELETE" });
      await readResponse(response);
      onSave?.();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function uploadConstantFile(file, fileId = null) {
    if (!file) return;
    if (!savedConfigId) {
      setError("Save the workflow before adding constant files.");
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      setError(`Files must be ${MAX_FILE_BYTES.toLocaleString()} bytes or smaller.`);
      return;
    }
    setFileBusy(true);
    setError("");
    try {
      const content_base64 = await encodeFile(file);
      const url = `/api/workflow-configs/${encodeURIComponent(savedConfigId)}/constant-files${fileId ? `/${encodeURIComponent(fileId)}` : ""}`;
      const response = await fetch(url, {
        method: fileId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, content_base64 }),
      });
      const saved = await readResponse(response);
      setConstantFiles((current) => {
        const withoutFile = current.filter((entry) => entry.id !== saved.id);
        return [...withoutFile, { ...saved, content: undefined }].sort((a, b) => a.filename.localeCompare(b.filename));
      });
      setNodes((current) => current.map((node) => (
        node.data.workflowNode.file_id === saved.id
          ? { ...node, data: { ...node.data, label: saved.filename } }
          : node
      )));
      if (previewFile?.id === saved.id) setPreviewFile({ ...saved });
    } catch (failure) {
      setError(failure.message);
    } finally {
      setFileBusy(false);
    }
  }

  async function inspectConstantFile(file) {
    setFileBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/workflow-configs/${encodeURIComponent(savedConfigId)}/constant-files/${encodeURIComponent(file.id)}`,
      );
      setPreviewFile(await readResponse(response));
    } catch (failure) {
      setError(failure.message);
    } finally {
      setFileBusy(false);
    }
  }

  async function removeConstantFile(file) {
    if (!window.confirm(`Remove ${file.filename} from this workflow?`)) return;
    setFileBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/workflow-configs/${encodeURIComponent(savedConfigId)}/constant-files/${encodeURIComponent(file.id)}`,
        { method: "DELETE" },
      );
      await readResponse(response);
      setConstantFiles((current) => current.filter((entry) => entry.id !== file.id));
      setNodes((current) => current.map((node) => {
        if (node.data.workflowNode.file_id !== file.id) return node;
        const workflowNode = { ...node.data.workflowNode, source_type: "manual" };
        delete workflowNode.file_id;
        return { ...node, data: { ...node.data, label: workflowNode.label || "Data source", workflowNode } };
      }));
      if (previewFile?.id === file.id) setPreviewFile(null);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setFileBusy(false);
    }
  }

  function agentsReceiving(fileId) {
    const sourceIds = new Set(nodes
      .filter((node) => node.data.workflowNode.file_id === fileId)
      .map((node) => node.id));
    return nodes
      .filter((node) => node.data.workflowNode.type === "agent"
        && edges.some((edge) => sourceIds.has(edge.source) && edge.target === node.id))
      .map((node) => node.data.label || "Agent");
  }

  return (
    <div className="workflow-editor" role="dialog" aria-modal="true" aria-labelledby="workflow-editor-title">
      <header className="workflow-editor-header">
        <h2 id="workflow-editor-title">{savedConfigId ? "Edit Workflow" : "New Workflow"}</h2>
        <label className="workflow-name">Name
          <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} placeholder="Workflow name" disabled={loading || loadFailed} />
        </label>
        <div className="workflow-editor-actions">
          {savedConfigId && <button className="delete-btn" onClick={handleDelete} disabled={loading || fileBusy}>Delete workflow</button>}
          <button onClick={onClose} disabled={loading}>Cancel</button>
          <button className="save-btn" onClick={handleSave} disabled={loading || fileBusy || loadFailed}>Save</button>
        </div>
      </header>
      {error && <div className="workflow-editor-error" role="alert">{error}</div>}
      {saveMessage && <div className="workflow-editor-success" role="status">{saveMessage}</div>}
      <div className="workflow-editor-body">
        <aside className="workflow-editor-palette">
          <h3>Add node</h3>
          {Object.entries(NODE_TYPES).map(([type, definition]) => (
            <button key={type} onClick={() => addNode(type)}
              disabled={loading || fileBusy || loadFailed || (type === "start" && nodes.some((node) => node.data.workflowNode.type === "start"))}>
              + {definition.label}{type === "start" && nodes.some((node) => node.data.workflowNode.type === "start") ? " (added)" : ""}
            </button>
          ))}
          <p>Connect nodes by dragging between their handles. Select a node or edge to edit it.</p>
          <section className="constant-file-library" aria-label="Constant files">
            <h3>Constant files</h3>
            <label className={`constant-file-upload${!savedConfigId || fileBusy ? " disabled" : ""}`}>
              Upload text file
              <input
                type="file"
                accept={FILE_ACCEPT}
                disabled={!savedConfigId || fileBusy || loading || loadFailed}
                onChange={(event) => {
                  void uploadConstantFile(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
            </label>
            {!savedConfigId && <p>Save the workflow before adding files.</p>}
            {constantFiles.map((file) => {
              const recipients = agentsReceiving(file.id);
              return (
                <article className="constant-file-item" key={file.id}>
                  <strong title={file.filename}>{file.filename}</strong>
                  <small>Receives: {recipients.length ? recipients.join(", ") : "No connected agents"}</small>
                  <div className="constant-file-actions">
                    <button onClick={() => inspectConstantFile(file)} disabled={fileBusy}>View</button>
                    <label className={`constant-file-replace${fileBusy ? " disabled" : ""}`}>
                      Replace
                      <input
                        type="file"
                        accept={FILE_ACCEPT}
                        disabled={fileBusy || loading || loadFailed}
                        onChange={(event) => {
                          void uploadConstantFile(event.target.files?.[0], file.id);
                          event.target.value = "";
                        }}
                      />
                    </label>
                    <button className="delete-btn" onClick={() => removeConstantFile(file)} disabled={fileBusy}>Remove</button>
                  </div>
                </article>
              );
            })}
            {previewFile && (
              <div className="constant-file-preview">
                <div><strong>{previewFile.filename}</strong><button onClick={() => setPreviewFile(null)} aria-label="Close file preview">×</button></div>
                <pre>{previewFile.content}</pre>
              </div>
            )}
          </section>
        </aside>
        <div className="workflow-editor-canvas">
          <ReactFlow key={graphLoaded ? "loaded" : "loading"} nodes={nodes} edges={edges} nodeTypes={flowNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
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
                <label>Agent
                  <select value={selectedNode.data.workflowNode.agent_id || ""} onChange={(event) => updateNode("agent_id", event.target.value)}>
                    <option value="">Select an agent</option>
                    {selectedNode.data.workflowNode.agent_id && !agentConfigs.some((agent) => agent.id === selectedNode.data.workflowNode.agent_id)
                      && <option value={selectedNode.data.workflowNode.agent_id}>{selectedNode.data.workflowNode.agent_id}</option>}
                    {agentConfigs.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}
                  </select>
                </label>
              )}
              {selectedNode.data.workflowNode.type === "agent" && (
                <section className="workflow-agent-inputs" aria-label="Connected context inputs">
                  <strong>Connected inputs provided as context</strong>
                  {selectedNodeInputs.length ? (
                    <ul>{selectedNodeInputs.map((input) => (
                      <li key={input.id}>{input.label} <span>({NODE_TYPES[input.type]?.label || input.type})</span></li>
                    ))}</ul>
                  ) : <p>No incoming connections. Connect a data source or previous step to provide context.</p>}
                </section>
              )}
              {selectedNode.data.workflowNode.type === "data_source" && (
                <>
                  <label>Source type
                    <select value={selectedNode.data.workflowNode.source_type || "manual"} onChange={(event) => updateNode("source_type", event.target.value)}>
                      <option value="constant_file">Constant file</option>
                      <option value="manual">Manual</option>
                      <option value="files">Other files</option>
                      <option value="chat">Chat</option>
                    </select>
                  </label>
                  {selectedNode.data.workflowNode.source_type === "constant_file" && (
                    <>
                      <label>File
                        <select value={selectedNode.data.workflowNode.file_id || ""} onChange={(event) => updateNode("file_id", event.target.value)}>
                          <option value="">Select a constant file</option>
                          {constantFiles.map((file) => <option value={file.id} key={file.id}>{file.filename}</option>)}
                        </select>
                      </label>
                      <p className="constant-file-recipients">
                        Receives: {edges.filter((edge) => edge.source === selectedNode.id)
                          .map((edge) => nodes.find((node) => node.id === edge.target))
                          .filter((node) => node?.data.workflowNode.type === "agent")
                          .map((node) => node.data.label || "Agent").join(", ") || "Connect this source to an agent."}
                      </p>
                      {!constantFiles.length && <p>Upload a constant file in the left panel first.</p>}
                    </>
                  )}
                </>
              )}
              <label>Output<input value={selectedNode.data.workflowNode.output || ""} onChange={(event) => updateNode("output", event.target.value)} /></label>
              {selectedNode.data.workflowNode.type === "review" && (
                <label>Input<input value={selectedNode.data.workflowNode.input || ""} onChange={(event) => updateNode("input", event.target.value)} /></label>
              )}
              <button className="delete-btn" onClick={deleteSelection} disabled={loading || fileBusy}>Delete node</button>
            </>
          ) : selectedEdge ? (
            <>
              <label>Data<input value={typeof selectedEdge.data?.workflowEdge?.data === "string" ? selectedEdge.data.workflowEdge.data : ""} onChange={(event) => updateEdge(event.target.value)} /></label>
              <button className="delete-btn" onClick={deleteSelection} disabled={loading || fileBusy}>Delete edge</button>
            </>
          ) : <p>Select a node or edge to edit its properties.</p>}
          </fieldset>
        </aside>
      </div>
    </div>
  );
}
