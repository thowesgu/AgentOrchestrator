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
                    input TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]', output TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', artifact TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_configs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, instructions TEXT NOT NULL,
                    expected_result TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )

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
                (id, agent_id, agent_name, input, stage, status, messages, output, error, artifact, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET agent_id=excluded.agent_id,
                  agent_name=excluded.agent_name, input=excluded.input, stage=excluded.stage,
                  status=excluded.status, messages=excluded.messages, output=excluded.output,
                  error=excluded.error, artifact=excluded.artifact, updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["agent_id"], value["agent_name"], value["input"],
                 value["stage"], value["status"], json.dumps(value.get("messages", [])),
                 value.get("output", ""), value.get("error", ""), value.get("artifact", "")),
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
        return {
            "id": row["id"], "agent_id": row["agent_id"], "agent_name": row["agent_name"],
            "input": row["input"], "stage": row["stage"], "status": row["status"],
            "messages": json.loads(row["messages"]), "output": row["output"],
            "error": row["error"], "artifact": row["artifact"],
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
                """INSERT INTO agent_configs(id, name, instructions, expected_result, updated_at)
                VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, instructions=excluded.instructions,
                  expected_result=excluded.expected_result, updated_at=CURRENT_TIMESTAMP""",
                (value["id"], value["name"], value["instructions"], value.get("expected_result", "")),
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
            "expected_result": row["expected_result"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def delete_agent_config(self, agent_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM agent_configs WHERE id = ?", (agent_id,))
