"""Regression tests for the things directory review actually rejects.

Run with:  ENABLE_AUTH=false pytest -q

Why these exist, specifically:

* FastMCP **swallows exceptions raised inside ``mcp_component_fn``** and registers
  the tool uncustomized with only a log warning. So a typo in
  ``server._customize_component`` would silently ship tools with no annotations —
  the single most-cited rejection reason at both Anthropic and OpenAI. Asserting
  the built server (not the source) is the only way to catch that.
* ``lazy_auth`` depends on two undocumented FastMCP internals. If an upgrade
  breaks either one, the server silently becomes fully gated (empty tool lists on
  Smithery/Glama) or wide open. These tests fail loudly instead.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Tools that must be marked read-only: they only ever GET existing records, and
# marking them read-only is what lets clients run them without a confirmation
# prompt on every call.
EXPECTED_READ_ONLY = {
    "get_interview_definition",
    "get_interview_result_details",
    "list_interviews",
    "list_candidates",
    "list_interview_results",
    "list_avatars",
    "list_sub_merchants",
    "get_merchant_analytics",
    "get_merchant_credit_usage",
    "get_merchant_status",
    "list_languages",
    "list_catalogue_directories",
    "get_catalogue_directory",
    "search_documentation",
    "get_documentation",
}


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Tool metadata — the Anthropic/OpenAI review criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_tool_has_annotations_and_a_title():
    """Anthropic: every tool needs a title AND readOnlyHint or destructiveHint."""
    import server

    missing_title: list[str] = []
    missing_hint: list[str] = []
    for tool in await server.mcp.list_tools():
        ann = _annotations(tool)
        if not (getattr(tool, "title", None) or ann.get("title")):
            missing_title.append(tool.name)
        if not (ann.get("readOnlyHint") is True or ann.get("destructiveHint") is True):
            missing_hint.append(tool.name)

    assert not missing_title, f"tools without a title: {sorted(missing_title)}"
    assert not missing_hint, (
        "tools without readOnlyHint=true or destructiveHint=true: "
        f"{sorted(missing_hint)}"
    )


@pytest.mark.asyncio
async def test_read_only_tools_are_marked_read_only():
    """A read tool wrongly marked destructive (or vice versa) is a rejection reason."""
    import server

    tools = {t.name: _annotations(t) for t in await server.mcp.list_tools()}
    for name in EXPECTED_READ_ONLY:
        assert name in tools, f"expected tool {name} is missing"
        assert tools[name].get("readOnlyHint") is True, f"{name} should be read-only"
        assert tools[name].get("destructiveHint") is not True, (
            f"{name} is read-only but marked destructive"
        )

    # Everything that is NOT in the read-only set must be marked destructive, so a
    # client always asks before it runs. Merchant/config helpers are exempt: they
    # are local UI/state helpers, not API writes.
    exempt = {"jobmojito_configuration", "list_my_merchants"}
    for name, ann in tools.items():
        if name in EXPECTED_READ_ONLY or name in exempt:
            continue
        assert ann.get("destructiveHint") is True, (
            f"{name} changes state but is not marked destructiveHint=true"
        )


@pytest.mark.asyncio
async def test_tool_names_within_length_limit():
    """Anthropic caps tool names at 64 characters."""
    import server
    from naming import MAX_TOOL_NAME_LENGTH

    too_long = [
        t.name for t in await server.mcp.list_tools() if len(t.name) > MAX_TOOL_NAME_LENGTH
    ]
    assert not too_long, f"tool names over {MAX_TOOL_NAME_LENGTH} chars: {too_long}"


@pytest.mark.asyncio
async def test_every_tool_has_a_description():
    import server

    undocumented = [
        t.name for t in await server.mcp.list_tools() if not (t.description or "").strip()
    ]
    assert not undocumented, f"tools with no description: {undocumented}"


def test_every_exposed_tool_has_an_annotation_justification():
    """OpenAI asks for a written justification per annotation at submission."""
    import naming

    justifications = naming.annotation_justifications()
    assert justifications, "no justifications generated"
    blank = [name for name, why in justifications.items() if len(why.strip()) < 40]
    assert not blank, f"justifications too thin to submit: {blank}"


def test_fallback_annotations_for_uncurated_endpoints():
    """A brand-new endpoint must still ship with usable annotations."""
    import naming

    read = naming.fallback_meta("GET", "/some-new-list")
    assert read.read_only is True and read.destructive is False

    write = naming.fallback_meta("POST", "/some-new-action")
    assert write.destructive is True and write.read_only is False


# ---------------------------------------------------------------------------
# Instructions & description copy
# ---------------------------------------------------------------------------


def test_short_description_fits_the_registry_limit():
    """The MCP Registry server.json schema caps `description` at 100 chars."""
    import server

    assert len(server.SHORT_DESCRIPTION) <= 100


def test_instructions_avoid_imperative_tool_calling_language():
    """Anthropic rejects descriptions that command Claude to call other tools."""
    import server

    banned = re.findall(
        r"\bALWAYS call\b|\bMUST call\b|\bcall .{0,30} FIRST\b",
        server.INSTRUCTIONS,
    )
    assert not banned, f"prompt-injection-flavoured phrasing in INSTRUCTIONS: {banned}"


def test_instructions_carry_the_responsible_use_language():
    """Hiring is a named high-risk use case in both vendors' usage policies.

    They require human review before a decision is finalised, and disclosure to
    affected individuals. Keeping that in the server instructions means it reaches
    the model on every session, not just the privacy policy PDF.
    """
    import server

    text = server.INSTRUCTIONS.lower()
    assert "not hiring decisions" in text or "decision-support" in text
    assert "human reviewer" in text


# ---------------------------------------------------------------------------
# Version identity across the three places it is declared
# ---------------------------------------------------------------------------


def test_pyproject_derives_the_version_from_the_code():
    """pyproject must not carry its own copy of the version.

    `server.py:SERVER_VERSION` is the one hand-edited version in this repo.
    setuptools reads it by AST via `[tool.setuptools.dynamic]`, so re-adding a
    literal `version = "..."` here would reintroduce exactly the drift this
    setup removes.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()

    assert re.search(r"^dynamic = \[.*\bversion\b", pyproject, re.M), (
        "pyproject should declare a dynamic version"
    )
    assert re.search(r'version = \{attr = "server\.SERVER_VERSION"\}', pyproject), (
        "the dynamic version must resolve from server.SERVER_VERSION"
    )
    assert not re.search(r'^version = "', pyproject, re.M), (
        "pyproject must not hard-code a version — it derives from server.py"
    )


def test_server_version_is_a_literal():
    """The AST read only works on a plain string literal.

    Give SERVER_VERSION a computed value and setuptools stops being able to
    extract it, silently falls back to *importing* server.py at build time, and
    the build then fails in any clean environment that lacks fastmcp.
    """
    source = (REPO_ROOT / "server.py").read_text()
    assert re.search(r'^SERVER_VERSION = "[^"]+"', source, re.M), (
        "SERVER_VERSION must stay a plain string literal"
    )


def test_server_json_version_matches_the_code():
    """server.json cannot derive from anything — mcp-publisher reads that file.

    CI rewrites it from SERVER_VERSION before publishing, so a drifted committed
    copy never reaches the registry. This keeps the checked-in repo honest too;
    `python scripts/set_version.py <version>` fixes it in one command.
    """
    import server

    server_json = json.loads((REPO_ROOT / "server.json").read_text())
    assert server_json["version"] == server.SERVER_VERSION, (
        "run: python scripts/set_version.py " + server.SERVER_VERSION
    )


def test_server_json_matches_the_deployed_shape():
    server_json = json.loads((REPO_ROOT / "server.json").read_text())

    assert len(server_json["description"]) <= 100
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server_json["name"])
    remotes = server_json["remotes"]
    assert remotes and remotes[0]["type"] == "streamable-http"
    assert remotes[0]["url"].endswith("/mcp")


# ---------------------------------------------------------------------------
# Unauthenticated routes
# ---------------------------------------------------------------------------


def _route_paths(mcp) -> set[str]:
    routes = mcp._get_additional_http_routes()  # noqa: SLF001 — no public accessor
    return {getattr(r, "path", None) for r in routes}


def test_operational_routes_are_registered():
    import server

    paths = _route_paths(server.mcp)
    assert "/healthz" in paths
    assert "/.well-known/openai-apps-challenge" in paths
    assert "/.well-known/mcp/server-card.json" in paths


@pytest.mark.asyncio
async def test_openai_challenge_is_inert_without_a_token(monkeypatch):
    """The verification endpoint must 404 until a token is configured."""
    import dataclasses

    from starlette.requests import Request

    import wellknown

    captured: dict = {}

    class _FakeMCP:
        def custom_route(self, path, methods, include_in_schema=True):
            def decorator(fn):
                captured[path] = fn
                return fn

            return decorator

        async def list_tools(self):
            return []

    def with_token(token):
        # Settings is a frozen dataclass, and wellknown binds it at import time,
        # so replace the module-level reference rather than mutating the instance.
        monkeypatch.setattr(
            wellknown,
            "settings",
            dataclasses.replace(wellknown.settings, openai_apps_challenge_token=token),
        )

    with_token(None)
    wellknown.register(_FakeMCP(), version="1.0.0", description="d")

    handler = captured["/.well-known/openai-apps-challenge"]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    assert (await handler(request)).status_code == 404

    with_token("tok-123")
    response = await handler(request)
    assert response.status_code == 200
    assert response.body == b"tok-123"


# ---------------------------------------------------------------------------
# Lazy auth
# ---------------------------------------------------------------------------


async def _run_lazy_auth(body: bytes) -> dict:
    """Drive LazyAuthASGIMiddleware over one request; return the downstream scope."""
    from lazy_auth import LazyAuthASGIMiddleware

    seen: dict = {}

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        seen["body"] = (await receive()).get("body")

    middleware = LazyAuthASGIMiddleware(downstream, mcp_path="/mcp")
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": []}

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):  # pragma: no cover - downstream never sends here
        pass

    await middleware(scope, receive, send)
    return seen


def _rpc(method: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "tools/list", "ping"])
async def test_lazy_auth_allows_discovery_methods(method):
    from lazy_auth import ANONYMOUS_CLIENT_ID

    seen = await _run_lazy_auth(_rpc(method))
    scope = seen["scope"]

    assert scope.get("user") is not None, f"{method} should be servable anonymously"
    assert scope["user"].access_token.client_id == ANONYMOUS_CLIENT_ID
    # The synthetic Authorization header is required: FastMCP 401s on a *missing*
    # header before it ever inspects scope["user"].
    assert any(k.lower() == b"authorization" for k, _ in scope["headers"])
    # The body must still reach the real handler intact.
    assert seen["body"] == _rpc(method)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["tools/call", "resources/read", "prompts/get", "completion/complete"]
)
async def test_lazy_auth_still_gates_everything_else(method):
    seen = await _run_lazy_auth(_rpc(method))
    scope = seen["scope"]

    assert scope.get("user") is None, f"{method} must NOT be servable anonymously"
    assert not scope["headers"], "no synthetic Authorization header should be injected"


@pytest.mark.asyncio
async def test_lazy_auth_gates_mixed_batches():
    """A batch that mixes a public method with a tool call must be gated."""
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call"},
        ]
    ).encode()
    seen = await _run_lazy_auth(body)
    assert seen["scope"].get("user") is None


@pytest.mark.asyncio
async def test_lazy_auth_ignores_unparseable_bodies():
    seen = await _run_lazy_auth(b"not json at all")
    assert seen["scope"].get("user") is None


def test_lazy_auth_provider_injects_all_middlewares():
    """The provider subclass must actually append our ASGI middleware."""
    from starlette.middleware import Middleware as ASGIMiddleware

    from lazy_auth import (
        LazyAuthASGIMiddleware,
        RejectedTokenGateASGIMiddleware,
        WWWAuthenticateScopeMiddleware,
        lazy_auth_provider_class,
    )

    class _FakeProvider:
        required_scopes = ["openid"]

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_middleware(self):
            return [ASGIMiddleware(object)]

    provider = lazy_auth_provider_class(_FakeProvider)(
        mcp_path="/mcp", advertise_scope="openid email"
    )
    classes = [m.cls for m in provider.get_middleware()]
    assert LazyAuthASGIMiddleware in classes
    assert WWWAuthenticateScopeMiddleware in classes
    assert RejectedTokenGateASGIMiddleware in classes
    # Ordering is load-bearing: the gate must sit inside WWWAuthenticateScope so
    # its 401 picks up `scope=`, and inside LazyAuth so anonymous discovery is
    # decided first. Later in the list == innermost == runs last.
    assert classes.index(RejectedTokenGateASGIMiddleware) > classes.index(
        WWWAuthenticateScopeMiddleware
    )
    assert classes.index(RejectedTokenGateASGIMiddleware) > classes.index(
        LazyAuthASGIMiddleware
    )

    # The gate is NOT conditional on lazy auth: an upstream-rejected token must
    # still produce a 401 challenge on a server with lazy auth turned off.
    disabled = lazy_auth_provider_class(_FakeProvider)(lazy_auth_enabled=False)
    disabled_classes = [m.cls for m in disabled.get_middleware()]
    assert LazyAuthASGIMiddleware not in disabled_classes
    assert RejectedTokenGateASGIMiddleware in disabled_classes


# ---------------------------------------------------------------------------
# Re-authentication: upstream 401 must become a transport 401
#
# The bug these cover: an upstream 401 came back as HTTP 200 with
# `isError: true`, and MCP clients only re-authenticate on an HTTP 401 carrying
# WWW-Authenticate. So the session stayed wedged and no client ever refreshed.
# ---------------------------------------------------------------------------

PRM_URL = "https://mcp.jobmojito.com/.well-known/oauth-protected-resource/mcp"


async def _run_gate(headers: list[tuple[bytes, bytes]], **kwargs) -> dict:
    """Drive RejectedTokenGateASGIMiddleware over one request."""
    from lazy_auth import RejectedTokenGateASGIMiddleware

    seen: dict = {"passed_through": False, "messages": []}

    async def downstream(scope, receive, send):
        seen["passed_through"] = True

    middleware = RejectedTokenGateASGIMiddleware(
        downstream, mcp_path="/mcp", resource_metadata_url=PRM_URL, **kwargs
    )
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}

    async def receive():  # pragma: no cover - the gate never reads the body
        return {"type": "http.disconnect"}

    async def send(message):
        seen["messages"].append(message)

    await middleware(scope, receive, send)
    return seen


@pytest.fixture
def clean_rejections():
    from session_verifier import rejected_tokens

    rejected_tokens.clear()
    yield rejected_tokens
    rejected_tokens.clear()


@pytest.mark.asyncio
async def test_rejected_token_gets_a_401_challenge(clean_rejections):
    """The whole point: a real 401 + WWW-Authenticate, not a 200 tool error."""
    from session_verifier import mark_token_rejected

    mark_token_rejected("dead.jwt.token")
    seen = await _run_gate([(b"authorization", b"Bearer dead.jwt.token")])

    assert not seen["passed_through"], "a rejected token must not reach the tool"
    start = seen["messages"][0]
    assert start["status"] == 401
    challenge = dict(start["headers"])[b"www-authenticate"].decode()
    # error="invalid_token" is what tells the client the token is the problem;
    # resource_metadata is where it goes to find the authorization server.
    assert 'error="invalid_token"' in challenge
    assert f'resource_metadata="{PRM_URL}"' in challenge


@pytest.mark.asyncio
async def test_healthy_token_passes_through(clean_rejections):
    seen = await _run_gate([(b"authorization", b"Bearer good.jwt.token")])
    assert seen["passed_through"]
    assert not seen["messages"]


@pytest.mark.asyncio
async def test_gate_never_blocks_anonymous_discovery(clean_rejections):
    """A poisoned token must not make tools/list vanish from directory crawlers."""
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from lazy_auth import ANONYMOUS_CLIENT_ID, RejectedTokenGateASGIMiddleware
    from session_verifier import mark_token_rejected

    mark_token_rejected("dead.jwt.token")

    passed = False

    async def downstream(scope, receive, send):
        nonlocal passed
        passed = True

    middleware = RejectedTokenGateASGIMiddleware(
        downstream, mcp_path="/mcp", resource_metadata_url=PRM_URL
    )
    token = AccessToken(token="", client_id=ANONYMOUS_CLIENT_ID, scopes=[], expires_at=None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer dead.jwt.token")],
        "user": AuthenticatedUser(token),
    }

    async def receive():  # pragma: no cover
        return {"type": "http.disconnect"}

    async def send(message):  # pragma: no cover
        pass

    await middleware(scope, receive, send)
    assert passed


@pytest.mark.asyncio
async def test_rejection_expires(clean_rejections):
    """A transient upstream auth failure must not lock a token out forever."""
    from session_verifier import rejected_tokens, token_fingerprint

    rejected_tokens.add(token_fingerprint("tok"), ttl_seconds=-1)
    seen = await _run_gate([(b"authorization", b"Bearer tok")])
    assert seen["passed_through"]


def test_token_fingerprint_never_reveals_the_token():
    """Fingerprints are logged and cached; the token is a live user credential."""
    from session_verifier import token_fingerprint

    token = "eyJhbGciOiJFUzI1NiJ9.super-secret-payload.signature"
    fingerprint = token_fingerprint(token)
    assert token not in fingerprint
    assert fingerprint != token_fingerprint(token + "x")
    assert fingerprint == token_fingerprint(token)


# --- the stateful session check itself ---------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.request = None


def _session_verifier(response=None, raises=None):
    """A SupabaseSessionVerifier whose JWT stage passes and whose HTTP is faked."""
    from mcp.server.auth.provider import AccessToken

    from session_verifier import SupabaseSessionVerifier

    verifier = SupabaseSessionVerifier(
        project_url="https://example.supabase.co",
        anon_key="anon-key",
        jwks_uri="https://example.supabase.co/auth/v1/.well-known/jwks.json",
        issuer="https://example.supabase.co/auth/v1",
        algorithm="ES256",
        audience="authenticated",
    )

    async def fake_jwt_verify(token):
        return AccessToken(
            token=token, client_id="user-1", scopes=[], expires_at=None, claims={"sub": "user-1"}
        )

    calls: list[str] = []

    class _FakeClient:
        is_closed = False

        async def get(self, url, headers=None):
            calls.append(url)
            if raises is not None:
                raise raises
            return response

    # Only the HTTP stage is faked here; callers monkeypatch the JWT stage,
    # which would otherwise need real JWKS crypto.
    verifier._client = _FakeClient()
    return verifier, fake_jwt_verify, calls


@pytest.mark.asyncio
async def test_dead_session_is_rejected_before_the_tool_runs(monkeypatch):
    """A JWT that verifies but has no session must fail at the gate, not upstream.

    Failing here is what produces a spec-correct 401 challenge; failing upstream
    produces a 200 the client ignores.
    """
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier, fake_jwt_verify, calls = _session_verifier(_FakeResponse(401, "bad jwt"))
    monkeypatch.setattr(JWTVerifier, "verify_token", lambda self, token: fake_jwt_verify(token))

    assert await verifier.verify_token("some.jwt.token") is None
    assert calls == ["https://example.supabase.co/auth/v1/user"]


@pytest.mark.asyncio
async def test_live_session_is_accepted_and_cached(monkeypatch):
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier, fake_jwt_verify, calls = _session_verifier(_FakeResponse(200, "{}"))
    monkeypatch.setattr(JWTVerifier, "verify_token", lambda self, token: fake_jwt_verify(token))

    assert await verifier.verify_token("some.jwt.token") is not None
    assert await verifier.verify_token("some.jwt.token") is not None
    assert len(calls) == 1, "the session check must be cached, not run per request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "raises"),
    [
        (_FakeResponse(500, "boom"), None),
        (None, RuntimeError("connection refused")),
    ],
)
async def test_session_check_fails_open(monkeypatch, response, raises):
    """A Supabase outage must not force every connected user to re-authenticate.

    Failing closed would convert a brief blip into a fleet-wide re-auth storm;
    failing open costs at most a doomed call the API would have rejected anyway.
    """
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier, fake_jwt_verify, _ = _session_verifier(response, raises)
    monkeypatch.setattr(JWTVerifier, "verify_token", lambda self, token: fake_jwt_verify(token))

    assert await verifier.verify_token("some.jwt.token") is not None


@pytest.mark.asyncio
async def test_session_check_does_not_rescue_a_bad_jwt(monkeypatch):
    """The session check only ever *narrows* what the JWT stage accepted."""
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier, _, calls = _session_verifier(_FakeResponse(200, "{}"))

    async def reject(token):
        return None

    monkeypatch.setattr(JWTVerifier, "verify_token", lambda self, token: reject(token))
    assert await verifier.verify_token("expired.jwt.token") is None
    assert not calls, "no point asking Supabase about a token that failed signature/exp"


def test_offline_access_is_advertised():
    """Without `offline_access` the user re-authorizes in a browser every hour.

    Supabase issues a refresh token only when the scope is requested, and clients
    read the scopes to request from the PRM / the `scope=` challenge parameter —
    both of which come from this setting. Drop it and every access-token expiry
    (1h) escalates from a silent refresh to a full interactive OAuth flow.

    Confirmed supported by the authorization server:
      GET $SUPABASE_PROJECT_URL/auth/v1/.well-known/oauth-authorization-server
      -> scopes_supported includes "offline_access"
      -> grant_types_supported includes "refresh_token"
    """
    from config import settings

    assert "offline_access" in settings.oauth_scopes_supported


def test_scopes_are_advertised_not_required():
    """Advertising a scope asks for a capability; requiring it rejects tokens.

    If `offline_access` were promoted to `required_scopes`, every token whose
    `scope` claim omitted it would 401 — turning a refresh improvement into a
    total outage.
    """
    import server

    assert not getattr(server._build_token_verifier(), "required_scopes", None)


def test_prm_url_matches_the_route_fastmcp_serves():
    """A challenge pointing at a 404 is worse than no challenge at all."""
    from config import settings

    assert settings.protected_resource_metadata_url == (
        f"{settings.base_url}/.well-known/oauth-protected-resource{settings.mcp_path}"
    )


# ---------------------------------------------------------------------------
# Error quality + result-size guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expect"),
    [
        ("HTTP error 403: Forbidden - {'detail': 'nope'}", "jobmojito_configuration"),
        ("HTTP error 404: Not Found", "list_"),
        ("HTTP error 422: Unprocessable - {'field': 'bad'}", "offending field"),
        ("HTTP error 503: Service Unavailable", "JobMojito-side failure"),
    ],
)
def test_upstream_errors_get_actionable_guidance(message, expect):
    from middleware import UpstreamErrorMiddleware

    rewritten = UpstreamErrorMiddleware._rewrite("list_candidates", message)
    assert rewritten is not None
    assert "What to do:" in rewritten
    assert expect in rewritten


def test_upstream_401_tells_the_model_to_retry_not_to_give_up():
    """The retry is the mechanism, not politeness.

    Retrying is what makes the client hit RejectedTokenGateASGIMiddleware and
    receive a real 401 challenge. Guidance that says "ask the user to reconnect"
    instead leaves the session wedged — no MCP client re-authorizes on the
    strength of an error string.
    """
    from middleware import UpstreamErrorMiddleware

    rewritten = UpstreamErrorMiddleware._rewrite(
        "list_avatars", "HTTP error 401: Unauthorized - {'error': 'nope'}"
    )
    assert "Retry this exact call once" in rewritten


def test_unrecognised_errors_are_left_alone():
    """Never mask a failure we don't understand."""
    from middleware import UpstreamErrorMiddleware

    assert UpstreamErrorMiddleware._rewrite("t", "something else entirely") is None


def test_oversized_upstream_detail_is_capped():
    from middleware import UpstreamErrorMiddleware

    rewritten = UpstreamErrorMiddleware._rewrite("t", "HTTP error 422: X - " + "y" * 5000)
    assert "(truncated)" in rewritten
    assert len(rewritten) < 2000


def test_healthz_is_never_cached(monkeypatch):
    """A liveness probe served from a CDN cache can report a dead server as ok.

    Horizon fronts this host with CloudFront. With no cache headers it applied
    its own default TTL, and /healthz was observed serving a pre-deploy version
    for 31 minutes (`x-cache: Hit from cloudfront`, `age: 1859`) while the new
    build was demonstrably handling MCP traffic. The wrong version was the
    visible symptom; the real risk is a cached `"status": "ok"` outliving the
    process it claims to be probing.
    """
    import asyncio

    import wellknown

    captured: dict = {}

    class _FakeMCP:
        def custom_route(self, path, methods, include_in_schema=True):
            def decorator(fn):
                captured[path] = fn
                return fn

            return decorator

    wellknown.register(_FakeMCP(), version="9.9.9", description="d")

    response = asyncio.run(captured["/healthz"](None))
    cache_control = response.headers.get("cache-control", "")

    assert "no-store" in cache_control, f"healthz must not be cacheable, got: {cache_control!r}"
    assert b"9.9.9" in response.body, "healthz must report the running version"


def test_server_card_states_an_explicit_ttl(monkeypatch):
    """Cacheable by design, but bounded — it carries `version` too."""
    import asyncio

    import wellknown

    captured: dict = {}

    class _FakeMCP:
        def custom_route(self, path, methods, include_in_schema=True):
            def decorator(fn):
                captured[path] = fn
                return fn

            return decorator

        async def list_tools(self):
            return []

    wellknown.register(_FakeMCP(), version="9.9.9", description="d")

    response = asyncio.run(captured["/.well-known/mcp/server-card.json"](None))
    assert "max-age" in response.headers.get("cache-control", "")
