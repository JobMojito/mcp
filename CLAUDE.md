# CLAUDE.md

Guidance for coding agents working in this repository. Read this first, then the
deeper docs under `docs/` when a task needs them.

## What this is

A **FastMCP** server that exposes the **JobMojito** hiring API (interviews and
role-play personas, the coaching catalogue, candidates and results, knowledge
bases, merchants/analytics, custom reports, portal configuration, the media library) plus documentation search as MCP tools, secured with
**Supabase OAuth**. Tools are generated from the live JobMojito **OpenAPI** spec
at startup; end-user Supabase JWTs are forwarded to the API so every call runs
with that user's own permissions.

Current surface: **67 API tools** (82 spec operations minus the 15 excluded by
`IGNORED_PATHS` / `IGNORED_OPERATIONS`) + `search_documentation`, `get_documentation`,
`jobmojito_configuration`, `list_my_merchants`, `invite_candidates`, `upload_media_file`,
`get_tool_reference`. `naming.py` is the inventory —
don't maintain a tool list anywhere else.

- Language: Python ≥ 3.10. Single async process, served over Streamable HTTP.
- Hosting: **Prefect Horizon / FastMCP Cloud**, entrypoint `server.py:mcp`.
- Flat module layout (all modules at repo root) so Horizon can run
  `server.py:mcp` with no build step.

## Commands

```bash
# Install (runtime + dev/test deps)
pip install -e ".[dev]"          # or: pip install -r requirements.txt

# Run locally WITHOUT auth (no Supabase token needed); serves http on $PORT (8000)
ENABLE_AUTH=false python server.py

# Run a specific port
ENABLE_AUTH=false PORT=8931 python server.py

# Tests (hermetic/offline — they point the OpenAPI URL at an unreachable host
# so the loader falls back to data/openapi.snapshot.json)
ENABLE_AUTH=false pytest -q

# Inspect the built server (tool list, schemas) the way Horizon builds it
fastmcp inspect server.py:mcp -f fastmcp

# Refresh the committed OpenAPI snapshot from the live endpoint
python scripts/update_snapshot.py
```

Always run `pytest` before finishing a change; the suite is fast and covers the
tool inventory, the ignore list, the schema-relaxation fixes
(`tests/test_smoke.py`) and the directory-listing requirements — titles,
annotations, justifications, version consistency, lazy auth, error rewriting
(`tests/test_listing_readiness.py`).

## Architecture at a glance

Request flow for an API tool: MCP client → (Supabase OAuth) → `server.py` tool →
`upstream.py` httpx2 client (forwards the user's JWT) → JobMojito API → response
validated against the (relaxed) OpenAPI output schema → back to client.

| File | Responsibility |
|------|----------------|
| `server.py` | Builds the `mcp` object (`build_server()`), server instructions, route maps, branding, registers docs/UI/merchant tools + logging middleware. **Entrypoint: `mcp`.** |
| `config.py` | `Settings` dataclass loaded from env (+ `.env` via dotenv). Single source of config. |
| `openapi_loader.py` | Loads spec (live → cache → snapshot), injects curated `operationId`s, **relaxes nullable/enum schemas** for output validation. |
| `naming.py` | `TOOL_META` maps `(METHOD, path)` → curated tool name + hint. `IGNORED_PATHS` excludes endpoints. |
| `raw_assessment.py` | Condenses the per-answer raw assessment blob for `get_interview_result_details` `view="full"`. |
| `upstream.py` | httpx2 `AsyncClient` + `SupabaseTokenForwardAuth` (forwards end-user JWT; `apikey` header; dev-token fallback). |
| `docs_tools.py` | `search_documentation` (help + developer in parallel) and `get_documentation`. |
| `featurebase.py` / `mintlify.py` | Help-center (Featurebase REST) and developer-docs (Mintlify) backends for docs search. |
| `merchants.py` | `jobmojito_configuration` (UI picker MCP App) + `list_my_merchants` (text fallback). |
| `invitations.py` | `invite_candidates`: `/invite-users` with candidate rows only (the endpoint is otherwise ignored — its `type` also creates staff accounts). |
| `tool_reference.py` | Short tool definitions in tools/list, the full ones from `get_tool_reference` (served without a token). |
| `media_upload.py` | `upload_media_file`: start, send the base64 bytes to the signed address, finish, for a client that cannot send a file itself. |
| `middleware.py` | Tool-call logging, upstream-error rewriting, result-size guard, output-validation errors, annotation backfill. |
| `lazy_auth.py` | Auth-layer ASGI middleware: serves `server/discover`/`initialize`/`tools/list` unauthenticated, adds `scope=` to the 401 challenge, 401s tokens the API has rejected, and reports the transport-level 400/404s that never reach a tool. |
| `session_verifier.py` | Token verifier that also resolves the JWT to a live Supabase session (what the Edge Functions check), + the rejected-token registry behind the re-auth challenge. |
| `wellknown.py` | Unauthenticated routes: `/healthz`, OpenAI domain challenge, Smithery server card. |
| `server.json` | Official MCP Registry entry (published by `.github/workflows/publish-registry.yml`). |
| `data/openapi.snapshot.json` | Committed cold-start fallback spec. Refresh via the script above. |

More detail: `docs/ARCHITECTURE.md`.

## Conventions & guardrails

- **Adding a module?** Add it to `pyproject.toml [tool.setuptools] py-modules` and
  keep it importable from the repo root (flat layout, no packages).
- **Tool naming AND annotations** are driven by `naming.py::TOOL_META` (a
  `ToolMeta` per endpoint: name, title, hint, annotations, justification). The name
  is injected as `operationId` and becomes the MCP tool name; `server.py::
  _customize_component` applies the rest. New JobMojito endpoints appear
  automatically with an auto-generated name and conservative method-derived
  annotations (`naming.fallback_meta`) until you add curated metadata.
- **Every tool MUST have a `title` and either `readOnlyHint: true` or
  `destructiveHint: true`.** Both the Anthropic and OpenAI directories reject
  servers that don't, and read-only tools run without a per-call confirmation
  prompt. Two traps: FastMCP *silently swallows* exceptions raised inside
  `mcp_component_fn`, and tools registered outside the OpenAPI path bypass it
  entirely — hence `ToolMetadataBackfillMiddleware` (safe default: destructive) and
  the assertions in `tests/test_listing_readiness.py`. Never weaken those tests to
  make a change pass.
- **Splitting read from write is a hard requirement,** not a style preference:
  Anthropic rejects any tool that can both read and write. Never add a catch-all
  `api_request(method=...)` tool.
- **The version lives in ONE place: `server.py::SERVER_VERSION`.** Bump it with
  `python scripts/set_version.py 1.2.3` and commit; nothing else is edited by
  hand. `pyproject.toml` resolves it at build time via
  `[tool.setuptools.dynamic]`, and `server.json` — which cannot derive anything,
  because `mcp-publisher` reads that file directly — is rewritten from
  `SERVER_VERSION` by the publish workflow, so a stale copy cannot reach the
  registry. **`SERVER_VERSION` must stay a plain string literal**: setuptools
  extracts it by AST, and a computed value makes it fall back to *importing*
  `server.py` at build time, which fails in any clean environment without
  fastmcp. Tests pin all three properties.
- **Publishing is triggered by `server.py` changing on `main`, not by a git tag.**
  The workflow compares `SERVER_VERSION` against the previous commit and exits
  early when it is unchanged, so ordinary edits to `server.py` are a no-op run
  rather than a failed publish. Registry versions are immutable — bump, never
  amend. `workflow_dispatch` forces a publish when you need one.
- **Excluding an endpoint:** add its path to `naming.py::IGNORED_PATHS`, or set
  the `IGNORED_TOOL_PATHS` env var (comma-separated) at deploy time.
- **Output validation stays ON** (`validate_output=True`). The JobMojito spec is
  tagged OpenAPI 3.1 but uses 3.0-style `nullable: true` (ignored by JSON Schema)
  and returns `null` for many "string" fields. `openapi_loader.relax_nullable_schemas`
  fixes this by widening `type` to `[..., "null"]` **and** adding `null` to any
  `enum`, on everything a **response** can reach. Do **not** disable output validation
  to work around a null error — extend the relaxer. Known gap: fields declared via
  `$ref`/`anyOf`/`allOf` (no direct `type`) aren't touched. Any schema fix must ship
  with a regression test.
- **A tool's input schema offers null only where the API declares `nullable: true`.**
  The model reads it, and a null it offers on a field the API refuses invites stray
  nulls. A component both a request and a response use is split: the request keeps it,
  the response gets a `<Name>__nullable_response` copy. Never widen a request schema
  to fix an output error. What a null does once sent is a separate question:
  `ToolMeta.null_body_args`.
- **Auth is mandatory in production and must never be bypassed.** The end-user's
  Supabase JWT is forwarded by `upstream.py`. For local testing use
  `ENABLE_AUTH=false` or supply `JOBMOJITO_DEV_BEARER_TOKEN`.
- **Lazy auth is deliberate, and narrow.** `lazy_auth.py` serves only capability
  discovery (`server/discover` on 2026-07-28; `initialize`, `ping` on the older
  versions; `tools/list` and the other `*/list` methods) without a token, so directory crawlers can show the tool list, plus
  `resources/read` of a `ui://` MCP App template (`PUBLIC_RESOURCE_PREFIXES`: static HTML, and OpenAI's
  scan fails without it). Every tool call, other resource read and prompt still requires a verified JWT. If you add a method to
  `PUBLIC_METHODS`, you are making it anonymous — be sure it exposes no data.
  It hooks two undocumented FastMCP internals, so `fastmcp` stays pinned `<5` and
  the lazy-auth tests gate any upgrade.
- **FastMCP 4 / MCP SDK 2 serve two protocol eras on one endpoint.** 2026-07-28
  is sessionless: no `initialize`, no `Mcp-Session-Id`, every request carries its
  version in `_meta` and the `MCP-Protocol-Version` / `Mcp-Method` headers, and
  `server/discover` replaces the handshake. 2025-11-25 and older keep
  `initialize` and sessions. Under FastMCP 3 a 2026-07-28 client got HTTP 400
  "Unsupported protocol version". Two FastMCP 4 changes to keep in mind: it uses
  **httpx2** for every HTTP call, so this server does too (an `httpx` client,
  `Auth` or `except httpx.` no longer matches); and SDK models store snake_case
  fields, so read annotations with `model_dump(by_alias=True)` (the wire names) or
  `read_only_hint`, never `readOnlyHint` as an attribute. App-only tools
  (`search_merchants`) are now listed in `tools/list`; hiding them is the host's
  job under the MCP Apps spec.
- **tools/list is short on purpose; `get_tool_reference` has the rest.** A client
  hands every tool's description and input schema to the model up front, and the
  generated definitions were ~65k tokens. `server._customize_component` captures
  each generated tool in full (`tool_reference.capture`) and then advertises: the
  "[Category] hint" as the description, each top-level argument's first sentence
  plus every sentence that states a behaviour (null, empty value, omit, only, never,
  default, e-mail, dry_run...), on an enum argument one "Values:" line with each
  option's first sentence (the spec's "Options — `a`: … | `b`: …", "- `a`: …" lines
  or inline "`a`: … | `b`: …"), no examples or nested descriptions, and an output
  schema without annotations. Where that trimmed text reads badly, write the short
  version in the spec source next to the field's description:
  `.openapi({ description: '<long, for the docs>', 'x-mcp-description': '<short>' })`.
  tools/list shows the override; `get_tool_reference` and Mintlify (which validates it,
  `mint openapi-check`, and ignores x- keys) show the long text. Write tool names in an
  override; endpoint names are rewritten there too. A flag typed as the strings
  'true'/'false' gets "Send the string 'true' or 'false'." added. An exception in this
  hook is swallowed by FastMCP and leaves tools untrimmed, so a new rule needs a test on
  a nullable schema. Separately, `openapi_loader.apply_tool_names` rewrites
  endpoint names in every description to tool names (`naming.MERGED_ENDPOINTS` for
  hidden endpoints), so write the spec in endpoint names and the model still reads tools.
  The same pass turns table-name ids (`profile_interview.id`) into where a model gets
  them (`naming.TABLE_IDS`), and `openapi_loader.boolean_query_flags` makes the
  'true'/'false' query enums plain booleans (httpx sends True as "true"). The structure (types,
  enums, required, nesting) is never touched — `tests/test_tool_reference.py`
  compares it. So a rule a model must follow belongs in the hint or in a sentence
  the rule regex keeps; detail belongs in the spec, where `get_tool_reference`
  serves it. FastMCP shares `$defs` objects between tools: trim copies, never in
  place. `get_tool_reference` is the one tool `lazy_auth` serves without a token
  (`PUBLIC_TOOL_CALLS`): it reads only the captured spec, never the API.
- **Tool text states facts; it never orders the model.** The Anthropic directory's
  compliance step attests that tool descriptions carry no instructions about model
  behaviour or other tools, and a reviewer reads argument texts and what
  `get_tool_reference` serves as tool text too. So "dry_run=true shows …", not "Use
  dry_run first"; "it takes the preview_token of preview_candidate_merge", not "Call
  preview_candidate_merge first"; no "tell/ask the user", "confirm with the user",
  "when the user asked", "use X instead", no developer-guide URLs. Hints are written that
  way; the API spec keeps its developer-docs voice and the MCP restates it:
  `naming._STATEMENT_FIXES` (spec pass, both `description` and `x-mcp-description`) and
  `naming._REFERENCE_FIXES` (applied by `tool_reference.capture` to the full texts, which
  also drops `developer.jobmojito.com` link sentences). Tests fail on a fix whose text the
  API reworded and on the phrasings above (`test_tool_descriptions_state_facts_...`).
  The server instructions may guide the model; they are not tool descriptions.
- **The tool error rate is a floor, not a total.** Streamable HTTP answers a stale
  `Mcp-Session-Id` with a 400, a terminated session with a 404 and a protocol
  version it does not support with a 400, all *before* dispatch, so those failures appear in no tool-call metric and the PostHog MCP
  adapter — which hooks the request handlers — never sees them either.
  `lazy_auth.TransportRejectionASGIMiddleware` reports them (401 excluded: that is
  the lazy-auth handshake, matching the `$exception` filter's carve-out). When a
  reliability question can't be answered from `$mcp_tool_call` alone, this is the
  layer that was missing — check it before concluding the error rate is what it
  looks like.
- **An MCP tool result costs ~2× the API's JSON, and OpenAPI `default`s are never
  sent.** The payload goes over the wire in both `content` and
  `structuredContent` (FastMCP's `ToolResult` derives the text copy from the
  structured one — spec-sanctioned, for clients that ignore `structuredContent`),
  and `MAX_TOOL_RESULT_CHARS` counts both — so an endpoint with fat rows can blow
  the budget at a page size the API considers modest (`list_avatars` at the spec's
  default of 50 returned ~247k chars and failed on every plain call). Fix it with
  `ToolMeta.param_defaults`, which feeds *both*
  `openapi_loader.apply_param_defaults` (the schema the model reads) and
  `middleware.CuratedDefaultsMiddleware` (what actually gets sent). The second is
  required: FastMCP's `RequestDirector` serialises only supplied arguments, so a
  spec `default` the model omits never reaches the API. Recipe in
  `docs/DEVELOPMENT.md`.
- **`MAX_TOOL_RESULT_CHARS` is 150,000 because that is a real client ceiling,**
  not a round number: Claude.ai/Desktop cap tool results near 150k characters,
  Claude Code at 25,000 tokens (~100k chars, raisable via `MAX_MCP_OUTPUT_TOKENS`),
  and ChatGPT/Cursor/Copilot truncate silently at undocumented sizes. Don't raise
  it past the strictest host you actually target — over a client's own limit the
  result is dropped or cut in half, which reads as a broken tool instead of a
  clear error. The rationale and per-client numbers live in `config.py`.
- **Dropping the duplicate payload copy is a last resort, never routine.** When a
  result is over budget, `ResultSizeGuardMiddleware` first checks whether
  `structuredContent` alone would fit; if so it replaces the duplicate `content`
  text with a short pointer and lets the call succeed. That trades client breadth
  (a client that ignores `structuredContent` now sees only the note) for a result
  that arrives at all, so it must stay conditional on being over the limit.
- **Narrowing a page is honest; narrowing a record is not.** Still over budget on
  a **read-only** tool called with a `limit`, the guard re-runs the call once at a
  page size solved from what just overflowed, and labels the result as one page.
  That is safe because the response envelope's `pagination.has_more` already tells
  the model there is more. The same move on a `view` would not be: a record that
  came back with fields pruned is indistinguishable from one that never had them,
  so an oversized `view` is refused with the narrower view **named** in the
  message. Writes are never repeated — `naming.read_only_tool_names()` is the
  gate, and it is built from the curated table so an uncurated new endpoint can
  never opt in by accident. `AUTO_NARROW_OVERSIZED_RESULTS=false` reverts to
  erroring immediately.
- **Pagination cannot narrow a single wide record — that's what `view` is for.**
  `get_interview_result_details` returns one interview whose per-answer raw
  assessment blobs dominate the payload; there is no `limit` to lower and the API
  has no field-selection parameter. `ToolMeta.views` declares named projections
  and needs *both* halves: `openapi_loader.inject_view_params` puts the argument
  in the schema (or the model can't ask for it) and
  `middleware.ResponseViewMiddleware` strips it before the upstream call (it is
  **not** an API parameter) and prunes the response. `view="full"` is not
  verbatim: `raw_assessment.py` condenses each `answer_assessment_raw_data` to its
  summaries (verbatim, it overflowed on every real interview). Every prunable field must be
  optional in the response schema, or output validation rejects the pruned
  result. Register `ResponseViewMiddleware` **last** so it is innermost and the
  size guard measures what the client actually gets. Recipe in
  `docs/DEVELOPMENT.md`.
- **Token refresh is the client's job; `offline_access` is what enables it.**
  Supabase access tokens last ~1h. This server is a resource server — it never
  receives a refresh token and must never store one. Its whole contribution is
  advertising `offline_access` in `OAUTH_SCOPES_SUPPORTED` (so Supabase issues a
  refresh token) and returning a 401 when a token stops working (so the client
  knows to use it). With both, expiry is a silent refresh; drop either and users
  re-authorize in a browser every hour. Scopes are *advertised*, never passed as
  `required_scopes` — requiring one rejects tokens instead of requesting a
  capability.
- **An auth failure must reach the client as an HTTP 401, never as a tool error.**
  MCP clients re-authenticate only on a transport 401 carrying `WWW-Authenticate`;
  a `ToolError` is an HTTP 200 with `isError: true` and no client acts on it, so
  the session stays wedged no matter what the message says. Two mechanisms keep
  this true and both must stay: `session_verifier.SupabaseSessionVerifier` runs
  the Edge Functions' *stateful* session check at the gate (a bare `JWTVerifier`
  only checks signature/`exp`/`iss`/`aud`, which is strictly more permissive than
  the API), and `lazy_auth.RejectedTokenGateASGIMiddleware` challenges any token
  the API has already 401'd. It cannot be done in-band on the failing call:
  Streamable HTTP flushes the SSE response headers *before* the tool runs. The
  session check deliberately **fails open** — a Supabase outage must not force a
  fleet-wide re-auth. Never "fix" a 401 by softening either one.
- **`BASE_URL` is the OAuth resource identifier**, not decoration. It must equal
  the URL clients connect to, character for character, or discovery succeeds while
  every client 401s. `server._validate_public_identity()` logs the exact curl to
  verify after a deploy.
- **Secrets** live in `.env` (gitignored). `.env.example` holds placeholders only —
  never commit real keys.
- **A value the API takes can be held back from a tool:** `naming.WITHHELD_VALUES`
  (with `WITHHELD_FIELD_TEXT` for the field's description). `openapi_loader.withhold_values`
  drops it from the advertised `enum`/`pattern` and `middleware.WithheldValuesMiddleware`
  refuses a call that sends it. Used for `request_candidate_documents`
  `source_field_id=form_national_id`: an AI assistant does not ask for a government ID
  (OpenAI's plugin scan, 9 Nov 2026); the employer still can in the admin.
- **Merchant scoping:** most API tools accept `merchant_id`. The UI picker is
  `jobmojito_configuration`; `list_my_merchants` is the non-UI fallback.

## Deployment (must-know)

The Horizon deployment must be **direct**, not behind the managed auth gateway,
because the gateway can't carry the Supabase token. Requirements: `ENABLE_AUTH=true`
and `BASE_URL` set to the public URL (e.g. `https://mcp.jobmojito.com`, no
`/mcp` suffix). Verify direct mode from the live discovery doc:

```bash
curl -s https://mcp.jobmojito.com/.well-known/oauth-protected-resource/mcp
# authorization_servers must be your Supabase project (…supabase.co/auth/v1)
```

Full steps, env var reference, and OAuth/session notes: `docs/DEPLOYMENT.md`.

## Where to look next

- `docs/ARCHITECTURE.md` — how the pieces fit, request lifecycle, extension points.
- `docs/DEVELOPMENT.md` — local setup, env vars, and recipes (add/rename/ignore a
  tool, refresh the snapshot, add a config field, debug validation errors).
- `docs/DEPLOYMENT.md` — Horizon deploy, auth/OAuth, session behavior, redeploy checklist.
- `README.md` — project overview.

**End-user** product docs are *not* in this repo. They live on
`help.jobmojito.com` (Featurebase) and `developer.jobmojito.com` (Mintlify), and
the server reads them live through `docs_tools.py`. The former `docs/cookbooks/`
guides were moved out to Mintlify — never re-add copies here, or the docs tools
start answering from two sources.
