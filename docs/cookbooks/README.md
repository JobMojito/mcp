# JobMojito cookbooks (Mintlify pages)

Task-oriented guides authored as Mintlify `.mdx` pages. They live here for
convenience, but they belong in your **Mintlify docs repo** (developer.jobmojito.com),
not the MCP server repo.

## Pages

| File | Page |
| --- | --- |
| `index.mdx` | Cookbooks overview (landing page with cards) |
| `create-an-interview.mdx` | Create an interview (3 options) |
| `invite-candidates.mdx` | Invite candidates (links / email) |
| `pre-screen-candidates.mdx` | Pre-screen candidates from a résumé |
| `review-results.mdx` | List, read transcripts, export reports |
| `manage-knowledge-base.mdx` | Upload KB documents for grounded questions |

## How to publish

1. Copy these `.mdx` files into your Mintlify docs repo, keeping the
   `docs/cookbooks/` folder layout.
2. Add them to your `docs.json` navigation. Example group:

```json
{
  "group": "Cookbooks",
  "pages": [
    "docs/cookbooks/index",
    "docs/cookbooks/create-an-interview",
    "docs/cookbooks/invite-candidates",
    "docs/cookbooks/pre-screen-candidates",
    "docs/cookbooks/review-results",
    "docs/cookbooks/manage-knowledge-base"
  ]
}
```

3. Commit & push. Mintlify rebuilds the site and re-indexes the search MCP
   automatically — so the JobMojito MCP server's `search_documentation` tool will
   start returning these cookbooks with no changes on the MCP side.

## Notes / verify before publishing

- Examples use the base URL `https://cool.jobmojito.com/functions/v1` and
  `Authorization: Bearer <SUPABASE_JWT>` (matches the OpenAPI `servers` + security).
- Field names, required flags, and enums were taken from the live OpenAPI spec.
  Request-body *example values* were authored here (the spec contains no examples),
  so sanity-check the placeholder values (`TEMPLATE_UUID`, `STORE_UUID`, etc.).
- `interview_template_id` comes from `GET /merchant-avatar-list` (`list_avatars`).
- Mintlify components used: `<Steps>`, `<Step>`, `<CodeGroup>`, `<ParamField>`,
  `<Card>`, `<CardGroup>`, `<Note>`, `<Tip>`, `<Warning>`, `<Info>` — all standard.
- Internal links use `/docs/cookbooks/...` to match this folder layout. Update
  them if your Mintlify routes the pages at a different path.
