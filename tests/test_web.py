import base64
import json
import unittest

from fastapi.testclient import TestClient

from agent_orchestrator.agent import ChatMessage
from agent_orchestrator.web import create_app
from agent_orchestrator.agent import WorkflowTransitionError


class FakeStore:
    def __init__(self) -> None:
        self.agent_configs = {
            "default": {"id": "default", "name": "Agent 1", "instructions": "Role", "expected_result": ""}
        }
        self.saved_workflow_configs = {}
        self.saved_constant_files = {}

    def agent_config(self, config_id: str) -> object | None:
        return self.agent_configs.get(config_id)

    def save_workflow_config(self, value: dict[str, object]) -> None:
        self.saved_workflow_configs[value["id"]] = value

    def workflow_config(self, config_id: str) -> object | None:
        return self.saved_workflow_configs.get(config_id)

    def constant_files(self, workflow_id: str) -> list[dict[str, object]]:
        return [
            {key: value for key, value in file.items() if key != "content"}
            for file in self.saved_constant_files.values()
            if file["workflow_id"] == workflow_id
        ]

    def constant_file(self, workflow_id: str, file_id: str) -> object | None:
        file = self.saved_constant_files.get(file_id)
        return file if file and file["workflow_id"] == workflow_id else None

    def save_constant_file(self, value: dict[str, object]) -> None:
        self.saved_constant_files[value["id"]] = {
            **value, "byte_size": len(value["content"].encode("utf-8")),
        }

    def delete_constant_file(self, workflow_id: str, file_id: str) -> bool:
        if self.constant_file(workflow_id, file_id) is None:
            return False
        del self.saved_constant_files[file_id]
        return True


class FakeAgent:
    def __init__(self) -> None:
        self.stage = "agent1"
        self.store = FakeStore()
        self.saved_sessions: dict[str, dict[str, object]] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def open_conversation(
        self, session_id: str | None, config_id: str = "default", workflow_id: str | None = None
    ) -> tuple[str, list[ChatMessage]]:
        return session_id or "new-session", [ChatMessage("assistant", "Hello.")]

    def list_agent_configs(self) -> list[dict[str, object]]:
        return list(self.store.agent_configs.values())

    def save_agent_config(self, value: dict[str, object], config_id: str | None = None) -> dict[str, object]:
        saved = {"id": config_id or "created-config", **value}
        self.store.agent_configs[saved["id"]] = saved
        return saved

    def delete_agent_config(self, config_id: str) -> None:
        if config_id not in self.store.agent_configs:
            raise KeyError(config_id)
        if any(session["agent_id"] == config_id and session["status"] != "completed" for session in self.saved_sessions.values()):
            raise WorkflowTransitionError("Active sessions exist.")
        del self.store.agent_configs[config_id]

    def list_sessions(self) -> list[dict[str, object]]:
        return list(self.saved_sessions.values())

    async def delete_session(self, session_id: str) -> None:
        if session_id not in self.saved_sessions:
            raise KeyError(session_id)
        del self.saved_sessions[session_id]

    async def send_message(self, session_id: str, content: str) -> list[ChatMessage]:
        if session_id != "new-session":
            raise KeyError(session_id)
        if self.stage != "agent1":
            raise WorkflowTransitionError("Agent 1 is not active.")
        return [ChatMessage("user", content), ChatMessage("assistant", "A complete answer.")]

    def get_workflow(self, session_id: str) -> dict[str, object]:
        if session_id != "new-session":
            raise KeyError(session_id)
        return {
            "stage": self.stage,
            "artifact_name": "agent-1-result.md" if self.stage == "review" else None,
            "artifact_content": "A complete answer." if self.stage == "review" else None,
            "artifact_ready": self.stage == "review",
            "output": "",
            "error": "",
        }

    async def edit_result(self, session_id: str) -> dict[str, object]:
        self.stage = "agent1"
        return self.get_workflow(session_id)

    async def finalize_result(self, session_id: str) -> dict[str, object]:
        self.stage = "review"
        return self.get_workflow(session_id)

    async def submit_for_review(self, session_id: str, prompt: str) -> dict[str, object]:
        self.stage = "review"
        return self.get_workflow(session_id)

    async def discard_workflow(self, session_id: str) -> dict[str, object]:
        self.stage = "discarded"
        return self.get_workflow(session_id)

    async def approve_result(self, session_id: str) -> dict[str, object]:
        self.stage = "agent2"
        return self.get_workflow(session_id)


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(FakeAgent()))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_start_creates_conversation(self) -> None:
        response = self.client.post("/api/sessions", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "new-session")
        self.assertEqual(response.json()["messages"][0]["role"], "assistant")
        self.assertEqual(response.json()["workflow"]["stage"], "agent1")

    def test_start_screen_is_served(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Copilot Chat", response.text)

    def test_whitespace_message_is_rejected(self) -> None:
        response = self.client.post(
            "/api/sessions/new-session/messages", json={"content": "   "}
        )

        self.assertEqual(response.status_code, 422)

    def test_workflow_config_accepts_json_objects_and_legacy_strings(self) -> None:
        graph = {"nodes": [{"id": "a", "type": "agent"}], "edges": []}
        created = self.client.post("/api/workflow-configs", json={"name": "Graph", "config": graph})
        self.assertEqual(created.status_code, 200)
        saved = created.json()
        self.assertEqual(json.loads(saved["config"]), graph)
        updated = self.client.put(
            f"/api/workflow-configs/{saved['id']}", json={"name": "Graph", "config": '{"nodes":[],"edges":[]}'}
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(json.loads(updated.json()["config"]), {"nodes": [], "edges": []})
        oversized = self.client.post("/api/workflow-configs", json={"name": "Graph", "config": {"data": "x" * 100_000}})
        self.assertEqual(oversized.status_code, 422)

    def test_constant_files_are_validated_persisted_replaceable_and_removable(self) -> None:
        workflow = self.client.post(
            "/api/workflow-configs",
            json={"name": "Files", "config": {"nodes": [], "edges": []}},
        ).json()
        base = f"/api/workflow-configs/{workflow['id']}/constant-files"
        upload = self.client.post(base, json={
            "filename": "notes.md",
            "content_base64": "SGVsbG8gV29ybGQ=",
        })
        self.assertEqual(upload.status_code, 200)
        file = upload.json()
        self.assertEqual(file["content"], "Hello World")
        self.assertEqual(file["byte_size"], 11)
        self.assertNotIn("content", self.client.get(base).json()[0])

        replaced = self.client.put(
            f"{base}/{file['id']}",
            json={"filename": "updated.txt", "content_base64": "bmV3IGNvbnRlbnQ="},
        )
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(replaced.json()["content"], "new content")
        self.assertEqual(self.client.delete(f"{base}/{file['id']}").status_code, 200)
        self.assertEqual(self.client.get(base).json(), [])

    def test_constant_files_reject_unsupported_invalid_and_oversized_content(self) -> None:
        workflow = self.client.post(
            "/api/workflow-configs",
            json={"name": "Files", "config": {"nodes": [], "edges": []}},
        ).json()
        base = f"/api/workflow-configs/{workflow['id']}/constant-files"
        unsupported = self.client.post(
            base, json={"filename": "image.png", "content_base64": "aW1hZ2U="}
        )
        self.assertEqual(unsupported.status_code, 422)
        malformed = self.client.post(
            base, json={"filename": "notes.txt", "content_base64": "not base64"}
        )
        self.assertEqual(malformed.status_code, 422)
        oversized = self.client.post(
            base,
            json={
                "filename": "notes.txt",
                "content_base64": base64.b64encode(b"a" * (1_048_576 + 1)).decode("ascii"),
            },
        )
        self.assertEqual(oversized.status_code, 422)

    def test_unknown_session_is_not_reported_as_success(self) -> None:
        response = self.client.post(
            "/api/sessions/expired/messages", json={"content": "Hello"}
        )

        self.assertEqual(response.status_code, 404)

    def test_release_actions_move_workflow_between_steps(self) -> None:
        start = self.client.post("/api/sessions", json={}).json()
        session_id = start["session_id"]
        response = self.client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "Make a file"}
        )
        self.assertEqual(response.json()["workflow"]["stage"], "agent1")

        finalized = self.client.post(
            f"/api/sessions/{session_id}/finalize",
            json={"prompt": "Please provide a final answer."},
        )
        self.assertEqual(finalized.status_code, 200)
        self.assertEqual(finalized.json()["stage"], "review")

        approved = self.client.post(f"/api/sessions/{session_id}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["stage"], "agent2")

    @property
    def agent(self) -> FakeAgent:
        return self.client.app.state.agent


if __name__ == "__main__":
    unittest.main()
