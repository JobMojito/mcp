# docs/

Two distinct audiences live here:

## Developer / coding-agent docs (this repo)

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the server is built: request
  lifecycle, modules, OpenAPI loading + schema relaxation, auth, docs tools,
  merchant selection, transport/sessions.
- **[DEVELOPMENT.md](DEVELOPMENT.md)** — local setup, environment variables,
  testing, and recipes (add/rename/exclude a tool, refresh the snapshot, fix a
  null-validation error, add config).
- **[DEPLOYMENT.md](DEPLOYMENT.md)** — Horizon deploy, direct-mode requirement and
  verification, OAuth flow, session behavior, redeploy checklist.

Start from **[../CLAUDE.md](../CLAUDE.md)** at the repo root — it's the entry
point a coding agent loads first.

## End-user product docs

- **[cookbooks/](cookbooks/)** — authored Mintlify guides for JobMojito **users**
  (creating interviews, inviting candidates, reviewing results, etc.). These are
  published to `developer.jobmojito.com`, not developer documentation for this
  server. The server reads help/developer docs live and does not duplicate them.
