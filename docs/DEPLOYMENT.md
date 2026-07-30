# Deployment

The server is hosted on **Prefect Horizon / FastMCP Cloud**, deployed from GitHub.

## Entrypoint

```
server.py:mcp
```

Horizon imports the module-level `mcp` object and serves it with its own runner
(it ignores any Dockerfile and the `if __name__ == "__main__"` block). The flat
module layout exists so this works with no build step — every module is at the
repo root and listed in `pyproject.toml [tool.setuptools] py-modules`.

## Must be deployed DIRECT (not behind the managed gateway)

FastMCP Cloud can put a managed OAuth **proxy/gateway** in front of a deployment.
This server must **not** use it, because the gateway terminates auth itself and
cannot carry the end user's Supabase token through to the API (you get double-auth
401s, and the JWT never reaches `upstream.py`). The deployment must serve the
server's own `SupabaseProvider` directly.

Direct mode is a consequence of the deployment's **Authentication** configuration
plus these environment variables — there is no separate "proxy runner" switch in
the UI:

- `ENABLE_AUTH=true`
- `BASE_URL=https://mcp.jobmojito.com`  ← public URL, **no** `/mcp` suffix

### Verify direct mode (source of truth)

```bash
curl -s https://mcp.jobmojito.com/.well-known/oauth-protected-resource/mcp
```

Expected:

```json
{
  "resource": "https://mcp.jobmojito.com/mcp",
  "authorization_servers": ["https://<project>.supabase.co/auth/v1"],
  "bearer_methods_supported": ["header"]
}
```

`authorization_servers` must point at **your Supabase project**. If it ever points
at a `fastmcp.app`/Prefect auth domain, the managed proxy is on and must be
disabled.

## Required environment variables (production)

Set these on the Horizon deployment (never commit them):

| Variable | Value |
|----------|-------|
| `ENABLE_AUTH` | `true` |
| `BASE_URL` | `https://mcp.jobmojito.com` (public, no `/mcp`) — **this is the OAuth resource identifier; it must equal the URL clients connect to exactly** |
| `MCP_PATH` | `/mcp` (default; combined with `BASE_URL` to form the resource id) |
| `ENABLE_LAZY_AUTH` | `true` (default) — serves `initialize`/`tools/list` without a token so directory crawlers can list tools. Tool calls still require a verified JWT. |
| `OAUTH_SCOPES_SUPPORTED` | `openid,email` — advertised in the PRM and in the `scope=` parameter of the 401 challenge |
| `OPENAI_APPS_CHALLENGE_TOKEN` | OpenAI plugin-directory domain verification. Served verbatim at `/.well-known/openai-apps-challenge`; the route 404s while unset. |
| `SERVER_ICON_URL` / `SERVER_ICON_MIME` | Square PNG/SVG logo for listings. Startup warns while this is a `.ico`. |
| `MAX_TOOL_RESULT_CHARS` | `120000` — refuse oversized results with pagination guidance instead of letting the client truncate them. `0` disables. |
| `SUPABASE_PROJECT_URL` | your Supabase project URL |
| `SUPABASE_ANON_KEY` | project anon key (`apikey` header for Edge Functions) |
| `SITE_URL` | `https://app.jobmojito.com` |
| `FEATUREBASE_API_KEY` | help-center search (optional but recommended) |
| `DEVELOPER_DOCS_MCP_URL` | `https://developer.jobmojito.com/mcp` |

Optional: `IGNORED_TOOL_PATHS`, `OPENAPI_CACHE_PATH`.
See `docs/DEVELOPMENT.md` for the full table.

## OAuth flow (end users)

The server is only an OAuth **resource server**. The authorization server is the
Supabase project; consent is served by the JobMojito app at
`SITE_URL + OAUTH_CONSENT_PATH`. Clients discover everything from the
`/.well-known/oauth-protected-resource/mcp` document above and drive the login
themselves. There is no anonymous access — every tool requires a signed-in user.

## Sessions & client reconnection

The transport is MCP Streamable HTTP with `Mcp-Session-Id` session tracking. The
server behaves per spec:

- non-initialize request with **no** session id → **400** "Missing session ID"
- request with an **unknown/expired** session id → **404** "Session not found"

A compliant client **must** re-`initialize` on a 404 (reusing its existing OAuth
token — re-init is a new *session*, not a new *login*). If a client instead keeps
calling on a dead session, you'll see bare 400/404s that never reach tool code
(so nothing appears in application logs). That is a **client** bug, not a server
one; the fix is on the client (update it, or, for Claude Desktop, prefer the
native custom connector over the `mcp-remote` bridge and clear `~/.mcp-auth` on a
stuck connection).

## Redeploy checklist

1. Merge to the branch Horizon deploys from.
2. Confirm the build picked up new modules (check `pyproject.toml` `py-modules`).
3. `fastmcp inspect server.py:mcp -f fastmcp` locally should be clean first.
4. After deploy, re-run the discovery `curl` to confirm direct mode.
5. Reconnect the client (fresh session) and smoke-test a read tool (e.g.
   `list_interviews`).

## Post-deploy smoke test

```bash
BASE=https://mcp.jobmojito.com

# 1. Liveness
curl -s $BASE/healthz

# 2. OAuth identity — `resource` MUST be exactly $BASE/mcp
curl -s $BASE/.well-known/oauth-protected-resource/mcp

# 3. Lazy auth: discovery is open, tool calls are not
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
# expect 200

curl -s -D - -o /dev/null -X POST $BASE/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_interviews","arguments":{}}}'
# expect 401 with:
#   WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp", scope="openid email"

# 4. OpenAI domain verification (only once OPENAI_APPS_CHALLENGE_TOKEN is set)
curl -s $BASE/.well-known/openai-apps-challenge
```

If step 2 returns a `resource` pointing at a different hostname, `BASE_URL` is
stale. Discovery will keep returning 200 and every client will fail token
validation with `401 invalid_token` — with nothing in the logs explaining why.

## Observability

`middleware.py` logs each tool call (name, argument keys, outcome, timing). Note
it **cannot** see transport-layer 400/404s (missing/expired session), which are
rejected before any tool runs — an empty-body 400/404 with no corresponding tool
log means the client is reusing a dead session, not an application error.
