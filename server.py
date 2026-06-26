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
JobMojito MCP server — manage AI-powered hiring (interviews, candidates,
pre-screening, knowledge bases, analytics) on the JobMojito platform, plus search
the documentation. Every action runs as the signed-in user via Supabase OAuth, so
results respect that user's own permissions.

HOW TO USE THIS SERVER (read this before calling tools):
1. To understand how something works — an endpoint, a field, a workflow, what an
   input means, or what a tool will do — call `search_documentation` FIRST, then
   `get_documentation(url)` to read the page. Do NOT call action tools
   speculatively just to discover their behavior or required inputs.
2. `search_documentation` is the single docs entry point: one call searches both
   the developer/API reference and the help center in parallel. You don't need to
   pick a source or find a separate search tool.
3. For a multi-step workflow (e.g. create an interview → invite candidates →
   review results), search the documentation for a step-by-step "cookbook" or
   guide and follow it, rather than chaining tools by trial and error.
4. Only call an action tool once you know which one you need and what inputs it
   expects. Prefer the read-only "Merchant lists" tools to look things up before
   creating or changing anything.

TOOLS BY CATEGORY (tool descriptions are prefixed with these labels):
• Documentation: search_documentation, get_documentation
• Interview (create/manage): create_interview, create_interview_from_questions,
  create_interview_for_candidate, get_interview_definition, set_interview_state,
  generate_interview_url, get_interview_result_details,
  request_another_interview_attempt, invite_users, register_users_for_interview
• Interview reports: generate_interview_report
• Pre-screening: upsert_pre_screening, pre_screen_resume_text,
  pre_screen_resume_binary
• Knowledge base: upload_knowledge_base_document
• Merchant lists (read-only): list_interviews, list_candidates,
  list_interview_results, list_avatars, list_sub_merchants, get_merchant_analytics

Typical flows:
- "Set up an interview for a role" → (optionally search_documentation for field
  meanings) → create_interview → generate_interview_url / invite_users.
- "Review a candidate's result" → list_interview_results → get_interview_result_details
  → generate_interview_report.
- "Screen a résumé" → upsert_pre_screening → pre_screen_resume_text/binary.
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
    """Categorize and annotate each generated API tool.

    Prefixes the description with its OpenAPI category (e.g. "[Interview]") and
    adds the category as an MCP tag, so tools are grouped/labeled even though MCP
    exposes a flat tool list.
    """
    route_tags = list(getattr(route, "tags", None) or [])
    category = route_tags[0] if route_tags else None
    hint = description_hint_for(route.method, route.path)
    existing = (component.description or "").strip()

    prefix_parts = []
    if category:
        prefix_parts.append(f"[{category}]")
    if hint:
        prefix_parts.append(hint)
    prefix = " ".join(prefix_parts)
    if prefix:
        component.description = f"{prefix}\n\n{existing}".strip() if existing else prefix

    component.tags.add("jobmojito-api")
    for tag in route_tags:
        component.tags.add(tag)


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

    # Documentation tools (live, single-source). A single `search_documentation`
    # tool queries the help center (Featurebase) and developer docs (Mintlify
    # semantic search) in parallel — no separate developer-docs tool is mounted,
    # which also avoids exposing the Mintlify skill resource.
    docs_tools.register(mcp)
    if settings.developer_docs_mcp_url:
        logger.info(
            "Developer-docs search via Mintlify at %s (%s).",
            settings.developer_docs_mcp_url,
            "client-credentials auth" if settings.developer_docs_uses_auth else "public, no auth",
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
