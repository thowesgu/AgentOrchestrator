import { useEffect, useRef, useState } from "react";
import { AgentEditor } from "./AgentEditor";
import { WorkflowEditor } from "./WorkflowEditor";
import { WorkflowVisualization } from "./WorkflowVisualization";

const WORKFLOW_STEPS = ["Agent 1", "Release", "Agent 2"];

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(payload.detail || "Something went wrong. Please try again.");
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

function stepStatus(stage, index) {
  if (stage === "discarded") {
    return index === 0 ? "done" : index === 1 ? "discarded" : "not run";
  }
  if (stage === "agent2_error") {
    return index < 2 ? "done" : "failed";
  }
  const activeIndex = stage === "agent1" ? 0 : stage === "review" ? 1 : 2;
  if (stage === "complete") return "done";
  return index < activeIndex ? "done" : index === activeIndex ? "active" : "up next";
}

export default function App() {
  const [sessions, setSessions] = useState([]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState("");
  const [sessionPrompt, setSessionPrompt] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [workflowId, setWorkflowId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [workflow, setWorkflow] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [draft, setDraft] = useState("");
  const [finalizationPrompt, setFinalizationPrompt] = useState("");
  const [pendingMessages, setPendingMessages] = useState([]);
  const [starting, setStarting] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState("");
  const [editingConfigId, setEditingConfigId] = useState(null);
  const [agentConfigs, setAgentConfigs] = useState([]);
  const [workflowConfigs, setWorkflowConfigs] = useState([]);
  const [editingWorkflowId, setEditingWorkflowId] = useState(null);
  const endOfMessages = useRef(null);
  const activeSessionRef = useRef(null);
  const selectedNodeRef = useRef(null);
  const sessionProcessing = Boolean(
    workflow?.agent_status === "running"
    || pendingMessages.includes(`${sessionId}:${selectedNodeId || ""}`),
  );
  const runtimeUnavailable = workflow?.agent_status === "unavailable";
  const isTerminal = workflow?.stage === "complete"
    || workflow?.stage === "discarded"
    || workflow?.stage === "agent2_error";

  async function refreshLists() {
    try {
      const [savedSessions, configs, workflows] = await Promise.all([
        request("/api/sessions"), request("/api/agent-configs"), request("/api/workflow-configs"),
      ]);
      setSessions(savedSessions);
      setAgentConfigs(configs);
      setWorkflowConfigs(workflows);
      setSelectedWorkflowId((current) => (
        workflows.some((item) => item.id === current) ? current : workflows[0]?.id || ""
      ));
    } catch (failure) {
      setError(failure.message);
    }
  }

  useEffect(() => {
    refreshLists();
    const timer = setInterval(refreshLists, 1200);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    endOfMessages.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!sessionId || isTerminal) return undefined;
    let cancelled = false;
    let timer;
    async function refreshWorkflow() {
      try {
        const current = await request(
          `/api/sessions/${encodeURIComponent(sessionId)}/workflow${selectedNodeId ? `?node_id=${encodeURIComponent(selectedNodeId)}` : ""}`,
        );
        if (cancelled) return;
        setWorkflow(current);
        if (Array.isArray(current.messages)) {
          setMessages((previous) => (
            JSON.stringify(previous) === JSON.stringify(current.messages)
              ? previous : current.messages
          ));
        }
        setFinalizationPrompt(current.expected_result || "");
        timer = setTimeout(refreshWorkflow, 900);
      } catch (failure) {
        if (!cancelled) setError(failure.message);
      }
    }
    refreshWorkflow();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [sessionId, selectedNodeId, isTerminal]);

  useEffect(() => {
    if (!sessionId || !["agent1", "agent2"].includes(workflow?.stage)) return;
    const active = workflow.active_node_ids || [];
    if (active.length && !active.includes(selectedNodeId)) {
      const next = active.includes(workflow.active_node_id) ? workflow.active_node_id : active[0];
      selectedNodeRef.current = next;
      setSelectedNodeId(next);
    }
  }, [sessionId, selectedNodeId, workflow]);

  async function selectSession(saved) {
    activeSessionRef.current = saved.id;
    setSessionId(saved.id);
    selectedNodeRef.current = null;
    setSelectedNodeId(null);
    setEditingConfigId(null);
    setMessages(saved.messages || []);
    setFinalizationPrompt(saved.expected_result || "");
    setWorkflow({
      stage: saved.stage, artifact_ready: false, artifact_content: null,
      output: saved.output || "", error: saved.error || "",
      interaction_mode: saved.interaction_mode || "interactive",
      expected_result: saved.expected_result || "",
    });
    setError(saved.error || "");
    try {
      const current = await request(`/api/sessions/${encodeURIComponent(saved.id)}/workflow`);
      if (activeSessionRef.current === saved.id) {
        setWorkflow(current);
        setMessages(current.messages || []);
        setFinalizationPrompt(current.expected_result || "");
      }
    } catch (failure) {
      if (activeSessionRef.current === saved.id) setError(failure.message);
    }

  }

  async function selectWorkflowNode(nodeId) {
    const node = workflow?.nodes?.find((item) => item.id === nodeId);
    if (!node || node.type !== "agent" || !workflow.active_node_ids?.includes(nodeId)) return;
    selectedNodeRef.current = nodeId;
    setSelectedNodeId(nodeId);
    try {
      const current = await request(
        `/api/sessions/${encodeURIComponent(sessionId)}/workflow?node_id=${encodeURIComponent(nodeId)}`,
      );
      if (activeSessionRef.current === sessionId && selectedNodeRef.current === nodeId) {
        setWorkflow(current);
        setMessages(current.messages || []);
        setFinalizationPrompt(current.expected_result || "");
      }
    } catch (failure) {
      setError(failure.message);
    }
  }

  async function removeSession(saved, event) {
    event?.stopPropagation();
    try {
      await request(`/api/sessions/${encodeURIComponent(saved.id)}`, { method: "DELETE" });
      if (sessionId === saved.id) goHome();
      await refreshLists();
    } catch (failure) {
      setError(failure.message);
    }
  }

  async function startChat(event) {
    event?.preventDefault();
    const content = sessionPrompt.trim();
    if (!content || !selectedWorkflowId) return;
    setStarting(true);
    setError("");
    let createdSessionId = null;
    let requestKey = null;
    try {
      const result = await request("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ workflow_id: selectedWorkflowId }),
      });
      createdSessionId = result.session_id;
      activeSessionRef.current = result.session_id;
      setSessionId(result.session_id);
      setWorkflowId(selectedWorkflowId);
      setMessages(result.messages);
      setWorkflow(result.workflow);
      const initialNodeId = result.workflow.active_node_id || null;
      selectedNodeRef.current = initialNodeId;
      setSelectedNodeId(initialNodeId);
      setFinalizationPrompt(result.workflow.expected_result || "");
      setSessionPrompt("");
      void refreshLists();
      requestKey = `${result.session_id}:${initialNodeId || ""}`;
      setPendingMessages((current) => [...current, requestKey]);
      const firstStep = await request(
        `/api/sessions/${encodeURIComponent(result.session_id)}/messages`,
        { method: "POST", body: JSON.stringify({ content, node_id: initialNodeId || undefined }) },
      );
      if (activeSessionRef.current === result.session_id) {
        if (selectedNodeRef.current === initialNodeId) {
          setMessages(firstStep.messages);
          setWorkflow(firstStep.workflow);
        }
        setError(firstStep.workflow.error || "");
      }
    } catch (failure) {
      if (createdSessionId && activeSessionRef.current === createdSessionId) {
        setError(failure.message);
      } else if (!createdSessionId) {
        setError(failure.message);
      }
    } finally {
      setStarting(false);
      if (requestKey) {
        setPendingMessages((current) => current.filter((key) => key !== requestKey));
      }
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const content = draft.trim();
    const activeId = sessionId;
    if (!content || !sessionId || sessionProcessing || !["agent1", "agent2"].includes(workflow?.stage)) return;

    setDraft("");
    setError("");
    const requestKey = `${activeId}:${selectedNodeId || ""}`;
    setPendingMessages((current) => [...current, requestKey]);
    try {
      const result = await request(
        `/api/sessions/${encodeURIComponent(activeId)}/messages`,
        { method: "POST", body: JSON.stringify({ content, node_id: selectedNodeId || undefined }) },
      );
      if (activeSessionRef.current === activeId && selectedNodeRef.current === selectedNodeId) {
        setMessages(result.messages);
        setWorkflow(result.workflow);
        setError(result.workflow.error || "");
      }
    } catch (failure) {
      if (activeSessionRef.current === activeId) setError(failure.message);
      if (activeSessionRef.current === activeId && selectedNodeRef.current === selectedNodeId) {
        setMessages((current) => [
          ...current,
          { role: "user", content, status: "failed" },
        ]);
      }
      if (failure.status === 404) {
        activeSessionRef.current = null;
        setSessionId(null);
      }
    } finally {
      setPendingMessages((current) => current.filter((key) => key !== requestKey));
    }
  }

  async function runAction(action, payload = null) {
    if (!sessionId || actionLoading) return;
    const activeId = sessionId;
    const nodeId = selectedNodeId;
    setActionLoading(true);
    setError("");
    try {
      const options = { method: "POST" };
      if (payload) {
        options.body = JSON.stringify({
          ...payload,
          ...(["finalize", "finalize-last"].includes(action) && nodeId ? { node_id: nodeId } : {}),
        });
      }
      const result = await request(
        `/api/sessions/${encodeURIComponent(sessionId)}/${action}`,
        options,
      );
      if (activeSessionRef.current === activeId
          && (!["finalize", "finalize-last"].includes(action) || selectedNodeRef.current === nodeId)) {
        setWorkflow(result);
        setError(result.error || "");
        setFinalizationPrompt(result.expected_result || "");
      }
    } catch (failure) {
      setError(failure.message);
    } finally {
      setActionLoading(false);
    }
  }

  function goHome() {
    activeSessionRef.current = null;
    setSessionId(null);
    setWorkflowId(null);
    selectedNodeRef.current = null;
    setSelectedNodeId(null);
    setMessages([]);
    setWorkflow(null);
    setDraft("");
    setFinalizationPrompt("");
    setError("");
    setEditingConfigId(null);
  }

  const stage = workflow?.stage || "agent1";
  const interactiveMode = workflow?.interaction_mode !== "autonomous";
  const chatVisible = ["agent1", "agent2"].includes(stage) && interactiveMode;
  const reviewVisible = stage === "review";
  const agentName = workflow?.active_agent_name || "Agent";
  const latestMessage = messages[messages.length - 1];
  const canUseLastAnswer = messages.length >= 3
    && latestMessage?.role === "assistant"
    && latestMessage?.status === "complete";

  return (
    <main className="app-shell">
      <header className="topbar">
        <a
          className={`wordmark ${isTerminal ? "" : "disabled-link"}`}
          href="/"
          onClick={(event) => {
            event.preventDefault();
            if (isTerminal) goHome();
          }}
          aria-disabled={!isTerminal}
        >
          <span className="brand-mark small" aria-hidden="true">✳</span> Copilot
        </a>
        <div className="online-indicator">
          <span className={["complete", "discarded", "agent2_error"].includes(stage) ? "status-dot complete-dot" : ""} />
          {stage === "complete" ? "Workflow complete" : stage === "discarded" ? "Workflow discarded" : stage === "agent2_error" ? "Workflow failed" : "Workflow in progress"}
        </div>
      </header>
      <div className="workflow-layout">
        <aside className="agents-sidebar" aria-label="Agents and sessions">
          <div className="agents-heading">
            <span className="brand-mark small" aria-hidden="true">✳</span>
            <strong>Orchestrator</strong>
          </div>
          <div className="sidebar-section-heading">
            <span>AGENT CONFIGS</span>
            <button className="icon-button" aria-label="Create agent config" onClick={() => { setEditingWorkflowId(null); setEditingConfigId("new"); }}>+</button>
          </div>
          <div className="config-list">
            {agentConfigs.map((config) => (
              <button
                key={config.id}
                className={`config-row ${editingConfigId === config.id ? "selected" : ""}`}
                onClick={() => { setEditingWorkflowId(null); setEditingConfigId(config.id); }}
              >
                <span className="config-name">{config.name}</span>
              </button>
            ))}
          </div>
          <div className="sidebar-section-heading">
            <span>WORKFLOW CONFIGS</span>
            <button className="icon-button" aria-label="Create workflow config" onClick={() => { setEditingConfigId(null); setEditingWorkflowId("new"); }}>+</button>
          </div>
          <div className="config-list">
            {workflowConfigs.map((config) => (
              <button
                key={config.id}
                className={`config-row ${editingWorkflowId === config.id ? "selected" : ""}`}
                onClick={() => { setEditingConfigId(null); setEditingWorkflowId(config.id); }}
              >
                <span className="config-name">{config.name}</span>
              </button>
            ))}
          </div>
          <div className="sidebar-section-heading sessions-heading"><span>SESSIONS</span></div>
          <button className={`new-session-button ${!sessionId ? "selected" : ""}`} onClick={goHome}>＋ <span>New session</span></button>
          <div className="session-list">
            {sessions.map((saved) => (
              <div className={`session-row ${sessionId === saved.id ? "selected" : ""}`} key={saved.id}>
                <button className="session-select" onClick={() => selectSession(saved)}>
                  <span className={`session-status status-${saved.status}`} aria-hidden="true" />
                  <span className="session-label">{saved.input || "New session"}<small>{saved.agent_name} · {saved.status === "running" ? "Running" : saved.status === "waiting" ? "Waiting" : "Completed"}</small></span>
                </button>
                <button className="icon-button subtle delete-session" aria-label="Delete session" onClick={(event) => removeSession(saved, event)}>×</button>
              </div>
            ))}
          </div>
        </aside>
        <section className="work-area" aria-label="Active workflow step">
          {editingConfigId && (
            <AgentEditor
              configId={editingConfigId === "new" ? null : editingConfigId}
              onClose={() => {
                setEditingConfigId(null);
                refreshLists();
              }}
              onSave={(saved) => {
                setEditingConfigId(null);
                refreshLists();
              }}
            />
          )}
          {editingWorkflowId && (
            <WorkflowEditor
              key={editingWorkflowId}
              configId={editingWorkflowId === "new" ? null : editingWorkflowId}
              onClose={() => {
                setEditingWorkflowId(null);
                refreshLists();
              }}
              onSave={(saved, options) => {
                if (!options?.keepOpen) setEditingWorkflowId(null);
                refreshLists();
              }}
            />
          )}
          {!editingConfigId && !editingWorkflowId && !sessionId && (
            <section className="new-session-panel">
              <div className="brand-mark" aria-hidden="true">✳</div>
              <p className="eyebrow">COPILOT WORKFLOW</p>
              <h1>A little help<br />goes a long way.</h1>
              <p className="intro">Choose a workflow and tell it what you would like to work on.</p>
              {error && <p className="alert" role="alert">{error}</p>}
              <form className="new-session-form" onSubmit={startChat}>
                <label htmlFor="workflow-picker">Selected workflow</label>
                <select id="workflow-picker" value={selectedWorkflowId} onChange={(event) => setSelectedWorkflowId(event.target.value)} required>
                  {workflowConfigs.map((config) => <option value={config.id} key={config.id}>{config.name}</option>)}
                </select>
                <label htmlFor="session-prompt">Your request</label>
                <textarea id="session-prompt" value={sessionPrompt} onChange={(event) => setSessionPrompt(event.target.value)} maxLength="20000" rows="5" placeholder="Describe what you would like the workflow to do…" required />
                <button className="primary-button" type="submit" disabled={starting || !sessionPrompt.trim() || !selectedWorkflowId}>
                  {starting ? "Starting session…" : "Start session"} {!starting && <span aria-hidden="true">↗</span>}
                </button>
              </form>
              <p className="footnote">The release-gated Agent 1 → review → Agent 2 workflow is unchanged.</p>
            </section>
          )}
          {sessionId && !editingConfigId && !editingWorkflowId && <>
          {chatVisible && (
            <section className="conversation" aria-label={`Conversation with ${agentName}`}>
              <div className="conversation-heading">
                <p className="eyebrow">{agentName.toUpperCase()} · ACTIVE</p>
                <h1>{stage === "agent1" ? "What should we work on?" : `Continue with ${agentName}`}</h1>
              </div>
              {workflow?.constant_files?.length > 0 && (
                <aside className="session-constant-files" aria-label={`Constant files provided to ${agentName}`}>
                  <strong>Connected files provided to {agentName}</strong>
                  <ul>{workflow.constant_files.map((file) => <li key={file.id}>{file.filename}</li>)}</ul>
                </aside>
              )}
              <div className="message-list" aria-live="polite">
                {messages.map((message, index) => (
                  <article
                    className={`message ${message.role === "user" ? "from-user" : "from-agent"} ${message.status === "failed" ? "failed" : ""}`}
                    key={`${index}-${message.role}`}
                  >
                    {message.role === "assistant" && <span className="message-avatar" aria-hidden="true">✳</span>}
                    <div className="message-content">
                      <p className="speaker">{message.role === "user" ? "You" : message.role === "instruction" ? "Configured result" : message.role === "context" ? "Connected workflow context" : agentName}</p>
                      <p className="message-text">{message.content}</p>
                      {message.status === "failed" && <p className="message-status">Not sent — please try again.</p>}
                    </div>
                  </article>
                ))}
                {sessionProcessing && (
                  <article className="message from-agent" aria-label="Agent 1 is replying">
                    <span className="message-avatar" aria-hidden="true">✳</span>
                    <div className="message-content">
                      <p className="speaker">{agentName}</p>
                      <p className="typing-indicator"><span /><span /><span /></p>
                    </div>
                  </article>
                )}
                <div ref={endOfMessages} />
              </div>
              {error && <p className="alert inline-alert" role="alert">{error}</p>}
              {runtimeUnavailable && (
                <p className="alert" role="alert">
                  This saved conversation is read-only because the agent runtime was restarted. Start a new session to continue.
                </p>
              )}
              {interactiveMode && <div className="conversation-actions">
                <div className="finalization-container">
                  <label>Configured result prompt:</label>
                  <p className="finalization-preview">{finalizationPrompt || "No expected result defined"}</p>
                </div>
                <p>When Agent 1 has enough context, send it this prompt to produce the result.</p>
                <button
                  className="primary-button"
                  onClick={() => runAction("finalize", { prompt: finalizationPrompt })}
                  disabled={runtimeUnavailable || actionLoading || sessionProcessing || !finalizationPrompt.trim()}
                >
                  {actionLoading ? "Creating result…" : "I’ve provided enough context"}
                </button>
                {!finalizationPrompt.trim() && (
                  <button
                    className="secondary-button"
                    onClick={() => runAction("finalize-last", {})}
                    disabled={runtimeUnavailable || actionLoading || sessionProcessing || !canUseLastAnswer}
                  >
                    Use last answer as result
                  </button>
                )}
              </div>}
              {!interactiveMode && <p className="composer-hint">Autonomous mode is creating the configured result from your initial request.</p>}
              {interactiveMode && <>
              <form className="composer" onSubmit={sendMessage}>
                <label className="visually-hidden" htmlFor="message">Your message</label>
                <textarea
                  id="message"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form.requestSubmit();
                    }
                  }}
                  placeholder="Answer Agent 1’s questions or add more context…"
                  rows="1"
                  maxLength="20000"
                  disabled={runtimeUnavailable || sessionProcessing || actionLoading}
                />
                <button
                  className="send-button"
                  type="submit"
                  aria-label="Send message"
                  disabled={runtimeUnavailable || sessionProcessing || actionLoading || !draft.trim()}
                >
                  ↑
                </button>
              </form>
              <p className="composer-hint">Enter to send · Shift + Enter for a new line</p>
              </>}
            </section>
          )}

          {reviewVisible && (
            <section className="review-panel" aria-label="Review Agent 1 result">
              <div className="conversation-heading">
                <p className="eyebrow">RELEASE · REVIEW REQUIRED</p>
                <h1>Review {agentName}’s result</h1>
                <p className="review-intro">The next workflow step will only start after you approve this exact version.</p>
              </div>
              {error && <p className="alert inline-alert" role="alert">{error}</p>}
              {runtimeUnavailable && (
                <p className="alert" role="alert">This saved review is read-only because the agent runtime was restarted.</p>
              )}
              {workflow.artifact_ready ? (
                <article className="file-card">
                  <div className="file-heading">
                    <span className="file-icon" aria-hidden="true">MD</span>
                    <div>
                      <strong>{workflow.artifact_name}</strong>
                      <span>Created by {agentName}</span>
                    </div>
                  </div>
                  <pre className="file-content">{workflow.artifact_content}</pre>
                </article>
              ) : (
                <div className="alert" role="alert">
                  The result file is not available, so it cannot be approved. Return to Agent 1 to try again.
                </div>
              )}
              <div className="review-actions">
                <button className="secondary-button" onClick={() => runAction("edit")} disabled={runtimeUnavailable || actionLoading}>
                  Continue discussion
                </button>
                <button className="discard-button" onClick={() => runAction("discard")} disabled={runtimeUnavailable || actionLoading}>
                  Discard
                </button>
                <button
                  className="primary-button approve-button"
                  onClick={() => runAction("approve")}
                  disabled={runtimeUnavailable || actionLoading || !workflow.artifact_ready}
                >
                  {actionLoading ? "Starting…" : "Approve"}
                  {!actionLoading && <span aria-hidden="true">✓</span>}
                </button>
              </div>
            </section>
          )}

          {["agent1", "agent2"].includes(stage) && !interactiveMode && (
            <section className="progress-panel" aria-live="polite">
              <div className="progress-mark" aria-hidden="true"><span /><span /><span /></div>
              <p className="eyebrow">{agentName.toUpperCase()} · ACTIVE</p>
              <h1>Workflow step in progress</h1>
              <p>{agentName} is processing its connected inputs.</p>
              {error && <p className="alert" role="alert">{error}</p>}
            </section>
          )}

          {stage === "complete" && (
            <section className="progress-panel terminal-panel">
              <div className="success-mark" aria-hidden="true">✓</div>
              <p className="eyebrow">WORKFLOW COMPLETE</p>
              <h1>Workflow complete</h1>
              <div className="output-content workflow-output" aria-label="Final workflow output">
                {workflow.output || "The workflow completed without producing output."}
              </div>
              <button className="secondary-button" onClick={goHome}>Back to start</button>
            </section>
          )}

          {stage === "discarded" && (
            <section className="progress-panel terminal-panel">
              <div className="discarded-mark" aria-hidden="true">×</div>
              <p className="eyebrow">WORKFLOW DISCARDED</p>
              <h1>This workflow has ended</h1>
              <p>Agent 2 was not started.</p>
              <button className="secondary-button" onClick={goHome}>Back to start</button>
            </section>
          )}

          {stage === "agent2_error" && (
            <section className="progress-panel terminal-panel">
              <div className="error-mark" aria-hidden="true">!</div>
              <p className="eyebrow">AGENT 2 · FAILED</p>
              <h1>The workflow could not finish</h1>
              <p className="alert" role="alert">{workflow.error || "Agent 2 could not process the approved result."}</p>
              <button className="secondary-button" onClick={goHome}>Back to start</button>
            </section>
          )}
          </>}
        </section>

        <aside className={`workflow-sidebar${sessionId ? " has-session" : ""}`} aria-label="Workflow progress">
          {sessionId ? (
            <>
              <p className="eyebrow">WORKFLOW</p>
              <h2>Workflow Visualization</h2>
              <WorkflowVisualization workflow={workflow} onSelectNode={selectWorkflowNode} />
            </>
          ) : (
            <div className="workflow-sidebar-empty">
              <p className="eyebrow">WORKFLOW</p>
              <h2>Your progress</h2>
              <p style={{ color: "#9a9e99", fontSize: "14px", marginTop: "16px" }}>
                Select or start a session to see the workflow visualization.
              </p>
            </div>
          )}
        </aside>
      </div>
    </main>
  );
}
