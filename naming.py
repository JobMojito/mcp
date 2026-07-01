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
        "Create a new interview and auto-generate its question sequence from position data.",
    ),
    ("POST", "/job-interview-create-from-array"): (
        "create_interview_from_questions",
        "Create a new interview from an explicit array of questions.",
    ),
    ("POST", "/job-interview-create-for-candidate-with-token"): (
        "create_interview_for_candidate",
        "Create an interview for a specific candidate and return an access-token URL.",
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
        "List available merchant avatar templates.",
    ),
    ("GET", "/merchant-sub-merchant-list"): (
        "list_sub_merchants",
        "List sub-merchants under the merchant account.",
    ),
    ("GET", "/merchant-analytics"): (
        "get_merchant_analytics",
        "Get the merchant's daily event analytics.",
    ),
    ("GET", "/merchant-status"): (
        "get_merchant_status",
        "Get a merchant status snapshot: credit balances, subscription, pending-work "
        "counts, candidate/result totals, and invitation headroom.",
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
