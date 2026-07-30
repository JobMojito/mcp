"""Curated, LLM-friendly names for each JobMojito API endpoint.

The published OpenAPI spec does NOT include `operationId` fields, so FastMCP
would fall back to auto-generating names from the HTTP method + path
(e.g. ``post_job_interview_create``). We instead inject a stable, hand-picked
``operationId`` onto each operation before handing the spec to FastMCP. The
operationId then becomes the MCP tool name directly — a single source of truth
for naming.

`TOOL_META` maps ``(METHOD, path)`` -> (tool_name, optional one-line hint that
is appended to the tool description to improve tool selection by the model).

If JobMojito adds new endpoints, they still get exposed automatically — they
just receive an auto-generated name until added here.
"""

from __future__ import annotations

# (HTTP method, path) -> (curated_tool_name, description_hint)
TOOL_META: dict[tuple[str, str], tuple[str, str]] = {
    # --- Interview reports ---
    ("POST", "/job-interview-pdf"): (
        "generate_interview_report",
        "Generate an interview result report (HTML/PDF/JSON) for a completed interview.",
    ),
    # --- Interview ---
    ("GET", "/job-interview-get"): (
        "get_interview_definition",
        "Get the definition/configuration of an interview (position).",
    ),
    ("POST", "/job-interview-set-state"): (
        "set_interview_state",
        "Change the state of an interview/position (e.g. open, closed).",
    ),
    ("POST", "/job-interview-result-request-another-attempt"): (
        "request_another_interview_attempt",
        "Re-open a submitted interview result so the candidate can retry.",
    ),
    ("POST", "/job-interview-token"): (
        "generate_interview_url",
        "Generate a signed public interview URL/token.",
    ),
    ("GET", "/job-interview-details"): (
        "get_interview_result_details",
        "Get full interview result details including transcript and scores.",
    ),
    ("POST", "/invite-users"): (
        "invite_users",
        "Invite users/candidates and create interview URLs for them.",
    ),
    ("POST", "/job-interview-register-users"): (
        "register_users_for_interview",
        "Register users/candidates for a specific interview.",
    ),
    ("POST", "/job-interview-create"): (
        "create_interview",
        "Create a new interview and auto-generate its question sequence from "
        "position data. The `interview_template_id` you pass also sets the "
        "modality (voice-only vs realtime/pre-recorded avatar) — see `list_avatars`.",
    ),
    ("POST", "/job-interview-create-from-array"): (
        "create_interview_from_questions",
        "Create a new interview from an explicit array of questions.",
    ),
    # NOTE: excluded from the MCP surface via IGNORED_PATHS below — HTTP only.
    # The curated name is kept here so it's ready if it's ever un-ignored.
    ("POST", "/job-interview-create-for-candidate-with-token"): (
        "create_interview_for_candidate",
        "Create an interview for a specific candidate and return an access-token URL.",
    ),
    ("POST", "/persona-create"): (
        "create_persona",
        "Create a role-play persona: an avatar that plays a defined role in a "
        "free-form conversation instead of a scored Q&A interview. Set "
        "`persona_role_avatar`/`persona_role_user` for the roles and `opening_line` "
        "for the avatar's first spoken line (defaults to a generic 'Hello'). The "
        "avatar's behaviour is four fields: `persona_avatar_who_is` (identity and "
        "what drives it), `persona_avatar_knowledge` (private facts it may use), "
        "`persona_avatar_progress` (how the conversation is allowed to move forward "
        "— starts with `Mode: turning point` or `Mode: steps` — and what gates the "
        "later personal details), and `persona_avatar_end_conditions` (when to stop). "
        "Coaching-platform feature.",
    ),
    # --- Pre-screening ---
    ("POST", "/pre-screening-create"): (
        "upsert_pre_screening",
        "Create or update a pre-screening assessment for a position.",
    ),
    ("POST", "/job-interview-pre-screening-api-resume-text"): (
        "pre_screen_resume_text",
        "Run pre-screening on a candidate from plain-text resume content.",
    ),
    ("POST", "/job-interview-pre-screening-api-resume-binary"): (
        "pre_screen_resume_binary",
        "Run pre-screening on a candidate from an uploaded (binary) resume file.",
    ),
    # --- Knowledge base ---
    ("POST", "/knowledge-base-document-upload"): (
        "upload_knowledge_base_document",
        "Upload and process a knowledge base document (multipart form-data).",
    ),
    # --- Merchant lists (read-only) ---
    ("GET", "/merchant-interview-list"): (
        "list_interviews",
        "List the merchant's interview definitions.",
    ),
    ("GET", "/merchant-candidate-list"): (
        "list_candidates",
        "List the merchant's candidates.",
    ),
    ("GET", "/merchant-result-list"): (
        "list_interview_results",
        "List the merchant's interview results.",
    ),
    ("GET", "/merchant-avatar-list"): (
        "list_avatars",
        "List available avatar/voice templates. Each item's `type` decides the "
        "interview modality: `interactive_elevenlabs` = voice-only (no video "
        "avatar); `interactive_heygen` = realtime interactive avatar (video); "
        "`offline_heygen` = pre-recorded, non-interactive avatar. An item's `id` is "
        "the `interview_template_id` you pass to the create-interview tools, so pick "
        "the template whose type matches the experience you want. Note: "
        "`offline_elai` and `offline_synthesia` are legacy integrations that may "
        "still appear here but cannot be used to create new interviews.",
    ),
    ("GET", "/merchant-sub-merchant-list"): (
        "list_sub_merchants",
        "List sub-merchants under the merchant account.",
    ),
    ("GET", "/merchant-analytics"): (
        "get_merchant_analytics",
        "Get the merchant's daily event analytics.",
    ),
    ("GET", "/merchant-analytics-credits-used"): (
        "get_merchant_credit_usage",
        "Get the merchant's credit usage.",
    ),
    ("GET", "/merchant-status"): (
        "get_merchant_status",
        "Get a merchant status snapshot: credit balances, subscription, pending-work "
        "counts, candidate/result totals, and invitation headroom.",
    ),
    ("GET", "/platform-languages-list"): (
        "list_languages",
        "List supported platform (mojito) languages: the `code` to pass as "
        "`mojito_language_code`, English/local names, SVG flag URL, per-interface "
        "enablement flags, and Azure speech accents.",
    ),
}


# Endpoints to exclude from the MCP entirely (never exposed as tools).
# Extend at deploy time with the IGNORED_TOOL_PATHS env var (comma-separated).
IGNORED_PATHS: set[str] = {
    "/invite-users",  # invites admin/merchant users — not for general agent use
    "/job-interview-create-for-candidate-with-token",  # candidate-token one-shot flow
    "/pre-screening-create",  # pre-screening management — excluded
    "/job-interview-pre-screening-api-resume-text",  # pre-screening — excluded
    "/job-interview-pre-screening-api-resume-binary",  # pre-screening — excluded
}


def operation_id_for(method: str, path: str) -> str | None:
    """Return the curated operationId/tool-name for a route, or None if unknown."""
    meta = TOOL_META.get((method.upper(), path))
    return meta[0] if meta else None


def description_hint_for(method: str, path: str) -> str | None:
    meta = TOOL_META.get((method.upper(), path))
    return meta[1] if meta else None
