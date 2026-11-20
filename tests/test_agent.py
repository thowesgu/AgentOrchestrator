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

    async def test_parallel_interactive_branches_are_independent_and_join(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        for agent_id, mode in (("left", "interactive"), ("right", "interactive"), ("join", "autonomous")):
            service.store.save_agent_config({
                "id": agent_id, "name": agent_id, "instructions": f"Instructions {agent_id}",
                "expected_result": f"Result {agent_id}", "interaction_mode": mode,
            })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                *[{"id": name, "type": "agent", "agent_id": name} for name in ("left", "right", "join")],
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "first", "target": "left"},
                {"source": "first", "target": "right"},
                {"source": "left", "target": "join"},
                {"source": "right", "target": "join"},
            ],
        }
        service.store.save_workflow_config({"id": "parallel", "name": "Parallel", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="parallel")
        await service.send_message(session_id, "Build a plan", "first")
        await service.submit_for_review(session_id, "First result", "first")
        await asyncio.gather(*service._tasks)

        left = service.get_workflow(session_id, "left")
        right = service.get_workflow(session_id, "right")
        self.assertEqual(set(left["active_node_ids"]), {"left", "right"})
        self.assertEqual(left["agent_status"], "waiting")
        self.assertEqual({item["node_id"] for item in left["agent_conversations"]}, {"left", "right"})
        self.assertEqual(right["expected_result"], "Result right")
        await service.send_message(session_id, "Left only", "left")
        self.assertEqual(service.get_workflow(session_id, "left")["messages"][-2]["content"], "Left only")
        self.assertNotIn("Left only", str(service.get_workflow(session_id, "right")["messages"]))
        restored = CopilotAgentService()
        restored.store = service.store
        self.assertEqual(restored.get_workflow(session_id, "left")["agent_status"], "unavailable")
        self.assertIn("Left only", str(restored.get_workflow(session_id, "left")["messages"]))
        self.assertNotIn("Left only", str(restored.get_workflow(session_id, "right")["messages"]))

        await service.submit_for_review(session_id, "ignored", "left")
        self.assertEqual(service.get_workflow(session_id, "right")["stage"], "agent2")
        self.assertEqual(set(service.get_workflow(session_id)["active_node_ids"]), {"right"})
        self.assertNotIn("join", service._workflows[session_id].node_statuses)
        await service.submit_for_review(session_id, "ignored", "right")
        await asyncio.gather(*service._tasks)

        result = service.get_workflow(session_id)
        self.assertEqual(result["stage"], "complete")
        join_instructions = client.created_messages[-1]["system_message"]["content"]
        self.assertIn("Reply to: Result left", join_instructions)
        self.assertIn("Reply to: Result right", join_instructions)

    async def test_delete_parallel_session_cancels_and_cleans_every_branch(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "other", "name": "Other", "instructions": "Other role",
            "expected_result": "Other result", "interaction_mode": "interactive",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                {"id": "other", "type": "agent", "agent_id": "other"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "start", "target": "other"},
            ],
        }
        service.store.save_workflow_config({"id": "cleanup", "name": "Cleanup", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="cleanup")
        await service.send_message(session_id, "Request", "first")
        await asyncio.gather(*service._tasks)
        entered = asyncio.Event()

        async def blocked_send(content: str) -> object:
            entered.set()
            await asyncio.Event().wait()

        client.sessions["session-2"].send_and_wait = blocked_send
        request = asyncio.create_task(service.send_message(session_id, "Wait", "other"))
        await entered.wait()
        self.assertEqual(service.get_workflow(session_id, "first")["agent_status"], "waiting")
        self.assertEqual(service.get_workflow(session_id, "other")["agent_status"], "running")
        await service.delete_session(session_id)
        self.assertTrue(request.cancelled())
        self.assertEqual(set(client.deleted_sessions), {"session-1", "session-2"})
        self.assertFalse(service._conversations)
        self.assertIsNone(service.store.session(session_id))

    async def test_start_fanout_runs_both_interactive_agents(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "other", "name": "Other", "instructions": "Other role",
            "expected_result": "Other result", "interaction_mode": "interactive",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                {"id": "other", "type": "agent", "agent_id": "other"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "start", "target": "other"},
            ],
        }
        service.store.save_workflow_config({"id": "fanout", "name": "Fanout", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="fanout")
        await service.send_message(session_id, "Shared request", "first")
        await asyncio.gather(*service._tasks)
        self.assertEqual(set(service.get_workflow(session_id)["active_node_ids"]), {"first", "other"})
        self.assertNotIn("Shared request", client.sessions["session-2"].messages[0])
        self.assertNotIn("Shared request", client.created_messages[1]["system_message"]["content"])
        await service.submit_for_review(session_id, "First result", "first")
        self.assertEqual(service.get_workflow(session_id)["stage"], "agent2")
        await service.submit_for_review(session_id, "Other result", "other")
        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")

    async def test_start_fanout_waits_for_additional_agent_inputs(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "other", "name": "Other", "instructions": "Other role",
            "expected_result": "Other result", "interaction_mode": "interactive",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "dependent", "type": "agent", "agent_id": "other"},
                {"id": "first", "type": "agent", "agent_id": "default"},
            ],
            "edges": [
                {"source": "start", "target": "dependent"},
                {"source": "start", "target": "first"},
                {"source": "first", "target": "dependent"},
            ],
        }
        service.store.save_workflow_config({"id": "dependency", "name": "Dependency", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="dependency")
        self.assertEqual(service.get_workflow(session_id)["active_node_id"], "first")
        await service.send_message(session_id, "Request", "first")
        self.assertEqual(len(client.sessions), 1)
        await service.submit_for_review(session_id, "First result", "first")
        await asyncio.gather(*service._tasks)
        self.assertEqual(service.get_workflow(session_id)["active_node_id"], "dependent")
        self.assertIn("Reply to: First result", client.created_messages[-1]["system_message"]["content"])

    async def test_review_after_later_interactive_agent_supports_edit_and_approval(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "second", "name": "Second", "instructions": "Second role",
            "expected_result": "Second result", "interaction_mode": "interactive",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                {"id": "second", "type": "agent", "agent_id": "second"},
                {"id": "review", "type": "review"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "first", "target": "second"},
                {"source": "second", "target": "review"},
            ],
        }
        service.store.save_workflow_config({"id": "review-later", "name": "Review", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="review-later")
        await service.send_message(session_id, "Request", "first")
        await service.submit_for_review(session_id, "First result", "first")
        await asyncio.gather(*service._tasks)
        await service.submit_for_review(session_id, "Second result", "second")
        self.assertEqual(service.get_workflow(session_id)["stage"], "review")
        await service.edit_result(session_id)
        self.assertEqual(service.get_workflow(session_id)["active_node_ids"], ["second"])
        await service.send_message(session_id, "Revise", "second")
        await service.submit_for_review(session_id, "Second result", "second")
        result = await service.approve_result(session_id)
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["output"], "Reply to: Second result")

    async def test_parallel_review_waits_for_other_branch_and_then_unblocks_join(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        for name in ("left", "right", "join"):
            service.store.save_agent_config({
                "id": name, "name": name, "instructions": name,
                "expected_result": f"{name} result", "interaction_mode": "interactive",
            })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                *[{"id": name, "type": "agent", "agent_id": name} for name in ("left", "right", "join")],
                {"id": "review", "type": "review"},
            ],
            "edges": [
                {"source": "start", "target": "left"},
                {"source": "start", "target": "right"},
                {"source": "left", "target": "review"},
                {"source": "review", "target": "join"},
                {"source": "right", "target": "join"},
            ],
        }
        service.store.save_workflow_config({"id": "review-parallel", "name": "Review", "config": json.dumps(graph)})
        session_id, _ = await service.open_conversation(None, workflow_id="review-parallel")
        await service.send_message(session_id, "Request", "left")
        await asyncio.gather(*service._tasks)
        await service.submit_for_review(session_id, "left result", "left")
        self.assertEqual(service.get_workflow(session_id)["stage"], "agent2")
        await service.submit_for_review(session_id, "right result", "right")
        self.assertEqual(service.get_workflow(session_id)["stage"], "review")
        self.assertEqual(service.get_workflow(session_id)["artifact_content"], "Reply to: left result")
        await service.approve_result(session_id)
        await asyncio.gather(*service._tasks)
        join = service.get_workflow(session_id, "join")
        self.assertEqual(join["agent_status"], "waiting")
        instructions = client.created_messages[-1]["system_message"]["content"]
        self.assertIn("Reply to: left result", instructions)
        self.assertIn("Reply to: right result", instructions)

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

    async def test_interactive_finalization_uses_configured_result_prompt(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "default",
            "name": "Agent 1",
            "instructions": "Role",
            "expected_result": "Return a concise project brief.",
            "interaction_mode": "interactive",
        })
        session_id, _ = await service.open_conversation(None)
        self.assertIn(
            "Interactive mode",
            client.created_messages[0]["system_message"]["content"],
        )

        await service.send_message(session_id, "Help me plan a project")
        discussion = service.get_workflow(session_id)
        self.assertEqual(discussion["stage"], "agent1")
        self.assertFalse(discussion["artifact_ready"])

        result = await service.submit_for_review(session_id, "Ignore the saved prompt")
        self.assertEqual(result["stage"], "review")
        self.assertEqual(
            client.session.messages[-1], "Return a concise project brief."
        )
        history = service._conversations[session_id].messages
        self.assertEqual(history[-2].role, "instruction")
        self.assertEqual(history[-2].content, "Return a concise project brief.")

    async def test_interactive_result_goes_directly_to_next_configured_agent(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "next-agent",
            "name": "Next agent",
            "instructions": "Turn the brief into an action plan.",
            "expected_result": "Return three actions.",
            "interaction_mode": "autonomous",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                {"id": "next", "type": "agent", "agent_id": "next-agent"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "first", "target": "next"},
            ],
        }
        service.store.save_workflow_config({
            "id": "direct", "name": "Direct", "config": json.dumps(graph),
        })
        session_id, _ = await service.open_conversation(
            None, workflow_id="direct"
        )

        await service.send_message(session_id, "Create a brief")
        result = await service.submit_for_review(session_id, "Return a brief.")
        await asyncio.gather(*service._tasks)

        self.assertEqual(result["stage"], "agent2")
        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")
        self.assertTrue(client.created_messages[1]["system_message"]["content"].startswith(
            "Turn the brief into an action plan."
        ))
        self.assertNotIn("Create a brief", client.sessions["session-2"].messages[0])
        self.assertIn("Return three actions.", client.sessions["session-2"].messages[0])
        self.assertIn(
            "Reply to: Return a brief.",
            client.created_messages[1]["system_message"]["content"],
        )

    async def test_linear_workflow_passes_all_connected_inputs_to_each_agent(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "next-agent",
            "name": "Next agent",
            "instructions": "Use the connected information.",
            "expected_result": "Return a summary.",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default", "label": "Research"},
                {"id": "second", "type": "agent", "agent_id": "next-agent", "label": "Summarize"},
                {"id": "source-a", "type": "data_source", "source_type": "manual", "label": "Brief", "output": "Project brief"},
                {"id": "source-b", "type": "data_source", "source_type": "manual", "label": "Constraints", "output": "Under budget"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "first", "target": "second"},
                {"source": "source-a", "target": "second"},
                {"source": "source-b", "target": "second"},
            ],
        }
        service.store.save_workflow_config({
            "id": "linear", "name": "Linear", "config": json.dumps(graph),
        })
        session_id, _ = await service.open_conversation(None, workflow_id="linear")

        await service.send_message(session_id, "Create a summary")
        result = await service.submit_for_review(session_id, "Return the research.")
        self.assertEqual(result["stage"], "agent2")
        await asyncio.gather(*service._tasks)

        active = service.get_workflow(session_id)
        next_agent_instructions = client.created_messages[1]["system_message"]["content"]
        self.assertEqual(active["active_node_id"], "second")
        self.assertEqual(active["active_agent_name"], "Next agent")
        self.assertIn("Project brief", next_agent_instructions)
        self.assertIn("Under budget", next_agent_instructions)
        self.assertIn("Reply to: Return the research.", next_agent_instructions)
        self.assertEqual(active["messages"][-1]["role"], "assistant")

        await service.finalize_result(session_id)
        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")

    async def test_downstream_agents_receive_original_request_only_from_connected_chat(self) -> None:
        for mode in ("interactive", "autonomous"):
            for connect_chat in (False, True):
                with self.subTest(mode=mode, connect_chat=connect_chat):
                    client = FakeClient()
                    service = self.track(CopilotAgentService())
                    service._client = client
                    for agent_id, interaction_mode in (("second", "interactive"), ("third", mode)):
                        service.store.save_agent_config({
                            "id": agent_id,
                            "name": agent_id,
                            "instructions": f"Instructions for {agent_id}",
                            "expected_result": f"Result for {agent_id}" if agent_id == "third" else "",
                            "interaction_mode": interaction_mode,
                        })
                    graph = {
                        "nodes": [
                            {"id": "start", "type": "start"},
                            {"id": "first", "type": "agent", "agent_id": "default"},
                            {"id": "second", "type": "agent", "agent_id": "second"},
                            {"id": "third", "type": "agent", "agent_id": "third"},
                            {"id": "chat", "type": "data_source", "source_type": "chat"},
                            {"id": "reference", "type": "data_source", "source_type": "manual", "output": "Selected reference"},
                            {"id": "unconnected", "type": "data_source", "source_type": "manual", "output": "Unselected reference"},
                        ],
                        "edges": [
                            {"source": "start", "target": "first"},
                            {"source": "first", "target": "second"},
                            {"source": "second", "target": "third"},
                            {"source": "reference", "target": "third"},
                            *([{"source": "chat", "target": "third"}] if connect_chat else []),
                        ],
                    }
                    service.store.save_workflow_config({
                        "id": "three-agents", "name": "Three agents", "config": json.dumps(graph),
                    })
                    session_id, _ = await service.open_conversation(None, workflow_id="three-agents")
                    await service.send_message(session_id, "Original private request")
                    await service.submit_for_review(session_id, "First result")
                    await asyncio.gather(*service._tasks)
                    await service.submit_for_review(session_id, "Second result")
                    await asyncio.gather(*service._tasks)

                    second_system = client.created_messages[1]["system_message"]["content"]
                    second_prompt = client.sessions["session-2"].messages[0]
                    third_system = client.created_messages[2]["system_message"]["content"]
                    third_prompt = client.sessions["session-3"].messages[0]
                    self.assertNotIn("Original private request", second_system + second_prompt)
                    self.assertIn("Reply to: Second result", third_system)
                    self.assertIn("Selected reference", third_system)
                    self.assertNotIn("Unselected reference", third_system + third_prompt)
                    self.assertNotIn("First result", third_system + third_prompt)
                    self.assertNotIn("Original private request", third_prompt)
                    if connect_chat:
                        self.assertIn("Original private request", third_system)
                    else:
                        self.assertNotIn("Original private request", third_system)
                    if mode == "autonomous":
                        self.assertIn("Result for third", third_prompt)

    async def test_interactive_result_waits_for_configured_review(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "next-agent",
            "name": "Next agent",
            "instructions": "Create an action plan.",
            "expected_result": "Return an action plan.",
            "interaction_mode": "autonomous",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "agent", "agent_id": "default"},
                {"id": "review", "type": "review"},
                {"id": "next", "type": "agent", "agent_id": "next-agent"},
            ],
            "edges": [
                {"source": "start", "target": "first"},
                {"source": "first", "target": "review"},
                {"source": "review", "target": "next"},
            ],
        }
        service.store.save_workflow_config({
            "id": "reviewed", "name": "Reviewed", "config": json.dumps(graph),
        })
        session_id, _ = await service.open_conversation(
            None, workflow_id="reviewed"
        )

        await service.send_message(session_id, "Create a brief")
        result = await service.submit_for_review(session_id, "Return a brief.")
        self.assertEqual(result["stage"], "review")

        await service.approve_result(session_id)
        await asyncio.gather(*service._tasks)

        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")
        self.assertTrue(client.created_messages[1]["system_message"]["content"].startswith(
            "Create an action plan."
        ))
        self.assertIn(
            "Reply to: Return a brief.",
            client.created_messages[1]["system_message"]["content"],
        )

    async def test_autonomous_agent_completes_from_initial_request(self) -> None:
        client = FakeClient()
        service = self.track(CopilotAgentService())
        service._client = client
        service.store.save_agent_config({
            "id": "default",
            "name": "Agent 1",
            "instructions": "Role",
            "expected_result": "Return a concise answer.",
            "interaction_mode": "autonomous",
        })
        graph = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "agent", "type": "agent", "agent_id": "default"},
            ],
            "edges": [{"source": "start", "target": "agent"}],
        }
        service.store.save_workflow_config({
            "id": "single", "name": "Single", "config": json.dumps(graph),
        })
        session_id, _ = await service.open_conversation(
            None, workflow_id="single"
        )

        await service.send_message(session_id, "Summarize this project")

        self.assertIn("Return a concise answer.", client.session.messages[0])
        self.assertEqual(service.get_workflow(session_id)["stage"], "complete")
        self.assertEqual(
            service.get_workflow(session_id)["output"],
            f"Reply to: {client.session.messages[0]}",
        )

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
