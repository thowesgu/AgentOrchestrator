import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_orchestrator.mcp_servers import check_server
from agent_orchestrator.storage import Store


class MCPServerStorageTests(unittest.TestCase):
    def test_server_configuration_and_status_survive_store_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.sqlite3"
            store = Store(database)
            store.save_mcp_server({
                "id": "server-1",
                "name": "Private tools",
                "config": {
                    "transport": "http",
                    "url": "https://example.test/mcp",
                    "auth": {"type": "bearer", "token": "private-token"},
                },
            })
            store.update_mcp_server_status(
                "server-1", "connected", "authenticated", "",
            )

            reopened = Store(database)
            public = reopened.mcp_server("server-1")
            internal = reopened.mcp_server_config("server-1")

            self.assertEqual(public["connection_status"], "connected")
            self.assertEqual(public["authentication_status"], "authenticated")
            self.assertNotIn("private-token", json.dumps(public))
            self.assertEqual(internal["config"]["auth"]["token"], "private-token")


class MCPServerConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_connection_check_completes_initialize_handshake(self) -> None:
        script = (
            "import json,sys; request=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':"
            "{'protocolVersion':request['params']['protocolVersion']}}),flush=True)"
        )
        with patch.dict(os.environ, {"AGENT_ORCHESTRATOR_ALLOW_MCP_STDIO": "1"}):
            status = await check_server({
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", script],
                "env": {},
            })

        self.assertEqual(status[0], "connected")
        self.assertEqual(status[1], "unknown")

    async def test_stdio_connection_check_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {"AGENT_ORCHESTRATOR_ALLOW_MCP_STDIO": ""}):
            status = await check_server({
                "transport": "stdio", "command": sys.executable, "args": [], "env": {},
            })

        self.assertEqual(status[0], "unknown")
        self.assertIn("disabled", status[2].lower())

    async def test_http_connection_check_distinguishes_invalid_authentication(self) -> None:
        from urllib.error import HTTPError
        from io import BytesIO

        error = HTTPError(
            "https://8.8.8.8/mcp", 401, "Unauthorized", {}, BytesIO(b""),
        )
        with patch("agent_orchestrator.mcp_servers.urllib.request.urlopen", side_effect=error):
            status = await check_server({
                "transport": "http",
                "url": "https://8.8.8.8/mcp",
                "auth": {"type": "none"},
            })

        self.assertEqual(status[0], "connected")
        self.assertEqual(status[1], "required")
        self.assertIn("authentication", status[2].lower())

    async def test_http_connection_check_blocks_private_destinations_by_default(self) -> None:
        with patch.dict(os.environ, {"AGENT_ORCHESTRATOR_MCP_ALLOW_PRIVATE_NETWORKS": ""}):
            with patch("agent_orchestrator.mcp_servers.urllib.request.urlopen") as open_url:
                status = await check_server({
                    "transport": "http",
                    "url": "http://127.0.0.1:8080/mcp",
                    "auth": {"type": "none"},
                })

        self.assertEqual(status[0], "disconnected")
        self.assertIn("private", status[2].lower())
        open_url.assert_not_called()

    async def test_http_connection_check_verifies_mcp_initialize_response(self) -> None:
        from io import BytesIO

        class FakeHeaders:
            @staticmethod
            def get_content_type() -> str:
                return "application/json"

        class FakeResponse:
            headers = FakeHeaders()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read(_size: int) -> bytes:
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": "2025-03-26"},
                }).encode()

        with patch(
            "agent_orchestrator.mcp_servers.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            status = await check_server({
                "transport": "http",
                "url": "https://8.8.8.8/mcp",
                "auth": {"type": "none"},
            })

        self.assertEqual(status, ("connected", "not_required", ""))


if __name__ == "__main__":
    unittest.main()
