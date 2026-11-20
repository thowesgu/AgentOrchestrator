import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"
EDGE = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
BROWSER = shutil.which("chromium") or shutil.which("google-chrome") or (str(EDGE) if EDGE.exists() else None)

SCRIPT = r"""
<script>
function waitFor(predicate) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const timer = setInterval(() => {
      try {
        const result = predicate();
        if (result) { clearInterval(timer); resolve(result); }
        else if (++attempts > 100) { clearInterval(timer); reject(new Error("Timed out")); }
      } catch (error) { clearInterval(timer); reject(error); }
    }, 30);
  });
}
function setValue(element, value) {
  const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
}
(async () => {
  try {
    await waitFor(() => document.body.innerText.includes("Saved Agent") && document.body.innerText.includes("Saved Workflow"));
    document.querySelector('[aria-label="Create agent config"]').click();
    const editor = await waitFor(() => document.querySelector(".agent-editor"));
    setValue(editor.querySelector("input"), "New Agent");
    setValue(editor.querySelector("textarea"), "New instructions");
    editor.querySelector(".save-btn").click();
    await waitFor(() => document.body.innerText.includes("New Agent"));
    document.querySelector(".session-select").click();
    const node = await waitFor(() => document.querySelector('.react-flow__node[data-id="right"]'));
    if (getComputedStyle(node).borderColor !== "rgb(201, 75, 75)") {
      throw new Error("Waiting interactive branch is not highlighted red");
    }
    node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await waitFor(() => document.querySelector(".message-list")?.innerText.includes("Right branch reply"));
    setValue(document.querySelector("#message"), "Right message");
    document.querySelector(".composer").requestSubmit();
    await waitFor(() => document.querySelector(".message-list")?.innerText.includes("Right message"));
    const release = await waitFor(() => [...document.querySelectorAll("button")]
      .find((button) => button.textContent.includes("provided enough context") && !button.disabled));
    release.click();
    await waitFor(() => document.body.innerText.includes("Workflow complete"));
    document.body.dataset.uiTest = "passed";
  } catch (error) {
    document.body.dataset.uiTest = `failed: ${error.message}`;
  }
})();
</script>
"""


@unittest.skipUnless(BROWSER and (DIST / "index.html").exists(), "Built frontend and Chromium or Edge required")
class FrontendBrowserTests(unittest.TestCase):
    def test_lists_creation_and_selected_branch_interaction(self) -> None:
        state = {"agents": [{"id": "saved", "name": "Saved Agent"}], "requests": []}
        nodes = [
            {"id": "left", "type": "agent", "label": "Left", "position": {"x": 0, "y": 0}},
            {"id": "right", "type": "agent", "label": "Right", "position": {"x": 250, "y": 0}},
        ]

        def workflow(node_id: str = "left", stage: str = "agent2") -> dict:
            return {
                "stage": stage, "status": "waiting", "agent_status": "waiting",
                "active_node_id": "left", "active_node_ids": ["left", "right"],
                "active_agent_name": node_id.title(), "node_statuses": {"left": "active", "right": "active"},
                "interaction_mode": "interactive", "expected_result": f"{node_id} result",
                "nodes": nodes, "edges": [],
                "agent_conversations": [
                    {"node_id": name, "interaction_mode": "interactive", "status": "waiting"}
                    for name in ("left", "right")
                ],
                "messages": [
                    {"role": "assistant", "content": f"{node_id.title()} branch reply"},
                ],
            }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def respond_json(self, value: object) -> None:
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path == "/api/sessions":
                    self.respond_json([{"id": "session", "input": "Saved session", "agent_name": "Left", "status": "waiting", "stage": "agent2"}])
                elif url.path == "/api/agent-configs":
                    self.respond_json(state["agents"])
                elif url.path == "/api/workflow-configs":
                    self.respond_json([{"id": "workflow", "name": "Saved Workflow"}])
                elif url.path == "/api/sessions/session/workflow":
                    self.respond_json(workflow(parse_qs(url.query).get("node_id", ["left"])[0]))
                elif url.path == "/" or url.path.startswith("/assets/"):
                    path = DIST / ("index.html" if url.path == "/" else url.path.lstrip("/"))
                    body = path.read_bytes()
                    if url.path == "/":
                        body = body.replace(b"</body>", SCRIPT.encode() + b"</body>")
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["requests"].append((self.path, payload))
                if self.path == "/api/agent-configs":
                    state["agents"].append({"id": "created", "name": payload["name"]})
                    self.respond_json(state["agents"][-1])
                elif self.path.endswith("/messages"):
                    result = workflow(payload.get("node_id", "left"))
                    result["messages"].append({"role": "user", "content": payload["content"]})
                    self.respond_json({"messages": result["messages"], "workflow": result})
                elif self.path.endswith("/finalize"):
                    self.respond_json(workflow(payload.get("node_id", "left"), "complete"))
                else:
                    self.send_error(404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as profile:
                result = subprocess.run(
                    [BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                     f"--user-data-dir={profile}", "--virtual-time-budget=10000", "--dump-dom",
                     f"http://127.0.0.1:{server.server_port}/"],
                    capture_output=True, text=True, timeout=30,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('data-ui-test="passed"', result.stdout)
            self.assertIn(("/api/sessions/session/messages", {"content": "Right message", "node_id": "right"}), state["requests"])
            self.assertIn(("/api/sessions/session/finalize", {"prompt": "right result", "node_id": "right"}), state["requests"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
