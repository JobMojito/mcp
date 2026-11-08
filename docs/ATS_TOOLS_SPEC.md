# ATS tools for the MCP: specification

Status: proposed, not built. Written 2026-11-08 from reading the code only; nothing here was run
against a database, and output sizes are estimates.

Goal: give the MCP (and so the admin AI assistant, which picks up every MCP write tool behind its
confirm card) the ATS features built since October: position process editing (meeting, Certo,
external assessment and pre-screening steps), candidate notes, tags, candidate search, segments,
tasks, duplicates, requested documents and the built-in reports.

## Delivery

Every tool is delivered the way existing tools are:

- one edge function per endpoint in `supabase-functions/supabase/functions/<name>/` with a Hono
  `route.ts` (zod schema, OpenAPI metadata) and `index.ts`;
- the route registered in `PUBLIC_ROUTES` (`openapi/document.ts`);
- a curated entry in `mcp/naming.py::TOOL_META` (name, title, hint, read-only or destructive
  annotations); shared logic in `_shared/<domain>/`, as `report-*` does with `_shared/reports/`.

Conventions for all new endpoints:

- the caller's JWT (`createRequestClient`), never the service role; existing RPCs where they exist;
- optional `merchant_id` through `resolveMerchant`; object-addressed tools derive the company from
  the object;
- errors: 403 auth or feature switch off (the message names the switch), 404 not visible,
  409 state conflict or stale edit, 422 validation;
- list endpoints are GET with the `{data, pagination}` envelope (`_shared/openapi/list.ts`);
- `logApiRead` with `via` on every read that returns candidate data;
- `dry_run` on writes that are risky or reach candidates.

Tests: 100% line and branch coverage of what each change touches, measured (SQL with
`tests/sql/coverage.sh`, Deno with `deno test --coverage`, MCP with pytest coverage), then the
docs, `scripts/update_snapshot.py` and a version bump. Deploy order: SQL, edge functions, MCP
restart, admin.

Tool count: 50 API tools today; this adds 41, to about 91. Measure the `tools/list` size and the
admin assistant's prompt cost in phase 1 before going further.

## Fix before exposing

1. `merchant_candidate_tags_set` (143) checks company access only: add `position_access` so staff
   outside a confidential position cannot tag its candidates.
2. Document request RPCs (169, 249) have no position access check: add it.
3. `merchant_position_step_update` (157:2166) replaces the whole config, clears the name on a blank
   value and runs no type validation: replaced by the patch RPC below.
4. The `merchant only` policy on `position_def_step` has no command restriction, so staff can write
   steps directly and skip the RPC rules (only the start of the policy was read).
5. `job-interview-update` has no staff check on `recruiter_profile_id`, and it appears to accept an
   AI step's id, which could change that step's `visibility` or `status` (not tested).
6. Legacy analytics RPCs such as `interview_analytics` are granted to `anon` and rely on row
   security: audit before any report reads them.
7. `merchant_segment_options` (143) lists every live position by name with no position access
   filter, so it may show confidential position names: filter before `list_candidate_search_options`
   exposes it.

## 1. Position process (13 tools)

Settings live in different places: AI interview and pre-screening steps on linked rows
(`interview_def_set`, `interview_def_pre_screening`), the others in `position_def_step.config`.
Step config is read live, so an edit reaches candidates already on the step immediately.

| Tool | Kind | Notes |
|---|---|---|
| `get_position_process` | read | Ordered steps with typed settings, linked ids, `structure_editable`, candidates per step, and the options each step type accepts (assessment providers, Certo connected, meeting forms) |
| `set_position_process` | write, `dry_run` | Add, remove, reorder steps. Draft positions only, wraps `merchant_position_process_save`; refuses removing a step with results |
| `update_position_meeting_step` | write, `dry_run` | `config.meeting`: duration, mode (slots or availability), interviewers, panel (2 to 8), conference provider, notice, horizon, data collection, e-mail variant |
| `update_position_certo_step` | write, `dry_run` | `wait_for_identity`, `include_contact`, `scope_types`; validated by the 257 trigger |
| `update_position_assessment_step` | write, `dry_run` | `config.external_assessment`: provider, instructions (max 3000), due days 1 to 60, pass score |
| `update_position_pre_screening_step` | write, `dry_run` | Strict schema over `interview_def_pre_screening` (type, form fields, rules, auto-approve); `/pre-screening-create` stays hidden |
| `update_position_interview_step` | write, `dry_run` | `allow_human_interview` and step options; question content stays in `update_interview` |
| `list_meeting_slots` | read | Slots of a meeting step with bookings |
| `save_meeting_slot` | write | Moving a booked slot re-notifies the candidate |
| `delete_meeting_slot` | destructive | Cancels bookings and notifies candidates |
| `set_position_lifecycle` | write | `merchant_definition_lifecycle_set` (switch `position_lifecycle`) |
| `set_position_team` | write | `merchant_position_team_set` |
| `duplicate_position` | write | `merchant_position_duplicate` |

All step types also take `advance` (auto or manual) and the `kombo` stage mapping.

One tool per step type, not one generic tool: the schemas differ too much, the model gets exact
fields, and the assistant's confirm card reads clearly.

New SQL:

- `merchant_position_step_config_patch(step_id, patch, expected_updated_at)`: locks the row, merges
  only the sent keys, refuses a stale `expected_updated_at` with 409.
- `private.position_step_config_error(type, config)` in a trigger, as the Certo trigger does, so
  admin, API and MCP share one validator; zod in the route documents the same shape.
- `dry_run` returns the changed keys, the candidates on the step, booked meetings and warnings.

## 2. Candidate card, notes, tags, search, tasks

| Tool | Kind | Notes |
|---|---|---|
| `get_candidate` | read | Profile, applications with steps, tags, segments, open tasks, duplicate flag, document requests, note count; audited |
| `list_candidate_notes` | read | Candidate and step-result notes visible to the caller |
| `add_candidate_note` | write | Exactly one target (`candidate_id` or `result_id`); team or private; mentions. No delete or score adjustment in v1 |
| `list_candidate_tags` | read | `merchant_candidate_tag_list`, with usage counts |
| `set_candidate_tags` | write | `merchant_candidate_tags_set` (max 5000 candidates); needs fix 1 |
| `save_candidate_tag` | write | Create; rename and recolor are owner only (268) |
| `delete_candidate_tag` | destructive | Owner only, hard delete |
| `search_candidates` | read | See below |
| `list_candidate_search_options` | read | See below |
| `list_tasks` | read | `merchant_task_list` (switch `tasks`) |
| `create_task` | write | Assignee must be staff; assigning someone else sends the `task-assigned` e-mail |
| `update_task` | write | `merchant_task_save` |
| `set_task_status` | write | Cancel is terminal |

Tag names must stay distinct from the coaching `catalogue_tag` tools. Notes attach to a candidate
or a step result; there is no application-level note, and adding one needs a schema change
(recommendation: none).

### `search_candidates`

Searches candidates with the segment builder's rules, so the assistant can filter on everything
the admin's segment builder offers, including tags, without saving a segment.

- Endpoint: `POST /merchant-candidate-search` (the rules are nested JSON; annotated read-only).
- Body: `merchant_id?`, `rules` (`{match: "all" | "any", rules: [...]}`, max 20 rules) or
  `segment_id` (members of a saved segment), optional `search` text, `limit` (1 to 200, default
  25), `offset`.
- Backed by `merchant_segment_candidates(merchant, segment_id, rules, search, limit, offset)`; rules
  are validated in SQL by `private.profile_interview_segment_rules_check`, so the route only checks
  the envelope and maps the check's error codes (`unknown_field`, `bad_op`, `bad_id`, ...) to 422.
- Returns per candidate: id, name, email, phone, emoji, country, last applied, last activity,
  application count, statuses, tag ids, auto tags; plus `pagination.total` from `total_count`.
- Audited with `logApiRead` as `candidate_list.read`.
- Access: the 157 version of `private.profile_interview_segment_facts` leaves out candidates of
  positions hidden from the caller, so the search respects position access. Cover this in the SQL
  test (a restricted member must not find a hidden candidate).

Rule fields, as `utils/segments/segmentRules.ts` and the SQL check define them:

| Field | Operators | Values |
|---|---|---|
| `tag` | any, all, none | tag ids |
| `auto_tag` | any, none | recent_applicant, completed_interview, no_show, inactive_90d, previous_applicant, prospect (switch `prospects`), possible_duplicate, careers_chat_lead |
| `country` | any, none | country codes |
| `source` | any, none | channels |
| `position` | any, none | position or interview ids |
| `stage` | any, none | step ids |
| `status` | any, none | invited, in_progress, completed, accepted, rejected, hired, declined, archived |
| `history` | any, none | interview, pre_screening, public_avatar |
| `applied` | within_days, older_than_days, between | days or dates |
| `activity` | within_days, older_than_days | days |
| `applications` | at_least, at_most | count |
| `invited` | is | boolean |
| `search` | contains | text |
| `emoji` | is | emoji |
| `reason` | any, none | decision reason ids (switch `decision_reasons`) |
| `skill` | any, all, none | skills (switch `segments_v2`) |
| `job_category` | any, none | categories (switch `segments_v2`) |
| `seniority` | any, none | entry-level to executive (switch `segments_v2`) |
| `custom_field` | any, none, contains, at_least, at_most | by field type (switch `custom_fields`) |

Not covered by rules, only by the Candidates list: recruiter (owner), "mentioned me", the activity
tabs. Owner can be added as a rule field later if needed.

### `list_candidate_search_options`

The ids and values the rules accept, so the model never guesses an id: tags, positions with their
steps, interviews, countries, sources, decision reasons, and with `segments_v2` the categories and
skills (`merchant_segment_options`, `merchant_segment_options_v2`), plus the custom fields when
that switch is on. Needs fix 7 first.

## 3. Documents, duplicates, segments

| Tool | Kind | Notes |
|---|---|---|
| `list_document_requests` | read | `merchant_document_request_list` (switch `document_requests`) |
| `request_candidate_documents` | write | E-mails the candidate unless `notify` is false; needs fix 2 |
| `cancel_document_request` | write | |
| `mark_document_request_reviewed` | write | `/merchant-document-download` stays hidden (302, `webhooks_v2`) |
| `list_candidate_duplicates` | read | Per candidate (`merchant_candidate_duplicates`); a company-wide list needs a new RPC |
| `preview_candidate_merge` | read | |
| `merge_candidates` | destructive | Owner or admin; no undo; requires a preview of the same pair first |
| `dismiss_candidate_duplicate` | write | `merchant_candidate_duplicate_decide` |
| `list_segments` | read | |
| `get_segment` | read | Rules, counts, analytics |
| `save_segment` | write | Rules checked in SQL; preview with `search_candidates` first |
| `delete_segment` | destructive | |
| `set_segment_members` | write | Static segments; never the same-email `_segment_add/_remove` |

`search_candidates` replaces the separate `preview_segment` and `list_segment_candidates` tools of
the first draft: it takes either ad hoc rules or a `segment_id`, and returns the total.

## 4. Reports (2 tools)

The 16 built-in reports (`analytics/analyticsCatalogue.ts`) are hand-written RPCs, browser selects
and PostHog queries, not `run_report` templates, and many of their gates exist only in admin code.

- `list_builtin_reports`: the registry with sections and the gates each needs.
- `run_builtin_report(id, section?, from, to, position_or_interview_id?, limit, offset)`: a registry in
  `_shared/reports`, the `BlockResult` shape, one RPC per section, the caller's JWT, gates enforced
  server side. Candidate-level sections are opt-in and audited.

Size: the ATS pipeline is estimated at 150 to 200 KB and the workload queue at about 225 KB, over
the 150k result budget, so both page by section. Feedback reports need new SQL; PostHog-based
reports come last.

## Phases

1. Fixes 1 to 6, then the position process tools.
2. `get_candidate`, notes, tags, `search_candidates` with its options tool (needs fix 7), tasks.
3. Documents, duplicates, segments.
4. Reports: aggregates, then candidate sections with audit, then PostHog.

## Decided defaults

- `merge_candidates` is included, owner or admin only, after a preview of the same pair.
- Segment bulk sends (invite, e-mail, SMS, WhatsApp, reject) are left out: they reach candidates
  and cannot be undone.
- Accept, reject, advance and skip are left out: the server's responsible-use instructions treat
  its output as decision support for a human reviewer.
- Later, read-only first: the pending decisions worklist (`merchant_pending_actions`), the calendar
  (`merchant_calendar_events`), meeting outcomes.
