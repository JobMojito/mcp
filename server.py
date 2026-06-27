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
import os
import re

from fastmcp import FastMCP
from mcp.types import Icon

try:  # FastMCP 3.x location
    from fastmcp.server.providers.openapi import MCPType, RouteMap
except ImportError:  # FastMCP 2.x fallback
    from fastmcp.server.openapi import MCPType, RouteMap

import docs_tools
import prompts
import ui_links
from config import settings
from naming import IGNORED_PATHS, description_hint_for
from openapi_loader import load_openapi_spec
from upstream import build_api_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("jobmojito_mcp")

INSTRUCTIONS = """\
JobMojito MCP server — manage AI-powered hiring (interviews, candidates,
pre-screening, knowledge bases, analytics) on the JobMojito platform, plus search
the documentation. Every action runs as the signed-in user via Supabase OAuth, so
results respect that user's own permissions.

AUTHENTICATION (required for EVERY tool):
You must be signed in via Supabase OAuth to use ANY tool — including
`search_documentation` and `get_documentation`. There is no anonymous access. If a
tool call returns an authentication error (e.g. 401 / invalid_token), it means you
are not signed in: tell the user to connect/authorize this server (log in through
the Supabase login prompt) and then retry. Do not attempt to bypass or work around
authentication.

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
• Admin UI links: get_admin_ui_link (open a candidate/interview/result in the app)
• Interview (create/manage): create_interview, create_interview_from_questions,
  get_interview_definition, set_interview_state, generate_interview_url,
  get_interview_result_details, request_another_interview_attempt,
  register_users_for_interview
• Interview reports: generate_interview_report
• Pre-screening: upsert_pre_screening, pre_screen_resume_text,
  pre_screen_resume_binary
• Knowledge base: upload_knowledge_base_document
• Merchant lists (read-only): list_interviews, list_candidates,
  list_interview_results, list_avatars, list_sub_merchants, get_merchant_analytics

Typical flows:
- "Set up an interview for a role" → (optionally search_documentation for field
  meanings) → create_interview → generate_interview_url / register_users_for_interview.
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
        "Configuring Supabase OAuth (project=%s, alg=%s, base_url=%s)",
        settings.supabase_project_url,
        settings.supabase_jwt_algorithm,
        settings.base_url,
    )
    if "localhost" in settings.base_url or "127.0.0.1" in settings.base_url:
        logger.warning(
            "BASE_URL is %s — for a deployed server this MUST be the public URL "
            "(e.g. https://<name>.fastmcp.app), or OAuth discovery/token validation "
            "will fail with 401 invalid_token.",
            settings.base_url,
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


def _ignored_paths() -> set[str]:
    """Endpoints excluded from the MCP: built-in list + IGNORED_TOOL_PATHS env."""
    paths = set(IGNORED_PATHS)
    extra = os.environ.get("IGNORED_TOOL_PATHS", "")
    paths |= {p.strip() for p in extra.split(",") if p.strip()}
    return paths


def build_server() -> FastMCP:
    spec = load_openapi_spec()
    client = build_api_client()
    auth = _build_auth()

    ignored = _ignored_paths()
    # Exclude ignored endpoints first, then map everything else to a Tool.
    route_maps = [
        RouteMap(pattern=rf"^{re.escape(p)}$", mcp_type=MCPType.EXCLUDE)
        for p in sorted(ignored)
    ] + [RouteMap(mcp_type=MCPType.TOOL)]

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        # --- Branding (reported to clients in the MCP `initialize` response) ---
        name="JobMojito",
        version="1.0.0",
        website_url="https://www.jobmojito.com",
        icons=[
            Icon(
                src="https://www.jobmojito.com/favicon.ico",
                mimeType="image/x-icon",
                sizes=["any"],
            )
        ],
        instructions=INSTRUCTIONS,
        auth=auth,
        # Expose every endpoint (incl. GET lists) as Tools, except ignored ones.
        route_maps=route_maps,
        mcp_component_fn=_customize_component,
        tags={"jobmojito"},
    )
    if ignored:
        logger.info("Excluded %d endpoint(s) from tools: %s", len(ignored), ", ".join(sorted(ignored)))

    # Documentation tools (live, single-source). A single `search_documentation`
    # tool queries the help center (Featurebase) and developer docs (Mintlify
    # semantic search) in parallel — no separate developer-docs tool is mounted,
    # which also avoids exposing the Mintlify skill resource.
    docs_tools.register(mcp)

    # Admin UI deep-link tool (candidate / interview / result).
    ui_links.register(mcp)

    # User-invocable workflow prompts (cookbook starters).
    prompts.register(mcp)
    logger.info(
        "Registered workflow prompts: create_interview, review_candidate, "
        "screen_resume, invite_candidates."
    )

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

    api_ops = sum(
        1
        for path, item in spec.get("paths", {}).items()
        if isinstance(item, dict) and path not in ignored
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    )
    logger.info(
        "JobMojito MCP server built successfully: %d API tools + documentation "
        "tools (search_documentation, get_documentation) + get_admin_ui_link.",
        api_ops,
    )
    return mcp


mcp = build_server()


if __name__ == "__main__":
    # Local dev convenience (honors $PORT). Horizon ignores this block and serves
    # the `mcp` object directly via its own runner.
    import os

    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
