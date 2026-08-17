# docs/

Developer / coding-agent documentation for this repository.

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the server is built: request
  lifecycle, modules, OpenAPI loading + schema relaxation, auth (incl. lazy auth),
  the middleware chain, docs tools, merchant selection, transport/sessions.
- **[DEVELOPMENT.md](DEVELOPMENT.md)** — local setup, environment variables,
  testing, and recipes (add/rename/exclude a tool, refresh the snapshot, fix a
  null-validation error, add config).
- **[DEPLOYMENT.md](DEPLOYMENT.md)** — Horizon deploy, direct-mode requirement and
  verification, OAuth flow, session behavior, redeploy checklist.

Start from **[../CLAUDE.md](../CLAUDE.md)** at the repo root — it's the entry
point a coding agent loads first.

## End-user product docs are elsewhere

Guides for JobMojito **users** (creating interviews, inviting candidates,
reviewing results, …) live on `help.jobmojito.com` (Featurebase) and
`developer.jobmojito.com` (Mintlify). This server reads them **live** via
`search_documentation` / `get_documentation` and keeps no copy — the
`docs/cookbooks/` folder that used to sit here was moved to Mintlify for exactly
that reason. Don't reintroduce product docs into this repo.
