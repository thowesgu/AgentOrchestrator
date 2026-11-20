import { useEffect, useState } from "react";
import "./MCPServerManager.css";

const EMPTY_FORM = {
  name: "",
  transport: "http",
  url: "",
  command: "",
  args: "[]",
  env: "{}",
  auth_type: "none",
  bearer_token: "",
  headers: "{}",
};
const ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v2/mcp";

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "The request failed. Please try again.");
  return payload;
}

function parseObject(value, label) {
  let parsed;
  try {
    parsed = JSON.parse(value || "{}");
  } catch {
    throw new Error(`${label} must be valid JSON.`);
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label} must be a JSON object.`);
  }
  return parsed;
}

function statusLabel(status) {
  return ({
    unknown: "Unknown",
    connected: "Connected",
    disconnected: "Disconnected",
    authenticated: "Authenticated",
    required: "Authentication required",
    not_required: "Not required",
  })[status] || status;
}

function detailsToForm(server) {
  const isHttp = server.transport === "http";
  return {
    name: server.name,
    transport: server.transport,
    url: isHttp ? server.url : "",
    command: isHttp ? "" : server.command,
    args: JSON.stringify(isHttp ? [] : server.args, null, 2),
    env: JSON.stringify(Object.fromEntries((server.env_names || []).map((name) => [name, ""])), null, 2),
    auth_type: isHttp ? server.auth_type : "none",
    bearer_token: "",
    headers: JSON.stringify(Object.fromEntries(
      (server.auth_header_names || []).map((name) => [name, ""]),
    ), null, 2),
  };
}

export function MCPServerManager({ serverId, servers, onClose, onSaved, onDeleted }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reauthenticating, setReauthenticating] = useState(false);
  const server = servers.find((item) => item.id === serverId);
  const isEditing = serverId !== "new";

  useEffect(() => {
    setError("");
    setNotice("");
    setReauthenticating(false);
    if (!isEditing) {
      setForm(EMPTY_FORM);
      return;
    }
    let cancelled = false;
    setLoading(true);
    request(`/api/mcp-servers/${encodeURIComponent(serverId)}`)
      .then((saved) => {
        if (!cancelled) setForm(detailsToForm(saved));
      })
      .catch((failure) => {
        if (!cancelled) setError(failure.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [serverId, isEditing]);

  function setField(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payloadForSave() {
    const payload = { name: form.name, transport: form.transport };
    if (form.transport === "http") {
      payload.url = form.url;
      payload.auth_type = form.auth_type;
      if (form.auth_type === "bearer") payload.bearer_token = form.bearer_token;
      if (form.auth_type === "headers") payload.headers = parseObject(form.headers, "Authentication headers");
    } else {
      let args;
      try {
        args = JSON.parse(form.args || "[]");
      } catch {
        throw new Error("Arguments must be a valid JSON array.");
      }
      if (!Array.isArray(args) || args.some((argument) => typeof argument !== "string")) {
        throw new Error("Arguments must be a JSON array of strings.");
      }
      payload.command = form.command;
      payload.args = args;
      payload.env = parseObject(form.env, "Environment variables");
      payload.replace_env = true;
    }
    return payload;
  }

  async function save(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const method = isEditing ? "PUT" : "POST";
      const path = isEditing
        ? `/api/mcp-servers/${encodeURIComponent(serverId)}`
        : "/api/mcp-servers";
      const saved = await request(path, {
        method,
        body: JSON.stringify(payloadForSave()),
      });
      setNotice("Server configuration saved. No agent has been granted access.");
      if (isEditing) {
        await onSaved(saved.id);
      } else {
        setForm(EMPTY_FORM);
        await onSaved(null);
      }
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function reconnect() {
    if (server?.auth_type === "atlassian_oauth") {
      await authorizeAtlassian(false);
      return;
    }
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const result = await request(
        `/api/mcp-servers/${encodeURIComponent(serverId)}/reconnect`,
        { method: "POST" },
      );
      setNotice(result.status_message || "The MCP connection is ready.");
      await onSaved(serverId);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function authorizeAtlassian(reauthenticate) {
    const popup = window.open("", "_blank");
    if (!popup) {
      setError("Allow pop-ups to authorize Atlassian.");
      return;
    }
    popup.opener = null;
    setLoading(true);
    setError("");
    setNotice("Connecting to Atlassian...");
    try {
      const path = `/api/mcp-servers/${encodeURIComponent(serverId)}/atlassian-oauth`;
      let result = await request(`${path}?reauthenticate=${reauthenticate}`, { method: "POST" });
      if (result.authorization_url) {
        popup.location.replace(result.authorization_url);
        setNotice("Complete authorization in the Atlassian window.");
        const deadline = Date.now() + 190000;
        let closeHandled = false;
        do {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          if (popup.closed && !closeHandled) {
            closeHandled = true;
            const cancelled = await request(`${path}/cancel`, { method: "POST" });
            if (cancelled.cancelled) throw new Error("Atlassian authorization was cancelled.");
          }
          result = await request(path);
        } while (result.status === "pending" && Date.now() < deadline);
      } else {
        popup.close();
      }
      if (result.status !== "connected") {
        throw new Error(result.message || "Authorization timed out. Try again.");
      }
      setNotice(result.message);
      await onSaved(serverId);
    } catch (failure) {
      popup.close();
      setError(failure.message);
      await onSaved(serverId);
    } finally {
      setLoading(false);
    }
  }

  async function reauthenticate(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const payload = { auth_type: form.auth_type };
      if (form.auth_type === "bearer") payload.bearer_token = form.bearer_token;
      if (form.auth_type === "headers") payload.headers = parseObject(form.headers, "Authentication headers");
      const result = await request(
        `/api/mcp-servers/${encodeURIComponent(serverId)}/reauthenticate`,
        { method: "POST", body: JSON.stringify(payload) },
      );
      setForm((current) => ({ ...current, bearer_token: "" }));
      setNotice(result.status_message || "Authentication updated.");
      await onSaved(serverId);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Delete MCP server "${form.name}"?`)) return;
    setLoading(true);
    setError("");
    try {
      await request(`/api/mcp-servers/${encodeURIComponent(serverId)}`, { method: "DELETE" });
      onDeleted();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="mcp-manager">
      <header className="mcp-manager-header">
        <div>
          <p className="eyebrow">MCP SERVERS</p>
          <h1>{isEditing ? (server?.name || "MCP server") : "Add MCP server"}</h1>
        </div>
        <button className="secondary-button" type="button" onClick={onClose}>Close</button>
      </header>
      {error && <div className="alert mcp-alert" role="alert">{error}</div>}
      {notice && <div className="mcp-notice" role="status">{notice}</div>}

      {isEditing && server && (
        <div className="mcp-status-card">
          <div className="mcp-status-grid">
            <div><span>Connection</span><strong className={`mcp-state state-${server.connection_status}`}>{statusLabel(server.connection_status)}</strong></div>
            <div><span>Authentication</span><strong className={`mcp-state state-${server.authentication_status}`}>{statusLabel(server.authentication_status)}</strong></div>
          </div>
          {server.status_message && <p className="mcp-status-message">{server.status_message}</p>}
          {server.checked_at && <p className="mcp-checked-at">Last checked {new Date(`${server.checked_at}Z`).toLocaleString()}</p>}
          <div className="mcp-actions">
            <button type="button" className="secondary-button" onClick={reconnect} disabled={loading}>
              {loading ? "Checking..." : "Reconnect"}
            </button>
            {server.auth_type === "atlassian_oauth" ? (
              <button type="button" className="secondary-button" onClick={() => authorizeAtlassian(true)} disabled={loading}>
                Reauthorize Atlassian
              </button>
            ) : server.transport === "http" && (
              <button
                type="button"
                className="secondary-button"
                onClick={() => {
                  setError("");
                  setNotice("");
                  setField("auth_type", server.auth_type);
                  setReauthenticating((value) => !value);
                }}
                disabled={loading}
              >
                {reauthenticating ? "Cancel reauthentication" : "Reauthenticate"}
              </button>
            )}
          </div>
        </div>
      )}

      {reauthenticating && (
        <form className="mcp-form mcp-reauth-form" onSubmit={reauthenticate}>
          <h2>Update authentication</h2>
          <p className="mcp-help">Saved credentials are hidden. Leave a value blank to keep that credential.</p>
          <AuthFields form={form} setField={setField} disabled={loading} isEditing={isEditing} allowAtlassian={false} />
          <button type="submit" className="primary-button" disabled={loading}>
            {loading ? "Checking..." : "Save and verify"}
          </button>
        </form>
      )}

      <form className="mcp-form" onSubmit={save}>
        <h2>Server configuration</h2>
        <label>
          Server name
          <input value={form.name} onChange={(event) => setField("name", event.target.value)} required maxLength={200} disabled={loading} />
        </label>
        <label>
          Transport
          <select value={form.transport} onChange={(event) => setField("transport", event.target.value)} disabled={loading}>
            <option value="http">Remote HTTP</option>
            <option value="stdio">Local process (stdio)</option>
          </select>
        </label>
        {form.transport === "http" ? (
          <>
            <label>
              MCP server URL
              <input type="url" value={form.url} onChange={(event) => setField("url", event.target.value)} placeholder="https://example.com/mcp" required disabled={loading || form.auth_type === "atlassian_oauth"} />
            </label>
            <AuthFields form={form} setField={setField} disabled={loading} isEditing={isEditing} />
            <p className="mcp-help">
              Local and private network URLs are blocked unless AGENT_ORCHESTRATOR_MCP_ALLOW_PRIVATE_NETWORKS=1 is set on the server.
            </p>
          </>
        ) : (
          <>
            <label>
              Command
              <input value={form.command} onChange={(event) => setField("command", event.target.value)} placeholder="node" required disabled={loading} />
            </label>
            <label>
              Arguments (JSON array)
              <textarea rows={3} value={form.args} onChange={(event) => setField("args", event.target.value)} disabled={loading} />
            </label>
            <label>
              Environment variables (JSON object)
              <textarea rows={4} value={form.env} onChange={(event) => setField("env", event.target.value)} disabled={loading} />
              {isEditing && <small>Existing variable names are shown; saved values are hidden. Blank values keep existing values.</small>}
            </label>
            <p className="mcp-help">
              For safety, checking a local process requires AGENT_ORCHESTRATOR_ALLOW_MCP_STDIO=1 on the server.
            </p>
          </>
        )}
        {form.transport === "http" && isEditing && (
          <p className="mcp-help">Saved credentials are hidden. Leave the token or existing header values blank to keep them.</p>
        )}
        <p className="mcp-help">Saving a server does not grant access to any agent.</p>
        <div className="mcp-form-actions">
          {isEditing && <button type="button" className="discard-button" onClick={remove} disabled={loading}>Delete server</button>}
          <button type="submit" className="primary-button" disabled={loading}>
            {loading ? "Saving..." : "Save configuration"}
          </button>
        </div>
      </form>
    </section>
  );
}

function AuthFields({ form, setField, disabled, isEditing, allowAtlassian = true }) {
  return (
    <>
      <label>
        Authentication
        <select value={form.auth_type} onChange={(event) => {
          setField("auth_type", event.target.value);
          if (event.target.value === "atlassian_oauth") setField("url", ATLASSIAN_MCP_URL);
        }} disabled={disabled}>
          <option value="none">No authentication</option>
          <option value="bearer">Bearer token</option>
          <option value="headers">Custom headers</option>
          {allowAtlassian && <option value="atlassian_oauth">Atlassian OAuth 2.1 (local only)</option>}
        </select>
      </label>
      {form.auth_type === "atlassian_oauth" && (
        <p className="mcp-help">Save the server, then reconnect to authorize in your browser. Atlassian may request broad read and write scopes; review its consent screen carefully. Authorization is kept in memory until the app restarts.</p>
      )}
      {form.auth_type === "bearer" && (
        <label>
          Bearer token
          <input
            type="password"
            autoComplete="new-password"
            value={form.bearer_token}
            onChange={(event) => setField("bearer_token", event.target.value)}
            placeholder={isEditing ? "Leave blank to keep the saved token" : "Bearer token"}
            disabled={disabled}
          />
        </label>
      )}
      {form.auth_type === "headers" && (
        <label>
          Authentication headers (JSON object)
          <textarea
            rows={3}
            value={form.headers}
            onChange={(event) => setField("headers", event.target.value)}
            disabled={disabled}
          />
        </label>
      )}
    </>
  );
}
