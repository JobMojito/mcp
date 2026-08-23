# CLAUDE.md

Guidance for coding agents working in this repository. Read this first, then the
deeper docs under `docs/` when a task needs them.

## What this is

A **FastMCP** server that exposes the **JobMojito** hiring API (interviews and
role-play personas, the coaching catalogue, candidates and results, knowledge
bases, merchants/analytics) plus documentation search as MCP tools, secured with
**Supabase OAuth**. Tools are generated from the live JobMojito **OpenAPI** spec
at startup; end-user Supabase JWTs are forwarded to the API so every call runs
with that user's own permissions.

Current surface: **25 API tools** (30 spec endpoints minus the 5 in
`IGNORED_PATHS`) + `search_documentation`, `get_documentation`,
`jobmojito_configuration`, `list_my_merchants`. `naming.py` is the inventory —
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
`upstream.py` httpx client (forwards the user's JWT) → JobMojito API → response
validated against the (relaxed) OpenAPI output schema → back to client.

| File | Responsibility |
|------|----------------|
| `server.py` | Builds the `mcp` object (`build_server()`), server instructions, route maps, branding, registers docs/UI/merchant tools + logging middleware. **Entrypoint: `mcp`.** |
| `config.py` | `Settings` dataclass loaded from env (+ `.env` via dotenv). Single source of config. |
| `openapi_loader.py` | Loads spec (live → cache → snapshot), injects curated `operationId`s, **relaxes nullable/enum schemas** for output validation. |
| `naming.py` | `TOOL_META` maps `(METHOD, path)` → curated tool name + hint. `IGNORED_PATHS` excludes endpoints. |
| `upstream.py` | httpx `AsyncClient` + `SupabaseTokenForwardAuth` (forwards end-user JWT; `apikey` header; dev-token fallback). |
| `docs_tools.py` | `search_documentation` (help + developer in parallel) and `get_documentation`. |
| `featurebase.py` / `mintlify.py` | Help-center (Featurebase REST) and developer-docs (Mintlify) backends for docs search. |
| `merchants.py` | `jobmojito_configuration` (UI picker MCP App) + `list_my_merchants` (text fallback). |
| `middleware.py` | Tool-call logging, upstream-error rewriting, result-size guard, output-validation errors, annotation backfill. |
| `lazy_auth.py` | Auth provider subclass that serves `initialize`/`tools/list` unauthenticated and adds `scope=` to the 401 challenge. |
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
  `enum`. Do **not** disable output validation to work around a null error — extend
  the relaxer. Known gap: fields declared via `$ref`/`anyOf`/`allOf` (no direct
  `type`) aren't touched. Any schema fix must ship with a regression test.
- **Auth is mandatory in production and must never be bypassed.** The end-user's
  Supabase JWT is forwarded by `upstream.py`. For local testing use
  `ENABLE_AUTH=false` or supply `JOBMOJITO_DEV_BEARER_TOKEN`.
- **Lazy auth is deliberate, and narrow.** `lazy_auth.py` serves only capability
  discovery (`initialize`, `tools/list`, `ping`, the other `*/list` methods)
  without a token, so directory crawlers can show the tool list. Every tool call,
  resource read and prompt still requires a verified JWT. If you add a method to
  `PUBLIC_METHODS`, you are making it anonymous — be sure it exposes no data.
  It hooks two undocumented FastMCP internals, so `fastmcp` stays pinned `<4` and
  the lazy-auth tests gate any upgrade.
- **`BASE_URL` is the OAuth resource identifier**, not decoration. It must equal
  the URL clients connect to, character for character, or discovery succeeds while
  every client 401s. `server._validate_public_identity()` logs the exact curl to
  verify after a deploy.
- **Secrets** live in `.env` (gitignored). `.env.example` holds placeholders only —
  never commit real keys.
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
