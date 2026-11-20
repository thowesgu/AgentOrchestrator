import base64
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_orchestrator.agent import ChatMessage
from agent_orchestrator.web import create_app
from agent_orchestrator.agent import WorkflowTransitionError
from agent_orchestrator.mcp_servers import public_server


VALID_WORKFLOW_GRAPH = {
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "agent", "type": "agent"},
    ],
    "edges": [{"source": "start", "target": "agent"}],
}


class FakeStore:
    def __init__(self) -> None:
        self.agent_configs = {
            "default": {"id": "default", "name": "Agent 1", "instructions": "Role", "expected_result": ""}
        }
        self.saved_workflow_configs = {}
        self.saved_constant_files = {}
        self.saved_mcp_servers = {}

    def agent_config(self, config_id: str) -> object | None:
        return self.agent_configs.get(config_id)

    def agent_usage(self, _config_id: str) -> dict[str, object]:
        return {
            "status": "unavailable",
            "credits": {"status": "unavailable", "value": None},
            "tokens": {"status": "unavailable", "value": None},
        }

    def save_agent_config(self, value: dict[str, object]) -> None:
        self.agent_configs[value["id"]] = value

    def save_workflow_config(self, value: dict[str, object]) -> None:
        self.saved_workflow_configs[value["id"]] = {
            **self.saved_workflow_configs.get(value["id"], {}), **value,
        }

    def workflow_config(self, config_id: str) -> object | None:
        return self.saved_workflow_configs.get(config_id)

    def workflow_usage(self, _config_id: str) -> dict[str, object]:
        return {
            "status": "unavailable",
            "credits": {"status": "unavailable", "value": None},
            "tokens": {"status": "unavailable", "value": None},
        }

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

    def save_mcp_server(self, value: dict[str, object]) -> None:
        existing = self.saved_mcp_servers.get(value["id"])
        if any(
            server["name"] == value["name"] and server["id"] != value["id"]
            for server in self.saved_mcp_servers.values()
        ):
            raise sqlite3.IntegrityError("duplicate server name")
        self.saved_mcp_servers[value["id"]] = {
            **(existing or {}),
            **value,
            "connection_status": "unknown",
            "authentication_status": "unknown",
            "status_message": "",
            "checked_at": None,
            "updated_at": "now",
        }

    def mcp_server_config(self, server_id: str) -> dict[str, object] | None:
        return self.saved_mcp_servers.get(server_id)

    def mcp_server(self, server_id: str) -> dict[str, object] | None:
        value = self.mcp_server_config(server_id)
        return public_server(value) if value else None

    def mcp_servers(self) -> list[dict[str, object]]:
        return [public_server(server) for server in self.saved_mcp_servers.values()]

    def update_mcp_server_status(
        self, server_id: str, connection_status: str, authentication_status: str,
        status_message: str,
    ) -> None:
        self.saved_mcp_servers[server_id].update({
            "connection_status": connection_status,
            "authentication_status": authentication_status,
            "status_message": status_message,
            "checked_at": "now",
        })

    def update_mcp_server_config(self, server_id: str, config: dict[str, object]) -> None:
        self.saved_mcp_servers[server_id].update({
            "config": config,
            "connection_status": "unknown",
            "authentication_status": "unknown",
            "status_message": "",
            "checked_at": None,
        })

    def delete_mcp_server(self, server_id: str) -> bool:
        return self.saved_mcp_servers.pop(server_id, None) is not None


class FakeAgent:
    def __init__(self) -> None:
        self.stage = "agent1"
        self.store = FakeStore()
        self.saved_sessions: dict[str, dict[str, object]] = {}
        self.last_node_id = None
        self.session_credit_limit: float | None = None
        self.copilot_quota: dict[str, object] = {
            "status": "unavailable",
            "message": "GitHub Copilot quota is currently unavailable.",
            "quotas": [],
        }

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get_copilot_quota(self) -> dict[str, object]:
        return self.copilot_quota

    async def open_conversation(
        self, session_id: str | None, config_id: str = "default", workflow_id: str | None = None
    ) -> tuple[str, list[ChatMessage]]:
        return session_id or "new-session", [ChatMessage("assistant", "Hello.")]

    def list_agent_configs(self) -> list[dict[str, object]]:
        return list(self.store.agent_configs.values())

    def list_agents(self) -> list[dict[str, object]]:
        return [
            {**config, "usage": self.store.agent_usage(config["id"])}
            for config in self.list_agent_configs()
        ]

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

    async def send_message(self, session_id: str, content: str, node_id: str | None = None) -> list[ChatMessage]:
        self.last_node_id = node_id
        if session_id != "new-session":
            raise KeyError(session_id)
        if self.stage != "agent1":
            raise WorkflowTransitionError("Agent 1 is not active.")
        return [ChatMessage("user", content), ChatMessage("assistant", "A complete answer.")]

    def get_workflow(self, session_id: str, node_id: str | None = None) -> dict[str, object]:
        if node_id:
            self.last_node_id = node_id
        if session_id != "new-session":
            raise KeyError(session_id)
        return {
            "stage": self.stage,
            "artifact_name": "agent-1-result.md" if self.stage == "review" else None,
            "artifact_content": "A complete answer." if self.stage == "review" else None,
            "artifact_ready": self.stage == "review",
            "output": "",
            "error": "",
            "session_credit_limit": self.session_credit_limit,
        }

    def set_session_credit_limit(self, session_id: str, credit_limit: float | None) -> None:
        if session_id != "new-session":
            raise KeyError(session_id)
        self.session_credit_limit = credit_limit

    async def edit_result(self, session_id: str) -> dict[str, object]:
        self.stage = "agent1"
        return self.get_workflow(session_id)

    async def finalize_result(self, session_id: str, node_id: str | None = None) -> dict[str, object]:
        self.last_node_id = node_id
        self.stage = "review"
        return self.get_workflow(session_id)

    async def submit_for_review(self, session_id: str, prompt: str, node_id: str | None = None) -> dict[str, object]:
        self.last_node_id = node_id
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
        self.fake_agent = FakeAgent()
        self.client = TestClient(create_app(self.fake_agent))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_start_creates_conversation(self) -> None:
        response = self.client.post("/api/sessions", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "new-session")
        self.assertEqual(response.json()["messages"][0]["role"], "assistant")
        self.assertEqual(response.json()["workflow"]["stage"], "agent1")

    def test_copilot_quota_endpoint_preserves_available_and_unavailable_states(self) -> None:
        unavailable = self.client.get("/api/copilot/quota")
        self.assertEqual(unavailable.status_code, 200)
        self.assertEqual(unavailable.json()["status"], "unavailable")
        self.assertEqual(unavailable.json()["quotas"], [])

        self.fake_agent.copilot_quota = {
            "status": "available",
            "account": "copilot-user",
            "scope": "Current GitHub Copilot account",
            "quotas": [{
                "id": "premium_interactions",
                "label": "Premium interactions",
                "unit": "premium interactions",
                "used_requests": 10,
                "entitlement_requests": 10,
                "remaining_requests": 0,
                "remaining_percentage": 0,
                "unlimited": False,
                "reset_date": None,
            }],
        }
        available = self.client.get("/api/copilot/quota")
        self.assertEqual(available.status_code, 200)
        self.assertEqual(available.json()["status"], "available")
        self.assertEqual(available.json()["quotas"][0]["remaining_requests"], 0)

    def test_selected_branch_is_forwarded_to_message_workflow_and_finalize(self) -> None:
        agent = self.client.app.state.agent
        response = self.client.get("/api/sessions/new-session/workflow?node_id=branch-b")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.last_node_id, "branch-b")
        agent.stage = "agent1"
        response = self.client.post(
            "/api/sessions/new-session/finalize-last",
            json={"node_id": "branch-b"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.last_node_id, "branch-b")
        agent.stage = "agent1"
        response = self.client.post(
            "/api/sessions/new-session/messages",
            json={"content": "Hello", "node_id": "branch-b"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.last_node_id, "branch-b")
        response = self.client.post(
            "/api/sessions/new-session/finalize",
            json={"prompt": "Produce result", "node_id": "branch-b"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(agent.last_node_id, "branch-b")

    def test_start_screen_is_served(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Copilot Chat", response.text)

    def test_agent_config_saves_interaction_mode_and_result_prompt(self) -> None:
        response = self.client.post("/api/agent-configs", json={
            "name": "Interactive",
            "instructions": "Discuss the task.",
            "expected_result": "Return a project brief.",
            "interaction_mode": "interactive",
            "credit_limit": 4,
        })

        self.assertEqual(response.status_code, 200)
        config_id = response.json()["id"]
        self.assertEqual(response.json()["interaction_mode"], "interactive")
        self.assertEqual(response.json()["expected_result"], "Return a project brief.")
        self.assertEqual(response.json()["credit_limit"], 4)

        updated = self.client.put(f"/api/agent-configs/{config_id}", json={
            "name": "Autonomous",
            "instructions": "Complete the task.",
            "expected_result": "Return a summary.",
            "interaction_mode": "autonomous",
            "credit_limit": 6,
        })
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["interaction_mode"], "autonomous")
        self.assertEqual(updated.json()["credit_limit"], 6)

    def test_session_credit_limit_can_be_saved_and_validated(self) -> None:
        response = self.client.put(
            "/api/sessions/new-session/limit", json={"credit_limit": 3.5}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_credit_limit"], 3.5)

        invalid = self.client.put(
            "/api/sessions/new-session/limit", json={"credit_limit": -1}
        )
        self.assertEqual(invalid.status_code, 422)

    def test_mcp_server_configuration_is_saved_without_exposing_credentials(self) -> None:
        created = self.client.post("/api/mcp-servers", json={
            "name": "Private tools",
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "auth_type": "bearer",
            "bearer_token": "do-not-return-this",
        })

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["connection_status"], "unknown")
        self.assertTrue(created.json()["has_credentials"])
        self.assertNotIn("do-not-return-this", created.text)
        server_id = created.json()["id"]
        details = self.client.get(f"/api/mcp-servers/{server_id}")
        self.assertEqual(details.status_code, 200)
        self.assertEqual(details.json()["auth_type"], "bearer")
        self.assertNotIn("do-not-return-this", details.text)
        self.assertEqual(self.client.get("/api/mcp-servers").json()[0]["id"], server_id)

    def test_mcp_reauthentication_saves_credentials_and_reports_check_result(self) -> None:
        server_id = self.client.post("/api/mcp-servers", json={
            "name": "Private tools",
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "auth_type": "bearer",
            "bearer_token": "old-secret",
        }).json()["id"]

        async def successful_check(_config: dict[str, object]) -> tuple[str, str, str]:
            return "connected", "authenticated", ""

        with patch("agent_orchestrator.web.check_server", successful_check):
            response = self.client.post(
                f"/api/mcp-servers/{server_id}/reauthenticate",
                json={"auth_type": "bearer", "bearer_token": "new-secret"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["connection_status"], "connected")
        self.assertEqual(response.json()["authentication_status"], "authenticated")
        self.assertNotIn("new-secret", response.text)
        self.assertEqual(
            self.agent.store.mcp_server_config(server_id)["config"]["auth"]["token"],
            "new-secret",
        )

    def test_mcp_server_rejects_embedded_url_credentials_and_bad_transports(self) -> None:
        invalid = [
            {"name": "Bad URL", "transport": "http", "url": "https://user:pass@example.test/mcp"},
            {"name": "Bad process", "transport": "stdio", "command": ""},
            {
                "name": "Bad header", "transport": "http", "url": "https://example.test/mcp",
                "auth_type": "headers", "headers": {"X-Token": "line one\r\nInjected: true"},
            },
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/mcp-servers", json=payload).status_code, 422)

    def test_mcp_server_update_preserves_hidden_credentials_when_left_blank(self) -> None:
        server_id = self.client.post("/api/mcp-servers", json={
            "name": "Private tools",
            "transport": "http",
            "url": "https://mcp.example.test/mcp",
            "auth_type": "bearer",
            "bearer_token": "preserved-token",
        }).json()["id"]

        updated = self.client.put(f"/api/mcp-servers/{server_id}", json={
            "name": "Renamed tools",
            "transport": "http",
            "url": "https://mcp.example.test/v2",
            "auth_type": "bearer",
            "bearer_token": "",
        })

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "Renamed tools")
        self.assertNotIn("preserved-token", updated.text)
        self.assertEqual(
            self.agent.store.mcp_server_config(server_id)["config"]["auth"]["token"],
            "preserved-token",
        )

    def test_autonomous_agent_requires_expected_result(self) -> None:
        response = self.client.post("/api/agent-configs", json={
            "name": "Autonomous",
            "instructions": "Complete the task.",
            "interaction_mode": "autonomous",
        })

        self.assertEqual(response.status_code, 422)

    def test_whitespace_message_is_rejected(self) -> None:
        response = self.client.post(
            "/api/sessions/new-session/messages", json={"content": "   "}
        )

        self.assertEqual(response.status_code, 422)

    def test_workflow_config_accepts_json_objects_and_legacy_strings(self) -> None:
        graph = VALID_WORKFLOW_GRAPH
        created = self.client.post("/api/workflow-configs", json={"name": "Graph", "config": graph, "credit_limit": 3.5})
        self.assertEqual(created.status_code, 200)
        saved = created.json()
        self.assertEqual(json.loads(saved["config"]), graph)
        updated = self.client.put(
            f"/api/workflow-configs/{saved['id']}",
            json={"name": "Graph", "config": json.dumps(graph)},
        )
        self.assertEqual(updated.status_code, 200)
        reopened = self.client.get(f"/api/workflow-configs/{saved['id']}")
        self.assertEqual(json.loads(reopened.json()["config"]), graph)
        self.assertEqual(reopened.json()["credit_limit"], 3.5)
        self.assertEqual(reopened.json()["usage"]["credits"]["status"], "unavailable")
        cleared = self.client.put(
            f"/api/workflow-configs/{saved['id']}",
            json={"name": "Graph", "config": graph, "credit_limit": None},
        )
        self.assertIsNone(cleared.json()["credit_limit"])
        invalid = self.client.post("/api/workflow-configs", json={"name": "Invalid", "config": graph, "credit_limit": -1})
        self.assertEqual(invalid.status_code, 422)
        oversized = self.client.post("/api/workflow-configs", json={"name": "Graph", "config": {"data": "x" * 100_000}})
        self.assertEqual(oversized.status_code, 422)

    def test_workflow_config_rejects_incomplete_or_cyclic_graphs(self) -> None:
        invalid_graphs = [
            {"nodes": [{"id": "agent", "type": "agent"}], "edges": []},
            {
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "agent", "type": "agent"},
                    {"id": "review", "type": "review"},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            },
            {
                "nodes": [{"id": "start", "type": "start"}, {"id": "agent", "type": "agent"}],
                "edges": [
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "start"},
                ],
            },
            {
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "source", "type": "data_source"},
                    {"id": "agent", "type": "agent"},
                ],
                "edges": [
                    {"source": "start", "target": "source"},
                    {"source": "source", "target": "agent"},
                ],
            },
            {
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "agent-1", "type": "agent"},
                    {"id": "agent-2", "type": "agent"},
                    {"id": "review", "type": "review"},
                ],
                "edges": [
                    {"source": "start", "target": "agent-1"},
                    {"source": "start", "target": "agent-2"},
                    {"source": "agent-1", "target": "review"},
                    {"source": "agent-2", "target": "review"},
                ],
            },
            {
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "source", "type": "data_source"},
                    {"id": "review", "type": "review"},
                ],
                "edges": [
                    {"source": "start", "target": "source"},
                    {"source": "source", "target": "review"},
                ],
            },
        ]
        for graph in invalid_graphs:
            with self.subTest(graph=graph):
                response = self.client.post(
                    "/api/workflow-configs", json={"name": "Invalid", "config": graph}
                )
                self.assertEqual(response.status_code, 422)

    def test_workflow_config_accepts_data_source_as_independent_root(self) -> None:
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "source", "type": "data_source"},
                {"id": "agent", "type": "agent"},
            ],
            "edges": [
                {"source": "start", "target": "agent"},
                {"source": "source", "target": "agent"},
            ],
        }
        response = self.client.post(
            "/api/workflow-configs", json={"name": "With source", "config": graph}
        )
        self.assertEqual(response.status_code, 200)

    def test_constant_files_are_validated_persisted_replaceable_and_removable(self) -> None:
        workflow = self.client.post(
            "/api/workflow-configs",
            json={"name": "Files", "config": VALID_WORKFLOW_GRAPH},
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
            json={"name": "Files", "config": VALID_WORKFLOW_GRAPH},
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
