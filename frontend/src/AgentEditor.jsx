import { useState, useEffect } from "react";
import "./AgentEditor.css";

export function AgentEditor({ configId, onClose, onSave }) {
  const [form, setForm] = useState({
    name: "",
    instructions: "",
    expected_result: "",
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
          <label>Expected Result (optional)</label>
          <textarea
            value={form.expected_result}
            onChange={(e) => setForm({ ...form, expected_result: e.target.value })}
            placeholder="Describe what the agent should output"
            rows={5}
            disabled={loading}
          />
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
