import asyncio
import base64
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx2
from fastapi.testclient import TestClient

from agent_orchestrator.atlassian_oauth import ATLASSIAN_MCP_URL, AtlassianOAuth
from agent_orchestrator.web import create_app
from tests.test_web import FakeAgent


class OAuthFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_authorization_pkce_refresh_and_replay(self):
        manager = AtlassianOAuth()
        requests = []
        token_calls = []
        issuer = "https://auth.atlassian.com/test"
        authorization_query = {}

        def reply(request):
            requests.append(request)
            path = request.url.path
            if request.url.host == "mcp.atlassian.com" and path == "/v2/mcp":
                if request.headers.get("authorization") not in {"Bearer access", "Bearer refreshed"}:
                    return httpx2.Response(401, headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp"',
                    })
                if request.method == "DELETE":
                    return httpx2.Response(204)
                if request.method == "GET":
                    return httpx2.Response(405)
                payload = json.loads(request.content)
                if payload["method"] == "initialize":
                    return httpx2.Response(200, json={
                        "jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "serverInfo": {"name": "mock", "version": "1"}},
                    })
                return httpx2.Response(202)
            if "oauth-protected-resource" in path:
                return httpx2.Response(200, json={"resource": ATLASSIAN_MCP_URL,
                                                 "authorization_servers": [issuer],
                                                 "scopes_supported": ["read:me", "offline_access"]})
            if "oauth-authorization-server" in path:
                return httpx2.Response(200, json={
                    "issuer": issuer, "authorization_endpoint": "https://auth.atlassian.com/authorize",
                    "token_endpoint": "https://auth.atlassian.com/oauth/token",
                    "registration_endpoint": "https://auth.atlassian.com/register",
                    "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                })
            if path == "/register":
                body = json.loads(request.content)
                return httpx2.Response(201, json={**body, "client_id": "client"})
            if path == "/oauth/token":
                form = parse_qs(request.content.decode())
                token_calls.append(form)
                if form["grant_type"] == ["authorization_code"]:
                    verifier = form["code_verifier"][0]
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                    self.assertEqual(challenge, authorization_query["code_challenge"][0])
                    self.assertEqual(form["code"], ["the-code"])
                    return httpx2.Response(200, json={"access_token": "access", "token_type": "Bearer",
                                                       "refresh_token": "refresh", "expires_in": 3600})
                self.assertEqual(form["refresh_token"], ["refresh"])
                return httpx2.Response(200, json={"access_token": "refreshed", "token_type": "Bearer",
                                                   "refresh_token": "refresh2", "expires_in": 3600})
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        real_client = httpx2.AsyncClient
        transport = httpx2.MockTransport(reply)

        def client_factory(**kwargs):
            return real_client(transport=transport, **kwargs)

        with patch("agent_orchestrator.atlassian_oauth.httpx2.AsyncClient", side_effect=client_factory):
            first = await manager.start("one", "http://127.0.0.1:8000/api/mcp-oauth/callback")
            self.assertEqual(first["status"], "pending")
            with self.assertRaises(ValueError):
                await manager.start("one", "http://127.0.0.1:8000/api/mcp-oauth/callback")
            other = await manager.start("two", "http://127.0.0.1:8000/api/mcp-oauth/callback")
            self.assertEqual(other["status"], "pending")
            self.assertTrue(await manager.cancel("two"))
            self.assertEqual(manager.attempts["two"].result, "failed")
            self.assertFalse(await manager.cancel("two"))
            authorization_query.update(parse_qs(urlsplit(first["authorization_url"]).query))
            self.assertEqual(authorization_query["code_challenge_method"], ["S256"])
            with self.assertRaises(ValueError):
                manager.complete("wrong-state", "the-code", None, None)
            state = authorization_query["state"][0]
            manager.complete(state, "the-code", None, None)
            self.assertFalse(await manager.cancel("one"))
            with self.assertRaises(ValueError):
                manager.complete(state, "the-code", None, None)
            await asyncio.wait_for(manager.attempts["one"].task, 5)
            self.assertEqual(manager.attempts["one"].result, "connected")
            manager.providers["one"].context.token_expiry_time = 1
            second = await manager.start("one", "http://127.0.0.1:8000/api/mcp-oauth/callback")
            self.assertEqual(second["status"], "connected")
            self.assertEqual(len(token_calls), 2)
            self.assertEqual(manager.storage["one"].tokens.access_token, "refreshed")
        await manager.close()
        self.assertFalse(manager.storage)

    async def test_denial_and_expired_attempt_are_not_success(self):
        manager = AtlassianOAuth()
        loop = asyncio.get_running_loop()
        from agent_orchestrator.atlassian_oauth import OAuthAttempt
        attempt = OAuthAttempt(loop.create_future(), loop.create_future(), state="pending")
        manager.attempts["one"] = attempt
        manager.complete("pending", None, None, "access_denied")
        with self.assertRaises(ValueError):
            await attempt.callback
        with self.assertRaises(ValueError):
            manager.complete("pending", "code", None, None)


class OAuthWebTests(unittest.TestCase):
    def test_local_boundary_and_configuration_validation(self):
        agent = FakeAgent()
        app = create_app(agent)
        with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 1234)) as local:
            invalid = local.post("/api/mcp-servers", json={
                "name": "Invalid", "transport": "http", "url": "https://example.com/mcp",
                "auth_type": "atlassian_oauth",
            })
            self.assertEqual(invalid.status_code, 422)
            created = local.post("/api/mcp-servers", json={
                "name": "Atlassian", "transport": "http", "url": ATLASSIAN_MCP_URL,
                "auth_type": "atlassian_oauth",
            })
            self.assertEqual(created.status_code, 200)
            server_id = created.json()["id"]
            endpoint = f"/api/mcp-servers/{server_id}/atlassian-oauth"
            self.assertEqual(local.post(endpoint).status_code, 403)
            self.assertEqual(local.post(endpoint, headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(local.get("/api/mcp-oauth/callback?state=bad&code=secret").status_code, 400)
            self.assertEqual(local.post(f"/api/mcp-servers/{server_id}/reconnect").status_code, 422)
            self.assertEqual(created.json()["authentication_status"], "required")
            self.assertNotIn("token", json.dumps(created.json()))
            with TestClient(app, base_url="http://localhost:8000", client=("192.0.2.1", 1234)) as remote:
                self.assertEqual(remote.post(endpoint, headers={"Origin": "http://localhost:8000"}).status_code, 403)
                self.assertEqual(remote.get("/api/mcp-oauth/callback?state=x&code=y").status_code, 403)
            self.assertEqual(local.get(endpoint, headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 403)
            with patch.object(app.state.atlassian_oauth, "start", return_value={"status": "connected"}) as start:
                response = local.post(endpoint, headers={"Origin": "http://localhost:8000"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(start.call_args.args[1], "http://127.0.0.1:8000/api/mcp-oauth/callback")

    def test_existing_oauth_credentials_are_discarded_on_configuration_change(self):
        from agent_orchestrator.atlassian_oauth import OAuthStorage
        from mcp.shared.auth import OAuthToken

        app = create_app(FakeAgent())
        with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1)) as client:
            server = client.post("/api/mcp-servers", json={
                "name": "Atlassian", "transport": "http", "url": ATLASSIAN_MCP_URL,
                "auth_type": "atlassian_oauth",
            }).json()
            credentials = OAuthStorage()
            credentials.tokens = OAuthToken(access_token="secret", token_type="Bearer")
            app.state.atlassian_oauth.storage[server["id"]] = credentials
            response = client.put(f"/api/mcp-servers/{server['id']}", json={
                "name": "Atlassian", "transport": "http", "url": "https://example.com/mcp",
                "auth_type": "none",
            })
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(server["id"], app.state.atlassian_oauth.storage)
            self.assertNotIn("secret", response.text)
