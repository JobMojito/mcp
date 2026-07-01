# Architecture

How the JobMojito MCP server is put together, for developers and coding agents.

## Overview

The server turns the JobMojito REST API (a set of Supabase Edge Functions) into
MCP tools, and adds a small number of hand-written tools (documentation search,
admin deep-links, merchant selection). It is a single asynchronous Python process
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
5. `middleware.py` logs the call name, argument keys, and outcome/timing.

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
  (adds a `[Category]` prefix + tag per tool), and `validate_output=True`.
- Registers the logging middleware, then the hand-written tools: `docs_tools`,
  `merchants`. (Admin UI deep-links are not a tool — the server instructions tell
  the agent how to build `candidate` / `interview` / `result` URLs from `SITE_URL`.)

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

## Naming & exclusions (`naming.py`)

`TOOL_META` is the single source of truth for curated tool names and the one-line
description hints that improve model tool-selection. `IGNORED_PATHS` lists
endpoints that must never be exposed (admin/candidate-token/pre-screening flows);
`_ignored_paths()` in `server.py` merges it with the `IGNORED_TOOL_PATHS` env var.

## Authentication (`upstream.py`, `server.py::_build_auth`)

`SupabaseProvider` makes the server an OAuth resource server derived from
`SUPABASE_PROJECT_URL` (endpoints under `/auth/v1`, ES256, audience
`authenticated`). Discovery is published at
`/.well-known/oauth-protected-resource/mcp`. There is **no anonymous access** in
production; every tool (including docs search) requires a signed-in user.

Token forwarding is per-request: `get_access_token()` yields the caller's JWT,
which is attached to the upstream call. For local development without OAuth, set
`ENABLE_AUTH=false` (no auth provider) and optionally `JOBMOJITO_DEV_BEARER_TOKEN`
(a real Supabase token) so API tools can still be exercised.

## Documentation tools (`docs_tools.py`, `featurebase.py`, `mintlify.py`)

`search_documentation` runs two backends in parallel and merges/ranks results:

- **Help center** — Featurebase REST (`featurebase.py`) when `FEATUREBASE_API_KEY`
  is set; otherwise an HTML fallback over `help.jobmojito.com`.
- **Developer docs** — Mintlify semantic search (`mintlify.py`) over
  `developer.jobmojito.com`. Uses the public `/mcp` endpoint (no auth); an
  authenticated `/authed/mcp` path with client credentials is supported but not
  enabled.

`get_documentation(url)` fetches a specific page (Featurebase article by id, or a
developer `.md`). Docs are read live/single-source — the repo does not duplicate
their content. (The `docs/cookbooks/*.mdx` files are authored end-user guides
published to Mintlify, a separate concern from these tools.)

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
