"""FastAPI application and static React frontend."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from agent_orchestrator.agent import (
    AgentUnavailableError,
    CopilotAgentService,
    WorkflowTransitionError,
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class StartSessionRequest(BaseModel):
    session_id: str | None = None
    config_id: str = "default"
    workflow_id: str | None = None
    prompt: Annotated[str, Field(default="", max_length=20_000)]


class SendMessageRequest(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=20_000)]


class SubmitForReviewRequest(BaseModel):
    prompt: Annotated[str, Field(min_length=1, max_length=20_000)]


class AgentConfigRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    instructions: Annotated[str, Field(min_length=1, max_length=50_000)]
    expected_result: Annotated[str, Field(default="", max_length=50_000)]

    @model_validator(mode="after")
    def validate(self) -> "AgentConfigRequest":
        self.name = self.name.strip()
        self.instructions = self.instructions.strip()
        if not self.name or not self.instructions:
            raise ValueError("Name and instructions are required.")
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
        return self


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
            messages = await service.send_message(session_id, content)
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
            "workflow": service.get_workflow(session_id),
        }

    @application.get("/api/sessions/{session_id}/workflow")
    async def get_workflow(session_id: str) -> dict[str, object]:
        try:
            return service.get_workflow(session_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error

    @application.post("/api/sessions/{session_id}/finalize")
    async def submit_for_review(session_id: str, request: SubmitForReviewRequest) -> dict[str, object]:
        try:
            return await service.submit_for_review(session_id, request.prompt.strip())
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="This workflow is no longer available."
            ) from error
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

    if FRONTEND_DIR.is_dir():
        assets_dir = FRONTEND_DIR / "assets"
        if assets_dir.is_dir():
            application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @application.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return application


app = create_app()
