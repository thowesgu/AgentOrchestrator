"""MCP server connection checks and credential-safe representations."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any

logger = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_TIMEOUT_SECONDS = 8


def public_server(value: dict[str, Any]) -> dict[str, Any]:
    """Return server configuration without stored credential values."""
    config = value["config"]
    auth = config.get("auth", {})
    transport = config["transport"]
    public_config: dict[str, Any] = {"transport": transport}
    if transport == "http":
        public_config["url"] = config["url"]
        public_config["auth_type"] = auth.get("type", "none")
        public_config["auth_header_names"] = sorted(auth.get("headers", {}).keys())
        public_config["has_credentials"] = bool(
            auth.get("token") or auth.get("headers")
        )
    else:
        public_config.update({
            "command": config["command"],
            "args": config.get("args", []),
            "env_names": sorted(config.get("env", {}).keys()),
        })
        public_config["auth_type"] = "environment"
        public_config["has_credentials"] = bool(config.get("env"))
    return {
        "id": value["id"],
        "name": value["name"],
        **public_config,
        "connection_status": value["connection_status"],
        "authentication_status": value["authentication_status"],
        "status_message": value["status_message"],
        "checked_at": value["checked_at"],
        "updated_at": value["updated_at"],
    }


def _read_mcp_response(response: Any) -> dict[str, Any]:
    content_type = response.headers.get_content_type()
    if content_type == "text/event-stream":
        for line in response:
            if line.startswith(b"data:"):
                return json.loads(line[5:].strip())
        raise ValueError("The server closed its event stream before replying.")
    return json.loads(response.read(64 * 1024))


def _private_networks_allowed() -> bool:
    return os.getenv("AGENT_ORCHESTRATOR_MCP_ALLOW_PRIVATE_NETWORKS", "").lower() in {
        "1", "true", "yes",
    }


def _stdio_checks_allowed() -> bool:
    return os.getenv("AGENT_ORCHESTRATOR_ALLOW_MCP_STDIO", "").lower() in {
        "1", "true", "yes",
    }


def _destination_is_public(url: str) -> bool:
    if _private_networks_allowed():
        return True
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)


def _probe_http(config: dict[str, Any]) -> tuple[str, str, str]:
    if not _destination_is_public(config["url"]):
        return "disconnected", "unknown", (
            "The URL could not be resolved to a public address or resolves to a private network. "
            "Set AGENT_ORCHESTRATOR_MCP_ALLOW_PRIVATE_NETWORKS=1 only for trusted local deployments."
        )
    auth = config.get("auth", {})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if auth.get("type") == "bearer" and auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth.get("type") == "headers":
        headers.update(auth.get("headers", {}))
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "AgentOrchestrator", "version": "0.1.0"},
        },
    }).encode("utf-8")
    request = urllib.request.Request(config["url"], data=payload, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=MCP_TIMEOUT_SECONDS) as response:
            result = _read_mcp_response(response)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return "connected", "required", (
                "The server requires valid authentication. Update the credentials and try again."
            )
        return "disconnected", "unknown", (
            f"The server returned HTTP {error.code}. Check the URL and server logs."
        )
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        logger.info("MCP HTTP connection check failed (%s).", type(error).__name__)
        return "disconnected", "unknown", (
            "Could not reach the server. Check its URL, network access, and availability."
        )
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as error:
        logger.info("MCP HTTP handshake failed (%s).", type(error).__name__)
        return "disconnected", "unknown", (
            "The server did not complete the MCP initialization handshake."
        )
    if (
        not isinstance(result, dict)
        or result.get("jsonrpc") != "2.0"
        or not isinstance(result.get("result"), dict)
        or not result["result"].get("protocolVersion")
    ):
        return "disconnected", "unknown", (
            "The endpoint responded but did not return a valid MCP initialization result."
        )
    authentication_status = (
        "authenticated" if auth.get("type") != "none" and (
            auth.get("token") or auth.get("headers")
        ) else "not_required"
    )
    return "connected", authentication_status, ""


async def _probe_stdio(config: dict[str, Any]) -> tuple[str, str, str]:
    environment = os.environ.copy()
    environment.update(config.get("env", {}))
    try:
        process = await asyncio.create_subprocess_exec(
            config["command"],
            *config.get("args", []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
    except (OSError, ValueError) as error:
        logger.info("MCP stdio server could not start (%s).", type(error).__name__)
        return "disconnected", "unknown", (
            "The server process could not start. Check the command, arguments, and environment."
        )

    try:
        assert process.stdin is not None
        assert process.stdout is not None
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "AgentOrchestrator", "version": "0.1.0"},
            },
        }
        process.stdin.write((json.dumps(initialize) + "\n").encode("utf-8"))
        await process.stdin.drain()
        line = await asyncio.wait_for(
            process.stdout.readline(), timeout=MCP_TIMEOUT_SECONDS
        )
        result = json.loads(line)
        if (
            not isinstance(result, dict)
            or result.get("jsonrpc") != "2.0"
            or not isinstance(result.get("result"), dict)
            or not result["result"].get("protocolVersion")
        ):
            return "disconnected", "unknown", (
                "The process responded but did not return a valid MCP initialization result."
            )
        return "connected", "unknown", (
            "Connection verified. This server does not report its authentication state."
        )
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
        logger.info("MCP stdio handshake failed (%s).", type(error).__name__)
        return "disconnected", "unknown", (
            "The process started but did not complete the MCP initialization handshake."
        )
    finally:
        if process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            logger.warning("MCP stdio probe process did not exit after being killed.")


async def check_server(config: dict[str, Any]) -> tuple[str, str, str]:
    """Run a bounded MCP initialization check without exposing credentials."""
    if config["transport"] == "http":
        return await asyncio.to_thread(_probe_http, config)
    if not _stdio_checks_allowed():
        return "unknown", "unknown", (
            "Local process checks are disabled. Set AGENT_ORCHESTRATOR_ALLOW_MCP_STDIO=1 "
            "only when this application is trusted to run configured commands."
        )
    return await _probe_stdio(config)
