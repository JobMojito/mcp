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

from dataclasses import dataclass, field, replace
from typing import Callable

from raw_assessment import condense_answer_assessment

# HTTP methods that are safe/read-only by definition (RFC 9110).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# MCP tool names are capped at 64 characters by the Anthropic review criteria.
MAX_TOOL_NAME_LENGTH = 64


#: Name of the MCP-only argument that selects a response projection.
#:
#: It is NOT an API parameter. ``openapi_loader.inject_view_params`` adds it to
#: the tool's input schema and ``middleware.ResponseViewMiddleware`` removes it
#: from the arguments before the upstream request is built, so it never reaches
#: JobMojito. (If it ever leaked, the Edge Functions' non-strict zod query
#: schemas would drop it rather than 422 — but the middleware is the contract.)
VIEW_PARAM_NAME = "view"

#: Marks a parameter this server injected, so a later load can tell it apart from
#: a parameter the API genuinely declares. Needed because the runtime OpenAPI
#: cache stores the PREPARED spec: without the marker, a cached copy of an
#: injected parameter would look like an API parameter, and an edited
#: ``ResponseView`` would never reach a server booting from cache.
VIEW_PARAM_MARKER = "x-mcp-injected"


@dataclass(frozen=True)
class ResponseView:
    """One named projection of a tool's response.

    ``drop`` lists dotted field paths to remove from the JSON before it is
    returned. ``[]`` marks an array level, so ``transcript[].ai_analysis``
    removes that field from every transcript entry. Paths that don't exist in a
    given response are simply ignored — the API's schema is passthrough
    (``catchall``) and fields come and go.

    Every field named here MUST be optional in the response schema, or output
    validation will reject the pruned result. ``tests/test_smoke.py`` pins that.

    ``condense`` pairs a path (same syntax) with a function that replaces the
    value there with a smaller one, for a field that is worth returning but not
    in its raw form. Only use it on a field the schema leaves untyped (`z.any`),
    so the condensed shape still validates.
    """

    name: str
    #: One line, shown to the model in the `view` parameter description. Say what
    #: is KEPT and what is dropped — this is how the model picks.
    summary: str
    drop: tuple[str, ...] = ()
    condense: tuple[tuple[str, Callable], ...] = ()


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
    #: Response projections offered through the MCP-only ``view`` argument, and
    #: which of them applies when the model doesn't pass one. Empty (the default)
    #: means the endpoint has no ``view`` argument and its response is returned
    #: verbatim. See ``RESPONSE_VIEW_WHY``.
    views: tuple[ResponseView, ...] = ()
    default_view: str = ""
    #: Body arguments sent as an explicit null when the model passes null, instead
    #: of being left out. See ``null_body_args.py`` for why this is opt-in and for
    #: the test a field has to pass. Name each one in ``hint``.
    null_body_args: tuple[str, ...] = ()
    #: ``(old, new)`` replacements in this endpoint's own description and its
    #: top-level body field descriptions, applied at load time by
    #: ``openapi_loader.apply_description_fixes``. For text that is true of the HTTP
    #: API but false through this tool, such as "null clears it" on a field whose
    #: null is left out. ``new`` must not contain ``old``: the cached spec is
    #: prepared a second time on the next boot.
    description_fixes: tuple[tuple[str, str], ...] = ()

    @property
    def effective_default_view(self) -> str:
        """The view applied when the model passes none.

        Both the advertised schema default (``view_parameter_schema``) and the
        runtime fallback (``response_view_rules``) read this one property, so the
        tool cannot promise one projection and return another.
        """
        if not self.views:
            return ""
        known = {view.name for view in self.views}
        if self.default_view in known:
            return self.default_view
        return self.views[-1].name

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
#: ``MAX_TOOL_RESULT_CHARS`` budget at a page size the API considers modest, and
#: the tool then fails on its *first* call with default arguments, which reads as
#: "broken". (``ResultSizeGuardMiddleware`` will drop the duplicate copy rather
#: than fail outright, but that is a last resort — don't budget against it.)
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


#: Why an endpoint would offer a ``view`` argument.
#:
#: Pagination fixes a list that is too long. It does nothing for a SINGLE record
#: that is too fat — there is only one row, and the caller cannot ask for less of
#: it. ``get_interview_result_details`` is the case in point: one completed
#: interview returns the whole transcript plus, per answer, the raw
#: pronunciation/sentiment assessment blob the scoring pipeline produced. That
#: blob is machine intermediate state; no agent reads it, and it dominates the
#: payload.
#:
#: A ``view`` therefore does for wide records what ``limit`` does for long lists:
#: it lets the default call return the fields a reviewer actually reads, while
#: ``view="full"`` still returns every field for anyone who needs it — with the
#: raw blob condensed to its summaries, because verbatim it never fit (see
#: ``raw_assessment.py``). The projection happens in this server, not upstream —
#: the API has no field-selection parameter.
RESPONSE_VIEW_WHY = (
    "a single record carries machine-only blobs large enough to overflow the "
    "tool result limit, and pagination cannot narrow one record"
)


#: Projections for ``get_interview_result_details``.
#:
#: Order matters only for readability; ``default_view`` picks the default. The
#: dropped fields are all optional in ``JobInterviewDetailsResponse`` (its zod
#: schema marks every field ``.optional()``), so a pruned response still passes
#: output validation.
_INTERVIEW_RESULT_VIEWS = (
    ResponseView(
        "summary",
        "scores, the overall AI analysis, and each question with the candidate's "
        "answer — no per-answer AI commentary, recording paths or raw assessment "
        "data. Use this to review or compare candidates.",
        drop=(
            "transcript[].ai_analysis",
            "transcript[].ai_analysis_recruiter",
            "transcript[].answer_assessment_raw_data",
            "transcript[].external_data",
            "transcript[].external_id",
            "transcript[].question_original",
            "transcript[].recording_local_path",
            "transcript[].recording_is_video",
            "transcript[].recording_url",
        ),
    ),
    ResponseView(
        "standard",
        "everything a human reviewer reads: the full transcript with per-answer "
        "analysis, scores and recordings, minus the raw machine assessment blobs. "
        "This is the default.",
        drop=(
            "transcript[].answer_assessment_raw_data",
            "transcript[].external_data",
        ),
    ),
    ResponseView(
        "full",
        "every field, including the per-answer raw assessment data — condensed: "
        "pronunciation and sentiment scores per recognised segment with the "
        "lowest-scoring words, reading/gaze statistics, STT reliability, proctor "
        "status and seconds per detected speaker. Word-, character- and "
        "frame-level arrays are left out (listed in `_omitted`). Only ask for "
        "this if you need those assessment details.",
        drop=(),
        # Verbatim, this view failed on every real interview it was asked for
        # (30 Sep / 2 Oct 2026: 477k-806k characters against a 150k limit, ~96%
        # of it word timings and per-frame gaze). See raw_assessment.py.
        condense=(
            ("transcript[].answer_assessment_raw_data", condense_answer_assessment),
        ),
    ),
)


def _read(
    name: str,
    title: str,
    hint: str,
    justification: str,
    param_defaults: tuple[tuple[str, object], ...] = (),
    views: tuple[ResponseView, ...] = (),
    default_view: str = "",
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
        views=views,
        default_view=default_view,
    )


def _write(
    name: str,
    title: str,
    hint: str,
    justification: str,
    idempotent: bool = False,
    null_body_args: tuple[str, ...] = (),
    description_fixes: tuple[tuple[str, str], ...] = (),
) -> ToolMeta:
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
        null_body_args=null_body_args,
        description_fixes=description_fixes,
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
# Which id of a list_interview_results row is the result: the two kinds of row differ.
_RESULT_ID = (
    "interview_result_id from list_interview_results: a single-stage row's `id`, a "
    "multi-stage row's `interview_result_id`."
)
# Field reference for the portal configuration documents (developer docs).
_PORTAL_COOKBOOKS = (
    "Field reference: https://developer.jobmojito.com/cookbooks/portal-branding/branding, "
    "https://developer.jobmojito.com/cookbooks/portal-branding/interview-portal, "
    "https://developer.jobmojito.com/cookbooks/portal-branding/coaching-portal, "
    "https://developer.jobmojito.com/cookbooks/portal-branding/portal-pages."
)


# (HTTP method, path) -> ToolMeta
TOOL_META: dict[tuple[str, str], ToolMeta] = {
    # --- Interview reports ---
    ("POST", "/job-interview-pdf"): _write(
        "generate_interview_report",
        "Generate interview report",
        "Generate an interview result report (HTML/PDF/JSON) for a completed "
        "interview. The report is decision-support material for a human reviewer, "
        "not an automated hiring decision. " + _RESULT_ID,
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
        "format create_interview accepts in `questions`, so you can read an "
        "interview here, change the array, and send it to update_interview. Each "
        "question's `id` identifies it — keep the ids you did not mean to change. "
        "For a position add include=process (its steps with their settings and "
        "updated_at, for update_position_step), slots (upcoming meeting slots), team, "
        "lifecycle, publishing.",
        _READ_WHY,
    ),
    ("POST", "/job-interview-update"): _write(
        "update_interview",
        "Update interview or position",
        "Update the configuration of an existing interview/position: name, "
        "description, avatar template, recording, scoring, tags, category and the rest "
        "of the create-time settings; for a position also its hiring `team` and "
        "recruiter. `status` is applied as set_interview_state applies it (archiving an "
        "active position can e-mail its open applicants: use set_interview_state with "
        "dry_run to see first); the vacancy lifecycle and the embed key are set with "
        "set_interview_state only. "
        "A position's steps are changed with set_position_process and "
        "update_position_step. Only the fields you send are changed. "
        "To change the questions, send `questions` — the WHOLE list you want the "
        "interview to end up with, in order, in the format "
        "get_interview_definition returns. OMIT `questions` and the existing "
        "questions are left completely alone; there is no way to change one "
        "question on its own, so read the interview first, edit that array, and "
        "send it back. Resending an unchanged array does nothing. Not updated by "
        "this tool at all: the welcome and thank-you messages and the "
        "instructional-video screen (stored as steps, not questions), and the "
        "language, which the existing questions are already written in (no tool "
        "changes these: point the user to the admin). Use "
        "`tags` to place a coaching session into a catalogue directory. "
        "MULTI-STAGE POSITIONS: the interview-only settings (recording, "
        "knowledge_base_store_id, candidate_expectations_json and the like) do not exist "
        "at position level and are refused with a 422 naming the field. Read the position "
        "with get_interview_definition include=process and update its AI interview step's "
        "interview (the step's interview_set_id) instead. "
        "A null argument counts as not sent and changes nothing, except on three "
        "fields where null is the only way to switch something off: "
        "`candidate_notification_channel` (no SMS / WhatsApp), "
        "`questions_random_subset` (ask all questions) and "
        "`pdf_export_auto_config` (no automatic report). Leave those three out "
        "unless that is what you mean.",
        "readOnlyHint=false / destructiveHint=true: this overwrites the "
        "configuration of a live interview in place, which is user-visible to "
        "candidates and cannot be undone from the tool. idempotentHint=true: "
        "sending the same body twice leaves the same stored state. " + _OPEN_WORLD,
        idempotent=True,
        null_body_args=(
            "candidate_notification_channel",
            "questions_random_subset",
            "pdf_export_auto_config",
        ),
        description_fixes=(
            (
                ", and sending null clears a nullable field.",
                ". A null argument counts as not sent, except on "
                "candidate_notification_channel, questions_random_subset and "
                "pdf_export_auto_config.",
            ),
            # A blank string clears these (BLANK_IS_NULL in job-interview-update).
            ("null keeps it always available.", "An empty string makes it always available."),
            ("null clears it.", "An empty string clears it."),
            ("null unlinks it.", "An empty string unlinks it."),
            # The API stores {} as null (normalizeCountryAvailability).
            ("Null = not used, every country.", "An empty object {} = not used, every country."),
            (
                "null (or an object with name null/blank) means hiring for yourself;",
                "An object with name null/blank means hiring for yourself;",
            ),
            ("null is treated like hidden.", "Send `hidden` to stop asking."),
            ("null uses the template default (Normal).", "2 is the template default (Normal)."),
            (
                "Omit/null keeps the template default pace.",
                "Omit to keep the current pace; `normal` is the default speaking pace.",
            ),
            # recording / recording_full_session: `none` is the explicit off value; a
            # null stays out, so a stray one cannot turn proctoring off. See
            # null_body_args.py.
            (" `none` switches it off (null does too).", " `none` switches it off; a null is ignored."),
            (
                "null clears the rubric.",
                "A null is ignored: send a new rubric to replace it, or "
                "regenerate_candidate_expectations together with `questions` to derive "
                "it again.",
            ),
            (
                "Omit/null to leave it out of any plan.",
                "Omit to keep the current stage. A null is ignored: use `other` when no "
                "stage fits.",
            ),
        ),
    ),
    ("POST", "/job-interview-set-state"): _write(
        "set_interview_state",
        "Change publication status, vacancy lifecycle or embedding",
        "Change an interview's or position's publication `status` (draft, active, "
        "archived...), or a position's vacancy `lifecycle` (open, applications_closed, "
        "on_hold, filled, cancelled, closed) with an optional reason_id and note, or its "
        "embed key (is_embedded). Moving a position to filled, cancelled or closed, or "
        "archiving it, e-mails every candidate whose application is still open, when the "
        "company has the position-closed notice on. on_hold and cancelled need a reason_id: one of the "
        "company's position status reasons from list_reasons type=position_status, "
        "whose applies_to has the lifecycle. "
        "dry_run=true answers the status change and whether the position-closed e-mail "
        "would go out, to how many candidates: run it first and confirm with the user. "
        "Prefer `archived` "
        "to `deleted`: a deleted interview can no longer be read or updated. This is the "
        "tool for status changes.",
        "readOnlyHint=false / destructiveHint=true: this mutates a live interview. "
        "Closing a position stops candidates from being able to complete it, which "
        "is user-visible and disruptive if done by mistake. idempotentHint=true: "
        "setting the same state twice has no additional effect. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/job-interview-result-request-another-attempt"): _write(
        "request_another_interview_attempt",
        "Re-open interview for another attempt",
        "Re-open a submitted interview result so the candidate can take it again: the "
        "attempt is marked failed, the same result goes back to draft and the recruiter's "
        "decision on it is cleared. The candidate may then continue, keeping the answers "
        "already given, or start over from scratch. There is no preview and nothing is "
        "e-mailed: confirm "
        "with the user first, then tell the candidate. To allow more attempts on the whole "
        "interview instead, raise interview_attempts with update_interview. Company "
        "owners, recruiters and platform admins only. " + _RESULT_ID,
        "readOnlyHint=false / destructiveHint=true: re-opening a submitted result "
        "changes the candidate's recorded outcome and can invalidate a completed "
        "assessment. " + _OPEN_WORLD,
    ),
    ("POST", "/assessment-result-set"): _write(
        "set_assessment_result",
        "Record an external assessment result",
        "Record the result of a candidate's external assessment step (status, score, "
        "pass / fail, report link). The step then waits for the recruiter's decision.",
        "readOnlyHint=false / destructiveHint=true: it writes a result onto a candidate's "
        "application and hands the step to the recruiter's decision, so it should not "
        "run without user confirmation. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-token"): _write(
        "generate_interview_url",
        "Generate shareable interview link",
        "Generate a signed link; nothing is sent. `type` picks who it is for and the ids it "
        "needs: interview-for-profile = one candidate's own interview link "
        "(interview_profile_id + interview_id); results-for-profile = that candidate's "
        "results (interview_profile_id); interview-result-candidate = one result for the "
        "candidate, interview-result-talent-seeker = one result for a reviewer "
        "(interview_result_id); interview-results-for-position = every result of an "
        "active interview or position for a reviewer (interview_id). "
        "interview_profile_id is the candidate's `id` from list_candidates. There is no "
        "anonymous link to an interview: to invite people use invite_candidates.",
        "readOnlyHint=false / destructiveHint=true: this mints a signed credential "
        "that grants anyone holding it access to the interview, so it should not "
        "run without user confirmation. " + _OPEN_WORLD,
    ),
    ("GET", "/job-interview-details"): _read(
        "get_interview_result_details",
        "Get interview result details",
        "Get full interview result details including transcript and scores. "
        "Scores are assistive output for a human reviewer. One result is a large "
        "record, so `view` controls how much of it comes back — the default "
        "(`standard`) omits only the raw machine assessment data. " + _RESULT_ID,
        _READ_WHY,
        views=_INTERVIEW_RESULT_VIEWS,
        default_view="standard",
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
        "Register candidates for an interview and return their personal interview "
        "links. Sends NO e-mail: to invite candidates by e-mail use invite_candidates.",
        "readOnlyHint=false / destructiveHint=true: creates or updates candidate "
        "records and mints personal interview links, which cannot be undone. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-create"): _write(
        "create_interview",
        "Create interview",
        "Create a new interview. Without `questions` its question sequence is "
        "generated from the position data. With `questions` (an explicit list) they are "
        "stored as you wrote them, and in the session the avatar says each one in its "
        "tone, rephrased, unless you pass `interview_tone=\"exact\"`, which reads them "
        "verbatim (compliance, translated or legally reviewed text). With `questions`, a "
        "rapport question is added first unless include_rapport_question=false (3 sent "
        "are 4 asked), and the candidate expectations are generated from them. `type` is "
        "the product (interview, assessment, coaching); `interview_type` is the style of "
        "an interview (pre-screening, second-interview...); for invite-only hiring use "
        "visibility=merchant_invite. A position's pre-screening "
        "STEP (resume and form) is something else: added with set_position_process, "
        "configured with update_position_step. The "
        "`interview_template_id` you pass "
        "also sets the modality (voice-only vs realtime/pre-recorded avatar): see "
        "`list_avatars`.",
        "readOnlyHint=false / destructiveHint=true: creates a new persistent "
        "interview definition on the account and consumes credits. " + _OPEN_WORLD,
    ),
    ("POST", "/job-interview-create-from-array"): _write(
        "create_interview_from_questions",
        "Create interview from questions",
        "Create a new interview from an explicit array of questions. The AI "
        "rephrases your wording by default; pass `interview_tone=\"exact\"` to "
        "have each question asked verbatim, which is what you want when the "
        "wording is a script (compliance, translated, or legally reviewed text).",
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
        "free-form conversation instead of a scored Q&A interview. FIRST CHOOSE "
        "`portal`, because it selects between two different products: "
        "`portal=\"interview\"` builds a SIMULATED PERSONA a recruiter invites "
        "candidates to — scored, billed to merchant credits, and listed with the "
        "recruiter's other results; `portal=\"coaching\"` (THE DEFAULT) builds a "
        "coaching persona learners start themselves from the catalogue, paid per "
        "second from their own coaching minutes and never visible to recruiters. Pass "
        "`portal=\"interview\"` explicitly for any hiring, screening or assessment "
        "use. Then set `persona_role_avatar`/`persona_role_user` for the roles and "
        "`opening_line` for the avatar's first spoken line (defaults to a generic "
        "'Hello').",
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
        "Markdown page is not included — read it with get_catalogue_directory. "
        "Returns 25 at a time; page with `offset` while `pagination.has_more` "
        "is true.",
        _READ_WHY,
        # Measured in production (PostHog $mcp_tool_call, 24 Aug 2026): a default
        # 50-row call came back at 122,984 chars on the wire, ~2,460 per row —
        # over the 120,000 budget in force at the time and uncomfortably close to
        # today's 150,000. 25 rows costs ~61,000, which leaves room for
        # directories whose tag filters and sub-directory lists run long.
        param_defaults=(("limit", 25),),
    ),
    ("GET", "/catalogue-tag-get"): _read(
        "get_catalogue_directory",
        "Get coaching catalogue directory",
        "Read one catalogue directory in full: its settings, its custom Markdown "
        "page (`content_md`), its resolved sub-directories, and the coaching "
        "sessions its tag filter currently matches — which is how you verify that "
        "a session's `tags` actually place it in this directory. Read before "
        "updating: `content_md`, `tags_sub` and `tags_interview_set_filter` are "
        "replaced wholesale, so you need the current value to extend it. Other "
        "language versions of the page are in `translations` (`languages` lists "
        "them, default first).",
        _READ_WHY,
    ),
    # One save tool: POST /catalogue-tag-create takes action create (default) or
    # update; /catalogue-tag-update stays in the API, hidden here (IGNORED_PATHS).
    ("POST", "/catalogue-tag-create"): _write(
        "save_catalogue_directory",
        "Create or update a coaching catalogue directory",
        "With action create (the default): create a directory (page) in the coaching "
        "portal catalogue. A directory nests other directories (`tags_sub`), lists "
        "coaching sessions whose own `tags` match its `tags_interview_set_filter`, and "
        "can carry a fully custom Markdown page (`content_md`) with `[sessions]`, "
        "`[directory:…]`, `[session:…]` and `[plan-progress]` directives; the top-level "
        "fields are the default language, `translations` adds the others. With action "
        "update and `id`: rename it, change which sessions it lists, re-order its "
        "sub-directories, author its page, or add, replace or remove a language version "
        "(`translations`, per language; null removes one). On update only the fields you "
        "send change; a top-level null counts as not sent, except `content_md: null`, the "
        "only way to remove the custom page and restore the default grid. "
        "Coaching-platform feature.",
        "readOnlyHint=false / destructiveHint=true: creates a publicly reachable "
        "catalogue page, or overwrites a live one in place. idempotentHint=false: each "
        "create makes another directory. " + _OPEN_WORLD,
        null_body_args=("content_md",),
        # The create endpoint's text (with action update it does what catalogue-tag-update
        # did): its "null clears" promises are false here, a top-level null is left out.
        description_fixes=(
            (
                "(null clears a nullable field; in `translations` null removes a language)",
                "(a top-level null argument counts as not sent, except on content_md; in "
                "`translations` a language sent as null is removed)",
            ),
            # The portal card hides a blank cover (CategoryCard), so a blank string is
            # the way to clear it on update.
            (
                "Cover image URL shown on the directory card.",
                "Cover image URL for the directory card. An empty string clears it.",
            ),
            (
                "Omit/null to leave it out of any plan.",
                "Omit to leave it out of any plan, or on update to keep the current stage. "
                "A null is ignored: use `other` when no stage fits.",
            ),
            # Each language's null does arrive: nested values are not left out. The
            # whole-object null does not, so say how to remove every version.
            (
                "and null removes that language.",
                "and a language sent as null is removed; to remove every version, send null "
                "for each language.",
            ),
            # content_md opted in (null_body_args): its null arrives and removes the page.
            (
                "; null renders the plain grid of sub-directories and sessions.",
                ". Sending null removes the custom page and renders the plain grid of "
                "sub-directories and sessions.",
            ),
        ),
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
        "Upload and process a knowledge base document (base64 file, `name` with its "
        "extension) into a store from list_knowledge_base_stores. An interview uses a store "
        "once create_interview / update_interview set its knowledge_base_store_id: link "
        "it before limiting a document with interview_ids. The optional "
        "interview_ids limits it to chosen interviews on the same knowledge base: "
        "leave it out for every interview, [] for none. Returns the limit it set.",
        "readOnlyHint=false / destructiveHint=true: stores a new document on the "
        "account and triggers processing that consumes credits. " + _OPEN_WORLD,
        # This tool sends JSON only; the multipart form is the HTTP API's other option.
        description_fixes=(
            (
                "Accepts the file either as multipart/form-data (binary `file`) or as "
                "application/json (base64 `file`).",
                "Send the file base64-encoded in `file`.",
            ),
            (
                " With multipart/form-data, repeat the field or send one JSON array such as "
                '`["<id>"]`, and write an empty list as `[]`.',
                "",
            ),
        ),
    ),
    ("GET", "/knowledge-base-document-list"): _read(
        "list_knowledge_base_documents",
        "List knowledge base documents",
        "List the company's knowledge base documents, optionally for one store, with the "
        "interviews each is limited to. Pass knowledge_base_id to read one document's limit.",
        _READ_WHY,
    ),
    ("POST", "/knowledge-base-document-set-interviews"): _write(
        "set_knowledge_base_document_interviews",
        "Change which interviews can use a document",
        "Replace the interview limit of an existing knowledge base document. interview_ids "
        "is required: a list limits it to those interviews on the same knowledge base, [] to "
        "none, and null opens it to every interview on the knowledge base.",
        "readOnlyHint=false / destructiveHint=true: replaces the document's list, and null "
        "exposes the document to every interview on the knowledge base. idempotentHint=true: "
        "the same list gives the same result, and nothing is processed or charged. " + _OPEN_WORLD,
        idempotent=True,
        null_body_args=("interview_ids",),
    ),
    # --- Merchant lists (read-only) ---
    ("GET", "/merchant-interview-list"): _read(
        "list_interviews",
        "List interviews and positions",
        "List the merchant's interviews and positions, with lifecycle, category, tags, "
        "recruiter and step count; filter by lifecycle, recruiter_profile_id or "
        "interview_category. type=interview (the default) also lists multi-stage positions.",
        _READ_WHY,
    ),
    ("GET", "/merchant-candidate-list"): _read(
        "list_candidates",
        "List and search candidates",
        "List the merchant's candidates, filtered by text, activity tab, source, channel, "
        "owner, prospect or applicant. To search like the admin's segment builder (tags, "
        "positions, steps, status, dates, countries, skills, custom fields...) pass `rules` "
        "as an object (or the same JSON as a string) {\"match\": \"all\", \"rules\": [...]}, field ids and values "
        "from list_candidate_filters, e.g. '{\"match\":\"all\",\"rules\":[{\"field\":"
        "\"tag\",\"op\":\"any\",\"values\":[\"<tag id>\"]},{\"field\":\"applied\","
        "\"op\":\"within_days\",\"value\":30}]}' (list operators take `values`, the "
        "others `value`); or `segment_id` for a segment's members.",
        _READ_WHY,
    ),
    ("GET", "/merchant-result-list"): _read(
        "list_interview_results",
        "List interview results",
        "List the merchant's interview results. Rows carry the candidate's "
        "scores and recruiter risk flags, so this returns 20 at a time; page "
        "with `offset` while `pagination.has_more` is true, or narrow with "
        "`tab`/`interview_id`/`filter_text`/`owner`/`position_def_step_id`. "
        "extended=true adds the owner, time on the step, outcome and decision reason. "
        "tab=shortlist is the shortlisted state of an application, not a candidate tag "
        "named Shortlist (find tagged candidates with list_candidates rules).",
        _READ_WHY,
        # Measured in production (PostHog $mcp_tool_call, Aug-Sep 2026): three
        # default 50-row calls came back at 124,120 / 149,276 / 263,118 chars on
        # the wire, i.e. ~2,480 to ~5,260 per row depending on how much analysis
        # and how many risk flags each result carries. The worst of those three
        # overflowed even the 150,000 budget; the other two only fitted once the
        # guard dropped the duplicate text copy, which PAGE_SIZE_WHY says not to
        # budget against. 20 rows costs ~105,000 at the worst observed row size.
        param_defaults=(("limit", 20),),
    ),
    ("GET", "/merchant-avatar-list"): _read(
        "list_avatars",
        "List avatars and voice templates",
        "List available avatar/voice templates. Each item's `type` decides the "
        "interview modality: `interactive_elevenlabs` = voice-only (no video "
        "avatar); `interactive_spatius` = realtime interactive 3D avatar "
        "(rendered in the candidate's browser, 1.25 credits, no early stop); "
        "`interactive_heygen` = premium realtime interactive video avatar; "
        "`offline_heygen` = pre-recorded, non-interactive avatar. An item's `id` is "
        "the `interview_template_id` you pass to the create-interview tools, so pick "
        "the template whose type matches the experience you want. Note: "
        "`offline_elai` and `offline_synthesia` are legacy integrations that may "
        "still appear here but cannot be used to create new interviews. Rows are "
        "large, so this returns 15 at a time; page with `offset` while "
        "`pagination.has_more` is true, or narrow with `type`/`filter_text`. The "
        "company's own templates only unless include_public=true: no voice template here "
        "does not mean there is none.",
        _READ_WHY,
        # Measured: ~2,700 chars of API JSON per row, ~4,900 on the wire once MCP
        # duplicates it into content + structuredContent. The spec's default of 50
        # is ~247,000 chars — well past MAX_TOOL_RESULT_CHARS — so list_avatars
        # failed on every default call. 15 lands near 74,000 with room for rows
        # whose media URLs run long. Left at 15 when the budget rose to 150,000:
        # a page that comfortably fits beats one that only fits after the guard
        # drops the duplicate copy. See PAGE_SIZE_WHY.
        param_defaults=(("limit", 15),),
    ),
    ("GET", "/knowledge-base-store-list"): _read(
        "list_knowledge_base_stores",
        "List knowledge base stores",
        "The company's knowledge base stores with their document counts: the "
        "knowledge_base_store_id create_interview draws on and upload_knowledge_base_document "
        "adds to. strict=true means the AI uses only these documents.",
        _READ_WHY,
    ),
    ("POST", "/knowledge-base-store-save"): _write(
        "save_knowledge_base_store",
        "Create or change a knowledge base store",
        "Without knowledge_base_store_id: a new store (name; strict true by default: the AI "
        "uses only its documents). With it: rename it or change strict, which changes how "
        "every interview on the store answers, so confirm with the user first. Add "
        "documents with upload_knowledge_base_document.",
        "readOnlyHint=false / destructiveHint=true: creates or changes a store that "
        "interviews draw on. idempotentHint=false: each create makes another store. "
        + _OPEN_WORLD,
    ),
    ("GET", "/merchant-user-list"): _read(
        "list_users",
        "List the company's users",
        "The company's users (owners, recruiters, hiring managers, admins) with the "
        "profile_id other tools take: a task assignee, a mention, an interviewer, a "
        "recruiter, a hiring team member. filter_text searches name and e-mail; role reader "
        "is a hiring manager (read only).",
        _READ_WHY,
    ),
    ("GET", "/merchant-sub-merchant-list"): _read(
        "list_sub_merchants",
        "List sub-merchants",
        "List the sub-merchants (child companies) under a merchant account, with their "
        "details. To pick the merchant to act for, use jobmojito_configuration or "
        "list_my_merchants instead.",
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
    # --- Portal configuration. Flow: get_portal_configuration -> edit only what
    # was asked -> update_portal_configuration with dry_run -> again without.
    ("GET", "/portal-configuration-get"): _read(
        "get_portal_configuration",
        "Get portal configuration",
        "Read the company's portal configuration as editable documents: `branding` "
        "(portal name, style, colours, logos, e-mail sender name), `interview-portal` "
        "(languages, home texts), `coaching-portal` (vertical, feature toggles, labels, "
        "languages, home blocks), `brands` (sub-brands, when switched on) and one "
        "`jobmojito.portal-site` document per portal (every page's draft content and the "
        "theme CSS, plus `style_settings` and `css_files` when switched on). Narrow with "
        "`sections` (branding, interview-portal, coaching-portal, interview-pages, "
        "coaching-pages, brands): the page documents are the large ones. With page documents the answer also "
        "has `block_types`: every block a page can hold, what it is for, when to use it and its fields; "
        "choose blocks from that list. This is the read step of every portal "
        "change: read the section(s) involved, change only what the user asked for, and "
        "send those documents back with update_portal_configuration. " + _PORTAL_COOKBOOKS,
        _READ_WHY,
    ),
    ("POST", "/portal-configuration-update"): _write(
        "update_portal_configuration",
        "Update portal configuration",
        "Import portal configuration documents, the same ones get_portal_configuration "
        "returns: branding, interview portal, coaching portal, brands, and portal sites "
        "(pages, menu, footer, theme CSS, style settings, CSS files). Read first, edit only the fields the user asked about, "
        "then send the documents with `dry_run: true` and show the user the returned "
        "summary, warnings and changes; repeat without `dry_run` to apply. Fields a "
        "document omits keep their value, and languages are only ever added. Validation "
        "is all-or-nothing: a 422 lists every error and nothing is written. Pages land as "
        "drafts the company reviews in the admin; set `publish: true` only when the user "
        "asked to publish the pages or theme, because it puts them live at once, and it "
        "also builds and publishes the stylesheet of any brand whose settings or CSS files "
        "the documents carry. Brands, style settings and CSS files are switched on per "
        "company by JobMojito; while off, those parts are refused with 422. Send a CSS "
        "file's `version` back, so a file someone else changed is refused. "
        + _PORTAL_COOKBOOKS,
        "readOnlyHint=false / destructiveHint=true: overwrites the company's live "
        "branding and portal settings in place and, with `publish`, puts pages and theme "
        "CSS live for candidates and learners; the tool cannot undo it (only "
        "`dry_run: true` writes nothing). idempotentHint=false: the documents are merged "
        "into whatever the configuration holds when the call runs, so the outcome depends "
        "on that state and the call is not declared safe to repeat. " + _OPEN_WORLD,
    ),
    # --- Career page editor: built stylesheets and the media library (SQL 302, 304, 313).
    # upload_media_file (media_upload.py) is the one-call upload for a client that
    # cannot send a file to an address itself.
    ("POST", "/portal-css-publish"): _write(
        "publish_portal_stylesheet",
        "Publish a brand stylesheet",
        "Build one brand's stylesheet from its style settings and CSS files and put it live "
        "at once. Leave out brand_id for the main brand. Write the drafts first with "
        "update_portal_configuration. Only when the user asked to publish. Returns warnings: "
        "tell the user about them, especially rules outside .jm-site.",
        "readOnlyHint=false / destructiveHint=true: changes the live look of the company's "
        "portal for every visitor. An earlier build stays in storage, but this tool cannot "
        "return to it. idempotentHint=false: each call records a new build. " + _OPEN_WORLD,
    ),
    # Component styles: named looks per block type, which the page editor offers on
    # every block of that type. The agent writes only components/<block type>/<style>.css,
    # one file per style.
    ("GET", "/portal-component-styles"): _read(
        "get_component_style_guide",
        "Get the component style guide",
        "Read before designing a style for a kind of page block (hero, cards, faq, ...): the "
        "selectors each block renders, its layout variants, the brand's CSS variables, and "
        "the styles the editor already offers per block type (written_by agent or company), "
        "each with the pages that use it (draft or published). Leave out brand_id for the "
        "main brand.",
        _READ_WHY,
    ),
    ("POST", "/portal-component-styles"): _write(
        "save_component_style",
        "Save a component style",
        "Write one named style for a block type as a DRAFT in its own file, "
        "components/<block_type>/<style>.css; the company's other CSS files are never changed. "
        "The page editor then offers it on every block of that type. Start every selector "
        "from the guide's root plus the style class. To apply it set styleName on blocks with "
        "update_portal_configuration; it goes live only with publish_portal_stylesheet "
        "(files: [file_id]), when the user asks. remove: true deletes a style's file: first "
        "tell the user which pages use it (the guide lists them); their blocks go back to the "
        "standard look once the stylesheet is published.",
        "readOnlyHint=false / destructiveHint=true: replaces or deletes a style's draft file, "
        "which the editor offers at once. Nothing goes live until the stylesheet is published. "
        "idempotentHint=true: the same call again leaves the file as it is. " + _OPEN_WORLD,
        idempotent=True,
    ),
    # Proposals only: nothing is stored, so read-only; each call asks the model again,
    # so not idempotent.
    ("POST", "/portal-component-styles-generate"): replace(
        _read(
            "generate_component_styles",
            "Generate component style alternatives",
            "Ask JobMojito's model for a few clearly different looks for one block type, from "
            "the block's content (pass it as block) and the brand's CSS variables. Returns "
            "name, description and CSS ready for save_component_style. Nothing is stored: show "
            "them and save only the one the user picks. You may also design styles yourself "
            "from get_component_style_guide.",
            "readOnlyHint=true: returns proposals and stores nothing. idempotentHint=false: "
            "each call asks the model again and the looks differ. " + _OPEN_WORLD,
        ),
        idempotent=False,
    ),
    ("GET", "/media-list"): _read(
        "list_media_files",
        "List media library files",
        "List the company's media library: images, MP4 video, audio and WOFF2 fonts, newest "
        "first. Use a file's `url` in page blocks, brand logos and font settings. Logos and "
        "images the company stored before are included. Filter by `folder` (\"\" is the top). "
        "Pass include_folders=true to also get every folder (empty ones included) with how "
        "many files each holds; use a name as `folder` here, in start_media_upload and in "
        "manage_media_file. A sub-company also sees its parent's files, marked read_only: "
        "usable, not changeable.",
        _READ_WHY,
    ),
    # Kept for the record only: GET /media-folders is hidden (IGNORED_OPERATIONS);
    # list_media_files include_folders=true returns the same folders.
    ("GET", "/media-folders"): _read(
        "list_media_folders",
        "List media library folders",
        "List the folders of the company's media library, empty ones included, with how many "
        "files each holds, A to Z.",
        _READ_WHY,
    ),
    ("POST", "/media-folders"): _write(
        "manage_media_folder",
        "Create, rename or delete a media folder",
        "Create an empty folder, rename one, or delete an empty one, when the user asked. "
        "Renaming moves every file in it to a new address: live pages keep the old one and "
        "page drafts move at once. A folder that still holds files cannot be deleted: move "
        "or delete the files first. Names: 1 to 60 letters, digits, spaces, \"-\", \"_\", "
        "\".\", \"(\" and \")\"; not starting with \".\", \"_\" or a space, and never \"profile\".",
        "readOnlyHint=false / destructiveHint=true: creates or removes a folder of the "
        "company's storage, and a rename gives every file in it a new address and rewrites "
        "page drafts; the tool cannot undo a rename except by renaming back. "
        "idempotentHint=false: a second rename answers that the folder does not exist. " + _OPEN_WORLD,
    ),
    ("POST", "/media-upload-start"): _write(
        "start_media_upload",
        "Start a media upload",
        "First step of an upload for a client that can send a file itself: returns a signed "
        "upload_url. PUT the file there with the declared Content-Type, then call "
        "finish_media_upload. If you cannot send a file to an address, use upload_media_file.",
        "readOnlyHint=false / destructiveHint=true: creates a row in the company's media "
        "library and an address anyone holding it can upload to for two hours. "
        "idempotentHint=false: each call starts another upload. " + _OPEN_WORLD,
    ),
    ("POST", "/media-upload-finish"): _write(
        "finish_media_upload",
        "Finish a media upload",
        "Last step of start_media_upload: checks the stored file and makes it usable. A file "
        "that is not what was declared, a video that is not H.264, or an SVG with anything "
        "active is removed and the error says why. Check `replaced` when replacing a file.",
        "readOnlyHint=false / destructiveHint=true: makes a file usable or removes a refused "
        "one, and a replacement moves page drafts to the new address. idempotentHint=true: "
        "repeating the call on a finished file returns it unchanged. " + _OPEN_WORLD,
        idempotent=True,
    ),
    # One tool for a file: POST /media-update takes action update (default) or delete;
    # /media-delete stays in the API, hidden here (IGNORED_PATHS).
    ("POST", "/media-update"): _write(
        "manage_media_file",
        "Describe, move or delete a media file",
        "With action update (the default): change a file's name, alt text, focal point or "
        "poster, or move it to another folder (\"\" is the top). Fields left out keep their "
        "value. alt replaces the whole set of languages. A null focal_x, focal_y or poster_url "
        "clears it. A move gives the file a new `url`: live pages keep the old one and page "
        "drafts move at once (drafts_updated). With action delete and only media_id: delete "
        "the file for good, once the user has confirmed it. Refused while a page, brand, CSS "
        "file, style setting, company logo or branding, interview, position, blog post, avatar "
        "or coaching catalogue uses it: the error lists what does, so tell the user rather "
        "than removing those uses yourself.",
        "readOnlyHint=false / destructiveHint=true: overwrites the file's description in "
        "place, a folder change gives the file a new address and rewrites page drafts, and "
        "delete removes the file and its resized copies for good. idempotentHint=false: a "
        "second delete answers that the file does not exist. " + _OPEN_WORLD,
        null_body_args=("focal_x", "focal_y", "poster_url"),
    ),
    ("POST", "/media-delete"): _write(
        "delete_media_file",
        "Delete a media file",
        "Delete a file from the media library for good, once the user has confirmed it. "
        "Refused while a page, brand, CSS file, style setting, company logo or branding, "
        "interview, position, blog post, avatar or coaching catalogue uses it: the error lists "
        "what does, so tell the user rather than removing those uses yourself.",
        "readOnlyHint=false / destructiveHint=true: removes the file and its resized copies "
        "and cannot be undone. idempotentHint=false: a second call answers that the file "
        "does not exist. " + _OPEN_WORLD,
    ),
    # --- Custom reports (SQL 133). Flow: list_report_fields -> write a
    # definition -> run_report (definition) -> save_report.
    ("GET", "/report-catalog"): _read(
        "list_report_fields",
        "List report datasets and fields",
        "List what a custom report can use on this account: datasets (interview "
        "results, invitations, position pipeline, ATS pipeline, credits, coaching "
        "sessions - only those the account and your role include), their dimensions "
        "(group by / conditions, with allowed values) and measures. Call this first: "
        "a report definition may only use ids listed here. Custom reports are "
        "switched on per account by JobMojito; while they are off, every report "
        "tool is refused with 403 'Custom reports are not enabled for this account.' "
        "Tell the user to contact JobMojito rather than retrying.",
        _READ_WHY,
    ),
    ("GET", "/report-list"): _read(
        "list_reports",
        "List reports",
        "List the reports you can run: custom reports you can open (yours, shared with "
        "you), the JobMojito templates, and the built-in reports (kind builtin, id "
        "builtin:<name>, with their sections and whether they are available to you). "
        "scope=builtin lists only the built-in ones; they are listed even while custom "
        "reports are switched off.",
        _READ_WHY,
    ),
    ("GET", "/report-get"): _read(
        "get_report",
        "Get custom report",
        "Get a custom report's JSON definition, visibility, your access and its "
        "version. Read it before save_report with report_id: the definition is replaced whole "
        "and the version must match.",
        _READ_WHY,
    ),
    # Kept for the record only: GET /report-run is hidden (IGNORED_PATHS); run_report
    # (POST /report-preview) runs saved, built-in and unsaved reports.
    ("GET", "/report-run"): _read(
        "run_saved_report",
        "Run custom report",
        "Run a saved custom report and return each block's rows.",
        _READ_WHY,
    ),
    ("POST", "/report-preview"): _read(
        "run_report",
        "Run a report",
        "Run a report and return each block's rows (aggregated numbers, at most 500 rows "
        "per block). Three ways: `report_id` of a saved custom report (your saved filters, "
        "then `filters`, apply; with `version` a newer report is refused with 409); "
        "`report_id` builtin:<name> of a built-in report from list_reports (`section`, "
        "`from` / `to`, `position_id` or `interview_id`, `limit` / `offset`; sections that "
        "list candidates by name run only with include_candidates=true and are audited); or "
        "an unsaved `definition`, which is validated against list_report_fields and run "
        "without storing anything: check a definition this way before save_report, a wrong "
        "block comes back with ok=false and an error naming the field and the valid ids. "
        "Report numbers are decision-support material for a human reviewer. Block types: "
        "line needs a date dimension first (with a grain), kpi and funnel take no "
        "dimensions. The period "
        "filters each dataset on its own date (its description in list_report_fields says "
        "which; a snapshot dataset ignores it); left out, the last 30 days.",
        "readOnlyHint=true: although the request is a POST (a definition is a JSON body), "
        "nothing is stored or changed; it only reads and aggregates existing records. "
        "idempotentHint=true: the same body returns the same data. " + _OPEN_WORLD,
    ),
    # One save tool: POST /report-create with report_id + version updates exactly as
    # /report-update does; /report-update stays in the API, hidden here (IGNORED_PATHS).
    ("POST", "/report-create"): _write(
        "save_report",
        "Save a custom report",
        "Without report_id: save a new custom report from a definition (schema "
        "jobmojito.report/v1); it is created as Only me, tell the user they can share it "
        "from the returned admin_url. With report_id and the version from get_report: "
        "replace that report's whole definition; on a report shared with other people the "
        "change is saved as a draft that the owner publishes in the admin, a private report "
        "updates at once. Check the definition with run_report (definition, no report_id) "
        "first.",
        "readOnlyHint=false / destructiveHint=true: stores a new report or overwrites a "
        "stored definition (or its draft). idempotentHint=false: each create makes another "
        "report and every update bumps the version. " + _OPEN_WORLD,
    ),
    ("POST", "/report-update"): _write(
        "update_report",
        "Update custom report",
        "Replace a custom report's definition (send the whole definition and the "
        "version from get_report). On a report shared with other people the change "
        "is saved as a draft that the owner publishes in the admin; a private "
        "report updates at once.",
        "readOnlyHint=false / destructiveHint=true: overwrites the stored "
        "definition (or its draft). idempotentHint=false: every save bumps the "
        "version, so repeating the call with the old version is refused. "
        + _OPEN_WORLD,
    ),
    # --- Positions (SQL 316): process, step settings, meeting slots, copies ---
    ("POST", "/merchant-position-process-save"): _write(
        "set_position_process",
        "Set a position's steps",
        "Add, remove or reorder the steps of a DRAFT position (pre-screening, AI interview, "
        "meeting, external assessment, Certo verification), sending the whole ordered list. "
        "New steps get the admin's defaults; change a step's settings with "
        "update_position_step. A step candidates already reached cannot be removed, nor a "
        "meeting step that still has slots. Read the current steps with "
        "get_interview_definition include=process. dry_run=true returns the diff (added, "
        "removed, moved, changed) without saving. No tool creates a position from "
        "scratch: copy one as a draft with duplicate_position, or create it in the admin.",
        "readOnlyHint=false / destructiveHint=true: replaces the position's step list. "
        "idempotentHint=true: the same list gives the same steps. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/merchant-position-step-update"): _write(
        "update_position_step",
        "Change a position step's settings",
        "Change one step's settings: send `step_id`, optionally `name`, `advance` and "
        "`kombo`, and only the settings object of the step's type (`meeting`, `certo`, "
        "`external_assessment`, `interview`, `pre_screening`); only the keys you send change "
        "and null removes a key, but a list you send (pre_screening form_fields, "
        "assessment_rules) replaces the whole list: send every item you want to keep. "
        "pre_screening id and status are read only. A new form field: {\"id\": "
        "\"custom_<timestamp>\", \"title\", \"field_type\": text|file|radio, \"type\": "
        "required|optional, \"options\": [\"Yes\", \"No\"] for radio}; append it to the "
        "fields get_interview_definition include=process returns. pre_screening all_rules_fulfilled_auto_approve "
        "approves a candidate who meets every rule without review; whether they then move "
        "on by themselves is `advance` (auto), a separate setting. Read the step first with get_interview_definition "
        "include=process and send its `updated_at` back as expected_updated_at: a newer "
        "change is refused with 409. Settings are live: candidates already on the step get "
        "them at once. Use dry_run=true first: it shows the changed keys, the candidates on "
        "the step, booked meetings and warnings, and saves nothing. Nobody is e-mailed. "
        "A new meeting duration applies to slots created afterwards; existing slots keep "
        "theirs (change them with manage_meeting_slot).",
        "readOnlyHint=false / destructiveHint=true: changes live settings that candidates "
        "already on the step get immediately. idempotentHint=true: the same values give the "
        "same settings. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/merchant-meeting-slot-manage"): _write(
        "manage_meeting_slot",
        "Create, change or delete a meeting slot",
        "Bookable time slots of a meeting step: action create, update or delete (with "
        "start_at, duration_minutes, capacity, location, meeting_url, interviewer and panel). "
        "Changing start_at, duration_minutes, location or meeting_url of a booked slot "
        "e-mails its booked candidates the new details; deleting one cancels their "
        "bookings and e-mails them and the interviewers. Use dry_run=true first: "
        "it lists the booked candidates who would be e-mailed. List slots with "
        "get_interview_definition include=slots. Creating a slot e-mails nobody. start_at "
        "is ISO 8601 with a UTC offset "
        "(2026-11-20T14:00:00+01:00) and not in the past: ask the user's time zone when "
        "it is not clear.",
        "readOnlyHint=false / destructiveHint=true: changes or removes bookable time and can "
        "cancel candidates' bookings, e-mailing them; a cancellation cannot be undone. "
        + _OPEN_WORLD,
    ),
    ("POST", "/merchant-position-duplicate"): _write(
        "duplicate_position",
        "Copy a position",
        "Copy a position with its steps and settings as a new draft, optionally with a new "
        "name, country, location, openings or availability date, or into another company you "
        "manage (target_merchant_id; admins and sub-merchant operators only). Candidates and "
        "results are not copied. Left out, interview_available_till and openings_total keep the "
        "original's; null makes the copy open (interview_available_till) or without a number of "
        "openings (openings_total).",
        "readOnlyHint=false / destructiveHint=true: creates a new position. "
        "idempotentHint=false: each call makes another copy. " + _OPEN_WORLD,
        # The original's closing date is often past: null is the only way to an open copy, and
        # a stray null only touches the new draft, which update_interview corrects.
        null_body_args=("interview_available_till", "openings_total"),
    ),
    ("POST", "/merchant-interview-duplicate"): _write(
        "duplicate_interview",
        "Copy an interview",
        "Copy a stand-alone interview (not a position's step) as a new draft, optionally "
        "renamed or into another company you manage. Results are not copied.",
        "readOnlyHint=false / destructiveHint=true: creates a new interview. "
        "idempotentHint=false: each call makes another copy. " + _OPEN_WORLD,
    ),
    # --- Candidates (SQL 317, 319): card, notes, tags, search values, duplicates, segments ---
    ("GET", "/merchant-candidate-get"): _read(
        "get_candidate",
        "Get a candidate",
        "One candidate in full: profile, applications with their steps and decisions, tags "
        "and automatic tags, counts of notes, open tasks, document requests and segments, and "
        "the duplicate flag. Add include=notes,duplicates,documents,tasks,segments for those "
        "lists; a section whose feature is off is named in `unavailable`. Candidate data is "
        "decision-support material for a human reviewer.",
        _READ_WHY,
    ),
    ("POST", "/merchant-candidate-note-add"): _write(
        "add_candidate_note",
        "Add a note to a candidate",
        "Add a note to a candidate (candidate_id) or to one result (result_id), visible to "
        "the team or private to you, optionally mentioning colleagues (each mention gets one "
        "e-mail). Notes cannot be changed or deleted through this tool.",
        "readOnlyHint=false / destructiveHint=true: stores a note the team sees and can "
        "e-mail mentioned colleagues. idempotentHint=false: each call adds another note. "
        + _OPEN_WORLD,
    ),
    ("POST", "/merchant-candidate-tags-set"): _write(
        "set_candidate_tags",
        "Tag candidates",
        "Add and remove the company's tags on up to 5000 candidates in one call "
        "(add_tag_ids, remove_tag_ids; ids from list_candidate_filters). Candidates you "
        "cannot see are skipped and counted in not_visible.",
        "readOnlyHint=false / destructiveHint=true: changes the tags of many candidates at "
        "once. idempotentHint=true: the same call gives the same tags. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("POST", "/merchant-candidate-tag-manage"): _write(
        "manage_candidate_tag",
        "Create, change or delete a candidate tag",
        "The company's candidate tag list: action create, update (rename, recolor) or "
        "delete. Rename, recolor and delete are for owners; delete removes the tag from "
        "every candidate for good (dry_run=true shows how many carry it).",
        "readOnlyHint=false / destructiveHint=true: delete removes the tag from every "
        "candidate and cannot be undone. " + _OPEN_WORLD,
    ),
    ("GET", "/merchant-candidate-filter-options"): _read(
        "list_candidate_filters",
        "List candidate search filters",
        "Every rule field and operator list_candidates accepts in `rules`, and every value "
        "with its id: tags with counts, automatic tags, positions with their steps, "
        "interviews, countries, sources, statuses, history, and where switched on decision "
        "reasons, job categories, skills, seniority and custom fields. Call it before "
        "building rules or tagging, so no id is guessed.",
        _READ_WHY,
    ),
    ("GET", "/merchant-candidate-merge-preview"): _read(
        "preview_candidate_merge",
        "Preview merging two candidates",
        "Show what merging merge_id into keep_id would do: both values of each field, the "
        "records that move, blockers, and `preview_token`. merge_candidates needs that "
        "token, and refuses it if either candidate changed since.",
        _READ_WHY,
    ),
    ("POST", "/merchant-candidate-merge"): _write(
        "merge_candidates",
        "Merge two candidates",
        "Merge merge_id into keep_id after preview_candidate_merge (send its preview_token; "
        "field_choices picks keep or merge per field). Owners and admins only. "
        "IRREVERSIBLE: there is no unmerge. The candidate is not told. Only run it when the "
        "user confirmed this exact pair.",
        "readOnlyHint=false / destructiveHint=true: permanently merges two candidate records "
        "and cannot be undone. " + _OPEN_WORLD,
    ),
    ("POST", "/merchant-candidate-duplicate-dismiss"): _write(
        "dismiss_candidate_duplicate",
        "Dismiss a possible duplicate",
        "Mark a suggested duplicate pair (duplicate_id from get_candidate "
        "include=duplicates) as not the same person, with an optional note.",
        "readOnlyHint=false / destructiveHint=true: records a decision the duplicate list "
        "follows from then on. idempotentHint=true. " + _OPEN_WORLD,
        idempotent=True,
    ),
    ("GET", "/merchant-segment-list"): _read(
        "list_segments",
        "List candidate segments",
        "List the company's candidate segments (dynamic, by rules, or static lists) with "
        "their size, or with segment_id one segment in full: rules, analytics and linked "
        "positions. List a segment's members with list_candidates segment_id.",
        _READ_WHY,
    ),
    ("POST", "/merchant-segment-manage"): _write(
        "manage_segment",
        "Create, change or delete a candidate segment",
        "action save (create without id, update with id; name, description, kind, and "
        "rules: the same {match, rules} list_candidates takes, as an object or a JSON "
        "string), delete, or add_members / remove_members of a static segment "
        "(candidate ids). Fields left out keep their value. Try the rules with "
        "list_candidates rules=... first. Sends nothing to candidates.",
        "readOnlyHint=false / destructiveHint=true: delete removes the segment for good. "
        + _OPEN_WORLD,
    ),
    # --- Tasks, document requests (SQL 318) ---
    ("GET", "/merchant-task-list"): _read(
        "list_tasks",
        "List tasks",
        "The hiring team's follow-up tasks: mine, created by me or all; by candidate, "
        "position, assignee, status or due date; with counts.",
        _READ_WHY,
    ),
    ("POST", "/merchant-task-save"): _write(
        "save_task",
        "Create or change a task",
        "Without task_id: create a task (title, notes, due date, assignee, about a candidate, "
        "application or position: a new task needs one of candidate_id, application_id or "
        "position_id). With task_id: change the fields you send, or the status (done, "
        "cancelled; a cancelled task stays cancelled). Assigning a task to someone else "
        "e-mails them (assignee_notified); assignee_profile_id comes from list_users. due_at "
        "is a date and time with offset: ask the user's time zone. Hiring managers are read only.",
        "readOnlyHint=false / destructiveHint=true: stores a task and can e-mail its "
        "assignee. " + _OPEN_WORLD,
    ),
    ("GET", "/merchant-document-request-list"): _read(
        "list_document_requests",
        "List document requests",
        "Documents asked from candidates: company-wide or for one candidate, by status, or "
        "attention=true for the ones that need a look (uploaded, overdue).",
        _READ_WHY,
    ),
    ("POST", "/merchant-document-request-create"): _write(
        "request_candidate_documents",
        "Ask a candidate for documents",
        "Ask a candidate for up to 10 documents (certificate, ID, reference...) with a "
        "message and due date. With notify (the default) the candidate is e-mailed. Use "
        "dry_run=true first: it shows whether the e-mail would go out (will_send, "
        "not_requested, no_valid_email, suppressed) and stores nothing. Leave "
        "documents[].source_field_id out for a plain upload: it only replaces one of the "
        "position's own file questions (form_national_id...).",
        "readOnlyHint=false / destructiveHint=true: e-mails the candidate, which cannot be "
        "undone. idempotentHint=false. " + _OPEN_WORLD,
    ),
    ("POST", "/merchant-document-request-update"): _write(
        "update_document_request",
        "Cancel a document request or mark it reviewed",
        "action cancel (the candidate can no longer upload) or reviewed (the uploads were "
        "checked). A request in the wrong state is refused with 409.",
        "readOnlyHint=false / destructiveHint=true: cancel cannot be undone. " + _OPEN_WORLD,
    ),
    # --- Reasons: decision and position status reasons (merchant_taxonomy) ---
    ("GET", "/merchant-reason-list"): _read(
        "list_reasons",
        "List the company's reasons",
        "The company's decision reasons (why a candidate was rejected or withdrew, kind "
        "reject / withdraw) and position status reasons (the reason_id set_interview_state "
        "needs for on_hold and cancelled, with the lifecycles each applies_to). type narrows "
        "to one; include_archived=true adds the archived ones.",
        _READ_WHY,
    ),
    ("GET", "/merchant-reason-get"): _read(
        "get_reason",
        "Get one of the company's reasons",
        "One decision or position status reason by id, archived ones included.",
        _READ_WHY,
    ),
    ("POST", "/merchant-reason-save"): _write(
        "save_reason",
        "Create, change or archive a reason",
        "Without reason_id: a new reason (decision: kind and label; position_status: label "
        "and applies_to). With reason_id: change it; fields left out keep their value; "
        "status archived stops offering it, active offers it again (an archived position "
        "status reason cannot be restored). Company-wide: a renamed reason is renamed on "
        "every past decision too, so confirm with the user first. Owners only.",
        "readOnlyHint=false / destructiveHint=true: it changes the company's reason list, "
        "which every recruiter and past decision uses. idempotentHint=false: each create "
        "makes another reason. " + _OPEN_WORLD,
    ),
    # --- Feedback ---
    ("POST", "/merchant-feature-request"): ToolMeta(
        name="submit_feature_request",
        title="Submit feature request",
        hint=(
            "Send a feature request to the JobMojito product team. It is posted on the "
            "public feature request board under the signed-in user's name, where they "
            "get status updates; the company is not named. Write a short title and a "
            "description of the need and why (no candidate data or company names). "
            "Confirm the wording with the user before submitting; at most 5 a day."
        ),
        justification=(
            "readOnlyHint=false / destructiveHint=true: publishes a post on the "
            "public feature request board under the user's name, a visible external "
            "side effect this tool cannot undo (it never changes or deletes existing "
            "data). idempotentHint=false: each call submits another request. "
            + _OPEN_WORLD
        ),
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    ),
    ("POST", "/merchant-technical-issue"): ToolMeta(
        name="report_technical_issue",
        title="Report technical issue",
        hint=(
            "Report a technical problem to JobMojito support. It opens a support "
            "conversation from the signed-in user's account (their e-mail), with their "
            "company and the interview or result ids given; support answers there. "
            "Describe what was done, what was expected and what happened (error text "
            "as shown). Confirm the wording with the user first; at most 5 a day."
        ),
        justification=(
            "readOnlyHint=false / destructiveHint=true: opens a conversation with the "
            "support team in the user's name, an external side effect this tool cannot "
            "undo (it never changes or deletes existing data). idempotentHint=false: "
            "each call opens another conversation. " + _OPEN_WORLD
        ),
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    ),
}


# Endpoints to exclude from the MCP entirely (never exposed as tools).
# Extend at deploy time with the IGNORED_TOOL_PATHS env var (comma-separated).
IGNORED_PATHS: set[str] = {
    "/invite-users",  # also creates staff accounts: candidates go through invitations.invite_candidates
    "/job-interview-create-for-candidate-with-token",  # candidate-token one-shot flow
    "/pre-screening-create",  # pre-screening management — excluded
    "/job-interview-pre-screening-api-resume-text",  # pre-screening — excluded
    "/job-interview-pre-screening-api-resume-binary",  # pre-screening — excluded
    "/merchant-document-download",  # 302 to a signed file URL for HR systems (webhooks v2) — not a tool
    # merged into another tool through an optional parameter; the endpoints stay in the API
    "/job-interview-create-from-array",  # create_interview with questions
    "/job-interview-register-users",  # invite_candidates with send_email=false
    "/catalogue-tag-update",  # save_catalogue_directory with action update
    "/media-delete",  # manage_media_file with action delete
    "/report-run",  # run_report (POST /report-preview with report_id)
    "/report-update",  # save_report with report_id
    "/merchant-analytics",  # run_report builtin:usage
    "/merchant-analytics-credits-used",  # run_report builtin:usage
}

# Single operations to exclude when the path serves other tools too.
IGNORED_OPERATIONS: set[tuple[str, str]] = {
    ("GET", "/media-folders"),  # list_media_files include_folders=true; POST stays manage_media_folder
}


#: What a model should call instead of an endpoint that has no tool of its own. The
#: spec's prose names endpoints ("in the format job-interview-get returns"); a model
#: only knows tool names, so ``openapi_loader.apply_tool_names`` rewrites them.
MERGED_ENDPOINTS: dict[str, str] = {
    "/invite-users": "invite_candidates",
    "/job-interview-create-from-array": "create_interview",
    "/job-interview-register-users": "invite_candidates with send_email=false",
    "/catalogue-tag-update": "save_catalogue_directory",
    "/media-delete": "manage_media_file with action delete",
    "/report-run": "run_report",
    "/report-update": "save_report with report_id",
    "/merchant-analytics": "run_report builtin:usage",
    "/merchant-analytics-credits-used": "run_report builtin:usage",
}


#: Table-name ids in the spec's prose ("Candidate id (profile_interview.id)") as where a
#: model gets that id. Rewritten by ``openapi_loader.apply_tool_names``; ids of other
#: tables (a user's profile.id, a taxonomy row) keep their table name.
TABLE_IDS: dict[str, tuple[str, str]] = {
    # table: (what it is, where a model gets it); the source is left out where the text
    # already names one (", from ...")
    "profile_interview": ("the `id` of a list_candidates row", ""),
    "position_def_set": ("a position `id`", "from list_interviews"),
    "interview_def_set": ("an interview `id`", "from list_interviews"),
    "position_def_step": ("a step `id`", "from get_interview_definition include=process"),
    "position_result": ("an application `id`", "from get_candidate"),
    "position_result_step": ("an application step `id`", "from get_candidate (or a multi-stage list_interview_results row)"),
    "interview_result": ("a result id", "from list_interview_results"),
    "profile_interview_tag": ("a tag `id`", "from list_candidate_filters"),
    "profile_interview_segment": ("a segment `id`", "from list_segments"),
    "profile_interview_document_request": ("a request `id`", "from list_document_requests"),
    "profile_interview_duplicate": ("a duplicate pair `id`", "from get_candidate include=duplicates"),
    "merchant_task": ("a task `id`", "from list_tasks"),
    "position_meeting_slot": ("a slot `id`", "from get_interview_definition include=slots"),
    "merchant": ("a company `id`", "from list_my_merchants"),
    # no article: the spec writes "not a profile.id", "(profile.id of a user of the company)"
    "profile": ("`profile_id` (list_users)", ""),
}


#: Query arguments the API reads as a JSON string that a model naturally sends as an
#: object (the same rules manage_segment takes as one). The schema accepts both
#: (``openapi_loader.accept_json_objects``) and ``ArgumentShapeMiddleware`` sends the string.
JSON_STRING_ARGS: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", "/merchant-candidate-list"): ("rules",),
    # the API reads a string too (merchant-segment-manage parses it)
    ("POST", "/merchant-segment-manage"): ("rules",),
}


def json_string_tool_args() -> dict[str, set[str]]:
    """Tool name -> its JSON-string arguments (JSON_STRING_ARGS by tool)."""
    return {TOOL_META[key].name: set(names) for key, names in JSON_STRING_ARGS.items() if key in TOOL_META}


def endpoint_tool_names() -> dict[str, str]:
    """Endpoint name (path without the slash) -> the tool name a model should read.

    A path served by two tools (GET and POST) maps to both names, joined with " / ".
    """
    names: dict[str, list[str]] = {}
    for (method, path), meta in TOOL_META.items():
        if not is_ignored(method, path):
            names.setdefault(path.lstrip("/"), []).append(meta.name)
    out = {slug: " / ".join(sorted(set(tools))) for slug, tools in names.items()}
    for path, text in MERGED_ENDPOINTS.items():
        out.setdefault(path.lstrip("/"), text)
    return out


def is_ignored(method: str, path: str) -> bool:
    """True when the operation is not exposed as a tool (by path or by method + path)."""
    return path in IGNORED_PATHS or (method.upper(), path) in IGNORED_OPERATIONS


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


def read_only_tool_names() -> frozenset[str]:
    """Names of the curated tools that only read.

    ``middleware.ResultSizeGuardMiddleware`` consults this before re-running a
    call with a smaller page size: repeating a GET is free, repeating anything
    that writes is not. Deliberately built from the curated table only —
    endpoints added to the API but not yet named here fall through to
    ``fallback_meta`` and are absent from this set, so a new write endpoint can
    never be retried by default.
    """
    return frozenset(meta.name for meta in TOOL_META.values() if meta.read_only)


@dataclass(frozen=True)
class ToolViewRules:
    """What ``middleware.ResponseViewMiddleware`` needs for one tool."""

    parameter: str
    default: str
    #: view name -> field paths to remove from the response
    drop: dict[str, tuple[str, ...]]
    #: view name -> (field path, function) pairs that shrink a value in place
    condense: dict[str, tuple[tuple[str, Callable], ...]] = field(default_factory=dict)

    def paths_for(self, view: str | None) -> tuple[str, ...]:
        """Fields to drop for ``view``, falling back to the default projection.

        An unknown view is treated as the default rather than an error: the value
        only ever narrows the response, so guessing wrong must not fail a call
        that would otherwise have succeeded.
        """
        if view not in self.drop:
            view = self.default
        return self.drop.get(view, ())

    def condensers_for(self, view: str | None) -> tuple[tuple[str, Callable], ...]:
        """Fields to condense for ``view``, with the same default fallback."""
        if view not in self.drop:
            view = self.default
        return self.condense.get(view, ())

    def narrower_than(self, view: str | None) -> str | None:
        """The narrowest view that drops strictly more than ``view`` does.

        ``middleware.ResultSizeGuardMiddleware`` uses this to name the argument
        that would actually help when a *single record* overflows the result
        limit. Pagination advice is useless there — there is one row and no
        `limit` to shrink — and telling the model to "request only the fields you
        need" when the tool has a `view` argument built for exactly that is how
        the advice failed in production (`get_interview_result_details`,
        10 Sep 2026: 164,779 chars, answered with "try `limit=10`").

        "Narrower" is measured as the number of dropped field paths, which is
        how ``ResponseView`` projections are defined — each one lists what it
        removes, so more paths is strictly less data. Returns None when the
        caller is already on the narrowest view, so the advice never suggests a
        no-op.
        """
        current = len(self.paths_for(view))
        best: str | None = None
        for name, paths in self.drop.items():
            if len(paths) <= current:
                continue
            if best is None or len(paths) > len(self.drop[best]):
                best = name
        return best


def response_view_rules() -> dict[str, ToolViewRules]:
    """``{tool_name: ToolViewRules}`` for every endpoint that offers a ``view``.

    Consumed by ``middleware.ResponseViewMiddleware``, which both strips the
    MCP-only argument before the upstream call and prunes the response after it.
    ``openapi_loader.inject_view_params`` reads the same ``ToolMeta.views`` to
    build the schema the model sees, so the two cannot describe different views.
    """
    return {
        meta.name: ToolViewRules(
            parameter=VIEW_PARAM_NAME,
            default=meta.effective_default_view,
            drop={view.name: view.drop for view in meta.views},
            condense={view.name: view.condense for view in meta.views if view.condense},
        )
        for meta in TOOL_META.values()
        if meta.views
    }


def view_parameter_schema(meta: ToolMeta) -> dict[str, object] | None:
    """The OpenAPI query parameter describing ``meta``'s response views.

    Returned as a query parameter because that is the only parameter location
    FastMCP turns into a flat tool argument for a GET. It is removed from the
    arguments before the request is built, so nothing is ever sent upstream.
    """
    if not meta.views:
        return None
    lines = "\n".join(f"- `{view.name}`: {view.summary}" for view in meta.views)
    default = meta.effective_default_view
    return {
        "name": VIEW_PARAM_NAME,
        "in": "query",
        "required": False,
        VIEW_PARAM_MARKER: True,
        "schema": {
            "type": "string",
            "enum": [view.name for view in meta.views],
            "default": default,
            "description": (
                "How much of the record to return. Handled by the MCP server, not "
                "the JobMojito API — it only narrows the response, never the query."
                f"\n\n{lines}"
            ),
        },
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
    for (method, path), meta in sorted(TOOL_META.items(), key=lambda kv: kv[1].name):
        if is_ignored(method, path) and not include_ignored:
            continue
        out[meta.name] = meta.justification
    # tools written by hand, outside the OpenAPI path
    from invitations import INVITE_CANDIDATES_JUSTIFICATION
    from media_upload import UPLOAD_MEDIA_FILE_JUSTIFICATION

    out["invite_candidates"] = INVITE_CANDIDATES_JUSTIFICATION
    out["upload_media_file"] = UPLOAD_MEDIA_FILE_JUSTIFICATION
    return out
