"""Make this server's idea of "valid token" match the JobMojito API's.

THE PROBLEM
-----------
Two gates check the end user's Supabase JWT, and they disagree:

* **This server** verifies it *statelessly* — JWKS signature, ``exp``, ``iss``,
  ``aud="authenticated"`` (``fastmcp``'s ``JWTVerifier``). Nothing else.
* **The JobMojito Edge Functions** verify it *statefully* — they resolve the JWT
  back to a live Supabase session (``auth.getUser`` → ``/auth/v1/user``), which
  additionally fails if the session was signed out, revoked, or rotated away.

A stateless check is always the more permissive of the two, so there is a class
of token that sails through here and is rejected upstream. The user then sees a
tool error, and — critically — **no re-authentication happens**, because a tool
error is an HTTP 200 carrying ``isError: true``. MCP clients only refresh or
re-authorize when they see an HTTP 401 with a ``WWW-Authenticate`` header on the
transport itself.

Note what this is *not*: it is not token expiry. ``JWTVerifier`` checks ``exp``
with no leeway and caches nothing, so an expired token is already rejected here
with a proper 401 and the client already refreshes. Expiry works. Dead sessions
are the gap.

Nor is refreshing something this server can do. It is an OAuth 2.1 *resource
server*: it receives an access token and never a refresh token. The refresh token
belongs to the MCP client, issued by Supabase as the authorization server. The
only lever we have is emitting the 401 that tells the client to use it.

THE FIX (two parts, both in this module)
----------------------------------------
1. ``SupabaseSessionVerifier`` — run the *same* stateful check the Edge Functions
   run, at the gate, before the tool executes. A dead session then fails during
   token verification, where FastMCP already emits a spec-correct
   ``401 Bearer error="invalid_token", resource_metadata="…"``. The client
   refreshes and retries. One extra HTTP call per token per TTL window.

2. ``rejected_tokens`` — a backstop for the residual race (session dies between
   verification and the API call, or the API 401s for some other reason).
   ``middleware.UpstreamErrorMiddleware`` records the token on an upstream 401;
   ``lazy_auth.RejectedTokenGateASGIMiddleware`` then answers the *next* request
   bearing that token with a real 401 instead of another 200-wrapped tool error.

Why a backstop and not an in-band rewrite: in the default Streamable HTTP mode
the SSE response headers are flushed *before* the tool runs
(``mcp/server/streamable_http.py`` — "Start the SSE response (this will send
headers immediately)"), so by the time the upstream 401 is known the 200 is long
gone. Rewriting it would mean buffering the entire SSE stream, which costs
keep-alives and progress events on every call to fix a rare one.

FAILURE POSTURE
---------------
The session check **fails open**. If Supabase Auth is unreachable, slow, or
returns 5xx, the token is treated as valid and the API gets the final say. The
alternative — failing closed — turns a brief Supabase blip into a forced
re-authentication for every connected user at once, which is far worse than
letting a handful of doomed calls through to a 401 they would have got anyway.

Only outright ``401``/``403`` from ``/auth/v1/user`` — an unambiguous "this
session is gone" — rejects the token.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx

from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger("jobmojito_mcp.auth")

#: Upper bound on how long a *successful* session check is trusted. Kept short:
#: it is the worst-case window between a user signing out and this server
#: noticing. Negative results are never cached — a freshly refreshed token must
#: work on the very next request.
DEFAULT_SESSION_TTL_SECONDS = 60

#: Stop the positive cache growing without bound on a busy server. Entries are
#: tiny (a hash + a float) and expire on their own; this is a safety valve, not
#: a tuning knob.
_MAX_CACHE_ENTRIES = 2048


def token_fingerprint(token: str) -> str:
    """A short, non-reversible id for a token — safe to cache on and to log.

    Never log or store the token itself: it is a live credential for the user's
    entire Supabase account, not just this server.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class _ExpiringTokenSet:
    """A tiny TTL set keyed on token fingerprints."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, float] = {}

    def add(self, fingerprint: str, *, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            return
        self._prune()
        self._entries[fingerprint] = time.monotonic() + ttl

    def __contains__(self, fingerprint: str) -> bool:
        expiry = self._entries.get(fingerprint)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self._entries.pop(fingerprint, None)
            return False
        return True

    def discard(self, fingerprint: str) -> None:
        self._entries.pop(fingerprint, None)

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._entries.items() if v <= now]
        for key in expired:
            self._entries.pop(key, None)
        if len(self._entries) >= _MAX_CACHE_ENTRIES:
            # Drop the soonest-to-expire entries; they cost the least to re-check.
            for key, _ in sorted(self._entries.items(), key=lambda kv: kv[1])[:_MAX_CACHE_ENTRIES // 4]:
                self._entries.pop(key, None)


# ---------------------------------------------------------------------------
# Backstop: tokens the JobMojito API has rejected
# ---------------------------------------------------------------------------

#: Tokens the upstream API answered 401 for. Populated by
#: ``middleware.UpstreamErrorMiddleware``, read by
#: ``lazy_auth.RejectedTokenGateASGIMiddleware``. TTL-bounded so a transient
#: upstream auth failure can't lock a token out permanently.
rejected_tokens = _ExpiringTokenSet(ttl_seconds=300)


def mark_token_rejected(token: str | None) -> None:
    """Record that the JobMojito API rejected this token's credentials."""
    if not token:
        return
    fingerprint = token_fingerprint(token)
    rejected_tokens.add(fingerprint)
    logger.info(
        "Token %s rejected upstream; the next request bearing it will get a "
        "401 challenge so the client re-authenticates.",
        fingerprint,
    )


def clear_token_rejection(token: str | None) -> None:
    """Forget a rejection (used when a token verifies cleanly again)."""
    if token:
        rejected_tokens.discard(token_fingerprint(token))


# ---------------------------------------------------------------------------
# Primary fix: stateful verification at the gate
# ---------------------------------------------------------------------------


class SupabaseSessionVerifier(JWTVerifier):
    """``JWTVerifier`` plus the session check the JobMojito API actually performs.

    Drop-in for the verifier ``SupabaseProvider`` builds by default — same JWKS,
    issuer, algorithm and audience — with one extra step after the JWT checks
    pass: a cached ``GET {project_url}/auth/v1/user`` using that JWT.
    """

    def __init__(
        self,
        *,
        project_url: str,
        auth_route: str = "auth/v1",
        anon_key: str | None = None,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        request_timeout_seconds: float = 5.0,
        **jwt_kwargs,
    ) -> None:
        super().__init__(**jwt_kwargs)
        self.user_endpoint = f"{project_url.rstrip('/')}/{auth_route.strip('/')}/user"
        self.anon_key = anon_key
        self.request_timeout_seconds = request_timeout_seconds
        self._live_sessions = _ExpiringTokenSet(session_ttl_seconds)
        self._client: httpx.AsyncClient | None = None

        if not anon_key:
            logger.warning(
                "Supabase session check enabled without SUPABASE_ANON_KEY. "
                "%s normally requires the project `apikey` header, so the check "
                "will fail open on every request and provide no protection.",
                self.user_endpoint,
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            # Already rejected on signature / exp / iss / aud. FastMCP turns this
            # into the 401 challenge; nothing further to add.
            return None

        if not await self._session_is_live(token, access):
            return None

        clear_token_rejection(token)
        return access

    async def _session_is_live(self, token: str, access: AccessToken) -> bool:
        fingerprint = token_fingerprint(token)
        if fingerprint in self._live_sessions:
            return True

        headers = {"Authorization": f"Bearer {token}"}
        if self.anon_key:
            headers["apikey"] = self.anon_key

        try:
            client = self._ensure_client()
            response = await client.get(self.user_endpoint, headers=headers)
        except Exception as exc:
            # Fail open — see the module docstring. A Supabase outage must not
            # become a re-authentication storm.
            logger.warning(
                "Supabase session check for token %s could not reach %s (%s: %s); "
                "treating the token as valid and letting the API decide.",
                fingerprint,
                self.user_endpoint,
                type(exc).__name__,
                exc,
            )
            return True

        if response.status_code in (401, 403):
            self._log_dead_session(fingerprint, access, response)
            return False

        if response.status_code >= 400:
            logger.warning(
                "Supabase session check for token %s got HTTP %s from %s; failing "
                "open (this is a Supabase-side problem, not a bad token).",
                fingerprint,
                response.status_code,
                self.user_endpoint,
            )
            return True

        # Never trust the session for longer than the token itself is valid.
        ttl = self._live_sessions.ttl_seconds
        expires_at = getattr(access, "expires_at", None)
        if expires_at is not None:
            ttl = min(ttl, max(0.0, expires_at - time.time()))
        self._live_sessions.add(fingerprint, ttl_seconds=ttl)
        return True

    def _ensure_client(self) -> httpx.AsyncClient:
        """Create the client lazily, inside the running loop."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.request_timeout_seconds, connect=3.0),
            )
        return self._client

    @staticmethod
    def _log_dead_session(fingerprint: str, access: AccessToken, response: httpx.Response) -> None:
        """Say exactly why a JWT that verified locally has no session behind it.

        This is the diagnostic that was missing: without it, the only evidence is
        a 401 from the API with no way to tell a revoked session from a wrong
        `apikey`, an audience mismatch, or a misconfigured project. Claims only —
        never the token.
        """
        claims = getattr(access, "claims", None) or {}
        detail = (response.text or "").strip()[:200]
        logger.warning(
            "Token %s passed JWT verification but has no live Supabase session "
            "(HTTP %s from %s). sub=%s iss=%s aud=%s exp=%s session_id=%s "
            "www-authenticate=%r body=%r — rejecting so the client re-authenticates.",
            fingerprint,
            response.status_code,
            response.request.url if response.request else "?",
            claims.get("sub"),
            claims.get("iss"),
            claims.get("aud"),
            claims.get("exp"),
            claims.get("session_id"),
            response.headers.get("www-authenticate"),
            detail,
        )
