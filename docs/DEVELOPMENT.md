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

Tests are hermetic: they set `JOBMOJITO_OPENAPI_URL` to an unreachable host so the
loader uses the committed snapshot, and disable the external doc sources.

- `tests/test_smoke.py` — tool inventory, ignore list, schema relaxation
  (nullable + enum), admin-URL building, docs parsers/ranking, Mintlify token
  caching.
- `tests/test_listing_readiness.py` — the directory-review contract: every tool
  has a title, annotations and a justification; read and write stay separate
  tools; `SERVER_VERSION` / `pyproject.toml` / `server.json` agree; the
  operational `/.well-known` routes exist; lazy auth installs both middlewares;
  upstream errors get actionable guidance.

**Add a regression test with any schema or naming fix**, and never weaken a
listing-readiness assertion to make a change pass — those encode requirements the
Anthropic and OpenAI directories enforce at submission.

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
| `MCP_PATH` | `/mcp` | Path the MCP endpoint is served on; combined with `BASE_URL` to form the OAuth resource id. |
| `ENABLE_LAZY_AUTH` | `true` | Serve `initialize`/`tools/list` without a token so directory crawlers can list tools. Tool calls still require a JWT. |
| `OAUTH_SCOPES_SUPPORTED` | `openid,email` | Advertised in the protected-resource metadata and in `scope=` on the 401 challenge. |
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
| `MAX_TOOL_RESULT_CHARS` | `120000` | Result-size ceiling; oversized results are refused with pagination guidance. `0` disables. |
| `FEATUREBASE_API_KEY` | – | Enables the Featurebase REST help-center source. |
| `DEVELOPER_DOCS_MCP_URL` | `https://developer.jobmojito.com/mcp` | Mintlify developer-docs MCP (public). |
| `DOCS_CACHE_TTL_MINUTES` | `60` | How long doc search results/indexes are cached. |
| `SERVER_ICON_URL` / `SERVER_ICON_MIME` | `https://jobmojito.com/favicon.png`, `image/png` | Logo advertised to clients and directories (a square 512×512 PNG); startup warns if pointed at a `.ico`. |
| `OPENAI_APPS_CHALLENGE_TOKEN` | – | Served verbatim at `/.well-known/openai-apps-challenge`; the route 404s while unset. |

See `config.py` for the complete list (Featurebase base/version, Mintlify
client-credentials, marketing/privacy/support URLs, registry server name, etc.).

## Recipes

### Add or rename an API tool
Tool metadata comes from `naming.py::TOOL_META` (`(METHOD, path) → ToolMeta`).
Build the entry with the `_read()` / `_write()` helpers — they set the annotation
quartet consistently — and supply all four arguments: `name`, `title`, `hint`,
`justification`. The name is injected as the OpenAPI `operationId` and becomes the
MCP tool name; `server.py::_customize_component` applies the rest.

```python
("POST", "/catalogue-tag-update"): _write(
    "update_catalogue_directory",
    "Update catalogue directory",
    "Rename or re-describe a coaching catalogue directory.",
    "readOnlyHint=false / destructiveHint=true: overwrites an existing record.",
),
```

Then update the expected-tools set in `tests/test_smoke.py` and run `pytest` +
`fastmcp inspect`. New endpoints appear automatically with an auto-generated name
and conservative method-derived annotations (`fallback_meta`) until curated here —
a `GET` becomes read-only, anything else is assumed destructive.

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
`pyproject.toml [tool.setuptools] py-modules`. Hand-written tools bypass
`_customize_component`, so give them a title and safety hints via
`server.py::TOOL_METADATA_OVERRIDES` — otherwise
`ToolMetadataBackfillMiddleware` falls back to the safe assumption (destructive)
and the tool needs a confirmation prompt it may not deserve.

### Bump the version
`SERVER_VERSION` (`server.py`), `version` (`pyproject.toml`) and `version`
(`server.json`) must all match — `tests/test_listing_readiness.py` enforces it and
the registry publish workflow re-checks it. Registry versions are immutable, so
bump all three together and tag `v<version>` to publish.

## Gotchas

- **Flat layout:** every module sits at the repo root and must be importable
  directly. New modules must be added to `py-modules` in `pyproject.toml`, or
  Horizon won't package them.
- **Don't disable output validation** to silence a null error — extend the
  relaxer instead (see above).
- **Secrets:** only in `.env`. Keep `.env.example` free of real values.
- **Snapshot vs cache — two different files.** `data/openapi.snapshot.json` is the
  *committed* cold-start fallback, refreshed deliberately via
  `scripts/update_snapshot.py`. The *runtime* cache is written on every successful
  live fetch and defaults to the OS temp dir; it holds the **prepared** spec
  (operationIds injected, schemas relaxed), not the raw one. Point
  `OPENAPI_CACHE_PATH` somewhere persistent (e.g. `data/openapi.cache.json`, which
  is untracked) only if you want it to survive reboots — and remember it then goes
  stale silently, because it is read only when the live fetch fails. Refresh it the
  way the server does:

  ```bash
  OPENAPI_CACHE_PATH=data/openapi.cache.json python -c "import openapi_loader; openapi_loader.load_openapi_spec()"
  ```
