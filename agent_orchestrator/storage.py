"""Small SQLite store for agent definitions and durable workflow summaries."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


TOOLS = (
    "Websuche",
    "Dateien lesen",
    "Dateien schreiben",
    "Terminalbefehle ausführen",
    "Code durchsuchen",
)
MAX_CONSTANT_FILE_BYTES = 1_048_576
SUPPORTED_CONSTANT_FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".log"}
)
PERMISSIONS = (
    "Netzwerkzugriff",
    "Lesezugriff auf Dateien",
    "Schreibzugriff auf Dateien",
    "Befehle ausführen",
    "Tools ohne zusätzliche Freigabe verwenden",
)


class Store:
    """SQLite persistence; each operation uses its own short-lived connection."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or os.getenv(
            "AGENT_ORCHESTRATOR_DB",
            str(Path.home() / ".agent-orchestrator" / "data.sqlite3"),
        )
        self.path = Path(configured_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    tools TEXT NOT NULL, permissions TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                    workflow_id TEXT, input TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]', output TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', artifact TEXT NOT NULL DEFAULT '',
                    interaction_mode TEXT NOT NULL DEFAULT 'interactive',
                    expected_result TEXT NOT NULL DEFAULT '',
                    run_state TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_configs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, instructions TEXT NOT NULL,
                    expected_result TEXT, interaction_mode TEXT NOT NULL DEFAULT 'interactive',
                    credit_limit REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_configs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
                    credit_limit REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_constant_files (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, filename TEXT NOT NULL,
                    content TEXT NOT NULL, byte_size INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
                    connection_status TEXT NOT NULL DEFAULT 'unknown',
                    authentication_status TEXT NOT NULL DEFAULT 'unknown',
                    status_message TEXT NOT NULL DEFAULT '',
                    checked_at TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS usage_events (
                    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    node_id TEXT, agent_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                    token_count INTEGER, credit_count REAL, workflow_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_session_idx ON usage_events(session_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_agent_idx ON usage_events(agent_id)"
            )
            self._migrate_sessions_table(connection)
            workflow_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_configs)")}
            if "credit_limit" not in workflow_columns:
                connection.execute("ALTER TABLE workflow_configs ADD COLUMN credit_limit REAL")
            usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
            if "workflow_id" not in usage_columns:
                connection.execute("ALTER TABLE usage_events ADD COLUMN workflow_id TEXT")
                connection.execute(
                    """UPDATE usage_events SET workflow_id = (
                        SELECT workflow_id FROM sessions WHERE sessions.id = usage_events.session_id
                    )"""
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_workflow_idx ON usage_events(workflow_id)"
            )

    def _migrate_sessions_table(self, connection: sqlite3.Connection) -> None:
        """Add workflow_id column if it doesn't exist."""
        cursor = connection.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "workflow_id" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN workflow_id TEXT")
        if "constant_files" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN constant_files TEXT NOT NULL DEFAULT '[]'"
            )
        if "interaction_mode" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN interaction_mode TEXT NOT NULL DEFAULT 'interactive'"
            )
        if "expected_result" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN expected_result TEXT NOT NULL DEFAULT ''"
            )
        if "run_state" not in columns:
            connection.execute(
                "ALTER TABLE sessions ADD COLUMN run_state TEXT NOT NULL DEFAULT '{}'"
            )
        if "credit_limit" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN credit_limit REAL")
        agent_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_configs)")
        }
        if "interaction_mode" not in agent_columns:
            connection.execute(
                "ALTER TABLE agent_configs ADD COLUMN interaction_mode TEXT NOT NULL DEFAULT 'interactive'"
            )
        if "credit_limit" not in agent_columns:
            connection.execute("ALTER TABLE agent_configs ADD COLUMN credit_limit REAL")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _agent(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "role": row["role"],
            "tools": json.loads(row["tools"]),
            "permissions": json.loads(row["permissions"]),
        }

    def agents(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._agent(row) for row in connection.execute(
                "SELECT * FROM agents ORDER BY rowid"
            )]

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return self._agent(row) if row else None

    def save_agent(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO agents(id, name, role, tools, permissions)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role,
                  tools=excluded.tools, permissions=excluded.permissions""",
                (value["id"], value["name"], value["role"],
                 json.dumps(value["tools"]), json.dumps(value["permissions"])),
            )

    def delete_agent(self, agent_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def save_session(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO sessions
                (id, agent_id, agent_name, workflow_id, input, stage, status, messages, output, error, artifact, constant_files, interaction_mode, expected_result, run_state, credit_limit, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET agent_id=excluded.agent_id,
                  agent_name=excluded.agent_name, workflow_id=excluded.workflow_id, input=excluded.input, stage=excluded.stage,
                  status=excluded.status, messages=excluded.messages, output=excluded.output,
                  error=excluded.error, artifact=excluded.artifact, constant_files=excluded.constant_files,
                  interaction_mode=excluded.interaction_mode, expected_result=excluded.expected_result,
                  run_state=excluded.run_state,
                  credit_limit=excluded.credit_limit,
                  updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["agent_id"], value["agent_name"], value.get("workflow_id"), value["input"],
                 value["stage"], value["status"], json.dumps(value.get("messages", [])),
                 value.get("output", ""), value.get("error", ""), value.get("artifact", ""),
                 json.dumps(value.get("constant_files", [])),
                 value.get("interaction_mode", "interactive"), value.get("expected_result", ""),
                 json.dumps(value.get("run_state", {}), ensure_ascii=False),
                 value.get("credit_limit")),
            )

    def sessions(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [self._session(row) for row in connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, rowid DESC"
            )]

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return self._session(row) if row else None

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        try:
            workflow_id = row["workflow_id"]
        except IndexError:
            workflow_id = None
        return {
            "id": row["id"], "agent_id": row["agent_id"], "agent_name": row["agent_name"],
            "workflow_id": workflow_id,
            "input": row["input"], "stage": row["stage"], "status": row["status"],
            "messages": json.loads(row["messages"]), "output": row["output"],
            "error": row["error"], "artifact": row["artifact"],
            "constant_files": json.loads(row["constant_files"]),
            "interaction_mode": row["interaction_mode"],
            "expected_result": row["expected_result"],
            "run_state": json.loads(row["run_state"]),
            "credit_limit": row["credit_limit"],
            "updated_at": row["updated_at"],
        }

    def delete_session(self, session_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def has_nonterminal_sessions(self, agent_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE agent_id = ? AND status != 'completed' LIMIT 1",
                (agent_id,),
            ).fetchone()
            return row is not None

    def save_agent_config(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO agent_configs(id, name, instructions, expected_result, interaction_mode, credit_limit, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, instructions=excluded.instructions,
                  expected_result=excluded.expected_result, interaction_mode=excluded.interaction_mode,
                  credit_limit=excluded.credit_limit,
                  updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["name"], value["instructions"], value.get("expected_result", ""),
                 value.get("interaction_mode", "interactive"), value.get("credit_limit")),
            )

    def agent_config(self, agent_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_configs WHERE id = ?", (agent_id,)
            ).fetchone()
            return self._agent_config(row) if row else None

    def agent_configs(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                self._agent_config(row)
                for row in connection.execute("SELECT * FROM agent_configs ORDER BY updated_at DESC")
            ]

    @staticmethod
    def _agent_config(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "instructions": row["instructions"],
            "expected_result": row["expected_result"] or "",
            "interaction_mode": row["interaction_mode"],
            "credit_limit": row["credit_limit"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _usage_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
        def metric(name: str) -> dict[str, Any]:
            values = [row[name] for row in rows if row[name] is not None]
            if not rows or not values:
                status = "unavailable"
                value = None
            else:
                status = "available" if len(values) == len(rows) else "partial"
                value = sum(values)
            return {"status": status, "value": value}

        return {
            "status": "available" if rows else "unavailable",
            "credits": metric("credit_count"),
            "tokens": metric("token_count"),
        }

    def record_usage_event(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO usage_events
                (event_id, session_id, node_id, agent_id, agent_name, token_count, credit_count, workflow_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    value["event_id"], value["session_id"], value.get("node_id"),
                    value["agent_id"], value["agent_name"], value.get("token_count"),
                    value.get("credit_count"), value.get("workflow_id"),
                ),
            )

    def session_usage(
        self, session_id: str, participants: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_events WHERE session_id = ? ORDER BY created_at, rowid",
                (session_id,),
            ).fetchall()
        groups: dict[tuple[str, str | None], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["agent_id"], row["node_id"]), []).append(row)

        agents = []
        seen: set[tuple[str, str | None]] = set()
        for participant in participants or []:
            key = (participant["agent_id"], participant.get("node_id"))
            seen.add(key)
            agents.append({
                **participant,
                **self._usage_metrics(groups.get(key, [])),
            })
        for key, group in groups.items():
            if key in seen:
                continue
            row = group[0]
            agents.append({
                "agent_id": row["agent_id"],
                "node_id": row["node_id"],
                "agent_name": row["agent_name"],
                **self._usage_metrics(group),
            })
        return {
            **self._usage_metrics(rows),
            "agents": agents,
        }

    def agent_usage(self, agent_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_events WHERE agent_id = ? ORDER BY created_at, rowid",
                (agent_id,),
            ).fetchall()
            sessions = connection.execute(
                "SELECT id, agent_id, run_state FROM sessions"
            ).fetchall()
            usage_session_ids = {
                row["session_id"]
                for row in connection.execute(
                    "SELECT DISTINCT session_id FROM usage_events WHERE agent_id = ?",
                    (agent_id,),
                )
            }
        relevant_session_ids = set()
        for session in sessions:
            if session["agent_id"] == agent_id:
                relevant_session_ids.add(session["id"])
                continue
            run_state = json.loads(session["run_state"])
            if not isinstance(run_state, dict):
                continue
            graph_nodes = run_state.get("graph_nodes", [])
            conversations = run_state.get("agent_conversations", [])
            if not isinstance(graph_nodes, list):
                graph_nodes = []
            if not isinstance(conversations, list):
                conversations = []
            if any(
                isinstance(node, dict) and node.get("agent_id") == agent_id
                for node in graph_nodes
            ) or any(
                isinstance(item, dict) and item.get("agent_id") == agent_id
                for item in conversations
            ):
                relevant_session_ids.add(session["id"])

        summary = self._usage_metrics(rows)
        if relevant_session_ids - usage_session_ids:
            for name in ("credits", "tokens"):
                if summary[name]["status"] == "available":
                    summary[name]["status"] = "partial"
            if summary["status"] == "available":
                summary["status"] = "partial"
        return summary

    def workflow_usage(self, workflow_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_events WHERE workflow_id = ? ORDER BY created_at, rowid",
                (workflow_id,),
            ).fetchall()
            sessions = {
                row["id"] for row in connection.execute(
                    "SELECT id FROM sessions WHERE workflow_id = ?", (workflow_id,)
                )
            }
        summary = self._usage_metrics(rows)
        if sessions - {row["session_id"] for row in rows}:
            for name in ("credits", "tokens"):
                if summary[name]["status"] == "available":
                    summary[name]["status"] = "partial"
            if summary["status"] == "available":
                summary["status"] = "partial"
        return summary

    def session_agent_usage(self, session_id: str, agent_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM usage_events
                WHERE session_id = ? AND agent_id = ? ORDER BY created_at, rowid""",
                (session_id, agent_id),
            ).fetchall()
        return self._usage_metrics(rows)

    def update_session_credit_limit(self, session_id: str, credit_limit: float | None) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET credit_limit=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (credit_limit, session_id),
            )
            return cursor.rowcount > 0

    def delete_agent_config(self, agent_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM agent_configs WHERE id = ?", (agent_id,))

    def save_workflow_config(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO workflow_configs(id, name, config, credit_limit, updated_at)
                VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, config=excluded.config,
                  credit_limit=CASE WHEN ? THEN excluded.credit_limit ELSE workflow_configs.credit_limit END,
                  updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["name"], value["config"], value.get("credit_limit"),
                 "credit_limit" in value),
            )

    def workflow_config(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_configs WHERE id = ?", (workflow_id,)
            ).fetchone()
            return self._workflow_config(row) if row else None

    def workflow_configs(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                self._workflow_config(row)
                for row in connection.execute("SELECT * FROM workflow_configs ORDER BY updated_at DESC")
            ]

    @staticmethod
    def _workflow_config(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "config": row["config"],
            "credit_limit": row["credit_limit"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def delete_workflow_config(self, workflow_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM workflow_constant_files WHERE workflow_id = ?", (workflow_id,)
            )
            connection.execute("DELETE FROM workflow_configs WHERE id = ?", (workflow_id,))

    def save_constant_file(self, value: dict[str, Any]) -> None:
        content = value["content"]
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO workflow_constant_files
                (id, workflow_id, filename, content, byte_size, updated_at)
                VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,
                  content=excluded.content, byte_size=excluded.byte_size,
                  updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["workflow_id"], value["filename"], content,
                 len(content.encode("utf-8"))),
            )

    def constant_file(self, workflow_id: str, file_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM workflow_constant_files
                WHERE workflow_id = ? AND id = ?""",
                (workflow_id, file_id),
            ).fetchone()
            return self._constant_file(row) if row else None

    def constant_files(self, workflow_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                self._constant_file(row, include_content=False)
                for row in connection.execute(
                    """SELECT * FROM workflow_constant_files
                    WHERE workflow_id = ? ORDER BY created_at, rowid""",
                    (workflow_id,),
                )
            ]

    @staticmethod
    def _constant_file(row: sqlite3.Row, include_content: bool = True) -> dict[str, Any]:
        value = {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "filename": row["filename"],
            "byte_size": row["byte_size"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_content:
            value["content"] = row["content"]
        return value

    def delete_constant_file(self, workflow_id: str, file_id: str) -> bool:
        with self._connection() as connection:
            file_row = connection.execute(
                """SELECT id, filename FROM workflow_constant_files
                WHERE workflow_id = ? AND id = ?""",
                (workflow_id, file_id),
            ).fetchone()
            if file_row is None:
                return False

            workflow_row = connection.execute(
                "SELECT config FROM workflow_configs WHERE id = ?", (workflow_id,)
            ).fetchone()
            if workflow_row is not None:
                config = json.loads(workflow_row["config"])
                if isinstance(config, dict) and isinstance(config.get("nodes"), list):
                    for node in config["nodes"]:
                        if isinstance(node, dict) and node.get("file_id") == file_id:
                            node.pop("file_id", None)
                            node["source_type"] = "manual"
                            if node.get("label") == file_row["filename"]:
                                node["label"] = "Data source"
                    connection.execute(
                        """UPDATE workflow_configs SET config = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?""",
                        (json.dumps(config, ensure_ascii=False), workflow_id),
                    )

            connection.execute(
                "DELETE FROM workflow_constant_files WHERE workflow_id = ? AND id = ?",
                (workflow_id, file_id),
            )
            return True

    def save_mcp_server(self, value: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO mcp_servers (id, name, config)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                  config=excluded.config, connection_status='unknown',
                  authentication_status='unknown', status_message='',
                  checked_at=NULL, updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["name"], json.dumps(value["config"], ensure_ascii=False)),
            )

    def mcp_server_config(self, server_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
            return self._mcp_server(row, include_config=True) if row else None

    def mcp_server(self, server_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
            return self._mcp_server(row) if row else None

    def mcp_servers(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                self._mcp_server(row)
                for row in connection.execute(
                    "SELECT * FROM mcp_servers ORDER BY created_at, rowid"
                )
            ]

    @staticmethod
    def _mcp_server(row: sqlite3.Row, include_config: bool = False) -> dict[str, Any]:
        value = {
            "id": row["id"],
            "name": row["name"],
            "connection_status": row["connection_status"],
            "authentication_status": row["authentication_status"],
            "status_message": row["status_message"],
            "checked_at": row["checked_at"],
            "updated_at": row["updated_at"],
            "config": json.loads(row["config"]),
        }
        if not include_config:
            from agent_orchestrator.mcp_servers import public_server

            return public_server(value)
        return value

    def update_mcp_server_status(
        self, server_id: str, connection_status: str, authentication_status: str,
        status_message: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE mcp_servers SET connection_status=?,
                  authentication_status=?, status_message=?,
                  checked_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
                (connection_status, authentication_status, status_message, server_id),
            )

    def update_mcp_server_config(self, server_id: str, config: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE mcp_servers SET config=?, connection_status='unknown',
                  authentication_status='unknown', status_message='',
                  checked_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (json.dumps(config, ensure_ascii=False), server_id),
            )

    def delete_mcp_server(self, server_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM mcp_servers WHERE id = ?", (server_id,)
            )
            return cursor.rowcount > 0
