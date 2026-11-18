# Agent Orchestrator

A release-gated React workflow backed by FastAPI and the GitHub Copilot SDK.
Discuss the task with Agent 1 for as long as needed. Choosing **Use last answer**
saves its latest reply as a temporary `agent-1-result.md` file for review. Then
approve it to start Agent 2, continue the discussion, or discard the workflow.
Agent 2's complete response is shown when processing finishes. The React build
and API are served by the same container. Agent definitions and session
summaries, messages, workflow state, and results are stored in SQLite. The default database is
`~/.agent-orchestrator/data.sqlite3`; set `AGENT_ORCHESTRATOR_DB` to choose a
different location. Active Copilot runtime connections are process-local.

## Run with Docker

1. Provide a GitHub Copilot SDK credential to the container as
   `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. Do not commit the
   credential. A Copilot subscription and model access are required. On a
   developer machine, signed-in Copilot CLI credentials may also be used, but
   credentials on the host are not automatically available inside Docker.
2. Build and run:

   ```sh
   docker build -t agent-orchestrator .
   docker run --rm -p 8000:8000 -e COPILOT_GITHUB_TOKEN agent-orchestrator
   ```
3. Open <http://localhost:8000>.

The backend defaults to the lower-cost `gpt-5.4-nano` model. Set
`COPILOT_MODEL` to another model identifier supported by the Copilot SDK if
needed. Access can vary by plan and account policy. If the runtime or
credentials are unavailable, the start screen reports that the agent could not
be started; the container logs retain the backend diagnostics.

## Run locally on Windows

Install Python 3.11+ and Node.js 22+, then run this once from PowerShell:

```powershell
.\start-local.ps1
```

The script creates `.venv` if needed, installs Python and frontend dependencies,
prepares the Copilot runtime, builds React, and starts the server at
<http://localhost:8000>. Press Ctrl+C to stop it. On later starts it reuses the
installed dependencies and only rebuilds the frontend.

The SDK can use a local Copilot CLI login. Otherwise, set `COPILOT_GITHUB_TOKEN`,
`GH_TOKEN`, or `GITHUB_TOKEN` in the same PowerShell window before starting the
script. Never commit credentials.
