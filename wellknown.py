"""Unauthenticated operational + discovery routes.

FastMCP custom routes are never wrapped by the auth middleware (by design — the
documented use case is health probes), and they survive a platform-built app
because they're stored on the ``mcp`` object and emitted by ``http_app()``. That
makes them the right home for endpoints that must be reachable without a token.

Routes registered here:

``GET /healthz``
    Liveness probe. Both directories measure availability — Anthropic publishes a
    30-day disconnect-rate SLO (≤5% = "Healthy") on the listing dashboard — so an
    uptime monitor pointed at a cheap endpoint is worth having. Returns 200 and a
    small JSON body; never touches the upstream API.

``GET /.well-known/openai-apps-challenge``
    Domain verification for the OpenAI plugin directory. OpenAI requires this path
    to return **only** that plugin's token, served from the MCP host or a parent
    domain. Returns 404 until ``OPENAI_APPS_CHALLENGE_TOKEN`` is set, so it is
    inert in local dev and in any deploy that hasn't started an OpenAI submission.

``GET /.well-known/mcp/server-card.json``
    Best-effort static description of the server for aggregators that want to show
    a tool list without connecting. Redundant once lazy auth is on (they can just
    call ``tools/list``), but harmless and useful as a fallback for crawlers that
    never speak MCP at all.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from config import settings

logger = logging.getLogger("jobmojito_mcp.wellknown")


def register(mcp, *, version: str, description: str) -> None:
    """Register the unauthenticated routes on the given FastMCP server."""

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Liveness probe — deliberately does no upstream I/O."""
        return JSONResponse(
            {
                "status": "ok",
                "server": "jobmojito-mcp",
                "version": version,
                "auth": "supabase-oauth" if settings.enable_auth else "disabled",
            }
        )

    @mcp.custom_route(
        "/.well-known/openai-apps-challenge", methods=["GET"], include_in_schema=False
    )
    async def openai_apps_challenge(request: Request) -> PlainTextResponse:
        """Domain-verification token for the OpenAI plugin directory.

        Must return the token and nothing else. 404 when unset so the endpoint
        does not advertise itself before a submission is under way.
        """
        token = settings.openai_apps_challenge_token
        if not token:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(token)

    @mcp.custom_route(
        "/.well-known/mcp/server-card.json", methods=["GET"], include_in_schema=False
    )
    async def server_card(request: Request) -> JSONResponse:
        """Static server description for directory crawlers.

        NOTE: there is no ratified schema for this file — it is a convention some
        aggregators (notably Smithery) look for when they cannot list tools over
        MCP. Fields mirror the official MCP Registry ``server.json`` shape so the
        two stay recognisably consistent.
        """
        try:
            tools = await mcp.list_tools()
        except Exception as exc:  # never let a crawler take the endpoint down
            logger.warning("server-card tool listing failed: %s", exc)
            tools = []

        return JSONResponse(
            {
                "name": settings.registry_server_name,
                "title": "JobMojito",
                "description": description,
                "version": version,
                "websiteUrl": settings.marketing_site_url,
                "documentationUrl": settings.documentation_url,
                "privacyPolicyUrl": settings.privacy_policy_url,
                "remotes": [
                    {"type": "streamable-http", "url": f"{settings.base_url}/mcp"}
                ],
                "authentication": {
                    "type": "oauth2",
                    "protectedResourceMetadata": (
                        f"{settings.base_url}/.well-known/oauth-protected-resource/mcp"
                    ),
                },
                "tools": [
                    {
                        "name": tool.name,
                        "title": getattr(tool, "title", None),
                        "description": (tool.description or "").strip()[:400],
                        "annotations": _annotations_dict(tool),
                    }
                    for tool in tools
                ],
            }
        )

    logger.info(
        "Registered unauthenticated routes: /healthz, "
        "/.well-known/openai-apps-challenge (%s), /.well-known/mcp/server-card.json",
        "token set" if settings.openai_apps_challenge_token else "inert — no token set",
    )


def _annotations_dict(tool) -> dict | None:
    """Normalise a tool's annotations (a pydantic model) to a plain dict."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations
    dump = getattr(annotations, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return None
