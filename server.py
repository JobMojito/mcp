"""JobMojito MCP server (FastMCP).

Entrypoint object: ``mcp`` — point Prefect Horizon at ``server.py:mcp``.

What this server exposes:
  * API tools auto-generated from the live JobMojito OpenAPI spec (interviews,
    candidates, knowledge base, analytics), with curated names, titles and MCP
    annotations.
  * 2 documentation tools that read developer + help docs live (single-source).
  * A merchant picker (``jobmojito_configuration``) and its text fallback.
  * Unauthenticated operational routes (health probe, directory verification).

End-user auth uses Supabase OAuth (FastMCP SupabaseProvider). The authenticated
user's Supabase JWT is forwarded to the JobMojito API on every tool call, so
every action runs with that user's own permissions.

Capability discovery (``initialize`` / ``tools/list``) is servable without a
token — see ``lazy_auth.py`` for why and how. Every tool call still requires a
verified token.
"""

from __future__ import annotations

import logging
import os
import re

from fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations

try:  # FastMCP 3.x location
    from fastmcp.server.providers.openapi import MCPType, OpenAPITool, RouteMap
except ImportError:  # FastMCP 2.x fallback
    from fastmcp.server.openapi import MCPType, OpenAPITool, RouteMap

import docs_tools
import merchants
import wellknown
from config import settings
from naming import IGNORED_PATHS, curated_defaults, fallback_meta, meta_for
from openapi_loader import load_openapi_spec
from upstream import build_api_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("jobmojito_mcp")

#: Kept in sync with ``pyproject.toml`` and ``server.json``. Directories treat the
#: version as the release identity, so bump all three together.
SERVER_VERSION = "1.1.1"

#: <=100 characters — the hard cap on ``description`` in the official MCP Registry
#: server.json schema, and the tightest length constraint of any listing surface.
SHORT_DESCRIPTION = (
    "Run AI interviews, manage candidates and read hiring analytics on JobMojito."
)

#: Metadata for tools that aren't generated from the OpenAPI spec and therefore
#: don't flow through ``naming.TOOL_META``. Anything omitted here is treated as
#: state-changing (see ``ToolMetadataBackfillMiddleware``) — safe by default.
TOOL_METADATA_OVERRIDES: dict[str, dict] = {
    "jobmojito_configuration": {
        "title": "Choose a JobMojito merchant",
        "readOnlyHint": True,
        "openWorldHint": False,
    },
    "list_my_merchants": {
        "title": "List merchants you can act as",
        "readOnlyHint": True,
        "openWorldHint": False,
    },
    "search_merchants": {
        "title": "Search sub-merchants",
        "readOnlyHint": True,
        "openWorldHint": False,
    },
}

INSTRUCTIONS = """\
JobMojito MCP server — manage AI-powered hiring (interviews, candidates,
invitations, knowledge bases, analytics) on the JobMojito platform, plus search
the documentation. Every action runs as the signed-in user, so results respect
that user's own permissions.

RESPONSIBLE USE (applies to every result this server returns):
JobMojito produces assistive output for hiring workflows. Interview scores,
transcripts, summaries and reports are decision-support material — they are not
hiring decisions. Present them as input for a qualified human reviewer, and never
describe a candidate as accepted, rejected, or ranked as final by this system.
When a user is preparing candidate-facing material, remind them that candidates
should be told AI is used in the process.

AUTHENTICATION:
Capability discovery works without a login, but every tool call requires the user
to have authorized this server. If a call fails with an authentication error (401
/ invalid_token), retry that exact call once — the server answers the retry with
a challenge that makes the client refresh its token, and the call then usually
succeeds without the user doing anything. Only if the second attempt fails too
should you ask the user to reconnect and authorize this server. Never retry an
auth failure with different arguments; it is not an input problem.

HOW TO USE THIS SERVER:
1. To understand how something works — an endpoint, a field, a workflow, what an
   input means — `search_documentation` is usually faster and more reliable than
   experimenting. It's the single docs entry point: one call searches both the
   developer/API reference and the help center in parallel. Then
   `get_documentation(url)` reads a page in full.
2. For a multi-step workflow (create an interview → invite candidates → review
   results), the documentation has step-by-step cookbooks worth following.
3. The read-only tools (`list_*`, `get_*`) are the safe way to look things up
   before creating or changing anything.
4. Merchant selection: many tools accept a `merchant_id`. When one is needed and
   none has been selected — or the user wants to switch merchants —
   `jobmojito_configuration` renders an interactive searchable picker, which is a
   better experience than asking the user to type a name. `list_my_merchants`
   returns the same options as text for clients that cannot render UI. After a
   merchant is chosen, pass `merchant_id=<chosen id>` on subsequent calls; omit it
   for the user's own account.

IDENTIFIERS & ADMIN LINKS:
Ids are easy to mix up — the same interview is `interview_def_set_id` (on create),
`position_id` (get/set-state), and `interview_id` (register/token); results use
`interview_result_id`, NOT the result row's `id`. There is no link-building tool.
For the full id-to-field map, where each id comes from, and admin link patterns:
get_documentation("https://developer.jobmojito.com/mcp/identifiers").

TOOLS BY CATEGORY (tool descriptions are prefixed with these labels):
• Documentation: search_documentation, get_documentation
• Configuration / merchants: jobmojito_configuration (searchable picker),
  list_my_merchants (text equivalent, supports a `search` filter)
• Interview (create/manage): create_interview, create_interview_from_questions,
  get_interview_definition, set_interview_state, request_another_interview_attempt
• Invitations: register_users_for_interview (register candidates and get their
  personal interview links), generate_interview_url (a signed link to share)
• Results & reports: get_interview_result_details, generate_interview_report
• Knowledge base: upload_knowledge_base_document
• Merchant lists (read-only): list_interviews, list_candidates,
  list_interview_results, list_avatars, list_sub_merchants, get_merchant_analytics,
  get_merchant_status

Typical flows:
- "Set up an interview for a role" → create_interview → generate_interview_url or
  register_users_for_interview.
- "Invite candidates to an interview" → register_users_for_interview (per-candidate
  links) or generate_interview_url (one shareable link).
- "Review a candidate's result" → list_interview_results → get_interview_result_details
  → generate_interview_report.
"""


def _server_icon() -> Icon:
    """The icon advertised to MCP clients.

    ``sizes`` is only emitted when SERVER_ICON_SIZES is set: an incorrect size
    hint is worse than none, since clients use it to pick between variants.
    """
    kwargs: dict = {
        "src": settings.server_icon_url,
        "mimeType": settings.server_icon_mime,
    }
    if settings.server_icon_sizes:
        kwargs["sizes"] = list(settings.server_icon_sizes)
    return Icon(**kwargs)


def _validate_public_identity() -> None:
    """Fail fast / warn loudly when BASE_URL doesn't match the served URL.

    ``BASE_URL`` is not cosmetic: it becomes the OAuth *resource identifier*
    advertised at ``/.well-known/oauth-protected-resource/mcp``. The MCP
    authorization spec — and Anthropic's connector review explicitly — require
    that value to equal the URL the client connected to, character for character.
    If the server moves to a new hostname and BASE_URL is left behind, discovery
    still returns 200 with a stale `resource`, clients fail token validation, and
    nothing in the logs says why. So we say it here.
    """
    if not settings.enable_auth:
        return
    if settings.base_url_looks_local:
        logger.warning(
            "BASE_URL is %s — for a deployed server this MUST be the public base "
            "URL with no /mcp suffix (e.g. https://mcp.jobmojito.com), or OAuth "
            "discovery advertises the wrong resource and clients get 401 "
            "invalid_token.",
            settings.base_url,
        )
        return
    logger.info(
        "Public identity: MCP endpoint=%s | OAuth resource=%s | PRM=%s",
        settings.mcp_endpoint,
        settings.mcp_endpoint,
        f"{settings.base_url}/.well-known/oauth-protected-resource"
        f"{settings.mcp_path}",
    )
    logger.info(
        "Verify after deploy:  curl -s %s/.well-known/oauth-protected-resource%s"
        "   # `resource` must equal %s",
        settings.base_url,
        settings.mcp_path,
        settings.mcp_endpoint,
    )


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

    from lazy_auth import lazy_auth_provider_class

    logger.info(
        "Configuring Supabase OAuth (project=%s, alg=%s, base_url=%s, lazy_auth=%s)",
        settings.supabase_project_url,
        settings.supabase_jwt_algorithm,
        settings.base_url,
        settings.enable_lazy_auth,
    )

    provider_class = lazy_auth_provider_class(SupabaseProvider)
    scopes = list(settings.oauth_scopes_supported)
    return provider_class(
        project_url=settings.supabase_project_url,
        base_url=settings.base_url,
        algorithm=settings.supabase_jwt_algorithm,
        # Advertised in the RFC 9728 protected-resource metadata so clients can
        # request the right scopes up front instead of guessing.
        scopes_supported=scopes,
        resource_name="JobMojito",
        token_verifier=_build_token_verifier(),
        # --- lazy-auth extras (see lazy_auth.py) ---
        mcp_path=settings.mcp_path,
        advertise_scope=" ".join(scopes) if scopes else None,
        lazy_auth_enabled=settings.enable_lazy_auth,
        resource_metadata_url=settings.protected_resource_metadata_url,
    )


def _build_token_verifier():
    """The token verifier, or None to accept ``SupabaseProvider``'s default.

    Returning a verifier here replaces the stateless ``JWTVerifier`` that
    ``SupabaseProvider`` would build, with one that also checks the Supabase
    *session* behind the JWT — the same thing the JobMojito Edge Functions check.
    Without it, this server accepts tokens the API rejects, and because a tool
    error is an HTTP 200 the client never learns to re-authenticate. See
    ``session_verifier.py`` for the full reasoning and the fail-open posture.

    The JWT parameters below must mirror ``SupabaseProvider.__init__`` exactly;
    they are the contract with Supabase Auth, not tunables.
    """
    if not settings.enable_session_check:
        logger.warning(
            "SUPABASE_SESSION_CHECK=false — tokens are verified statelessly only. "
            "A revoked or signed-out Supabase session will pass this gate and be "
            "rejected by the JobMojito API instead."
        )
        return None

    from session_verifier import SupabaseSessionVerifier

    project_url = settings.supabase_project_url
    logger.info(
        "Supabase session check enabled (ttl=%ss): every token is resolved to a "
        "live session before any tool runs, so a dead session gets a 401 "
        "challenge instead of a 200-wrapped tool error.",
        settings.session_check_ttl_seconds,
    )
    return SupabaseSessionVerifier(
        project_url=project_url,
        anon_key=settings.supabase_anon_key,
        session_ttl_seconds=settings.session_check_ttl_seconds,
        jwks_uri=f"{project_url}/auth/v1/.well-known/jwks.json",
        issuer=f"{project_url}/auth/v1",
        algorithm=settings.supabase_jwt_algorithm,
        audience="authenticated",
    )


def _customize_component(route, component) -> None:
    """Categorize and annotate each generated API tool.

    Three things happen here, and all three are load-bearing for directory review:

    1. The description is prefixed with its OpenAPI category (e.g. "[Interview]")
       and a curated one-line hint, so the model picks the right tool.
    2. A human-readable ``title`` is set (both the top-level MCP ``title`` and
       ``annotations.title`` — clients read one or the other depending on age).
    3. MCP annotations are set from ``naming.TOOL_META``. Anthropic and OpenAI both
       reject servers whose tools lack ``title`` plus ``readOnlyHint``/
       ``destructiveHint``; a read-only tool also runs without a per-call
       confirmation prompt, which is a real UX win for the ``list_*`` tools.

    CAUTION: FastMCP *swallows* exceptions raised in this callback and registers
    the tool uncustomized, with only a log warning. A typo here therefore fails
    silently in production. ``tests/test_listing_readiness.py`` asserts the result
    rather than trusting the logs — keep it that way.
    """
    route_tags = list(getattr(route, "tags", None) or [])
    category = route_tags[0] if route_tags else None
    meta = meta_for(route.method, route.path) or fallback_meta(route.method, route.path)
    existing = (component.description or "").strip()

    prefix_parts = []
    if category:
        prefix_parts.append(f"[{category}]")
    if meta.hint:
        prefix_parts.append(meta.hint)
    prefix = " ".join(prefix_parts)
    if prefix:
        component.description = f"{prefix}\n\n{existing}".strip() if existing else prefix

    if isinstance(component, OpenAPITool):
        component.title = meta.title
        component.annotations = ToolAnnotations(**meta.annotations())
        component.meta = {
            "jobmojito": {
                "category": category or "general",
                "http_method": route.method.upper(),
            }
        }

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
    _validate_public_identity()

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
        version=SERVER_VERSION,
        website_url=settings.marketing_site_url,
        icons=[_server_icon()],
        instructions=INSTRUCTIONS,
        auth=auth,
        # Expose every endpoint (incl. GET lists) as Tools, except ignored ones.
        route_maps=route_maps,
        mcp_component_fn=_customize_component,
        tags={"jobmojito"},
        # Output validation stays ON so the agent gets typed results. The spec's
        # response fields are relaxed to nullable in openapi_loader (the API returns
        # null for some non-nullable-declared strings), so real responses validate.
        validate_output=True,
    )
    if ignored:
        logger.info("Excluded %d endpoint(s) from tools: %s", len(ignored), ", ".join(sorted(ignored)))

    if settings.server_icon_url.endswith(".ico"):
        logger.warning(
            "SERVER_ICON_URL is a .ico (%s). MCP clients accept it, but the "
            "Anthropic and OpenAI directories want a square PNG/SVG logo "
            "(>=48px, ideally 512x512). Publish one and set SERVER_ICON_URL / "
            "SERVER_ICON_MIME before submitting a listing.",
            settings.server_icon_url,
        )

    # --- Middleware. Order matters: registration order is execution order, so the
    # logger wraps everything and records failures the guards raise. ---
    from middleware import (
        CuratedDefaultsMiddleware,
        OutputValidationErrorMiddleware,
        ResultSizeGuardMiddleware,
        ToolCallLoggingMiddleware,
        ToolMetadataBackfillMiddleware,
        UpstreamErrorMiddleware,
    )

    # Server-side tool-call logging (name + arg keys + outcome/timing). Helps
    # diagnose app-level failures incl. output-schema validation (-32602). It does
    # NOT see transport-layer 400/404s (missing/expired Mcp-Session-Id), which are
    # rejected before any tool runs.
    mcp.add_middleware(ToolCallLoggingMiddleware())
    # Put curated page sizes on the wire. FastMCP only sends arguments the model
    # supplied, so an OpenAPI `default` is advertised but never transmitted —
    # without this, list_avatars would still fetch the API's 50 rows and blow the
    # result limit on a plain call. Registered first so the logged arguments and
    # the size guard both see the values actually used.
    mcp.add_middleware(CuratedDefaultsMiddleware(curated_defaults()))
    # Turns raw "HTTP error 403: Forbidden - {...}" into a cause + next step, so the
    # model stops permuting arguments against a permissions problem.
    mcp.add_middleware(UpstreamErrorMiddleware())
    # Refuse oversized results with pagination guidance rather than letting the
    # client silently truncate them.
    mcp.add_middleware(ResultSizeGuardMiddleware(settings.max_tool_result_chars))
    # Rewrites the SDK's path-less "Output validation error: <msg>" into one that
    # names the offending field(s), so an agent knows exactly what didn't match.
    # Registered after logging so the logger still records the failed call.
    mcp.add_middleware(OutputValidationErrorMiddleware())
    # Safety net: any tool registered outside the OpenAPI path (docs tools, the
    # merchant picker, future MCP App providers) still gets a title and safety
    # hints, so one forgotten registration can't fail a directory review.
    mcp.add_middleware(ToolMetadataBackfillMiddleware(overrides=TOOL_METADATA_OVERRIDES))

    # Documentation tools (live, single-source). A single `search_documentation`
    # tool queries the help center (Featurebase) and developer docs (Mintlify
    # semantic search) in parallel — no separate developer-docs tool is mounted,
    # which also avoids exposing the Mintlify skill resource.
    docs_tools.register(mcp)

    # Merchant selection (list_my_merchants + clickable picker for UI clients).
    merchants.register(mcp)

    # Unauthenticated operational + directory-verification routes.
    wellknown.register(mcp, version=SERVER_VERSION, description=SHORT_DESCRIPTION)

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
        "tools (search_documentation, get_documentation).",
        api_ops,
    )

    # PostHog MCP analytics + exception reporting. Deliberately last: the adapter
    # wraps the tool manager and the list_tools handler, so every tool (OpenAPI,
    # docs, merchants) must already be registered. No-op without POSTHOG_API_KEY.
    import posthog_analytics

    posthog_analytics.install(
        mcp,
        api_key=settings.posthog_api_key,
        host=settings.posthog_host,
        debug=settings.posthog_debug,
        enable_intent=settings.posthog_enable_intent,
    )

    return mcp


mcp = build_server()


if __name__ == "__main__":
    # Local dev convenience (honors $PORT). Horizon ignores this block and serves
    # the `mcp` object directly via its own runner.
    import os

    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
