"""JobMojito MCP server (FastMCP).

Entrypoint object: ``mcp`` — point Prefect Horizon at ``server.py:mcp``.

What this server exposes:
  * 21 API tools auto-generated from the live JobMojito OpenAPI spec
    (all endpoints surfaced as Tools, with curated names).
  * 2 documentation tools that read developer + help docs live (single-source).
  * A Supabase OAuth consent page route (for end-user auth).

End-user auth uses Supabase OAuth (FastMCP SupabaseProvider). The authenticated
user's Supabase JWT is forwarded to the JobMojito API on every tool call.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

try:  # FastMCP 3.x location
    from fastmcp.server.providers.openapi import MCPType, RouteMap
except ImportError:  # FastMCP 2.x fallback
    from fastmcp.server.openapi import MCPType, RouteMap

import docs_tools
from config import settings
from naming import description_hint_for
from openapi_loader import load_openapi_spec
from upstream import build_api_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("jobmojito_mcp")

INSTRUCTIONS = """\
JobMojito MCP server. Provides tools to manage AI interviews, candidates,
pre-screening, knowledge bases and merchant analytics via the JobMojito API,
plus documentation search.

Workflow tips:
- Use `search_documentation` then `get_documentation` to learn how a feature,
  endpoint, or field works before calling an API tool.
- API tools act on behalf of the signed-in user (their Supabase permissions apply).
"""


def _build_auth():
    """Construct the Supabase OAuth provider, or None when auth is disabled."""
    if not settings.enable_auth:
        logger.warning("ENABLE_AUTH=false — server is running WITHOUT authentication.")
        return None
    if not settings.supabase_project_url:
        raise RuntimeError(
            "SUPABASE_PROJECT_URL is required when ENABLE_AUTH=true. "
            "Set it (e.g. https://<ref>.supabase.co) or set ENABLE_AUTH=false for local testing."
        )
    from fastmcp.server.auth.providers.supabase import SupabaseProvider

    logger.info(
        "Configuring Supabase OAuth (project=%s, alg=%s)",
        settings.supabase_project_url,
        settings.supabase_jwt_algorithm,
    )
    return SupabaseProvider(
        project_url=settings.supabase_project_url,
        base_url=settings.base_url,
        algorithm=settings.supabase_jwt_algorithm,
    )


def _customize_component(route, component) -> None:
    """Append a short selection hint to each generated API tool's description."""
    hint = description_hint_for(route.method, route.path)
    if hint:
        existing = (component.description or "").strip()
        component.description = f"{hint}\n\n{existing}".strip() if existing else hint
    component.tags.add("jobmojito-api")


def build_server() -> FastMCP:
    spec = load_openapi_spec()
    client = build_api_client()
    auth = _build_auth()

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="JobMojito",
        instructions=INSTRUCTIONS,
        auth=auth,
        # Decision: expose every endpoint (incl. GET lists) as Tools.
        route_maps=[RouteMap(mcp_type=MCPType.TOOL)],
        mcp_component_fn=_customize_component,
        tags={"jobmojito"},
    )

    # Documentation tools (live, single-source). Help center is always covered;
    # developer docs are handled by the mounted Mintlify MCP when federated.
    docs_tools.register(mcp)

    # Federate the Mintlify developer-docs MCP (semantic search + docs filesystem)
    # via OAuth client-credentials, when credentials are configured.
    if settings.federate_developer_docs:
        try:
            import mintlify

            mcp.mount(mintlify.build_proxy())
            logger.info(
                "Mounted Mintlify developer-docs MCP from %s (%s).",
                settings.developer_docs_mcp_url,
                "client-credentials auth" if settings.developer_docs_uses_auth else "public, no auth",
            )
        except Exception as exc:  # don't let doc federation break server startup
            logger.warning("Could not mount Mintlify developer-docs MCP: %s", exc)
    else:
        logger.info(
            "Mintlify developer-docs federation disabled (DEVELOPER_DOCS_MCP_URL empty)."
        )

    # The OAuth consent screen is handled entirely by Supabase / the JobMojito app
    # (Supabase Site URL = app.jobmojito.com + OAUTH_CONSENT_PATH). This MCP server
    # is only the OAuth resource server and does not serve a consent page.
    logger.info(
        "OAuth consent handled by Supabase at %s%s.",
        settings.site_url,
        settings.oauth_consent_path,
    )

    logger.info("JobMojito MCP server built successfully.")
    return mcp


mcp = build_server()


if __name__ == "__main__":
    # Local dev convenience. Horizon ignores this block and serves `mcp` directly.
    mcp.run(transport="http", host="0.0.0.0", port=8000)
