"""Auth-layer ASGI middleware attached to the FastMCP object.

Three concerns, all of which need a slot in the Starlette stack that Prefect
Horizon cannot take away from us (see HOW THIS ATTACHES below):

* ``LazyAuthASGIMiddleware`` — serve capability discovery without a token.
* ``WWWAuthenticateScopeMiddleware`` — add ``scope=`` to 401/403 challenges.
* ``RejectedTokenGateASGIMiddleware`` — answer with a real HTTP 401 once the
  JobMojito API has rejected a token, so clients actually re-authenticate
  instead of reading a 200-wrapped tool error. See ``session_verifier.py``.

WHY LAZY AUTH
-------------
FastMCP enforces auth at the ASGI *transport* layer: when an ``AuthProvider`` is
configured, ``RequireAuthMiddleware`` wraps the whole ``/mcp`` route and rejects
every unauthenticated request — including ``initialize`` and ``tools/list`` —
before any JSON-RPC method dispatch happens.

That is stricter than the MCP ecosystem expects, and it costs us listings:

* **Smithery / Glama** can only display a server's tool list if ``tools/list``
  works without credentials. A fully gated server shows up with *zero tools*,
  which is the main quality signal a browsing user sees.
* Anthropic documents "lazy authentication" (advertise capabilities, authenticate
  on first real call) as the preferred pattern for directory connectors.
* Clients can then show the user what the server does *before* asking them to
  complete an OAuth flow.

WHAT THIS DOES
--------------
Allows a small allow-list of discovery methods through unauthenticated, and 401s
everything else — crucially, still with a spec-compliant
``WWW-Authenticate: Bearer ... resource_metadata="..."`` header, so a compliant
client starts the OAuth flow at exactly the right moment (the first ``tools/call``).

**No data is exposed.** The allow-list contains only capability-discovery methods.
Every tool invocation, resource read and prompt render still requires a verified
Supabase token, and ``upstream.py`` still forwards that user's own JWT.

HOW IT ATTACHES (important for Prefect Horizon)
-----------------------------------------------
Horizon imports ``server.py:mcp`` and builds the ASGI app itself, so we can never
call ``mcp.http_app(middleware=[...])``. The one object-level hook is
``AuthProvider.get_middleware()``: ``fastmcp/server/http.py::create_streamable_http_app``
calls it and splices the result into the Starlette middleware stack. Middleware
appended there lands *innermost* — after ``AuthenticationMiddleware`` (so real
tokens are already verified into ``scope["user"]``) but before routing, and
therefore before ``RequireAuthMiddleware``. That is exactly the slot we need, and
it is the common denominator of every code path that can serve this server.

STABILITY
---------
This relies on two FastMCP internals that are not a documented public contract:

1. ``AuthProvider.get_middleware()``'s return value being spliced into the app stack.
2. ``RequireAuthMiddleware`` short-circuiting with a 401 when the ``Authorization``
   header is *entirely absent*, before it inspects ``scope["user"]`` — which is why
   we must also inject a synthetic header, not just a synthetic user.

``fastmcp`` is pinned in ``pyproject.toml`` and ``tests/test_listing_readiness.py``
asserts the end-to-end behaviour (initialize/tools-list 200, tools/call 401).
If a FastMCP upgrade breaks either assumption, that test fails loudly rather than
the server silently becoming either wide open or fully gated.
"""

from __future__ import annotations

import json
import logging

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.authentication import AuthCredentials
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("jobmojito_mcp.auth")

#: JSON-RPC methods servable without a token. Capability discovery only — nothing
#: here reads or writes JobMojito data.
PUBLIC_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "notifications/cancelled",
        "ping",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    }
)

#: client_id stamped on the synthetic token used for anonymous discovery. Code
#: that needs to distinguish "anonymous discovery" from "real user" must compare
#: against this — ``get_access_token()`` returns a token object, not None.
ANONYMOUS_CLIENT_ID = "__anonymous_discovery__"


class LazyAuthASGIMiddleware:
    """Let capability-discovery methods through unauthenticated; 401 the rest."""

    def __init__(
        self,
        app: ASGIApp,
        mcp_path: str = "/mcp",
        public_methods: frozenset[str] = PUBLIC_METHODS,
        granted_scopes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.public_methods = public_methods
        self.granted_scopes = list(granted_scopes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.mcp_path:
            return await self.app(scope, receive, send)
        # A real, already-verified token wins: never downgrade an authenticated user.
        if isinstance(scope.get("user"), AuthenticatedUser):
            return await self.app(scope, receive, send)
        if scope.get("method") != "POST":
            # GET (SSE stream) and DELETE (session teardown) keep the normal gate.
            return await self.app(scope, receive, send)

        # Buffer the body so we can read the JSON-RPC method, then replay it.
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Delegate onward (e.g. http.disconnect) — do NOT synthesise one.
            return await receive()

        methods = _rpc_methods(body)
        if methods and methods <= self.public_methods:
            token = AccessToken(
                token="",
                client_id=ANONYMOUS_CLIENT_ID,
                scopes=list(self.granted_scopes),
                expires_at=None,
            )
            scope["user"] = AuthenticatedUser(token)
            scope["auth"] = AuthCredentials(list(self.granted_scopes))
            # REQUIRED: RequireAuthMiddleware 401s on a *missing* Authorization
            # header before it ever looks at scope["user"], so a synthetic user
            # alone is not enough.
            headers = list(scope.get("headers", []))
            if not any(key.lower() == b"authorization" for key, _ in headers):
                headers.append((b"authorization", b"Bearer anonymous"))
                scope["headers"] = headers
            logger.debug("Anonymous discovery allowed for %s", sorted(methods))

        await self.app(scope, replay, send)


def _rpc_methods(body: bytes) -> set[str]:
    """Extract the JSON-RPC method name(s) from a request body (batch-aware)."""
    try:
        payload = json.loads(body)
    except Exception:
        return set()
    if isinstance(payload, dict):
        method = payload.get("method")
        return {str(method)} if method else set()
    if isinstance(payload, list):
        return {str(m.get("method")) for m in payload if isinstance(m, dict) and m.get("method")}
    return set()


class RejectedTokenGateASGIMiddleware:
    """Answer with a real 401 when the JobMojito API has rejected this token.

    An upstream 401 surfaces today as a ``ToolError`` inside an HTTP **200** —
    ``isError: true`` in the JSON-RPC result. MCP clients never re-authenticate on
    that, because their auth layer only reacts to an HTTP 401 carrying
    ``WWW-Authenticate``. So the model is told to "ask the user to reconnect" and
    nothing reconnects.

    ``session_verifier.SupabaseSessionVerifier`` catches the common case before
    the tool ever runs. This closes the residual gap: once the API has rejected a
    token, the *next* request bearing it is answered here — before dispatch, so
    before the SSE headers are flushed — with the challenge the client needs.

    It cannot be done in-band on the failing call itself: Streamable HTTP flushes
    the SSE response headers before the tool executes, so the 200 is already on
    the wire by the time the upstream 401 is known.

    Anonymous capability discovery is deliberately unaffected — a poisoned token
    must not make ``tools/list`` disappear from directory crawlers.
    """

    def __init__(
        self,
        app: ASGIApp,
        mcp_path: str = "/mcp",
        resource_metadata_url: str | None = None,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.mcp_path:
            return await self.app(scope, receive, send)

        user = scope.get("user")
        if isinstance(user, AuthenticatedUser):
            token = getattr(user.access_token, "client_id", None)
            if token == ANONYMOUS_CLIENT_ID:
                # Anonymous discovery: nothing to gate.
                return await self.app(scope, receive, send)

        bearer = _bearer_from_scope(scope)
        if not bearer:
            return await self.app(scope, receive, send)

        from session_verifier import rejected_tokens, token_fingerprint

        fingerprint = token_fingerprint(bearer)
        if fingerprint not in rejected_tokens:
            return await self.app(scope, receive, send)

        logger.info(
            "Challenging token %s: the JobMojito API rejected it, so this request "
            "gets a 401 instead of another 200-wrapped tool error.",
            fingerprint,
        )
        await _send_invalid_token_challenge(send, self.resource_metadata_url)


def _bearer_from_scope(scope: Scope) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() != b"authorization":
            continue
        raw = value.decode("latin-1").strip()
        if raw.lower().startswith("bearer "):
            token = raw[7:].strip()
            # LazyAuthASGIMiddleware injects this placeholder for discovery.
            return token if token and token != "anonymous" else None
    return None


async def _send_invalid_token_challenge(send: Send, resource_metadata_url: str | None) -> None:
    """Emit the RFC 6750 / RFC 9728 challenge that makes a client re-authorize."""
    description = (
        "The JobMojito API rejected this access token. The Supabase session "
        "behind it is no longer valid — refresh the token or re-authorize."
    )
    parts = ['error="invalid_token"', f'error_description="{description}"']
    if resource_metadata_url:
        parts.append(f'resource_metadata="{resource_metadata_url}"')
    challenge = f"Bearer {', '.join(parts)}"

    body = json.dumps(
        {"error": "invalid_token", "error_description": description}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", challenge.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class WWWAuthenticateScopeMiddleware:
    """Append ``scope="..."`` to 401/403 ``WWW-Authenticate`` challenges.

    RFC 6750 §3.1 allows a ``scope`` parameter on the challenge, and the MCP
    authorization spec says servers SHOULD include it so a client can request the
    right scopes up front. Neither FastMCP nor the upstream MCP SDK emits it, so
    we append it on the way out.
    """

    def __init__(self, app: ASGIApp, scope_value: str) -> None:
        self.app = app
        self.scope_value = scope_value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] in (401, 403):
                headers = []
                for key, value in message["headers"]:
                    if key.lower() == b"www-authenticate" and b"scope=" not in value:
                        value = value + f', scope="{self.scope_value}"'.encode()
                    headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def lazy_auth_provider_class(base_class: type) -> type:
    """Build a subclass of ``base_class`` that injects our ASGI middleware.

    Written as a factory so ``SupabaseProvider`` is imported lazily by the caller
    (it pulls in optional auth dependencies) and so the same behaviour can be
    layered onto a different provider later without touching this module.
    """

    class _LazyAuthProvider(base_class):  # type: ignore[valid-type,misc]
        """Auth provider that permits unauthenticated capability discovery."""

        def __init__(
            self,
            *args,
            mcp_path: str = "/mcp",
            public_methods: frozenset[str] = PUBLIC_METHODS,
            advertise_scope: str | None = None,
            lazy_auth_enabled: bool = True,
            resource_metadata_url: str | None = None,
            **kwargs,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._mcp_path = mcp_path
            self._public_methods = public_methods
            self._advertise_scope = advertise_scope
            self._lazy_auth_enabled = lazy_auth_enabled
            self._resource_metadata_url = resource_metadata_url

        def get_middleware(self) -> list:
            middleware = list(super().get_middleware())
            if self._advertise_scope:
                middleware.append(
                    ASGIMiddleware(
                        WWWAuthenticateScopeMiddleware,
                        scope_value=self._advertise_scope,
                    )
                )
            if self._lazy_auth_enabled:
                middleware.append(
                    ASGIMiddleware(
                        LazyAuthASGIMiddleware,
                        mcp_path=self._mcp_path,
                        public_methods=self._public_methods,
                        granted_scopes=tuple(getattr(self, "required_scopes", None) or ()),
                    )
                )
                logger.info(
                    "Lazy auth enabled: %s servable without a token; every other "
                    "method still requires a verified Supabase JWT.",
                    ", ".join(sorted(self._public_methods)),
                )
            # Appended last => innermost, so it runs *after* LazyAuth has had its
            # say about anonymous discovery, and its 401 still travels back out
            # through WWWAuthenticateScopeMiddleware to pick up `scope=`.
            middleware.append(
                ASGIMiddleware(
                    RejectedTokenGateASGIMiddleware,
                    mcp_path=self._mcp_path,
                    resource_metadata_url=self._resource_metadata_url,
                )
            )
            return middleware

    return _LazyAuthProvider
