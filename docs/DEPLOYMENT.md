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
- `BASE_URL=https://<your-app>.fastmcp.app`  ← public URL, **no** `/mcp` suffix

### Verify direct mode (source of truth)

```bash
curl -s https://<your-app>.fastmcp.app/.well-known/oauth-protected-resource/mcp
```

Expected:

```json
{
  "resource": "https://<your-app>.fastmcp.app/mcp",
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
| `BASE_URL` | `https://<your-app>.fastmcp.app` (public, no `/mcp`) |
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

## Observability

`middleware.py` logs each tool call (name, argument keys, outcome, timing). Note
it **cannot** see transport-layer 400/404s (missing/expired session), which are
rejected before any tool runs — an empty-body 400/404 with no corresponding tool
log means the client is reusing a dead session, not an application error.
