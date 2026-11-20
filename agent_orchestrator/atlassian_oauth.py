"""Single-user, local Atlassian MCP OAuth authorization."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v2/mcp"
logger = logging.getLogger(__name__)


class OAuthStorage:
    """Keep the user's tokens and dynamic client registration in process memory."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


@dataclass
class OAuthAttempt:
    authorization_url: asyncio.Future[str]
    callback: asyncio.Future[AuthorizationCodeResult]
    task: asyncio.Task[None] | None = None
    state: str = ""
    result: str = "pending"
    message: str = "Connecting to Atlassian."


class AtlassianOAuth:
    def __init__(self) -> None:
        self.attempts: dict[str, OAuthAttempt] = {}
        self.storage: dict[str, OAuthStorage] = {}
        self.providers: dict[str, OAuthClientProvider] = {}

    def authorized(self, server_id: str) -> bool:
        return bool(self.storage.get(server_id) and self.storage[server_id].tokens)

    async def start(self, server_id: str, redirect_uri: str, *, reauthenticate: bool = False) -> dict[str, str]:
        existing = self.attempts.get(server_id)
        if existing and existing.result == "pending":
            raise ValueError("An authorization attempt is already in progress.")
        if reauthenticate:
            self.storage.pop(server_id, None)
            self.providers.pop(server_id, None)

        loop = asyncio.get_running_loop()
        attempt = OAuthAttempt(loop.create_future(), loop.create_future())
        self.attempts[server_id] = attempt
        storage = self.storage.setdefault(server_id, OAuthStorage())

        async def redirect_handler(url: str) -> None:
            parsed = urlsplit(url)
            states = parse_qs(parsed.query).get("state", [])
            if (
                parsed.scheme != "https" or parsed.hostname != "auth.atlassian.com"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or len(states) != 1
            ):
                raise ValueError("Atlassian returned an unexpected authorization URL.")
            attempt.state = states[0]
            attempt.message = "Complete authorization in your browser."
            if not attempt.authorization_url.done():
                attempt.authorization_url.set_result(url)

        async def callback_handler() -> AuthorizationCodeResult:
            return await attempt.callback

        provider = self.providers.get(server_id)
        if provider is None:
            provider = OAuthClientProvider(
                server_url=ATLASSIAN_MCP_URL,
                client_metadata=OAuthClientMetadata(
                    client_name="Agent Orchestrator",
                    redirect_uris=[AnyUrl(redirect_uri)],
                    token_endpoint_auth_method="none",
                ),
                storage=storage,
                redirect_handler=redirect_handler,
                callback_handler=callback_handler,
            )
            self.providers[server_id] = provider
        else:
            provider.context.redirect_handler = redirect_handler
            provider.context.callback_handler = callback_handler
            if str(provider.context.client_metadata.redirect_uris[0]) != redirect_uri:
                raise ValueError("The local callback address changed. Reauthenticate to continue.")

        attempt.task = asyncio.create_task(self._run(server_id, attempt, provider))
        done, _ = await asyncio.wait(
            {attempt.authorization_url, attempt.task}, timeout=30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if attempt.authorization_url in done:
            return {"status": "pending", "authorization_url": attempt.authorization_url.result()}
        if attempt.task not in done:
            attempt.task.cancel()
            await asyncio.gather(attempt.task, return_exceptions=True)
            attempt.result = "failed"
            attempt.message = "Atlassian did not respond. Check your connection and try again."
        return {"status": attempt.result, "message": attempt.message}

    async def _run(self, server_id: str, attempt: OAuthAttempt, provider: OAuthClientProvider) -> None:
        try:
            async with asyncio.timeout(180):
                async with httpx2.AsyncClient(auth=provider, timeout=30) as http_client:
                    transport = streamable_http_client(
                        ATLASSIAN_MCP_URL, http_client=http_client,
                    )
                    async with Client(transport, mode="legacy"):
                        if not self.authorized(server_id):
                            raise ValueError("Atlassian did not issue an OAuth token.")
            attempt.result = "connected"
            attempt.message = "Atlassian authorization and MCP connection succeeded."
        except asyncio.CancelledError:
            attempt.result = "failed"
            attempt.message = "Authorization was cancelled."
            raise
        except Exception as error:
            logger.warning("Atlassian MCP authorization failed (%s).", type(error).__name__)
            attempt.result = "failed"
            attempt.message = (
                "Atlassian authorization or connection failed. Check your access and try again."
            )

    def complete(self, state: str, code: str | None, issuer: str | None, error: str | None) -> None:
        attempt = next(
            (item for item in self.attempts.values()
             if item.state and secrets.compare_digest(item.state.encode(), state.encode())),
            None,
        )
        if not attempt or attempt.callback.done() or attempt.result != "pending":
            raise ValueError("This authorization request is invalid or has expired.")
        attempt.state = ""
        if error or not code:
            attempt.callback.set_exception(ValueError("Atlassian authorization was denied or cancelled."))
        else:
            attempt.callback.set_result(AuthorizationCodeResult(code=code, state=state, iss=issuer))
        attempt.message = "Finishing authorization with Atlassian."

    async def cancel(self, server_id: str) -> bool:
        attempt = self.attempts.get(server_id)
        if not attempt or not attempt.state or not attempt.task or attempt.task.done():
            return False
        attempt.state = ""
        attempt.task.cancel()
        await asyncio.gather(attempt.task, return_exceptions=True)
        return True

    async def clear(self, server_id: str) -> None:
        attempt = self.attempts.pop(server_id, None)
        if attempt and attempt.task and not attempt.task.done():
            attempt.task.cancel()
            await asyncio.gather(attempt.task, return_exceptions=True)
        self.storage.pop(server_id, None)
        self.providers.pop(server_id, None)

    async def close(self) -> None:
        for server_id in list(self.attempts):
            await self.clear(server_id)
