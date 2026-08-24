# Architecture

How the JobMojito MCP server is put together, for developers and coding agents.

## Overview

The server turns the JobMojito REST API (a set of Supabase Edge Functions) into
MCP tools, and adds a small number of hand-written tools (documentation search,
merchant selection). It is a single asynchronous Python process
built on FastMCP 3.x and served over the MCP Streamable HTTP transport. The object
Horizon serves is `mcp`, created by `build_server()` in `server.py`.

Two ideas drive the design:

1. **The API is the source of truth.** Tools are generated from the live OpenAPI
   spec at startup, so the tool set tracks the API automatically. Naming and
   exclusions are the only curated layer on top.
2. **Every call is the end user's.** Authentication uses Supabase OAuth, and the
   authenticated user's JWT is forwarded to the API on each request, so JobMojito
   enforces that user's own permissions rather than a shared service identity.

## Request lifecycle (API tool)

1. An MCP client connects and authenticates via Supabase OAuth. The server is an
   OAuth *resource server* (`SupabaseProvider`); the authorization server is the
   Supabase project, and consent is handled by the JobMojito app.
2. The client calls a tool. FastMCP validates the **input** against the tool's
   schema (derived from the OpenAPI request schema).
3. The generated tool calls the JobMojito API through the shared httpx client in
   `upstream.py`. `SupabaseTokenForwardAuth` pulls the current request's access
   token via `get_access_token()` and sets `Authorization: Bearer <jwt>`; the
   `apikey` header is added when `SUPABASE_ANON_KEY` is set.
4. The API responds. FastMCP validates the **output** against the (relaxed)
   OpenAPI response schema and returns structured content to the client.
5. The middleware chain (`middleware.py`) wraps all of the above: it logs the call
   name/argument keys/outcome/timing, rewrites upstream HTTP errors and
   output-validation errors into actionable messages, and refuses oversized
   results. See *Middleware* below.

## Building the server (`server.py`)

`build_server()`:

- Loads the spec via `openapi_loader.load_openapi_spec()`.
- Builds the httpx client via `upstream.build_api_client()`.
- Constructs auth via `_build_auth()` (`SupabaseProvider`, or `None` when
  `ENABLE_AUTH=false`).
- Computes route maps: excluded paths first (`MCPType.EXCLUDE`), then a catch-all
  mapping every remaining route to `MCPType.TOOL` (all endpoints, including GET
  lists, become Tools).
- Calls `FastMCP.from_openapi(...)` with branding (name, version, website, icon),
  the server `instructions`, `route_maps`, `mcp_component_fn=_customize_component`
  and `validate_output=True`.
- Registers the middleware chain (see below), then the hand-written tools:
  `docs_tools`, `merchants`, and the unauthenticated operational routes from
  `wellknown`. (Admin UI deep-links are not a tool — the server instructions tell
  the agent how to build `candidate` / `interview` / `result` URLs from `SITE_URL`.)

`_customize_component` runs per generated tool and does three load-bearing things:
prefixes the description with its OpenAPI `[Category]` and the curated hint; sets
a human-readable `title` (both the MCP `title` and `annotations.title`); and
applies the MCP annotations from `naming.py`. FastMCP **swallows exceptions**
raised in this callback and registers the tool uncustomized with only a log
warning, so `tests/test_listing_readiness.py` asserts the built result rather than
trusting the logs.

The module-level `mcp = build_server()` is what Horizon serves. The
`if __name__ == "__main__"` block is a local-dev convenience that runs the HTTP
transport on `$PORT`; Horizon ignores it and serves the `mcp` object directly.

## OpenAPI loading & schema relaxation (`openapi_loader.py`)

`load_openapi_spec()` tries, in order: **live fetch** (`JOBMOJITO_OPENAPI_URL`) →
**local cache** (a temp file, `OPENAPI_CACHE_PATH` to override) → **committed
snapshot** (`data/openapi.snapshot.json`). On a successful live fetch it refreshes
the cache. Every path runs through `_prepare()`, which does two things:

- `inject_operation_ids()` — the published spec has no `operationId`s, so FastMCP
  would auto-name tools from method+path. We inject curated names from
  `naming.py::TOOL_META`; unknown routes are left to auto-naming (new endpoints
  still surface).
- `relax_nullable_schemas()` — see below.

### The nullable/enum trap (important)

The JobMojito spec declares `openapi: 3.1.0` but uses the **OpenAPI 3.0**
`nullable: true` keyword, which JSON Schema (and FastMCP's output validator under
3.1) **ignore**. The API returns `null` for many fields declared as `type: string`
(e.g. `emoji`, `billing_single_position_end_at`, `cover_image_url`,
`calc_duration`). With output validation on, that raises
`None is not of type 'string'` (surfaced to clients as JSON-RPC `-32602`).

`relax_nullable_schemas()` walks every typed property in `paths` + `components`
and, via `_allow_null()`:

- widens `type: "string"` → `type: ["string", "null"]`;
- if the property has an `enum`, appends `null` to it (a null value fails the enum
  check otherwise, even after the type is widened) — matters for nullable **enum**
  response fields such as `status` / `recommendation`;
- sets `nullable: true` (harmless, helps any 3.0 consumer).

`required` arrays are intentionally left untouched, so tool **inputs** still
require their fields; only value **nullability** changes.

**Known gap:** properties declared via `$ref`, `anyOf`, `allOf`, or `oneOf` (no
direct `type` key) are not touched. If a future null-validation error names such a
field, that's where to extend the relaxer.

## Naming, annotations & exclusions (`naming.py`)

`TOOL_META` maps `(METHOD, path)` → a `ToolMeta` dataclass and is the single
source of truth for everything curated about a tool:

| Field | Used for |
|-------|----------|
| `name` | injected as `operationId`; becomes the MCP tool name (≤ 64 chars) |
| `title` | MCP `title` + `annotations.title` (clients read one or the other) |
| `hint` | one-line description prefix that improves model tool-selection |
| `read_only` / `destructive` / `idempotent` / `open_world` | MCP annotations (`readOnlyHint`, …) |
| `justification` | the written rationale OpenAI asks for at submission; surfaced by `annotation_justifications()` |

The `_read()` / `_write()` helpers set the annotation quartet consistently — use
them rather than constructing `ToolMeta` by hand. Endpoints that aren't listed
still get exposed: `fallback_meta()` derives a name and conservative,
method-based annotations (`GET` → read-only, everything else → destructive) so a
new API endpoint is never silently unannotated.

Because directory review rejects any tool that both reads and writes, read and
write live in separate tools — never add a catch-all `api_request(method=…)`.

`IGNORED_PATHS` lists endpoints that must never be exposed (user invites, the
candidate-token one-shot flow, the pre-screening endpoints); `_ignored_paths()`
in `server.py` merges it with the `IGNORED_TOOL_PATHS` env var. Currently 30 spec
endpoints minus 5 ignored = **25 generated API tools**, plus the four
hand-written ones (`search_documentation`, `get_documentation`,
`jobmojito_configuration`, `list_my_merchants`).

## Middleware (`middleware.py`)

Registration order is execution order, so the logger is registered first and
wraps everything below it.

| Middleware | What it does |
|------------|--------------|
| `ToolCallLoggingMiddleware` | Logs tool name, argument **keys** (never values), outcome and timing. Cannot see transport-level 400/404s — those are rejected before any tool runs. |
| `UpstreamErrorMiddleware` | Turns `HTTP error 403: Forbidden - {...}` into cause + concrete next step (keeping a capped upstream detail), so the model stops permuting arguments against a permissions problem. Unrecognised errors are re-raised untouched. |
| `ResultSizeGuardMiddleware` | Rejects results over `MAX_TOOL_RESULT_CHARS` (default 120 000) with pagination guidance. Deliberately an error, not a truncation — a half-list that looks complete is worse, and truncating structured content would break its output schema. |
| `OutputValidationErrorMiddleware` | Rewrites the SDK's path-less "Output validation error" into one that names the offending field(s). |
| `ToolMetadataBackfillMiddleware` | Safety net at `tools/list` time: any tool registered outside the OpenAPI path gets a title and annotations, defaulting to the *safe* (destructive) assumption. |

## Directory-listing readiness (`lazy_auth.py`, `wellknown.py`)

The server is built to pass the Anthropic connector directory, the OpenAI plugin
directory and the MCP Registry without code changes.

- **`lazy_auth.py`** wraps the auth provider class so capability discovery
  (`initialize`, `ping`, `tools/list` and the other `*/list` methods) is served
  **without** a token — crawlers can render the tool list — while every
  `tools/call`, resource read and prompt still requires a verified JWT.
  `WWWAuthenticateScopeMiddleware` adds `scope="openid email"` to the 401
  challenge. This hooks undocumented FastMCP internals, which is why `fastmcp` is
  pinned `<4` and the lazy-auth tests gate any upgrade.
- **`wellknown.py`** registers unauthenticated routes: `GET /healthz` (uptime
  probe), `GET /.well-known/openai-apps-challenge` (domain verification; 404s
  while `OPENAI_APPS_CHALLENGE_TOKEN` is unset) and
  `GET /.well-known/mcp/server-card.json` (Smithery-style server card).
- **`server.json`** is the MCP Registry entry, published by
  `.github/workflows/publish-registry.yml` on a `v*` tag. Its `version` must match
  `SERVER_VERSION` in `server.py` and `pyproject.toml` — a test enforces it, and
  registry versions are immutable.

## Authentication (`upstream.py`, `server.py::_build_auth`)

`SupabaseProvider` makes the server an OAuth resource server derived from
`SUPABASE_PROJECT_URL` (endpoints under `/auth/v1`, ES256, audience
`authenticated`). Discovery is published at
`/.well-known/oauth-protected-resource/mcp`, where `resource` must equal the URL
clients actually connect to (`BASE_URL` + `MCP_PATH`) character for character —
`server._validate_public_identity()` warns at startup when it doesn't.

**Nothing is executed anonymously.** Every tool call — including docs search —
requires a signed-in user; only capability discovery is open, and only because
`ENABLE_LAZY_AUTH` is on (see *Directory-listing readiness*).

Token forwarding is per-request: `get_access_token()` yields the caller's JWT,
which is attached to the upstream call. For local development without OAuth, set
`ENABLE_AUTH=false` (no auth provider) and optionally `JOBMOJITO_DEV_BEARER_TOKEN`
(a real Supabase token) so API tools can still be exercised.

### Token refresh — whose job it is

Supabase access tokens last ~1 hour. **This server never refreshes them and must
not try.** As an OAuth 2.1 resource server it receives an access token and never
a refresh token; the refresh token belongs to the MCP client, issued by Supabase.
There is no channel in MCP for a client to hand a server its refresh token, and
building one — a per-user encrypted refresh-token store, rotation, revocation —
would make this server a confused deputy holding credentials for users' entire
Supabase accounts.

What the server owes the client is exactly two things, and both are config:

1. **Advertise `offline_access`** (`OAUTH_SCOPES_SUPPORTED`). Supabase issues a
   refresh token only when that scope is requested, and clients take the scopes
   to request from the PRM's `scopes_supported` and the `scope=` parameter of the
   401 challenge — both fed by this setting.
2. **Return a 401 when the token stops working**, so the client knows *when* to
   refresh (below).

Given both, expiry is invisible: the client gets a 401, exchanges its refresh
token at Supabase's `/auth/v1/oauth/token`, and retries. No user interaction.
Miss either one and the same expiry escalates to a full browser flow — the first
because there is nothing to refresh with, the second because nothing tells the
client to.

Verify the authorization server still supports it:

```bash
curl -s $SUPABASE_PROJECT_URL/auth/v1/.well-known/oauth-authorization-server
# scopes_supported must include "offline_access"
# grant_types_supported must include "refresh_token"
```

Scopes here are only ever *advertised*, never passed as `required_scopes`.
Requiring a scope rejects tokens that lack it; advertising one asks the client to
obtain a capability. Confusing the two turns a refresh improvement into an outage.

### Making clients re-authenticate (`session_verifier.py`)

Two gates check the same JWT and used to disagree. This server verified it
*statelessly* (signature, `exp`, `iss`, `aud`); the JobMojito Edge Functions
verify it *statefully*, resolving it to a live Supabase session. The stateless
check is always more permissive, so a signed-out or revoked session passed here
and 401'd upstream — and the user got a **tool error**, which is an HTTP 200 with
`isError: true`. MCP clients re-authenticate only on a transport-level 401 with
`WWW-Authenticate`, so nothing ever reconnected.

Note this was never token *expiry*: `JWTVerifier` checks `exp` with no leeway and
no caching, so expired tokens already 401 correctly and clients already refresh.
And refreshing is not this server's job — a resource server receives an access
token and never a refresh token; the refresh token lives with the client. The
only lever available is emitting the 401.

So:

- `SupabaseSessionVerifier` (installed via `SupabaseProvider(token_verifier=…)`)
  runs the Edge Functions' session check — a cached `GET /auth/v1/user` — after
  the JWT stage passes. A dead session now fails during verification, where
  FastMCP already emits `401 Bearer error="invalid_token", resource_metadata=…`.
  One extra HTTP call per token per `SUPABASE_SESSION_CHECK_TTL`.
- It **fails open** on timeouts and 5xx. Failing closed would turn a brief
  Supabase outage into a forced re-auth for every connected user at once.
- `lazy_auth.RejectedTokenGateASGIMiddleware` covers the residual race:
  `UpstreamErrorMiddleware` records any token the API 401s, and the next request
  bearing it gets the challenge. This can't happen in-band on the failing call —
  Streamable HTTP flushes the SSE response headers before the tool runs, so the
  200 is already on the wire by the time the upstream 401 is known.
- Anonymous capability discovery is exempt, so a poisoned token never empties the
  tool list for directory crawlers.

## Documentation tools (`docs_tools.py`, `featurebase.py`, `mintlify.py`)

`search_documentation` runs two backends in parallel and merges/ranks results:

- **Help center** — Featurebase REST (`featurebase.py`) when `FEATUREBASE_API_KEY`
  is set; otherwise an HTML fallback over `help.jobmojito.com`.
- **Developer docs** — Mintlify semantic search (`mintlify.py`) over
  `developer.jobmojito.com`. Uses the public `/mcp` endpoint (no auth); an
  authenticated `/authed/mcp` path with client credentials is supported but not
  enabled.

`get_documentation(url)` fetches a specific page (Featurebase article by id, or a
developer `.md`), restricted to the allowed doc hosts. Docs are read
live/single-source — the repo does not duplicate their content, and the end-user
guides that once lived under `docs/cookbooks/` were moved to Mintlify to keep it
that way.

## Merchant selection (`merchants.py`)

Many endpoints accept `merchant_id`, and a user may have many sub-merchants.
`jobmojito_configuration` is an MCP App (UI) with a searchable "Sub merchant"
picker (type-and-click server search) rendered in clients that support the MCP
Apps UI extension. `list_my_merchants` is the text fallback for clients without UI
support. After a selection, the model passes `merchant_id=<id>` on subsequent
calls (omitted for the user's own account).

## Transport & sessions

The server uses MCP Streamable HTTP. Sessions are identified by `Mcp-Session-Id`.
Per spec, the server returns **400** for a non-initialize request with a missing
session id and **404** for an unknown/expired session; a compliant client must
re-`initialize` on 404. This is standard transport behavior, not application
logic — see `docs/DEPLOYMENT.md` for the operational implications.
