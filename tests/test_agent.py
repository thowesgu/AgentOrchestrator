import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from copilot.session_events import AssistantMessageData

from agent_orchestrator.agent import (
    AgentUnavailableError,
    CopilotAgentService,
    WorkflowTransitionError,
)
from agent_orchestrator.storage import Store


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.should_fail = False
        self.messages: list[str] = []
        self.disconnected = False

    async def send_and_wait(self, content: str) -> object:
        self.messages.append(content)
        if self.should_fail:
            raise RuntimeError("model request failed")
        return SimpleNamespace(data=AssistantMessageData(content=f"Reply to: {content}", message_id="reply-1"))

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeClient:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.created_messages: list[dict[str, object]] = []
        self.deleted_sessions: list[str] = []
        self.fail_next_session = False
        self.delete_error: Exception | None = None

    async def start(self) -> None:
        return None

    async def create_session(self, **options: object) -> FakeSession:
        session_id = f"session-{len(self.sessions) + 1}"
        session = FakeSession(session_id)
        session.should_fail = self.fail_next_session
        self.fail_next_session = False
        self.sessions[session_id] = session
        self.created_messages.append(options)
        return session

    async def delete_session(self, session_id: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted_sessions.append(session_id)

    async def stop(self) -> None:
        return None

    @property
    def session(self) -> FakeSession:
        return self.sessions["session-1"]


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for service in getattr(self, "services", []):
            await service.stop()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def track(self, service: CopilotAgentService) -> CopilotAgentService:
        if not hasattr(self, "services"):
            self.services: list[CopilotAgentService] = []
            self.temporary_directory = tempfile.TemporaryDirectory()
        service.store = Store(Path(self.temporary_directory.name) / "data.sqlite3")
        service.store.save_agent_config({
            "id": "default", "name": "Agent 1", "instructions": "Role", "expected_result": "",
        })
        service._artifact_directory = Path(self.temporary_directory.name) / "artifacts"
        service._artifact_directory.mkdir(exist_ok=True)
        self.services.append(service)
        return service

    async def test_opens_and_reuses_conversation(self) -> None:
        service = self.track(CopilotAgentService())
        service._client = FakeClient()

        session_id, first_history = await service.open_conversation(None)
        resumed_id, resumed_history = await service.open_conversation(session_id)

        self.assertEqual(session_id, resumed_id)
        self.assertEqual(first_history, resumed_history)
        self.assertEqual(first_history[0].role, "assistant")

    async def test_workflow_graph_is_available_for_live_and_restored_sessions(self) -> None:
        service = self.track(CopilotAgentService())
        service._client = FakeClient()
        graph = {
            "nodes": [
                {"id": "input", "type": "data_source", "position": {"x": 800, "y": 400}},
                {"id": "agent", "type": "agent", "position": {"x": 1200, "y": 400}},
            ],
            "edges": [{"source": "input", "target": "agent", "data": "request"}],
        }
        service.store.save_workflow_config({"id": "workflow-1", "name": "Workflow", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="workflow-1")

        result = service.get_workflow(session_id)
        self.assertEqual(result["nodes"], graph["nodes"])
        self.assertEqual(result["edges"], graph["edges"])
        self.assertEqual(service.store.session(session_id)["workflow_id"], "workflow-1")

        restored = CopilotAgentService()
        restored.store = service.store
        result = restored.get_workflow(session_id)
        self.assertEqual(result["nodes"], graph["nodes"])
        self.assertEqual(result["edges"], graph["edges"])

    async def test_only_connected_constant_files_are_injected_and_recorded(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        graph = {
            "nodes": [
                {"id": "source-for-default", "type": "data_source", "source_type": "constant_file", "file_id": "file-default"},
                {"id": "source-for-other", "type": "data_source", "source_type": "constant_file", "file_id": "file-other"},
                {"id": "default-agent", "type": "agent", "agent_id": "default"},
                {"id": "other-agent", "type": "agent", "agent_id": "other-agent"},
            ],
            "edges": [
                {"source": "source-for-default", "target": "default-agent"},
                {"source": "source-for-other", "target": "other-agent"},
            ],
        }
        service.store.save_workflow_config({
            "id": "workflow-files", "name": "With files", "config": json.dumps(graph),
        })
        service.store.save_constant_file({
            "id": "file-default", "workflow_id": "workflow-files",
            "filename": "shared.md", "content": "Only for the default agent",
        })
        service.store.save_constant_file({
            "id": "file-other", "workflow_id": "workflow-files",
            "filename": "private.md", "content": "Only for another agent",
        })

        session_id, _ = await service.open_conversation(None, workflow_id="workflow-files")
        system_message = client.created_messages[0]["system_message"]["content"]
        accessed_files = service.get_workflow(session_id)["constant_files"]

        self.assertIn("Only for the default agent", system_message)
        self.assertNotIn("Only for another agent", system_message)
        self.assertEqual(accessed_files, [{"id": "file-default", "filename": "shared.md"}])
        self.assertEqual(
            service.store.session(session_id)["constant_files"],
            [{"id": "file-default", "filename": "shared.md"}],
        )

    async def test_connected_missing_constant_file_is_reported_before_session_creation(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        graph = {
            "nodes": [
                {"id": "source", "type": "data_source", "source_type": "constant_file", "file_id": "missing"},
                {"id": "agent", "type": "agent", "agent_id": "default"},
            ],
            "edges": [{"source": "source", "target": "agent"}],
        }
        service.store.save_workflow_config({
            "id": "broken-workflow", "name": "Broken", "config": json.dumps(graph),
        })

        with self.assertRaisesRegex(WorkflowTransitionError, "constant file is missing"):
            await service.open_conversation(None, workflow_id="broken-workflow")
        self.assertEqual(client.created_messages, [])

    async def test_removing_constant_file_clears_graph_reference_and_workflow_delete_cleans_files(self) -> None:
        service = self.track(CopilotAgentService())
        graph = {
            "nodes": [
                {"id": "source", "type": "data_source", "source_type": "constant_file", "file_id": "file-1"},
            ],
            "edges": [],
        }
        service.store.save_workflow_config({
            "id": "workflow-cleanup", "name": "Cleanup", "config": json.dumps(graph),
        })
        service.store.save_constant_file({
            "id": "file-1", "workflow_id": "workflow-cleanup",
            "filename": "notes.txt", "content": "Notes",
        })

        self.assertTrue(service.store.delete_constant_file("workflow-cleanup", "file-1"))
        updated = json.loads(service.store.workflow_config("workflow-cleanup")["config"])
        self.assertNotIn("file_id", updated["nodes"][0])
        self.assertEqual(updated["nodes"][0]["source_type"], "manual")

        service.store.save_constant_file({
            "id": "file-2", "workflow_id": "workflow-cleanup",
            "filename": "more.txt", "content": "More notes",
        })
        service.store.delete_workflow_config("workflow-cleanup")
        self.assertIsNone(service.store.constant_file("workflow-cleanup", "file-2"))

    async def test_unknown_session_starts_new_conversation(self) -> None:
        service = self.track(CopilotAgentService())
        service._client = FakeClient()

        session_id, _ = await service.open_conversation("unknown-session")

        self.assertEqual(session_id, "session-1")

    async def test_startup_error_is_visible_to_caller(self) -> None:
        service = self.track(CopilotAgentService())
        service._startup_error = RuntimeError("runtime unavailable")

        with self.assertRaises(AgentUnavailableError):
            await service.open_conversation(None)

    async def test_message_history_contains_completed_reply(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        history = await service.send_message(session_id, "Hello")

        self.assertEqual([message.role for message in history], ["assistant", "user", "assistant"])
        self.assertEqual(history[-1].content, "Reply to: Hello")
        self.assertEqual(service.get_workflow(session_id)["stage"], "agent1")
        self.assertFalse(service.get_workflow(session_id)["artifact_ready"])

    async def test_discussion_continues_until_latest_answer_is_finalized(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        await service.send_message(session_id, "First request")
        await service.send_message(session_id, "One more thing")
        workflow = service.get_workflow(session_id)

        self.assertEqual(workflow["stage"], "agent1")
        self.assertFalse(workflow["artifact_ready"])
        self.assertEqual(len(client.sessions), 1)

        result = await service.finalize_result(session_id)

        self.assertEqual(result["stage"], "review")
        self.assertEqual(result["artifact_content"], "Reply to: One more thing")

    async def test_submit_for_review_sends_prompt_and_records_answer(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        await service.send_message(session_id, "Let's discuss this")
        workflow = service.get_workflow(session_id)

        self.assertEqual(workflow["stage"], "agent1")
        self.assertFalse(workflow["artifact_ready"])

        result = await service.submit_for_review(session_id, "Please provide your final answer")

        self.assertEqual(result["stage"], "review")
        self.assertEqual(result["artifact_content"], "Reply to: Please provide your final answer")
        self.assertIn("Please provide your final answer", client.session.messages)

    async def test_submit_for_review_cannot_proceed_during_agent_response(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        with self.assertRaises(WorkflowTransitionError):
            await service.submit_for_review(session_id, "Direct submission prompt")

    async def test_failed_message_is_marked_and_not_returned_as_reply(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        client.session.should_fail = True

        with self.assertRaises(AgentUnavailableError):
            await service.send_message(session_id, "Hello")

        self.assertEqual(service._conversations[session_id].messages[-1].status, "failed")
        self.assertEqual(service.get_workflow(session_id)["stage"], "agent1")

    async def test_edit_reuses_agent_one_and_saves_revised_result(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        await service.send_message(session_id, "First request")
        await service.finalize_result(session_id)

        await service.edit_result(session_id)
        history = await service.send_message(session_id, "Please revise it")
        await service.finalize_result(session_id)

        self.assertEqual(history[-1].content, "Reply to: Please revise it")
        self.assertEqual(len(client.sessions), 1)
        self.assertEqual(service.get_workflow(session_id)["stage"], "review")

    async def test_approval_closes_agent_one_and_runs_agent_two_once(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        await service.send_message(session_id, "Create a result")
        await service.finalize_result(session_id)

        first = await service.approve_result(session_id)
        second = await service.approve_result(session_id)
        await asyncio.gather(*service._tasks)

        self.assertEqual(first["stage"], "agent2")
        self.assertIn(second["stage"], {"agent2", "complete"})
        self.assertEqual(len(client.sessions), 2)
        self.assertEqual(client.deleted_sessions.count(session_id), 1)
        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")
        self.assertIn("Reply to: Create a result", client.sessions["session-2"].messages[0])

    async def test_discard_closes_agent_one_without_starting_agent_two(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        await service.send_message(session_id, "Create a result")
        await service.finalize_result(session_id)

        result = await service.discard_workflow(session_id)

        self.assertEqual(result["stage"], "discarded")
        self.assertEqual(client.deleted_sessions, [session_id])
        self.assertEqual(len(client.sessions), 1)

    async def test_failed_result_write_stays_with_agent_one(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        with patch.object(service, "_write_artifact", side_effect=OSError("disk full")):
            await service.send_message(session_id, "Create a result")
            result = await service.finalize_result(session_id)

        workflow = service.get_workflow(session_id)
        self.assertEqual(workflow["stage"], "agent1")
        self.assertFalse(workflow["artifact_ready"])
        self.assertIn("could not be saved", workflow["error"])
        self.assertEqual(result["stage"], "agent1")

    async def test_missing_result_cannot_be_approved(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        await service.send_message(session_id, "Create a result")
        await service.finalize_result(session_id)
        service._workflows[session_id].artifact_path.unlink()

        result = await service.approve_result(session_id)

        self.assertEqual(result["stage"], "review")
        self.assertFalse(result["artifact_ready"])
        self.assertEqual(len(client.sessions), 1)
        self.assertEqual(client.deleted_sessions, [])

    async def test_agent_two_failure_does_not_complete_workflow(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)
        await service.send_message(session_id, "Create a result")
        await service.finalize_result(session_id)
        client.fail_next_session = True

        await service.approve_result(session_id)
        await asyncio.gather(*service._tasks)

        workflow = service.get_workflow(session_id)
        self.assertEqual(workflow["stage"], "agent2_error")
        self.assertFalse(workflow["output"])
        self.assertTrue(workflow["error"])

    async def test_destroy_shutdown_error_falls_back_to_session_disconnect(self) -> None:
        client = FakeClient()
        client.delete_error = ValueError("'destroy' is not a valid ShutdownType")
        service = self.track(CopilotAgentService())
        service._client = client
        session_id, _ = await service.open_conversation(None)

        await service._delete_session(session_id, client.session)

        self.assertTrue(client.session.disconnected)
        self.assertEqual(client.deleted_sessions, [])

    async def test_sessions_persist_and_agent_deletion_requires_terminal_sessions(self) -> None:
        service = self.track(CopilotAgentService())
        client = FakeClient()
        service._client = client
        session_id, _ = await service.open_conversation(None)

        saved = service.list_sessions()[0]
        self.assertEqual(saved["agent_id"], "default")
        self.assertEqual(saved["status"], "waiting")
        persisted = Store(service.store.path)
        self.assertEqual(persisted.sessions()[0]["id"], session_id)
        with self.assertRaises(WorkflowTransitionError):
            service.delete_agent("default")

        service._workflows[session_id].stage = "complete"
        service._statuses[session_id] = "completed"
        service._persist(session_id)
        service.delete_agent("default")
        self.assertEqual(service.list_agents(), [])

    async def test_deleting_active_session_cancels_its_model_request(self) -> None:
        service = self.track(CopilotAgentService())
        client = FakeClient()
        service._client = client
        session_id, _ = await service.open_conversation(None)
        second_id, _ = await service.open_conversation(None)
        entered = asyncio.Event()

        async def blocked_send(content: str) -> object:
            entered.set()
            await asyncio.Event().wait()

        client.session.send_and_wait = blocked_send
        request = asyncio.create_task(service.send_message(session_id, "Long request"))
        await entered.wait()
        await service.send_message(second_id, "Independent request")

        statuses = {record["id"]: record["status"] for record in service.list_sessions()}
        self.assertEqual(statuses[session_id], "running")
        self.assertEqual(statuses[second_id], "waiting")

        await service.delete_session(session_id)
        await asyncio.gather(request, return_exceptions=True)

        remaining_ids = {record["id"] for record in service.list_sessions()}
        self.assertNotIn(session_id, remaining_ids)
        self.assertIn(second_id, remaining_ids)
        self.assertEqual(client.deleted_sessions, [session_id])
        self.assertTrue(request.cancelled())


if __name__ == "__main__":
    unittest.main()
