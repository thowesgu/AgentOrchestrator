import { useEffect, useRef, useState } from "react";

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
  const [agents, setAgents] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [options, setOptions] = useState({ tools: [], permissions: [] });
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [sessionPrompt, setSessionPrompt] = useState("");
  const [agentForm, setAgentForm] = useState(null);
  const [agentError, setAgentError] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [workflow, setWorkflow] = useState(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState("");
  const endOfMessages = useRef(null);
  const activeSessionRef = useRef(null);
  const sessionIsRunning = sessions.some((saved) => saved.id === sessionId && saved.status === "running");
  const sessionProcessing = sessionIsRunning || (loading && activeSessionRef.current === sessionId);
  const isTerminal = workflow?.stage === "complete"
    || workflow?.stage === "discarded"
    || workflow?.stage === "agent2_error";

  async function refreshLists() {
    try {
      const [savedAgents, savedSessions] = await Promise.all([
        request("/api/agents"), request("/api/sessions"),
      ]);
      setAgents(savedAgents);
      setSessions(savedSessions);
      setSelectedAgentId((current) => current || savedAgents[0]?.id || "");
    } catch (failure) {
      setAgentError(failure.message);
    }
  }

  useEffect(() => {
    request("/api/options").then(setOptions).catch((failure) => setAgentError(failure.message));
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
    setMessages(saved.messages || []);
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

  async function saveAgent(event) {
    event.preventDefault();
    setAgentError("");
    const editing = agentForm?.id;
    try {
      const result = await request(editing ? `/api/agents/${encodeURIComponent(editing)}` : "/api/agents", {
        method: editing ? "PUT" : "POST",
        body: JSON.stringify({
          name: agentForm.name, role: agentForm.role,
          tools: agentForm.tools, permissions: agentForm.permissions,
        }),
      });
      setAgentForm(null);
      setSelectedAgentId(result.id);
      await refreshLists();
    } catch (failure) {
      setAgentError(failure.message);
    }
  }

  async function removeAgent(agent) {
    try {
      await request(`/api/agents/${encodeURIComponent(agent.id)}`, { method: "DELETE" });
      if (selectedAgentId === agent.id) setSelectedAgentId("");
      await refreshLists();
    } catch (failure) {
      setAgentError(failure.message);
    }
  }

  async function startChat(event) {
    event?.preventDefault();
    const content = sessionPrompt.trim();
    if (!content || !selectedAgentId) return;
    setStarting(true);
    setError("");
    let createdSessionId = null;
    try {
      const result = await request("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ agent_id: selectedAgentId }),
      });
      createdSessionId = result.session_id;
      activeSessionRef.current = result.session_id;
      setSessionId(result.session_id);
      setMessages(result.messages);
      setWorkflow(result.workflow);
      setSessionPrompt("");
      setLoading(true);
      setMessages([{ role: "user", content, status: "sending" }]);
      void refreshLists();
      const reply = await request(
        `/api/sessions/${encodeURIComponent(result.session_id)}/messages`,
        { method: "POST", body: JSON.stringify({ content }) },
      );
      if (activeSessionRef.current === result.session_id) {
        setMessages(reply.messages);
        setWorkflow(reply.workflow);
        setError(reply.workflow.error || "");
      }
      await refreshLists();
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

  async function runAction(action) {
    if (!sessionId || actionLoading) return;
    setActionLoading(true);
    setError("");
    try {
      const result = await request(
        `/api/sessions/${encodeURIComponent(sessionId)}/${action}`,
        { method: "POST" },
      );
      setWorkflow(result);
      setError(result.error || "");
    } catch (failure) {
      setError(failure.message);
    } finally {
      setActionLoading(false);
    }
  }

  function goHome() {
    activeSessionRef.current = null;
    setSessionId(null);
    setMessages([]);
    setWorkflow(null);
    setDraft("");
    setError("");
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
            <span>AGENTS</span>
            <button className="icon-button" aria-label="Create agent" onClick={() => setAgentForm({ name: "", role: "", tools: [], permissions: [] })}>+</button>
          </div>
          <div className="agent-list">
            {agents.map((agent) => (
              <div className={`agent-row ${selectedAgentId === agent.id ? "selected" : ""}`} key={agent.id}>
                <button className="agent-select" onClick={() => { setSelectedAgentId(agent.id); goHome(); }}>
                  <span className="agent-avatar">✳</span><span>{agent.name}</span>
                </button>
                <button className="icon-button subtle" aria-label={`Edit ${agent.name}`} onClick={() => setAgentForm({ ...agent })}>⋯</button>
              </div>
            ))}
          </div>
          {agentForm && (
            <form className="agent-editor" onSubmit={saveAgent}>
              <label>Agent name<input required maxLength="100" value={agentForm.name} onChange={(event) => setAgentForm({ ...agentForm, name: event.target.value })} /></label>
              <label>Role description<textarea required rows="3" value={agentForm.role} onChange={(event) => setAgentForm({ ...agentForm, role: event.target.value })} /></label>
              <fieldset><legend>Allowed tools</legend>{options.tools.map((item) => <label className="option-row" key={item}><input type="checkbox" checked={agentForm.tools.includes(item)} onChange={(event) => setAgentForm({ ...agentForm, tools: event.target.checked ? [...agentForm.tools, item] : agentForm.tools.filter((value) => value !== item) })} />{item}</label>)}</fieldset>
              <fieldset><legend>Permissions</legend>{options.permissions.map((item) => <label className="option-row" key={item}><input type="checkbox" checked={agentForm.permissions.includes(item)} onChange={(event) => setAgentForm({ ...agentForm, permissions: event.target.checked ? [...agentForm.permissions, item] : agentForm.permissions.filter((value) => value !== item) })} />{item}</label>)}</fieldset>
              <div className="editor-actions"><button type="button" className="secondary-button" onClick={() => setAgentForm(null)}>Cancel</button><button type="submit" className="primary-button">Save</button></div>
              {agentForm.id && <button type="button" className="discard-button" onClick={() => removeAgent(agentForm)}>Delete agent</button>}
            </form>
          )}
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
          {agentError && <p className="alert sidebar-alert" role="alert">{agentError}</p>}
        </aside>
        <section className="work-area" aria-label="Active workflow step">
          {!sessionId && (
            <section className="new-session-panel">
              <div className="brand-mark" aria-hidden="true">✳</div>
              <p className="eyebrow">COPILOT WORKFLOW</p>
              <h1>A little help<br />goes a long way.</h1>
              <p className="intro">Choose an agent and tell it what you would like to work on.</p>
              {error && <p className="alert" role="alert">{error}</p>}
              <form className="new-session-form" onSubmit={startChat}>
                <label htmlFor="agent-picker">Selected agent</label>
                <select id="agent-picker" value={selectedAgentId} onChange={(event) => setSelectedAgentId(event.target.value)} required>
                  {agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}
                </select>
                <label htmlFor="session-prompt">Your request</label>
                <textarea id="session-prompt" value={sessionPrompt} onChange={(event) => setSessionPrompt(event.target.value)} maxLength="20000" rows="5" placeholder="Describe what you would like the agent to do…" required />
                <button className="primary-button" type="submit" disabled={starting || !sessionPrompt.trim() || !selectedAgentId}>
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
                <p>When you are done discussing, use the latest answer as the result.</p>
                <button
                  className="secondary-button"
                  onClick={() => runAction("finalize")}
                  disabled={actionLoading || sessionProcessing || !canUseLastAnswer}
                >
                  {actionLoading ? "Saving result…" : "Use last answer"}
                </button>
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

        <aside className="workflow-sidebar" aria-label="Workflow progress">
          <p className="eyebrow">WORKFLOW</p>
          <h2>Your progress</h2>
          <ol className="step-list">
            {WORKFLOW_STEPS.map((label, index) => {
              const status = stepStatus(stage, index);
              return (
                <li className={`workflow-step ${status === "active" ? "is-active" : ""} ${status === "done" ? "is-done" : ""} ${status === "failed" || status === "discarded" ? "is-ended" : ""}`} key={label}>
                  <span className="step-indicator" aria-hidden="true">
                    {status === "done" ? "✓" : status === "discarded" ? "×" : index + 1}
                  </span>
                  <span className="step-copy">
                    <strong>{label}</strong>
                    <span>{status === "active" && stage === "review" ? "Waiting for your decision" : status === "active" && stage === "agent2" ? "Processing approved file" : status === "active" ? "In progress" : status}</span>
                  </span>
                </li>
              );
            })}
          </ol>
          {stage === "complete" && (
            <button className="sidebar-home" onClick={goHome}>Start a new workflow <span aria-hidden="true">↗</span></button>
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
