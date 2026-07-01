# CLAUDE.md

Guidance for coding agents working in this repository. Read this first, then the
deeper docs under `docs/` when a task needs them.

## What this is

A **FastMCP** server that exposes the **JobMojito** hiring API (interviews,
candidates, pre-screening, knowledge bases, analytics) plus documentation search
as MCP tools, secured with **Supabase OAuth**. Tools are generated from the live
JobMojito **OpenAPI** spec at startup; end-user Supabase JWTs are forwarded to the
API so every call runs with that user's own permissions.

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
tool inventory, the ignore list, and the schema-relaxation fixes.

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
| `middleware.py` | Server-side tool-call logging (name + arg keys + outcome/timing). |
| `data/openapi.snapshot.json` | Committed cold-start fallback spec. Refresh via the script above. |

More detail: `docs/ARCHITECTURE.md`.

## Conventions & guardrails

- **Adding a module?** Add it to `pyproject.toml [tool.setuptools] py-modules` and
  keep it importable from the repo root (flat layout, no packages).
- **Tool naming** is driven by `naming.py::TOOL_META` (injected as `operationId`,
  which becomes the MCP tool name). New JobMojito endpoints appear automatically
  with an auto-generated name until you add curated metadata.
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
- **Secrets** live in `.env` (gitignored). `.env.example` holds placeholders only —
  never commit real keys.
- **Merchant scoping:** most API tools accept `merchant_id`. The UI picker is
  `jobmojito_configuration`; `list_my_merchants` is the non-UI fallback.

## Deployment (must-know)

The Horizon deployment must be **direct**, not behind the managed auth gateway,
because the gateway can't carry the Supabase token. Requirements: `ENABLE_AUTH=true`
and `BASE_URL` set to the public URL (e.g. `https://jobmojito.fastmcp.app`, no
`/mcp` suffix). Verify direct mode from the live discovery doc:

```bash
curl -s https://jobmojito.fastmcp.app/.well-known/oauth-protected-resource/mcp
# authorization_servers must be your Supabase project (…supabase.co/auth/v1)
```

Full steps, env var reference, and OAuth/session notes: `docs/DEPLOYMENT.md`.

## Where to look next

- `docs/ARCHITECTURE.md` — how the pieces fit, request lifecycle, extension points.
- `docs/DEVELOPMENT.md` — local setup, env vars, and recipes (add/rename/ignore a
  tool, refresh the snapshot, add a config field, debug validation errors).
- `docs/DEPLOYMENT.md` — Horizon deploy, auth/OAuth, session behavior, redeploy checklist.
- `docs/cookbooks/` — **end-user** product docs (Mintlify), a different audience
  from these developer docs.
- `README.md` — project overview.
