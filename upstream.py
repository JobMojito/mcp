"""HTTP client used by the generated API tools to call the JobMojito API.

Key responsibility: forward the *authenticated end-user's* Supabase JWT to the
upstream API on every request. When a user connects through Supabase OAuth, each
MCP request carries their access token; we propagate that exact token as
``Authorization: Bearer <jwt>`` so JobMojito enforces the user's own permissions
(rather than a single shared service token).

Supabase Edge Functions also commonly require the project ``apikey`` header — we
add it when ``SUPABASE_ANON_KEY`` is configured.
"""

from __future__ import annotations

import logging
from typing import Generator

import httpx

from config import settings

logger = logging.getLogger("jobmojito_mcp.upstream")


def _current_bearer_token() -> str | None:
    """Best-effort fetch of the current request's Supabase access token.

    Returns the raw JWT string, or None if there is no authenticated context
    (e.g. auth disabled for local testing, or a non-request code path).
    """
    try:
        # Imported lazily so the module also works with auth disabled / outside
        # a request context.
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        return None

    if token is not None:
        # AccessToken exposes the raw token string on `.token`.
        raw = getattr(token, "token", None)
        if raw:
            return raw

    # Local development fallback: a manually supplied Supabase token so API tools
    # can be exercised without the full OAuth flow. Never set this in production.
    if settings.dev_bearer_token:
        return settings.dev_bearer_token
    return None


class SupabaseTokenForwardAuth(httpx.Auth):
    """httpx auth flow that injects the current user's Supabase JWT per request."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        bearer = _current_bearer_token()
        if bearer:
            request.headers["Authorization"] = f"Bearer {bearer}"
        else:
            logger.debug("No authenticated token in context for %s", request.url)
        yield request


def build_api_client() -> httpx.AsyncClient:
    """Create the AsyncClient FastMCP uses for all generated API tools."""
    headers: dict[str, str] = {"Accept": "application/json"}
    if settings.supabase_anon_key:
        # Edge Functions gateway often requires the anon key as `apikey`.
        headers["apikey"] = settings.supabase_anon_key

    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers=headers,
        auth=SupabaseTokenForwardAuth(),
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
    )
