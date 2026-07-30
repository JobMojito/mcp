# JobMojito MCP Server

An [MCP](https://modelcontextprotocol.io) server for the **JobMojito API**, built
with [FastMCP](https://gofastmcp.com) and designed to deploy on
[Prefect Horizon](https://horizon.prefect.io) (the MCP platform from the FastMCP
team — `app.prefect.cloud` → Horizon).

It exposes:

- **21 API tools** auto-generated from JobMojito's live OpenAPI spec (interviews,
  pre-screening, knowledge base, merchant lists/analytics). All endpoints —
  including the `GET` lists — are surfaced as tools with clean, curated names.
- **2 documentation tools** (`search_documentation`, `get_documentation`) that read
  the developer docs and help center **live** — docs stay single-source on their
  existing platforms; nothing is copied into this repo.
- **Supabase OAuth** so end users can log in directly from Claude, ChatGPT, etc.
  The signed-in user's Supabase JWT is forwarded to the JobMojito API on every call.

---

## How it fits together

```
MCP client (Claude / ChatGPT)
        │  OAuth login (Supabase OAuth Server, DCR)
        ▼
JobMojito MCP server (this repo, on Horizon)
        │  forwards the user's Supabase JWT
        ▼
JobMojito API  (https://cool.jobmojito.com/functions/v1)
```

| Concern            | Approach |
|--------------------|----------|
| API → tools        | `FastMCP.from_openapi(...)`, all routes mapped to **Tools**, curated names via injected `operationId`s |
| Spec freshness     | Fetched **live at startup**; a runtime cache + committed snapshot are fallbacks |
| Auth               | `SupabaseProvider` (Remote OAuth); per-request JWT forwarding to the upstream API |
| Docs               | Built-in tools reading `developer.jobmojito.com/llms.txt` + `.md` and the `help.jobmojito.com` (Featurebase) help center |

### Files

| File | Purpose |
|------|---------|
| `server.py` | Entry point — builds and exposes `mcp` (point Horizon at `server.py:mcp`) |
| `config.py` | Environment-driven settings |
| `openapi_loader.py` | Live fetch + cache/snapshot fallback + operationId injection |
| `naming.py` | Curated tool names & description hints per endpoint |
| `upstream.py` | httpx client that forwards the user's Supabase JWT to the API |
| `docs_tools.py` | `search_documentation` / `get_documentation` |
| `featurebase.py` | Featurebase REST client (help-center articles) |
| `mintlify.py` | Mintlify developer-docs MCP proxy + client-credentials auth |
| `scripts/update_snapshot.py` | Refresh the committed fallback spec |
| `scripts/try_docs.py` | Local smoke test for the documentation tools |
| `data/openapi.snapshot.json` | Offline fallback spec (regenerate from your machine) |
| `lazy_auth.py` | Unauthenticated capability discovery + `scope=` on the 401 challenge |
| `wellknown.py` | `/healthz`, OpenAI domain challenge, Smithery server card |
| `server.json` | Official MCP Registry entry |
| `.github/workflows/publish-registry.yml` | Publishes `server.json` to the MCP Registry on a `v*` tag |

---

## Local development & testing (macOS)

```bash
# 1. Set up a virtualenv and install deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure secrets in .env (auto-loaded; gitignored)
cp .env.example .env        # then fill in your keys/credentials

# 3. Static check — see exactly what Horizon will deploy
fastmcp inspect server.py:mcp

# 4. Run the unit tests (offline; uses the snapshot fallback)
ENABLE_AUTH=false pytest -q --asyncio-mode=auto

# 5. Smoke-test the docs tools live (Featurebase + Mintlify federation)
ENABLE_AUTH=false python scripts/try_docs.py "how do I create an interview"

# 6. Run the server and connect a client
ENABLE_AUTH=false python server.py        # → http://localhost:8000/mcp
#   then, in another terminal, open the MCP Inspector:
npx @modelcontextprotocol/inspector
#   transport: "Streamable HTTP", URL: http://localhost:8000/mcp
```

Testing notes:

- **Docs tools** (`search_documentation`, and the mounted Mintlify
  `search_*_developer_*` / `query_docs_filesystem_*`) work with `ENABLE_AUTH=false`
  — no Supabase login needed. `scripts/try_docs.py` is the fastest check.
- **API tools** (`create_interview`, `list_candidates`, …) call the real
  JobMojito API and need a Supabase user token. For local testing, set
  `JOBMOJITO_DEV_BEARER_TOKEN` in `.env` to a valid Supabase access token; it's
  forwarded when `ENABLE_AUTH=false`. (Never set this in production — there the
  token comes from the user's OAuth session.)
- **Full OAuth locally** is rarely needed; if you want it, run with
  `ENABLE_AUTH=true`, set `BASE_URL=http://localhost:8000`, and connect a client
  that supports OAuth (it will bounce you through Supabase).

> **Refresh the fallback snapshot** from a network that can reach the API:
> `python scripts/update_snapshot.py` (the committed snapshot in this repo is a
> thin structural fallback; this replaces it with the full live spec).

---

## Supabase OAuth setup

This server uses Supabase's **OAuth Server** feature (which supports Dynamic
Client Registration, so MCP clients self-register). The MCP server is the OAuth
**resource server** — it validates tokens; Supabase issues them.

**Project:** `https://momsbvnltsydezmoesqt.supabase.co`
`SupabaseProvider` derives every endpoint from this project URL:

| Endpoint | URL |
|----------|-----|
| Authorize | `…/auth/v1/oauth/authorize` |
| Token | `…/auth/v1/oauth/token` |
| JWKS | `…/auth/v1/.well-known/jwks.json` |
| OIDC discovery | `…/auth/v1/.well-known/openid-configuration` |

Steps:

1. **Supabase Dashboard → Authentication → OAuth Server**
   - Enable the **OAuth Server** and **Allow Dynamic OAuth Apps**
   - **Site URL:** `https://app.jobmojito.com`
   - **Authorization Path:** `/oauth/consent`
2. Env: `SUPABASE_PROJECT_URL=https://momsbvnltsydezmoesqt.supabase.co`,
   `BASE_URL=https://mcp.jobmojito.com` (public base, no `/mcp`),
   `SUPABASE_JWT_ALGORITHM=ES256` (switch to `RS256` if your JWKS shows RSA keys).
3. If the JobMojito Edge Functions require an `apikey` header, set `SUPABASE_ANON_KEY`.

**Consent is handled by Supabase / your app, not this server.** Because the Site
URL is `app.jobmojito.com`, Supabase serves the approve/deny screen at
`https://app.jobmojito.com/oauth/consent`, where the user already has a Supabase
session. This MCP server is only the OAuth resource server — it does not serve a
consent page. `SITE_URL` and `OAUTH_CONSENT_PATH` are informational and should
match your Supabase OAuth Server settings.

> Token note: `SupabaseProvider` cannot validate token *audience* (Supabase Auth
> doesn't implement RFC 8707 resource indicators yet). This is expected and
> logged at startup.

---

## Authentication model

**Every tool requires the user to be signed in via Supabase OAuth** — including
`search_documentation` and `get_documentation`. There is no anonymous access.
Keep `ENABLE_AUTH=true`; the signed-in user's token is forwarded to the JobMojito
API so calls respect that user's permissions.

## Deploy to Prefect Horizon

1. Push this repo to GitHub.
2. At [horizon.prefect.io](https://horizon.prefect.io), sign in with GitHub and
   select this repo.
3. Configure:
   - **Entrypoint:** `server.py:mcp`
   - **Dependencies:** auto-detected from `requirements.txt`
   - **Horizon Authentication:** **off** — this server provides its own Supabase
     OAuth, so let it be the auth layer.
4. Set environment variables / secrets:
   - `ENABLE_AUTH=true`
   - `BASE_URL=https://mcp.jobmojito.com` — **critical.** This must be the
     server's **public base URL, with no `/mcp` and no trailing slash**. It's what
     `SupabaseProvider` advertises as the OAuth resource; if it's left at the
     `http://localhost:8000` default, clients get `401 invalid_token`. The startup
     log prints `base_url=…` and warns if it's localhost.
   - `SUPABASE_PROJECT_URL=https://momsbvnltsydezmoesqt.supabase.co`
   - `SUPABASE_ANON_KEY` if the Edge Functions need the `apikey` header.
   - `FEATUREBASE_API_KEY` for help-center docs.
5. **Deploy.** Your MCP endpoint is `https://mcp.jobmojito.com/mcp`. Horizon
   redeploys on every push to `main`.

### Testing the login

A `401` on `initialize` is the **normal first step** of MCP OAuth — the client is
meant to read the discovery metadata and run the Supabase login. Horizon's
**Inspector / ChatMCP do not perform that login**, so they'll show a bare 401.
Test by adding `https://mcp.jobmojito.com/mcp` as a **custom connector in
Claude.ai**, which runs the full OAuth flow and prompts the Supabase login.

Make sure the Supabase side is configured (or the flow 401s regardless): OAuth
Server enabled + Dynamic Client Registration on; Site URL `https://app.jobmojito.com`
+ Authorization Path `/oauth/consent`; and that consent page hosted on the app.

Because the OpenAPI spec is fetched live at startup, each redeploy picks up the
latest JobMojito API automatically.

---

## Keeping documentation single-source

Docs are **not** duplicated here. `search_documentation` builds a live index and
`get_documentation` fetches a page on demand, both reading the source platforms
directly. Edit docs there and the MCP reflects changes within the cache TTL
(`DOCS_CACHE_TTL_MINUTES`, default 60).

| Source | How it's read | Credentials |
|--------|---------------|-------------|
| `developer.jobmojito.com` (Mintlify) | **Mintlify MCP** mounted into this server (semantic `search` + `query_docs_filesystem`, incl. the imported OpenAPI) when client credentials are set; else public `llms.txt`/`.md` via the built-in tools | MCP client id/secret (to federate) |
| `help.jobmojito.com` (Featurebase) | **REST API** if `FEATUREBASE_API_KEY` is set (structured articles incl. body), else public-HTML scraping | API key (optional) |

### Why this server *and* Mintlify

Mintlify imports the OpenAPI, but its MCP is **read-only documentation** — it lets
an agent search and read the API reference. It cannot *call* the API. This server
turns the same OpenAPI into **authenticated, callable tools** (acting as the
signed-in Supabase user). So: Mintlify answers "how does it work?"; this server
"does the thing." The federation mounts Mintlify's doc-search tools alongside the
action tools so end users get both from one connector.

### Developer docs: Mintlify federation

`developer.jobmojito.com` is a Mintlify site whose MCP exposes `search` +
`query_docs_filesystem` tools (including the imported OpenAPI). This server mounts
it as a proxy. When federated, the built-in developer-`llms.txt` source is
disabled to avoid duplication.

**Public by default (no credentials).** Mintlify serves a public MCP at `/mcp`.
Because the developer docs are public, `DEVELOPER_DOCS_MCP_URL` defaults to
`https://developer.jobmojito.com/mcp` and is mounted with no auth — it works out
of the box.

**Authenticated endpoint (optional).** Mintlify also offers an `/authed/mcp`
endpoint with **OAuth client-credentials**, for group-restricted content. It is
**not enabled by default** — the site's `/.well-known/mcp` only advertises a
`public` server until you enable it. To use it:

1. Mintlify dashboard → **MCP server page → Enable MCP Server** (otherwise
   `/authed/mcp/oauth/token` returns 404).
2. Create a client credential there (*MCP client credentials*, **not** Mintlify
   API keys) → set `DEVELOPER_DOCS_MCP_CLIENT_ID` / `_CLIENT_SECRET`.
3. Set `DEVELOPER_DOCS_MCP_URL=https://developer.jobmojito.com/authed/mcp`.

Auth is attached only when the URL contains `/authed` **and** both credentials
are set; the server then exchanges them at `{url}/oauth/token` and
caches/refreshes the token.

### Featurebase: which credential is which

Featurebase exposes docs three ways — the credential shape tells you which:

- **REST API** → a single **API key** (sent as `Authorization: Bearer <key>` and
  `X-API-Key`), base `https://do.featurebase.app`. Create it in the Featurebase
  dashboard: log in via [auth.featurebase.app/choose-org](https://auth.featurebase.app/choose-org)
  → **Settings → Advanced → API** → copy the key. (The direct `/settings/api` URL
  404s — you must enter through your org first.) This is what this server uses for
  the help center (set `FEATUREBASE_API_KEY`).
- **Hosted MCP** (`mcp-read.featurebase.app`, the *Reader* connector) →
  **interactive OAuth (authorization code + PKCE, no client secret)**. Designed
  for a human to click "Connect" in Claude/ChatGPT. Best added **directly** as a
  connector in your AI client, not proxied from this server (there's no machine
  token / client-credentials grant).
- **Public web** → `llms.txt` / `.md` (developer docs), no auth.

> A **Client ID + Client secret** pair is *not* a Featurebase docs credential —
> neither the REST API (API key) nor the MCP (PKCE, no secret) uses that shape.
> If you have one, it's likely a Featurebase SSO/identity OAuth app or from a
> different system. For docs, use the REST **API key** above.

### Featurebase Reader MCP (not federated)

Unlike Mintlify, Featurebase's Reader MCP (`mcp-read.featurebase.app`) only
supports interactive OAuth (no client-credentials), so it isn't mounted here. If
you want its native search, add it **directly** as a connector in your AI client.
`HELP_DOCS_MCP_URL` is kept in config for reference only.


---

## Directory listings & discoverability

The server is built to satisfy the Anthropic connector directory, the OpenAI
plugin directory (ChatGPT + Codex) and the official MCP Registry without further
code changes. What that means in practice:

| Requirement | Where it lives |
|---|---|
| Every tool has a `title` + `readOnlyHint`/`destructiveHint`, with a written justification | `naming.py::TOOL_META`, applied by `server.py::_customize_component`, backfilled by `middleware.ToolMetadataBackfillMiddleware` |
| Read and write are separate tools (no catch-all) | `naming.py` — asserted in `tests/test_listing_readiness.py` |
| `tools/list` works without credentials, so crawlers can show the tool list | `lazy_auth.py` |
| `scopes_supported` in the protected-resource metadata, `scope=` on the 401 | `server.py::_build_auth` + `lazy_auth.WWWAuthenticateScopeMiddleware` |
| Actionable errors instead of bare 4xx/5xx | `middleware.UpstreamErrorMiddleware` |
| Results stay under the client's size ceiling | `middleware.ResultSizeGuardMiddleware` |
| Health probe for uptime monitoring | `GET /healthz` |
| OpenAI domain verification | `GET /.well-known/openai-apps-challenge` (set `OPENAI_APPS_CHALLENGE_TOKEN`) |
| MCP Registry entry | `server.json` + the publish workflow |

Generate the annotation justifications OpenAI asks for at submission:

```bash
python -c "import naming,json;print(json.dumps(naming.annotation_justifications(),indent=2))"
```

Verify the public identity after any deploy or hostname change — this is the
single most common way an otherwise-healthy server 401s every client:

```bash
curl -s https://mcp.jobmojito.com/.well-known/oauth-protected-resource/mcp
# "resource" MUST equal "https://mcp.jobmojito.com/mcp"
curl -s https://mcp.jobmojito.com/healthz
```

### Still to do outside this repo

The code is ready; these are not code problems:

1. **A Claude Team or Enterprise plan.** The Anthropic submission portal lives
   under `claude.ai/admin-settings/` and is unavailable on individual plans.
2. **A square PNG/SVG logo** (512×512), then set `SERVER_ICON_URL` /
   `SERVER_ICON_MIME`. The server warns on startup while it's still a `.ico`.
3. **A public "Connect JobMojito to Claude & ChatGPT" docs page** — required by
   Anthropic (documentation URL) and OpenAI (support URL).
4. **A populated reviewer test account** with no MFA and no email confirmation
   step; OpenAI rejects submissions whose test account requires either.
5. **DNS TXT record on `jobmojito.com`** for the `com.jobmojito/*` registry
   namespace, and the `MCP_REGISTRY_PRIVATE_KEY` repo secret — see the header
   comment in `.github/workflows/publish-registry.yml`.
6. **Allowlist Anthropic's egress range `160.79.104.0/21`** on the MCP host and
   anything in front of Supabase. A blocking WAF is Anthropic's most common
   documented failure mode.
