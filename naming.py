"""Curated, LLM-friendly names + annotations for each JobMojito API endpoint.

The published OpenAPI spec does NOT include `operationId` fields, so FastMCP
would fall back to auto-generating names from the HTTP method + path
(e.g. ``post_job_interview_create``). We instead inject a stable, hand-picked
``operationId`` onto each operation before handing the spec to FastMCP. The
operationId then becomes the MCP tool name directly — a single source of truth
for naming.

`TOOL_META` maps ``(METHOD, path)`` -> :class:`ToolMeta`, which carries:

* ``name``          — the MCP tool name (injected as operationId)
* ``title``         — human-readable display title (MCP `title` + annotations.title)
* ``hint``          — one-line description prefix that improves tool selection
* ``read_only`` / ``destructive`` / ``idempotent`` / ``open_world``
                    — MCP tool annotations (`readOnlyHint` etc.)
* ``justification`` — WHY those annotation values are correct

Why the annotations matter: both the Anthropic connector directory and the
OpenAI plugin directory reject servers whose tools lack `title` plus either
`readOnlyHint: true` or `destructiveHint: true`, and OpenAI additionally asks for
a written justification per annotation at submission time. Keeping the
justification next to the values means the submission answer and the running
code can never drift apart — see ``annotation_justifications()``.

If JobMojito adds new endpoints, they still get exposed automatically — they
receive an auto-generated name and method-derived annotations (see
:func:`fallback_meta`) until they are added here.
"""

from __future__ import annotations

from dataclasses import dataclass

# HTTP methods that are safe/read-only by definition (RFC 9110).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# MCP tool names are capped at 64 characters by the Anthropic review criteria.
MAX_TOOL_NAME_LENGTH = 64


@dataclass(frozen=True)
class ToolMeta:
    """Curated metadata for one API endpoint."""

    name: str
    title: str
    hint: str
    justification: str
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = True
    #: Query-parameter defaults to override on this endpoint, as
    #: ``(("limit", 15),)``. Applied to the spec at load time by
    #: ``openapi_loader.apply_param_defaults`` — a tuple, not a dict, because
    #: ``ToolMeta`` is frozen/hashable.
    #:
    #: The only reason to use this is a spec default that cannot fit in a tool
    #: result. See ``PAGE_SIZE_WHY``.
    param_defaults: tuple[tuple[str, object], ...] = ()

    def annotations(self) -> dict[str, object]:
        """The MCP `annotations` payload for this tool."""
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


#: Why an endpoint would override the API's own page-size default.
#:
#: An MCP tool result costs the client roughly **twice** the API's JSON: the
#: protocol carries the same payload in both ``content`` (as text) and
#: ``structuredContent``, and ``MAX_TOOL_RESULT_CHARS`` counts both — correctly,
#: since both go over the wire. So an endpoint whose rows are large can blow the
#: 120,000-char budget at a page size the API considers modest, and the tool then
#: fails on its *first* call with default arguments, which reads as "broken".
#:
#: Measure before setting one — `chars_per_row ≈ 2 × the API's JSON per row` —
#: and leave headroom; row size varies with the data (long signed URLs, long
#: free-text fields). Lowering this only changes the default: the model can still
#: pass a bigger ``limit`` explicitly, and ``pagination.has_more`` tells it when
#: to page.
PAGE_SIZE_WHY = (
    "rows are large enough that the API's default page size overflows the tool "
    "result limit"
)


def _read(
    name: str,
    title: str,
    hint: str,
    justification: str,
    param_defaults: tuple[tuple[str, object], ...] = (),
) -> ToolMeta:
    """A read-only lookup tool: safe to run without user confirmation."""
    return ToolMeta(
        name=name,
        title=title,
        hint=hint,
        justification=justification,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
        param_defaults=param_defaults,
    )


def _write(name: str, title: str, hint: str, justification: str, idempotent: bool = False) -> ToolMeta:
    """A state-changing tool: the client should confirm before running it."""
    return ToolMeta(
        name=name,
        title=title,
        hint=hint,
        justification=justification,
        read_only=False,
        destructive=True,
        idempotent=idempotent,
        open_world=True,
    )


# Shared justification fragments, so the wording stays consistent.
_OPEN_WORLD = (
    "openWorldHint=true because the tool calls the external JobMojito API rather "
    "than acting on local state."
)
_READ_WHY = (
    "readOnlyHint=true: the endpoint only reads existing records and never creates, "
    "modifies or deletes anything. idempotentHint=true: repeated calls return the "
    "same data. " + _OPEN_WORLD
)


# (HTTP method, path) -> ToolMeta
TOOL_META: dict[tuple[str, str], ToolMeta] = {
    # --- Interview reports ---
    ("POST", "/job-interview-pdf"): _write(
        "generate_interview_report",
        "Generate interview report",
        "Generate an interview result report (HTML/PDF/JSON) for a completed "
        "interview. The report is decision-support material for a human reviewer, "
        "not an automated hiring decision.",
        "readOnlyHint=false / destructiveHint=true: rendering a report is a POST "
        "that produces a stored, shareable artifact and consumes account credits, "
        "so the client should confirm before running it. idempotentHint=true: "
        "re-rendering the same result yields the same report. " + _OPEN_WORLD,
        idempotent=True,
    ),
    # --- Interview ---
    ("GET", "/job-interview-get"): _read(
        "get_interview_definition",
        "Get interview definition",
        "Get the definition/configuration of an interview (position), including "
        "its ordered `questions` array. The questions come back in the same "
        "format create_interview_from_questions accepts, so you can read an "
        "interview here, change the array, and send it to update_interview. Each "
        "question's `id` identifies it — keep the ids you did not mean to change.",
        _READ_WHY,
    ),
    ("POST", "/job-interview-update"): _write(
        "update_interview",
        "Update interview or position",
        "Update the configuration of an existing interview/position: name, "
        "description, avatar template, recording, scoring, tags and the rest of "
        "the create-time settings. Only the fields you send are changed. "
        "To change the questions, send `questions` — the WHOLE list you want the "
        "interview to end up with, in order, in the format "
        "get_interview_definition returns. OMIT `questions` and the existing "
        "questions are left completely alone; there is no way to change one "
        "question on its own, so read the interview first, edit that array, and "
        "send it back. Resending an unchanged array does nothing. Not updated by "
        "this tool at all: the welcome and thank-you messages and the "
        "instructional-video screen (stored as steps, not questions), and the "
        "language, which the existing questions are already written in. Use "
        "`tags` to place a coaching session into a catalogue directory.",
        "readOnlyHint=false / destructiveHint=true: this overwrites the "
        "configuration of a live interview in place, which is user-visible to "
        "candidates and cannot be undone from the tool. idempotentHint=true: "
        "sending the same body twice leaves the same stored state. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/job-interview-set-state"): _write(
        "set_interview_state",
        "Change interview state",
        "Change the state of an interview/position (e.g. open, closed).",
        "readOnlyHint=false / destructiveHint=true: this mutates a live interview. "
        "Closing a position stops candidates from being able to complete it, which "
        "is user-visible and disruptive if done by mistake. idempotentHint=true: "
        "setting the same state twice has no additional effect. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/job-interview-result-request-another-attempt"): _write(
        "request_another_interview_attempt",
        "Re-open interview for another attempt",
        "Re-open a submitted interview result so the candidate can retry.",
        "readOnlyHint=false / destructiveHint=true: re-opening a submitted result "
        "changes the candidate's recorded outcome and can invalidate a completed "
        "assessment. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-token"): _write(
        "generate_interview_url",
        "Generate shareable interview link",
        "Generate a signed public interview URL/token.",
        "readOnlyHint=false / destructiveHint=true: this mints a signed credential "
        "that grants anyone holding it access to the interview, so it should not "
        "run without user confirmation. " + _OPEN_WORLD,
    ),
    ("GET", "/job-interview-details"): _read(
        "get_interview_result_details",
        "Get interview result details",
        "Get full interview result details including transcript and scores. "
        "Scores are assistive output for a human reviewer.",
        _READ_WHY,
    ),
    ("POST", "/invite-users"): _write(
        "invite_users",
        "Invite users",
        "Invite users/candidates and create interview URLs for them.",
        "readOnlyHint=false / destructiveHint=true: sends invitations to real "
        "people, which cannot be undone. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-register-users"): _write(
        "register_users_for_interview",
        "Register candidates for interview",
        "Register users/candidates for a specific interview and return their "
        "personal interview links.",
        "readOnlyHint=false / destructiveHint=true: creates candidate records and "
        "may trigger outbound invitation email to real people — an irreversible, "
        "externally visible side effect. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-create"): _write(
        "create_interview",
        "Create interview",
        "Create a new interview and auto-generate its question sequence from "
        "position data. The `interview_template_id` you pass also sets the "
        "modality (voice-only vs realtime/pre-recorded avatar) — see `list_avatars`.",
        "readOnlyHint=false / destructiveHint=true: creates a new persistent "
        "interview definition on the account and consumes credits. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-create-from-array"): _write(
        "create_interview_from_questions",
        "Create interview from questions",
        "Create a new interview from an explicit array of questions.",
        "readOnlyHint=false / destructiveHint=true: creates a new persistent "
        "interview definition on the account and consumes credits. " + _OPEN_WORLD,
    ),
    # NOTE: excluded from the MCP surface via IGNORED_PATHS below — HTTP only.
    # The curated metadata is kept here so it's ready if it's ever un-ignored.
    ("POST", "/job-interview-create-for-candidate-with-token"): _write(
        "create_interview_for_candidate",
        "Create interview for candidate",
        "Create an interview for a specific candidate and return an access-token URL.",
        "readOnlyHint=false / destructiveHint=true: creates a persistent record and "
        "mints an access credential. " + _OPEN_WORLD,
    ),
    ("POST", "/persona-create"): _write(
        "create_persona",
        "Create role-play persona",
        "Create a role-play persona: an avatar that plays a defined role in a "
        "free-form conversation instead of a scored Q&A interview. Set "
        "`persona_role_avatar`/`persona_role_user` for the roles and `opening_line` "
        "for the avatar's first spoken line (defaults to a generic 'Hello'). "
        "Coaching-platform feature.",
        "readOnlyHint=false / destructiveHint=true: creates a new persistent "
        "persona on the account. " + _OPEN_WORLD,
    ),
    # --- Coaching catalogue ---
    ("GET", "/catalogue-tag-list"): _read(
        "list_catalogue_directories",
        "List coaching catalogue directories",
        "List the coaching-catalogue directories you can see (your merchant's own "
        "plus the platform-wide public ones). Start here to find a directory id, "
        "to pick a parent for a new one, or to walk the tree with `parent_tag`; "
        "`is_start_directory` marks the page the catalogue opens on. The custom "
        "Markdown page is not included — read it with get_catalogue_directory.",
        _READ_WHY,
    ),
    ("GET", "/catalogue-tag-get"): _read(
        "get_catalogue_directory",
        "Get coaching catalogue directory",
        "Read one catalogue directory in full: its settings, its custom Markdown "
        "page (`content_md`), its resolved sub-directories, and the coaching "
        "sessions its tag filter currently matches — which is how you verify that "
        "a session's `tags` actually place it in this directory. Read before "
        "updating: `content_md`, `tags_sub` and `tags_interview_set_filter` are "
        "replaced wholesale, so you need the current value to extend it.",
        _READ_WHY,
    ),
    ("POST", "/catalogue-tag-create"): _write(
        "create_catalogue_directory",
        "Create coaching catalogue directory",
        "Create a directory (page) in the coaching portal catalogue. A directory "
        "nests other directories (`tags_sub`), lists coaching sessions whose own "
        "`tags` match its `tags_interview_set_filter`, and can carry a fully "
        "custom Markdown page (`content_md`) with `[sessions]`, `[directory:…]`, "
        "`[session:…]` and `[plan-progress]` directives. Coaching-platform feature.",
        "readOnlyHint=false / destructiveHint=true: creates a persistent, "
        "publicly reachable catalogue page on the merchant's coaching portal. "
        + _OPEN_WORLD,
    ),
    ("POST", "/catalogue-tag-update"): _write(
        "update_catalogue_directory",
        "Update coaching catalogue directory",
        "Update a coaching catalogue directory: rename it, change which sessions "
        "it lists (`tags_interview_set_filter`), re-order its sub-directories "
        "(`tags_sub`), or author its custom Markdown page (`content_md`). Only "
        "the fields you send are changed. Coaching-platform feature.",
        "readOnlyHint=false / destructiveHint=true: overwrites a live, publicly "
        "reachable catalogue page in place. idempotentHint=true: sending the same "
        "body twice leaves the same stored state. " + _OPEN_WORLD,
        idempotent=True,
    ),
    # --- Pre-screening ---
    ("POST", "/pre-screening-create"): _write(
        "upsert_pre_screening",
        "Create or update pre-screening",
        "Create or update a pre-screening assessment for a position.",
        "readOnlyHint=false / destructiveHint=true: overwrites an existing "
        "pre-screening configuration in place. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/job-interview-pre-screening-api-resume-text"): _write(
        "pre_screen_resume_text",
        "Pre-screen resume (text)",
        "Run pre-screening on a candidate from plain-text resume content. Output is "
        "assistive only and must be reviewed by a qualified human before it informs "
        "any hiring decision.",
        "readOnlyHint=false / destructiveHint=true: stores a pre-screening result "
        "against the candidate and consumes credits. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-pre-screening-api-resume-binary"): _write(
        "pre_screen_resume_binary",
        "Pre-screen resume (file)",
        "Run pre-screening on a candidate from an uploaded (binary) resume file. "
        "Output is assistive only and must be reviewed by a qualified human before "
        "it informs any hiring decision.",
        "readOnlyHint=false / destructiveHint=true: stores a pre-screening result "
        "against the candidate and consumes credits. " + _OPEN_WORLD,
    ),
    # --- Knowledge base ---
    ("POST", "/knowledge-base-document-upload"): _write(
        "upload_knowledge_base_document",
        "Upload knowledge base document",
        "Upload and process a knowledge base document (multipart form-data).",
        "readOnlyHint=false / destructiveHint=true: stores a new document on the "
        "account and triggers processing that consumes credits. " + _OPEN_WORLD,
    ),
    # --- Merchant lists (read-only) ---
    ("GET", "/merchant-interview-list"): _read(
        "list_interviews",
        "List interviews",
        "List the merchant's interview definitions.",
        _READ_WHY,
    ),
    ("GET", "/merchant-candidate-list"): _read(
        "list_candidates",
        "List candidates",
        "List the merchant's candidates.",
        _READ_WHY,
    ),
    ("GET", "/merchant-result-list"): _read(
        "list_interview_results",
        "List interview results",
        "List the merchant's interview results.",
        _READ_WHY,
    ),
    ("GET", "/merchant-avatar-list"): _read(
        "list_avatars",
        "List avatars and voice templates",
        "List available avatar/voice templates. Each item's `type` decides the "
        "interview modality: `interactive_elevenlabs` = voice-only (no video "
        "avatar); `interactive_heygen` = realtime interactive avatar (video); "
        "`offline_heygen` = pre-recorded, non-interactive avatar. An item's `id` is "
        "the `interview_template_id` you pass to the create-interview tools, so pick "
        "the template whose type matches the experience you want. Note: "
        "`offline_elai` and `offline_synthesia` are legacy integrations that may "
        "still appear here but cannot be used to create new interviews. Rows are "
        "large, so this returns 15 at a time; page with `offset` while "
        "`pagination.has_more` is true, or narrow with `type`/`filter_text`.",
        _READ_WHY,
        # Measured: ~2,700 chars of API JSON per row, ~4,900 on the wire once MCP
        # duplicates it into content + structuredContent. The spec's default of 50
        # is ~247,000 chars — over twice MAX_TOOL_RESULT_CHARS — so list_avatars
        # failed on every default call. 15 lands near 74,000 with room for rows
        # whose media URLs run long. See PAGE_SIZE_WHY.
        param_defaults=(("limit", 15),),
    ),
    ("GET", "/merchant-sub-merchant-list"): _read(
        "list_sub_merchants",
        "List sub-merchants",
        "List sub-merchants under the merchant account.",
        _READ_WHY,
    ),
    ("GET", "/merchant-analytics"): _read(
        "get_merchant_analytics",
        "Get merchant analytics",
        "Get the merchant's daily event analytics.",
        _READ_WHY,
    ),
    ("GET", "/merchant-analytics-credits-used"): _read(
        "get_merchant_credit_usage",
        "Get merchant credit usage",
        "Get the merchant's credit usage.",
        _READ_WHY,
    ),
    ("GET", "/merchant-status"): _read(
        "get_merchant_status",
        "Get merchant status",
        "Get a merchant status snapshot: credit balances, subscription, pending-work "
        "counts, candidate/result totals, and invitation headroom.",
        _READ_WHY,
    ),
    ("GET", "/platform-languages-list"): _read(
        "list_languages",
        "List supported languages",
        "List supported platform (mojito) languages: the `code` to pass as "
        "`mojito_language_code`, English/local names, SVG flag URL, per-interface "
        "enablement flags, and Azure speech accents.",
        _READ_WHY,
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


def meta_for(method: str, path: str) -> ToolMeta | None:
    """Return the curated metadata for a route, or None if the route is unknown."""
    return TOOL_META.get((method.upper(), path))


def fallback_meta(method: str, path: str) -> ToolMeta:
    """Annotations for an endpoint that has no curated entry yet.

    New JobMojito endpoints appear as tools automatically. Rather than shipping
    them with no annotations at all (which fails directory review), derive a
    conservative default from the HTTP method: safe methods are read-only,
    everything else is treated as destructive so clients ask before running it.
    """
    is_safe = method.upper() in SAFE_METHODS
    return ToolMeta(
        name=path.strip("/").replace("/", "_").replace("-", "_") or "root",
        title=path.strip("/").replace("-", " ").replace("_", " ").title() or path,
        hint="",
        justification=(
            "Auto-derived from the HTTP method because this endpoint has no curated "
            f"entry in naming.TOOL_META yet ({method.upper()} is "
            f"{'safe/read-only' if is_safe else 'unsafe/state-changing'} per RFC 9110). "
            + _OPEN_WORLD
        ),
        read_only=is_safe,
        destructive=not is_safe,
        idempotent=is_safe,
        open_world=True,
    )


def curated_defaults() -> dict[str, dict[str, object]]:
    """``{tool_name: {param: default}}`` for every endpoint with overrides.

    Consumed by ``middleware.CuratedDefaultsMiddleware``, which is what puts the
    value on the wire — the spec's `default` alone is only advertised to the
    model, never sent (see that class for why).
    """
    return {
        meta.name: dict(meta.param_defaults)
        for meta in TOOL_META.values()
        if meta.param_defaults
    }


def operation_id_for(method: str, path: str) -> str | None:
    """Return the curated operationId/tool-name for a route, or None if unknown."""
    meta = meta_for(method, path)
    return meta.name if meta else None


def description_hint_for(method: str, path: str) -> str | None:
    """Return the curated one-line description hint for a route."""
    meta = meta_for(method, path)
    return meta.hint if meta else None


def annotation_justifications(include_ignored: bool = False) -> dict[str, str]:
    """Tool name -> annotation justification, for directory submission forms.

    OpenAI's plugin submission asks for a written justification for every tool
    annotation. Generating that list from the same table the server runs on means
    the submitted answer can never drift from the deployed behaviour.

    Run ``python -c "import naming,json;print(json.dumps(naming.annotation_justifications(),indent=2))"``
    to produce the copy-pasteable answer set.
    """
    out: dict[str, str] = {}
    for (_method, path), meta in sorted(TOOL_META.items(), key=lambda kv: kv[1].name):
        if path in IGNORED_PATHS and not include_ignored:
            continue
        out[meta.name] = meta.justification
    return out
