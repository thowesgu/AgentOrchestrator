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
  const [draft, setDraft] = useState("");
  const [finalizationPrompt, setFinalizationPrompt] = useState("");
  const [showFinalizationPrompt, setShowFinalizationPrompt] = useState(false);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState("");
  const [editingConfigId, setEditingConfigId] = useState(null);
  const [agentConfigs, setAgentConfigs] = useState([]);
  const [workflowConfigs, setWorkflowConfigs] = useState([]);
  const [editingWorkflowId, setEditingWorkflowId] = useState(null);
  const endOfMessages = useRef(null);
  const activeSessionRef = useRef(null);
  const sessionIsRunning = sessions.some((saved) => saved.id === sessionId && saved.status === "running");
  const sessionProcessing = sessionIsRunning || (loading && activeSessionRef.current === sessionId);
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
      setSelectedWorkflowId((current) => current || workflows[0]?.id || "");
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
    if (!sessionId || workflow?.stage !== "agent2") return undefined;
    let cancelled = false;
    let timer;
    async function refreshWorkflow() {
      try {
        const current = await request(
          `/api/sessions/${encodeURIComponent(sessionId)}/workflow`,
        );
        if (cancelled) return;
        setWorkflow(current);
        if (current.stage === "agent2") {
          timer = setTimeout(refreshWorkflow, 900);
        }
      } catch (failure) {
        if (!cancelled) setError(failure.message);
      }
    }
    refreshWorkflow();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [sessionId, workflow?.stage]);

  async function selectSession(saved) {
    activeSessionRef.current = saved.id;
    setSessionId(saved.id);
    setEditingConfigId(null);
    setMessages(saved.messages || []);
    const config = agentConfigs.find(c => c.id === saved.agent_id);
    if (config?.expected_result) {
      setFinalizationPrompt(config.expected_result);
    }
    setWorkflow({
      stage: saved.stage, artifact_ready: false, artifact_content: null,
      output: saved.output || "", error: saved.error || "",
    });
    setError(saved.error || "");
    try {
      const current = await request(`/api/sessions/${encodeURIComponent(saved.id)}/workflow`);
      if (activeSessionRef.current === saved.id) setWorkflow(current);
    } catch (failure) {
      if (activeSessionRef.current === saved.id) setError(failure.message);
    }
  }

  async function removeSession(saved, event) {
    event?.stopPropagation();
    try {
      await request(`/api/sessions/${encodeURIComponent(saved.id)}`, { method: "DELETE" });
      if (sessionId === saved.id) goHome();
      await refreshLists();
    } catch (failure) {
      setAgentError(failure.message);
    }
  }

  async function startChat(event) {
    event?.preventDefault();
    const content = sessionPrompt.trim();
    if (!content || !selectedWorkflowId) return;
    setStarting(true);
    setError("");
    let createdSessionId = null;
    try {
      const result = await request("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ config_id: "75fd9b5a-b982-441c-8349-6194865463c8", prompt: content, workflow_id: selectedWorkflowId }),
      });
      createdSessionId = result.session_id;
      activeSessionRef.current = result.session_id;
      setSessionId(result.session_id);
      setWorkflowId(selectedWorkflowId);
      setMessages(result.messages);
      setWorkflow(result.workflow);
      setSessionPrompt("");
      setLoading(false);
      void refreshLists();
    } catch (failure) {
      if (createdSessionId && activeSessionRef.current === createdSessionId) {
        setError(failure.message);
      } else if (!createdSessionId) {
        setError(failure.message);
      }
    } finally {
      setStarting(false);
      setLoading(false);
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const content = draft.trim();
    const activeId = sessionId;
    if (!content || !sessionId || loading || workflow?.stage !== "agent1") return;

    setDraft("");
    setError("");
    setLoading(true);
    try {
      const result = await request(
        `/api/sessions/${encodeURIComponent(activeId)}/messages`,
        { method: "POST", body: JSON.stringify({ content }) },
      );
      if (activeSessionRef.current === activeId) {
        setMessages(result.messages);
        setWorkflow(result.workflow);
        setError(result.workflow.error || "");
      }
    } catch (failure) {
      if (activeSessionRef.current === activeId) setError(failure.message);
      if (activeSessionRef.current === activeId) {
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
      setLoading(false);
    }
  }

  async function runAction(action, payload = null) {
    if (!sessionId || actionLoading) return;
    setActionLoading(true);
    setError("");
    try {
      const options = { method: "POST" };
      if (payload) {
        options.body = JSON.stringify(payload);
      }
      const result = await request(
        `/api/sessions/${encodeURIComponent(sessionId)}/${action}`,
        options,
      );
      setWorkflow(result);
      setError(result.error || "");
      if (action === "finalize") {
        setShowFinalizationPrompt(false);
        setFinalizationPrompt("");
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
    setMessages([]);
    setWorkflow(null);
    setDraft("");
    setFinalizationPrompt("");
    setShowFinalizationPrompt(false);
    setError("");
    setEditingConfigId(null);
  }

  const stage = workflow?.stage || "agent1";
  const chatVisible = stage === "agent1";
  const reviewVisible = stage === "review";
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
          <span className={stage === "complete" ? "status-dot complete-dot" : ""} />
          {stage === "complete" ? "Workflow complete" : "Workflow in progress"}
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
            <button className="icon-button" aria-label="Create agent config" onClick={() => setEditingConfigId("new")}>+</button>
          </div>
          <div className="config-list">
            {agentConfigs.map((config) => (
              <button
                key={config.id}
                className={`config-row ${editingConfigId === config.id ? "selected" : ""}`}
                onClick={() => setEditingConfigId(config.id)}
              >
                <span className="config-name">{config.name}</span>
              </button>
            ))}
          </div>
          <div className="sidebar-section-heading">
            <span>WORKFLOW CONFIGS</span>
            <button className="icon-button" aria-label="Create workflow config" onClick={() => setEditingWorkflowId("new")}>+</button>
          </div>
          <div className="config-list">
            {workflowConfigs.map((config) => (
              <button
                key={config.id}
                className={`config-row ${editingWorkflowId === config.id ? "selected" : ""}`}
                onClick={() => setEditingWorkflowId(config.id)}
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
              onSave={(saved) => {
                setEditingWorkflowId(null);
                refreshLists();
              }}
            />
          )}
          {!editingConfigId && !sessionId && (
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
          {sessionId && <>
          {chatVisible && (
            <section className="conversation" aria-label="Conversation with Agent 1">
              <div className="conversation-heading">
                <p className="eyebrow">AGENT 1 · ACTIVE</p>
                <h1>What should we work on?</h1>
              </div>
              <div className="message-list" aria-live="polite">
                {messages.map((message, index) => (
                  <article
                    className={`message ${message.role === "user" ? "from-user" : "from-agent"} ${message.status === "failed" ? "failed" : ""}`}
                    key={`${index}-${message.role}`}
                  >
                    {message.role === "assistant" && <span className="message-avatar" aria-hidden="true">✳</span>}
                    <div className="message-content">
                      <p className="speaker">{message.role === "user" ? "You" : "Agent 1"}</p>
                      <p className="message-text">{message.content}</p>
                      {message.status === "failed" && <p className="message-status">Not sent — please try again.</p>}
                    </div>
                  </article>
                ))}
                {sessionProcessing && (
                  <article className="message from-agent" aria-label="Agent 1 is replying">
                    <span className="message-avatar" aria-hidden="true">✳</span>
                    <div className="message-content">
                      <p className="speaker">Agent 1</p>
                      <p className="typing-indicator"><span /><span /><span /></p>
                    </div>
                  </article>
                )}
                <div ref={endOfMessages} />
              </div>
              {error && <p className="alert inline-alert" role="alert">{error}</p>}
              <div className="conversation-actions">
                {!showFinalizationPrompt ? (
                  <>
                    <p>Get a final answer from Agent 1 to review.</p>
                    <button
                      className="secondary-button"
                      onClick={() => setShowFinalizationPrompt(true)}
                      disabled={actionLoading || sessionProcessing}
                    >
                      Request final answer
                    </button>
                  </>
                ) : (
                  <>
                    <div className="finalization-container">
                      <label>Expected result:</label>
                      <p className="finalization-preview">{finalizationPrompt || "No expected result defined"}</p>
                    </div>
                    <div className="prompt-actions">
                      <button
                        className="secondary-button"
                        onClick={() => setShowFinalizationPrompt(false)}
                        disabled={actionLoading}
                      >
                        Cancel
                      </button>
                      <button
                        className="primary-button"
                        onClick={() => runAction("finalize", { prompt: finalizationPrompt })}
                        disabled={actionLoading || !finalizationPrompt.trim()}
                      >
                        {actionLoading ? "Requesting answer…" : "Get answer for review"}
                      </button>
                    </div>
                  </>
                )}
              </div>
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
                  placeholder="Describe what Agent 1 should create…"
                  rows="1"
                  maxLength="20000"
                  disabled={sessionProcessing}
                />
                <button
                  className="send-button"
                  type="submit"
                  aria-label="Send message"
                  disabled={sessionProcessing || !draft.trim()}
                >
                  ↑
                </button>
              </form>
              <p className="composer-hint">Enter to send · Shift + Enter for a new line</p>
            </section>
          )}

          {reviewVisible && (
            <section className="review-panel" aria-label="Review Agent 1 result">
              <div className="conversation-heading">
                <p className="eyebrow">RELEASE · REVIEW REQUIRED</p>
                <h1>Review Agent 1’s result</h1>
                <p className="review-intro">Agent 2 will only start after you approve this exact version.</p>
              </div>
              {error && <p className="alert inline-alert" role="alert">{error}</p>}
              {workflow.artifact_ready ? (
                <article className="file-card">
                  <div className="file-heading">
                    <span className="file-icon" aria-hidden="true">MD</span>
                    <div>
                      <strong>{workflow.artifact_name}</strong>
                      <span>Created by Agent 1</span>
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
                <button className="secondary-button" onClick={() => runAction("edit")} disabled={actionLoading}>
                  Continue discussion
                </button>
                <button className="discard-button" onClick={() => runAction("discard")} disabled={actionLoading}>
                  Discard
                </button>
                <button
                  className="primary-button approve-button"
                  onClick={() => runAction("approve")}
                  disabled={actionLoading || !workflow.artifact_ready}
                >
                  {actionLoading ? "Starting…" : "Approve"}
                  {!actionLoading && <span aria-hidden="true">✓</span>}
                </button>
              </div>
            </section>
          )}

          {stage === "agent2" && (
            <section className="progress-panel" aria-live="polite">
              <div className="progress-mark" aria-hidden="true"><span /><span /><span /></div>
              <p className="eyebrow">AGENT 2 · ACTIVE</p>
              <h1>Processing your approved result</h1>
              <p>Agent 1 is closed. Agent 2 is working from the version you approved.</p>
              {error && <p className="alert" role="alert">{error}</p>}
            </section>
          )}

          {stage === "complete" && (
            <section className="progress-panel terminal-panel">
              <div className="success-mark" aria-hidden="true">✓</div>
              <p className="eyebrow">WORKFLOW COMPLETE</p>
              <h1>Your result is ready</h1>
              <p>Agent 2 has finished processing the approved file.</p>
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
              <WorkflowVisualization workflow={workflow} />
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
      {stage === "complete" && (
        <div className="modal-backdrop" role="presentation">
          <section className="completion-modal" role="dialog" aria-modal="true" aria-labelledby="completion-title">
            <div className="success-mark modal-check" aria-hidden="true">✓</div>
            <p className="eyebrow">WORKFLOW COMPLETE</p>
            <h2 id="completion-title">Your result is ready</h2>
            <div className="output-content">{workflow.output}</div>
            <button className="primary-button" onClick={goHome}>Back to start <span aria-hidden="true">↗</span></button>
          </section>
        </div>
      )}
    </main>
  );
}
