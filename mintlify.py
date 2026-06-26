"""Developer-docs search via the Mintlify MCP.

developer.jobmojito.com is a Mintlify site whose hosted MCP exposes a semantic
search tool over the docs (including the imported OpenAPI reference). Rather than
mounting that MCP as a proxy (which would also surface its skill resource and a
separate search tool), we call its search tool directly from inside this server's
unified ``search_documentation`` tool.

Endpoints:
  * Public:  {site}/mcp                 — no credentials.
  * Authed:  {site}/authed/mcp          — OAuth client-credentials. The token is
             obtained from {site}/authed/mcp/oauth/token via
             grant_type=client_credentials and cached/refreshed automatically.
             (Requires "Enable MCP Server" in the Mintlify dashboard.)
"""

from __future__ import annotations

import logging
import re
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


def is_enabled() -> bool:
    """True when a Mintlify developer-docs MCP endpoint is configured."""
    return bool(settings.developer_docs_mcp_url)


# Cache the site-specific search tool name (e.g. search_job_mojito_developer_agent).
_search_tool_name: str | None = None

_MD_LINK = re.compile(r"\[(?P<title>[^\]]{2,200})\]\((?P<url>https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)\]]+")


def _result_text(result) -> str:
    """Extract text from a FastMCP CallToolResult."""
    blocks = getattr(result, "content", None) or []
    parts = [getattr(b, "text", "") for b in blocks if getattr(b, "text", "")]
    if parts:
        return "\n".join(parts)
    data = getattr(result, "data", None) or getattr(result, "structured_content", None)
    return "" if data is None else str(data)


def _parse_items(text: str, limit: int) -> list[dict]:
    """Best-effort extraction of {title, url} pairs from search result text."""
    items: list[dict] = []
    seen: set[str] = set()
    for m in _MD_LINK.finditer(text):
        url = m.group("url").strip()
        if url in seen:
            continue
        seen.add(url)
        items.append({"title": m.group("title").strip(), "url": url, "source": "developer"})
        if len(items) >= limit:
            return items
    if not items:  # fall back to bare URLs
        for m in _BARE_URL.finditer(text):
            url = m.group(0)
            if url in seen:
                continue
            seen.add(url)
            items.append({"title": url, "url": url, "source": "developer"})
            if len(items) >= limit:
                break
    return items


async def search_developer_docs(query: str, limit: int = 8) -> dict:
    """Run the Mintlify developer-docs semantic search and return results.

    Returns {"items": [{title,url,source}], "text": <raw result snippet>}.
    Never raises — returns empty items on any error so the unified search can
    still serve help-center results.
    """
    if not is_enabled():
        return {"items": [], "text": ""}

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    global _search_tool_name
    auth = build_auth() if settings.developer_docs_uses_auth else None
    transport = StreamableHttpTransport(url=settings.developer_docs_mcp_url, auth=auth)
    try:
        async with Client(transport) as client:
            if _search_tool_name is None:
                tools = await client.list_tools()
                for t in tools:
                    name = t.name.lower()
                    if "search" in name and "filesystem" not in name:
                        _search_tool_name = t.name
                        break
            if not _search_tool_name:
                return {"items": [], "text": ""}
            result = await client.call_tool(_search_tool_name, {"query": query})
        text = _result_text(result)
        return {"items": _parse_items(text, limit), "text": text}
    except Exception as exc:
        logger.warning("Mintlify developer-docs search failed: %s", exc)
        return {"items": [], "text": "", "error": str(exc)}
