# Agent Orchestrator

A release-gated React workflow backed by FastAPI and the GitHub Copilot SDK.
Configure each agent with an interactive or autonomous mode and an expected
result. Interactive agents discuss the task until you choose **I've provided
enough context**; they then receive the configured result prompt. Autonomous
agents produce that result from the initial request.
If an interactive agent has no configured result prompt, use **Use last answer
as result** to release its latest reply. Results go to a configured
review step, or directly to the next agent when no review step is present.
Agent steps may fan out to multiple agents; branches run in parallel,
interactive branches wait for user input, and downstream agents wait until all
connected branches have completed. Waiting interactive agents are highlighted
in red; click an agent in the workflow visualization to open its conversation.
Reviews on parallel paths are presented when the other active paths have
finished, and each approved result is passed to its connected steps.
Downstream agents receive only their directly connected workflow inputs; the
original request is not included automatically. Connect a chat data source to
an agent when it needs that request, including additional agents connected
directly to Start. A connected agent's output may itself contain information
from earlier steps.
The React build and API are served by the same container. Agent definitions and
session summaries, messages, workflow state, and results are stored in SQLite. The default database is
`~/.agent-orchestrator/data.sqlite3`; set `AGENT_ORCHESTRATOR_DB` to choose a
different location. Active Copilot runtime connections are process-local.
After a server restart, saved conversations remain available read-only; start a
new session to interact with an agent again.

Workflow editors can retain UTF-8 `.txt`, `.md`, `.csv`, `.json`, `.yaml`,
`.yml`, `.xml`, and `.log` files up to 1 MiB per file. Save a workflow, upload
files in its editor, select a file on a data-source node, and connect that node
directly to the intended agent node. Only files connected to the active agent
are included in its session context; the session lists the files it received.
Replacing a file affects future sessions, and removing a file or its workflow
also removes the retained file content.

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
