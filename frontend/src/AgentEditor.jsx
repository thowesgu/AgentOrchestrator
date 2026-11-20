import { useState, useEffect } from "react";
import "./AgentEditor.css";

export function AgentEditor({ configId, onClose, onSave }) {
  const [form, setForm] = useState({
    name: "",
    instructions: "",
    expected_result: "",
    interaction_mode: "interactive",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!configId) return;
    setLoading(true);
    fetch(`/api/agent-configs/${encodeURIComponent(configId)}`)
      .then((r) => r.json())
      .then((data) => {
        setForm(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [configId]);

  async function handleSave() {
    if (!form.name.trim() || !form.instructions.trim()) {
      setError("Name and instructions are required.");
      return;
    }
    if (form.interaction_mode === "autonomous" && !form.expected_result.trim()) {
      setError("An expected result is required for autonomous agents.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const method = configId ? "PUT" : "POST";
      const url = configId
        ? `/api/agent-configs/${encodeURIComponent(configId)}`
        : "/api/agent-configs";
      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "Save failed");
      }
      const saved = await response.json();
      onSave?.(saved);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="agent-editor">
      <div className="editor-header">
        <h2>{configId ? "Edit Agent" : "New Agent"}</h2>
        <button onClick={onClose} className="close-btn">✕</button>
      </div>
      {error && <div className="error-message">{error}</div>}
      <div className="editor-form">
        <div className="form-group">
          <label>Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Agent name"
            disabled={loading}
          />
        </div>
        <div className="form-group">
          <label>Instructions</label>
          <textarea
            value={form.instructions}
            onChange={(e) => setForm({ ...form, instructions: e.target.value })}
            placeholder="Agent instructions and prompt"
            rows={10}
            disabled={loading}
          />
        </div>
        <div className="form-group">
          <label htmlFor="interaction-mode">Interaction mode</label>
          <select
            id="interaction-mode"
            value={form.interaction_mode || "interactive"}
            onChange={(e) => setForm({ ...form, interaction_mode: e.target.value })}
            disabled={loading}
          >
            <option value="interactive">Interactive</option>
            <option value="autonomous">Autonomous</option>
          </select>
          <p className="field-description">
            Interactive agents discuss the task with you until you indicate they have enough context.
            Autonomous agents produce the configured result immediately from your request.
          </p>
        </div>
        <div className="form-group">
          <label htmlFor="expected-result">
            Expected Result{form.interaction_mode === "autonomous" ? "" : " (optional)"}
          </label>
          <textarea
            id="expected-result"
            value={form.expected_result}
            onChange={(e) => setForm({ ...form, expected_result: e.target.value })}
            placeholder="Describe what the agent should output"
            rows={5}
            disabled={loading}
            required={form.interaction_mode === "autonomous"}
          />
          {form.interaction_mode === "autonomous" && (
            <p className="field-description">Required for autonomous agents; this instruction is sent with the initial task.</p>
          )}
        </div>
        <div className="editor-actions">
          <button onClick={handleSave} disabled={loading} className="save-btn">
            {loading ? "Saving..." : "Save"}
          </button>
          <button onClick={onClose} disabled={loading} className="cancel-btn">
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
