"""FastAPI application and static React frontend."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from agent_orchestrator.agent import (
    AgentUnavailableError,
    CopilotAgentService,
    WorkflowTransitionError,
)
from agent_orchestrator.storage import (
    MAX_CONSTANT_FILE_BYTES,
    SUPPORTED_CONSTANT_FILE_EXTENSIONS,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
WORKFLOW_NODE_TYPES = frozenset({"start", "agent", "data_source", "review"})


def _validate_workflow_config(config: str) -> None:
    try:
        graph = json.loads(config)
    except json.JSONDecodeError as error:
        raise ValueError("Workflow config must be valid JSON.") from error
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
    ):
        raise ValueError("Workflow config must contain nodes and edges.")

    nodes = graph["nodes"]
    edges = graph["edges"]
    ids: set[str] = set()
    starts: list[str] = []
    node_types: dict[str, str] = {}
    for node in nodes:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not node["id"]
            or node["id"] in ids
            or not isinstance(node.get("type"), str)
            or node["type"] not in WORKFLOW_NODE_TYPES
        ):
            raise ValueError("Workflow contains an unsupported node or duplicate node ID.")
        ids.add(node["id"])
        node_types[node["id"]] = node["type"]
        if node["type"] == "start":
            starts.append(node["id"])

    if len(starts) != 1:
        raise ValueError("A workflow must contain exactly one Start node.")

    adjacency = {node_id: [] for node_id in ids}
    incoming = dict.fromkeys(ids, 0)
    incoming_sources: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        if (
            not isinstance(edge, dict)
            or not isinstance(edge.get("source"), str)
            or not isinstance(edge.get("target"), str)
            or edge.get("source") not in ids
            or edge.get("target") not in ids
        ):
            raise ValueError("Every connection must join two existing nodes.")
        if node_types[edge["target"]] in {"start", "data_source"}:
            raise ValueError("Start and data source nodes cannot have incoming connections.")
        adjacency[edge["source"]].append(edge["target"])
        incoming[edge["target"]] += 1
        incoming_sources[edge["target"]].append(edge["source"])

    for node in nodes:
        if node["type"] != "review":
            continue
        sources = incoming_sources[node["id"]]
        if len(sources) != 1:
            raise ValueError("Every review node must have exactly one incoming connection.")
        if node_types[sources[0]] != "agent":
            raise ValueError("A review node must receive its input from an agent.")

    start_id = starts[0]
    if incoming[start_id]:
        raise ValueError("The Start node cannot have an incoming connection.")
    if not adjacency[start_id]:
        raise ValueError("Connect the Start node to at least one step.")

    data_source_ids = {
        node["id"] for node in nodes if node["type"] == "data_source"
    }
    reachable = {start_id, *data_source_ids}
    pending = list(reachable)
    while pending:
        for target in adjacency[pending.pop()]:
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    if reachable != ids:
        raise ValueError("Connect every node to Start; all nodes must be reachable.")

    remaining_incoming = dict(incoming)
    ready = [node_id for node_id, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        source = ready.pop()
        visited += 1
        for target in adjacency[source]:
            remaining_incoming[target] -= 1
            if remaining_incoming[target] == 0:
                ready.append(target)
    if visited != len(ids):
        raise ValueError("Workflow connections cannot contain a cycle.")


class StartSessionRequest(BaseModel):
    session_id: str | None = None
    config_id: str = "default"
    workflow_id: str | None = None
    prompt: Annotated[str, Field(default="", max_length=20_000)]


class SendMessageRequest(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=20_000)]
    node_id: str | None = None


class SubmitForReviewRequest(BaseModel):
    prompt: Annotated[str, Field(min_length=1, max_length=20_000)]
    node_id: str | None = None


class FinalizeLastRequest(BaseModel):
    node_id: str | None = None


class AgentConfigRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    instructions: Annotated[str, Field(min_length=1, max_length=50_000)]
    expected_result: Annotated[str, Field(default="", max_length=50_000)]
    interaction_mode: Literal["interactive", "autonomous"] = "interactive"

    @model_validator(mode="after")
    def validate(self) -> "AgentConfigRequest":
        self.name = self.name.strip()
        self.instructions = self.instructions.strip()
        self.expected_result = self.expected_result.strip()
        if not self.name or not self.instructions:
            raise ValueError("Name and instructions are required.")
        if self.interaction_mode == "autonomous" and not self.expected_result:
            raise ValueError("An expected result is required for autonomous agents.")
        return self


class WorkflowConfigRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    config: str | dict[str, object]

    @model_validator(mode="after")
    def validate(self) -> "WorkflowConfigRequest":
        self.name = self.name.strip()
        if isinstance(self.config, dict):
            self.config = json.dumps(self.config, ensure_ascii=False)
        else:
            self.config = self.config.strip()
        if not self.name or not self.config:
            raise ValueError("Name and config are required.")
        if len(self.config) > 100_000:
            raise ValueError("Config must not exceed 100000 characters.")
        _validate_workflow_config(self.config)
        return self


class ConstantFileRequest(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_base64: Annotated[
        str, Field(min_length=4, max_length=((MAX_CONSTANT_FILE_BYTES + 2) // 3) * 4)
    ]

    @model_validator(mode="after")
    def validate(self) -> "ConstantFileRequest":
        self.filename = self.filename.strip()
        if (
            not self.filename
            or "/" in self.filename
            or "\\" in self.filename
            or any(ord(character) < 32 for character in self.filename)
        ):
            raise ValueError("Provide a file name without a path.")
        extension = Path(self.filename).suffix.lower()
        if extension not in SUPPORTED_CONSTANT_FILE_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_CONSTANT_FILE_EXTENSIONS))
            raise ValueError(f"Unsupported file type. Use one of: {supported}.")
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("File content must be valid base64-encoded data.") from error
        if not content:
            raise ValueError("The selected file is empty.")
        if len(content) > MAX_CONSTANT_FILE_BYTES:
            raise ValueError(
                f"Files must be {MAX_CONSTANT_FILE_BYTES} bytes or smaller."
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Only UTF-8 text files are supported.") from error
        return self

    @property
    def content(self) -> str:
        return base64.b64decode(self.content_base64, validate=True).decode("utf-8")


def create_app(agent: CopilotAgentService | None = None) -> FastAPI:
    service = agent or CopilotAgentService()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    application = FastAPI(title="Agent Orchestrator", lifespan=lifespan)
    application.state.agent = service

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/sessions")
    async def list_sessions() -> list[dict[str, object]]:
        return service.list_sessions()

    @application.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        try:
            await service.delete_session(session_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Session not found.") from error
        return {"status": "deleted"}

    @application.post("/api/sessions")
    async def start_session(request: StartSessionRequest) -> dict[str, object]:
        try:
            session_id, messages = await service.open_conversation(
                request.session_id, request.config_id, request.workflow_id
            )
        except AgentUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Agent not found.") from error
        if request.prompt.strip():
            try:
                messages = await service.send_message(session_id, request.prompt.strip())
            except AgentUnavailableError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
            except WorkflowTransitionError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "session_id": session_id,
            "messages": [asdict(message) for message in messages],
            "workflow": service.get_workflow(session_id),
        }

    @application.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: SendMessageRequest) -> dict[str, object]:
        content = request.content.strip()
        if not content:
            raise HTTPException(status_code=422, detail="Message cannot be empty.")
        try:
            messages = (
                await service.send_message(session_id, content, request.node_id)
                if request.node_id
                else await service.send_message(session_id, content)
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This conversation is no longer available."
            ) from error
        except AgentUnavailableError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "messages": [asdict(message) for message in messages],
            "workflow": (
                service.get_workflow(session_id, request.node_id)
                if request.node_id
                else service.get_workflow(session_id)
            ),
        }

    @application.get("/api/sessions/{session_id}/workflow")
    async def get_workflow(session_id: str, node_id: str | None = None) -> dict[str, object]:
        try:
            return (
                service.get_workflow(session_id, node_id)
                if node_id else service.get_workflow(session_id)
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error

    @application.post("/api/sessions/{session_id}/finalize")
    async def submit_for_review(session_id: str, request: SubmitForReviewRequest) -> dict[str, object]:
        try:
            return (
                await service.submit_for_review(
                    session_id, request.prompt.strip(), request.node_id
                )
                if request.node_id
                else await service.submit_for_review(session_id, request.prompt.strip())
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
        except AgentUnavailableError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/sessions/{session_id}/finalize-last")
    async def finalize_last(session_id: str, request: FinalizeLastRequest) -> dict[str, object]:
        try:
            return (
                await service.finalize_result(session_id, request.node_id)
                if request.node_id
                else await service.finalize_result(session_id)
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
        except AgentUnavailableError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/sessions/{session_id}/edit")
    async def edit_result(session_id: str) -> dict[str, object]:
        try:
            return await service.edit_result(session_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/sessions/{session_id}/discard")
    async def discard_workflow(session_id: str) -> dict[str, object]:
        try:
            return await service.discard_workflow(session_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
        except AgentUnavailableError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/sessions/{session_id}/approve")
    async def approve_result(session_id: str) -> dict[str, object]:
        try:
            return await service.approve_result(session_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
        except AgentUnavailableError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except WorkflowTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/agent-configs")
    async def list_agent_configs() -> list[dict[str, object]]:
        return service.store.agent_configs()

    @application.post("/api/agent-configs")
    async def create_agent_config(request: AgentConfigRequest) -> dict[str, object]:
        config_id = str(uuid.uuid4())
        service.store.save_agent_config({
            "id": config_id,
            "name": request.name,
            "instructions": request.instructions,
            "expected_result": request.expected_result,
            "interaction_mode": request.interaction_mode,
        })
        return service.store.agent_config(config_id)

    @application.put("/api/agent-configs/{config_id}")
    async def update_agent_config(config_id: str, request: AgentConfigRequest) -> dict[str, object]:
        if not service.store.agent_config(config_id):
            raise HTTPException(status_code=404, detail="Agent config not found.")
        service.store.save_agent_config({
            "id": config_id,
            "name": request.name,
            "instructions": request.instructions,
            "expected_result": request.expected_result,
            "interaction_mode": request.interaction_mode,
        })
        return service.store.agent_config(config_id)

    @application.get("/api/agent-configs/{config_id}")
    async def get_agent_config(config_id: str) -> dict[str, object]:
        config = service.store.agent_config(config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Agent config not found.")
        return config

    @application.delete("/api/agent-configs/{config_id}")
    async def delete_agent_config(config_id: str) -> dict[str, str]:
        service.store.delete_agent_config(config_id)
        return {"status": "deleted"}

    @application.get("/api/workflow-configs")
    async def list_workflow_configs() -> list[dict[str, object]]:
        return service.store.workflow_configs()

    @application.post("/api/workflow-configs")
    async def create_workflow_config(request: WorkflowConfigRequest) -> dict[str, object]:
        config_id = str(uuid.uuid4())
        service.store.save_workflow_config({
            "id": config_id,
            "name": request.name,
            "config": request.config,
        })
        return service.store.workflow_config(config_id)

    @application.put("/api/workflow-configs/{config_id}")
    async def update_workflow_config(config_id: str, request: WorkflowConfigRequest) -> dict[str, object]:
        if not service.store.workflow_config(config_id):
            raise HTTPException(status_code=404, detail="Workflow config not found.")
        service.store.save_workflow_config({
            "id": config_id,
            "name": request.name,
            "config": request.config,
        })
        return service.store.workflow_config(config_id)

    @application.get("/api/workflow-configs/{config_id}")
    async def get_workflow_config(config_id: str) -> dict[str, object]:
        config = service.store.workflow_config(config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Workflow config not found.")
        return config

    @application.delete("/api/workflow-configs/{config_id}")
    async def delete_workflow_config(config_id: str) -> dict[str, str]:
        service.store.delete_workflow_config(config_id)
        return {"status": "deleted"}

    @application.get("/api/workflow-configs/{config_id}/constant-files")
    async def list_constant_files(config_id: str) -> list[dict[str, object]]:
        if not service.store.workflow_config(config_id):
            raise HTTPException(status_code=404, detail="Workflow config not found.")
        return service.store.constant_files(config_id)

    @application.post("/api/workflow-configs/{config_id}/constant-files")
    async def create_constant_file(
        config_id: str, request: ConstantFileRequest
    ) -> dict[str, object]:
        if not service.store.workflow_config(config_id):
            raise HTTPException(status_code=404, detail="Workflow config not found.")
        file_id = uuid.uuid4().hex
        service.store.save_constant_file({
            "id": file_id,
            "workflow_id": config_id,
            "filename": request.filename,
            "content": request.content,
        })
        return service.store.constant_file(config_id, file_id)

    @application.get("/api/workflow-configs/{config_id}/constant-files/{file_id}")
    async def get_constant_file(config_id: str, file_id: str) -> dict[str, object]:
        constant_file = service.store.constant_file(config_id, file_id)
        if constant_file is None:
            raise HTTPException(status_code=404, detail="Constant file not found.")
        return constant_file

    @application.put("/api/workflow-configs/{config_id}/constant-files/{file_id}")
    async def replace_constant_file(
        config_id: str, file_id: str, request: ConstantFileRequest
    ) -> dict[str, object]:
        if service.store.constant_file(config_id, file_id) is None:
            raise HTTPException(status_code=404, detail="Constant file not found.")
        service.store.save_constant_file({
            "id": file_id,
            "workflow_id": config_id,
            "filename": request.filename,
            "content": request.content,
        })
        return service.store.constant_file(config_id, file_id)

    @application.delete("/api/workflow-configs/{config_id}/constant-files/{file_id}")
    async def delete_constant_file(config_id: str, file_id: str) -> dict[str, str]:
        if not service.store.workflow_config(config_id):
            raise HTTPException(status_code=404, detail="Workflow config not found.")
        try:
            deleted = service.store.delete_constant_file(config_id, file_id)
        except (json.JSONDecodeError, TypeError) as error:
            raise HTTPException(
                status_code=409,
                detail="The workflow configuration is invalid, so its file references could not be safely removed.",
            ) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="Constant file not found.")
        return {"status": "deleted"}

    if FRONTEND_DIR.is_dir():
        assets_dir = FRONTEND_DIR / "assets"
        if assets_dir.is_dir():
            application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @application.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return application


app = create_app()
