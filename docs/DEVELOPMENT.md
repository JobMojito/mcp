# Development

Local setup, configuration, and common tasks.

## Prerequisites

- Python ≥ 3.10
- A virtual environment is recommended (`python -m venv .venv && source .venv/bin/activate`)

## Install

```bash
pip install -e ".[dev]"     # runtime + pytest
# or, runtime only:
pip install -r requirements.txt
```

Runtime deps: `fastmcp[apps]>=3.4,<4`, `httpx`, `python-dotenv`.

## Run locally

```bash
# No auth (simplest). Serves Streamable HTTP on http://localhost:8000/mcp
ENABLE_AUTH=false python server.py

# Choose a port
ENABLE_AUTH=false PORT=8931 python server.py
```

With `ENABLE_AUTH=false` there is no OAuth provider and API calls go out without a
user token. To exercise API tools against the real backend, either keep auth on
and connect through a client, or set `JOBMOJITO_DEV_BEARER_TOKEN` to a valid
Supabase access token (development only — never in production).

At startup the loader fetches the live spec; if the network/host is unavailable it
falls back to the local cache and then `data/openapi.snapshot.json`.

## Test

```bash
ENABLE_AUTH=false pytest -q
```

Tests are hermetic: `tests/test_smoke.py` sets `JOBMOJITO_OPENAPI_URL` to an
unreachable host so the loader uses the committed snapshot, and disables the
external doc sources. They cover the tool inventory, ignore list, schema
relaxation (nullable + enum), admin-URL building, and the docs parsers/ranking.
**Add a regression test with any schema or naming fix.**

## Inspect the built server

```bash
fastmcp inspect server.py:mcp -f fastmcp
```

Exit code 0 with the expected tool list means Horizon will build it too. Useful
after changing tool names, the ignore list, or anything in `build_server()`.

## Configuration (environment variables)

All config flows through `config.py::Settings`. A local `.env` (gitignored) is
loaded automatically; `.env.example` documents every variable with placeholders.
Selected keys:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_AUTH` | `true` | Master switch for Supabase OAuth. Set `false` only for local dev. |
| `BASE_URL` | `http://localhost:8000` | Public server URL used for OAuth discovery. Must be the real https URL in production. |
| `PORT` | `8000` | Local HTTP port (honored by the `__main__` block). |
| `JOBMOJITO_API_BASE_URL` | `https://cool.jobmojito.com/functions/v1` | Upstream API base. |
| `JOBMOJITO_OPENAPI_URL` | `<api base>/openapi` | Live OpenAPI spec URL. |
| `SUPABASE_PROJECT_URL` | `https://momsbvnltsydezmoesqt.supabase.co` | Supabase project (auth server). |
| `SUPABASE_JWT_ALGORITHM` | `ES256` | JWT signing alg. |
| `SUPABASE_ANON_KEY` | – | Sent as `apikey` header to Edge Functions. |
| `JOBMOJITO_DEV_BEARER_TOKEN` | – | Dev-only fallback token when no auth context. |
| `SITE_URL` | `https://app.jobmojito.com` | JobMojito app base (consent + the admin deep-link patterns embedded in the server instructions). |
| `OAUTH_CONSENT_PATH` | `/oauth/consent` | Consent path on the app. |
| `IGNORED_TOOL_PATHS` | – | Comma-separated extra endpoint paths to exclude. |
| `OPENAPI_CACHE_PATH` | temp dir | Override the runtime spec cache location. |
| `FEATUREBASE_API_KEY` | – | Enables the Featurebase REST help-center source. |
| `DEVELOPER_DOCS_MCP_URL` | `https://developer.jobmojito.com/mcp` | Mintlify developer-docs MCP (public). |

See `config.py` for the complete list (docs cache TTL, Featurebase base/version,
Mintlify client-credentials, etc.).

## Recipes

### Add or rename an API tool
Tool names come from `naming.py::TOOL_META` (`(METHOD, path) → (name, hint)`),
injected as the OpenAPI `operationId`. Add/edit the entry, then update the
expected-tools set in `tests/test_smoke.py` and run `pytest` + `fastmcp inspect`.
New endpoints appear automatically with an auto-generated name until curated here.

### Exclude an endpoint
Add its path to `naming.py::IGNORED_PATHS` (permanent) or set `IGNORED_TOOL_PATHS`
at deploy time (per-environment). Add it to the ignored-tools assertion in the
smoke test.

### Refresh the OpenAPI snapshot
```bash
python scripts/update_snapshot.py
```
Commit the updated `data/openapi.snapshot.json`. This is only the cold-start
fallback; the server still fetches live at boot. Refresh it when the API changes
shape so offline/first-run builds stay accurate.

### Fix a null-validation error (`-32602 … must be string`)
This means a field came back `null` that the relaxer didn't cover. First confirm
the field's schema (`type` + whether it has an `enum` or uses `$ref`/`anyOf`).
`relax_nullable_schemas` already handles any property with a direct `type` (and
its enum). If the field is declared via `$ref`/`anyOf`/`allOf` with no direct
`type`, extend the relaxer to handle that shape. Do **not** disable
`validate_output`. Add a regression test using the real snapshot.

### Add a configuration field
Add it to `config.py::Settings`, populate it in `load_settings()` with an env
lookup + sensible default, and document it in `.env.example` and the table above.

### Add a hand-written tool
Create a module, expose a `register(mcp)` (or an MCP App), call it from
`build_server()` in `server.py`, and add the module to
`pyproject.toml [tool.setuptools] py-modules`.

## Gotchas

- **Flat layout:** every module sits at the repo root and must be importable
  directly. New modules must be added to `py-modules` in `pyproject.toml`, or
  Horizon won't package them.
- **Don't disable output validation** to silence a null error — extend the
  relaxer instead (see above).
- **Secrets:** only in `.env`. Keep `.env.example` free of real values.
- **`data/openapi.cache.json` vs the temp cache:** the committed file under
  `data/` is the snapshot fallback; the *runtime* cache defaults to the OS temp
  dir (`OPENAPI_CACHE_PATH` to relocate). Don't rely on the temp cache persisting.
