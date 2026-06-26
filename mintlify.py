"""Federate the Mintlify developer-docs MCP into this server.

developer.jobmojito.com is a Mintlify site whose hosted MCP exposes a semantic
`search` tool and a `query_docs_filesystem` tool over the docs (including the
imported OpenAPI reference). Mintlify supports OAuth **client-credentials**, so
this server can connect headlessly and re-expose those tools to end users.

Flow (https://www.mintlify.com/docs/ai/model-context-protocol):
  POST {site}/authed/mcp/oauth/token
       grant_type=client_credentials&client_id=...&client_secret=...
    -> { access_token, expires_in, refresh_token, ... }
  then connect to {site}/authed/mcp with Authorization: Bearer <access_token>.

The access token is cached and refreshed automatically on expiry.
"""

from __future__ import annotations

import logging
import time
from typing import Generator

import httpx

from config import settings

logger = logging.getLogger("jobmojito_mcp.mintlify")


class MintlifyClientCredentialsAuth(httpx.Auth):
    """httpx auth that obtains/caches a Mintlify MCP access token (client creds)."""

    # Ensure the token response body is fully read before it's handed back.
    requires_response_body = True

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        leeway_seconds: int = 60,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._leeway = leeway_seconds
        self._access_token: str | None = None
        self._expiry: float = 0.0

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.time() < (self._expiry - self._leeway)

    def _token_request(self) -> httpx.Request:
        return httpx.Request(
            "POST",
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

    def _store_token(self, response: httpx.Response) -> None:
        try:
            data = response.json()
        except Exception:
            data = {}
        self._access_token = data.get("access_token")
        try:
            expires_in = float(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._expiry = time.time() + expires_in
        if not self._access_token:
            logger.warning(
                "Mintlify token endpoint returned no access_token (HTTP %s).",
                response.status_code,
            )

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if not self._token_valid():
            self._store_token((yield self._token_request()))
        if self._access_token:
            request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request

    async def async_auth_flow(self, request: httpx.Request):
        if not self._token_valid():
            self._store_token((yield self._token_request()))
        if self._access_token:
            request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request


def build_auth() -> MintlifyClientCredentialsAuth:
    token_url = settings.developer_docs_token_endpoint
    if not (token_url and settings.developer_docs_mcp_client_id and settings.developer_docs_mcp_client_secret):
        raise RuntimeError("Mintlify developer-docs MCP credentials are not fully configured.")
    return MintlifyClientCredentialsAuth(
        token_url=token_url,
        client_id=settings.developer_docs_mcp_client_id,
        client_secret=settings.developer_docs_mcp_client_secret,
    )


def build_proxy():
    """Build a FastMCP proxy for the Mintlify developer-docs MCP (lazy connect).

    Uses client-credentials auth only for an authenticated (`/authed/`) endpoint;
    the public `/mcp` endpoint is mounted without credentials.
    """
    from fastmcp.client.transports import StreamableHttpTransport

    # Non-deprecated proxy API (FastMCP 3.x), with a fallback for older builds.
    try:
        from fastmcp.server import create_proxy
        from fastmcp.server.providers.proxy import ProxyClient
    except ImportError:  # pragma: no cover - older FastMCP
        from fastmcp import FastMCP
        from fastmcp.server.proxy import ProxyClient

        create_proxy = FastMCP.as_proxy  # type: ignore[assignment]

    auth = build_auth() if settings.developer_docs_uses_auth else None
    transport = StreamableHttpTransport(url=settings.developer_docs_mcp_url, auth=auth)
    return create_proxy(ProxyClient(transport), name="JobMojito Developer Docs")
