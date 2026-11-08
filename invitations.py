"""Candidate invitations — the same path as the admin console's Add candidates.

``invite_candidates`` calls ``POST /invite-users`` with every row typed
``candidate``, which is what the admin dialog sends: the API creates or finds the
candidate, applies the company's invitation budget and queues the invitation
e-mail (``merchant_send_interview_invitation``), with the recruiter's message.

``/invite-users`` itself stays out of the generated tools (``naming.IGNORED_PATHS``)
because its ``type`` field also creates staff accounts and grants access to other
companies. This tool never sends a ``type`` the caller chose: a prompt-injected
agent can invite candidates, never a team member.

With ``send_email=false`` the same tool registers the candidates through
``POST /job-interview-register-users`` instead and returns each one's personal
interview link; nothing is e-mailed. That endpoint is hidden from the generated
tools (it was ``register_users_for_interview``), so there is one tool for both, and
the result always says whether an e-mail went out.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx2
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from upstream import build_api_client

logger = logging.getLogger("jobmojito_mcp.invitations")

#: One request at most; the admin dialog sends lists of this size too.
MAX_CANDIDATES = 50

INVITE_CANDIDATES_ANNOTATIONS = {
    "title": "Invite candidates to an interview or position",  # e-mail, or links with send_email=false
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

INVITE_CANDIDATES_JUSTIFICATION = (
    "readOnlyHint=false / destructiveHint=true: e-mails an interview invitation to "
    "real people and uses the company's invitation budget, which cannot be undone; "
    "with send_email=false it creates or updates candidate records and mints personal "
    "interview links. openWorldHint=true: the e-mail leaves the platform."
)


class CandidateToInvite(BaseModel):
    email: Annotated[str, Field(description="The candidate's e-mail address; the invitation goes here.")]
    name: Annotated[str | None, Field(description="Display name, e.g. 'Jane Doe'.")] = None
    phone: Annotated[
        str | None,
        Field(description="Optional mobile number in international format (+421903123456) for SMS / WhatsApp notifications."),
    ] = None


def build_invite_body(
    interview_id: str,
    candidates: list[CandidateToInvite],
    *,
    message_from_recruiter: str | None = None,
    language: str | None = None,
    merchant_id: str | None = None,
) -> dict[str, Any]:
    """The /invite-users request the admin's Add candidates sends: candidate rows only."""
    users = []
    for c in candidates:
        row: dict[str, Any] = {"email": c.email.strip(), "type": "candidate", "interview_id": interview_id}
        if c.name and c.name.strip():
            row["name"] = c.name.strip()
        if c.phone and c.phone.strip():
            row["phone"] = c.phone.strip()
        users.append(row)
    body: dict[str, Any] = {
        # like the admin dialog: a candidate still waiting on an earlier invitation gets it again
        "force_invite": True,
        "users": users,
    }
    if message_from_recruiter and message_from_recruiter.strip():
        body["message_from_recruiter"] = message_from_recruiter.strip()
    if language:
        body["mojito_language_code"] = language
    if merchant_id:
        body["merchant_id"] = merchant_id
    return body


def build_register_body(
    interview_id: str,
    candidates: list[CandidateToInvite],
    *,
    merchant_id: str | None = None,
    is_test: bool = False,
) -> dict[str, Any]:
    """The /job-interview-register-users request: personal links, no e-mail."""
    users = []
    for c in candidates:
        row: dict[str, Any] = {"email": c.email.strip()}
        if c.name and c.name.strip():
            row["name"] = c.name.strip()
        if c.phone and c.phone.strip():
            row["phone"] = c.phone.strip()
        users.append(row)
    body: dict[str, Any] = {"interview_id": interview_id, "users": users}
    if is_test:
        body["is_test"] = True
    if merchant_id:
        body["merchant_id"] = merchant_id
    return body


def summarize_links(rows: Any) -> dict[str, Any]:
    """Per-candidate link plus counts, saying plainly that nothing was e-mailed."""
    results = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        results.append(
            {
                "email": r.get("email"),
                "result": r.get("result") or "Error: no result",
                "profile_interview_id": r.get("profile_interview_id"),
                "interview_url": r.get("interview_url"),
            }
        )
    registered = [r for r in results if r["result"] == "ok"]
    return {
        "email_sent": False,
        "registered": len(registered),
        "failed": len(results) - len(registered),
        "results": results,
        "note": (
            "No e-mail was sent. Give each interview_url to its candidate yourself; "
            "do not tell the user an invitation went out."
        ),
    }


def summarize_results(rows: Any) -> dict[str, Any]:
    """Per-candidate outcome plus counts, so the agent reports what really happened."""
    results = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        results.append(
            {
                "email": r.get("email"),
                "result": r.get("result") or "Error: no result",
                "profile_interview_id": r.get("profile_interview_id"),
            }
        )
    invited = [r for r in results if r["result"] == "Invited"]
    failed = [r for r in results if str(r["result"]).startswith("Error")]
    return {
        "email_sent": True,
        "invited": len(invited),
        "failed": len(failed),
        "other": len(results) - len(invited) - len(failed),
        "results": results,
        "note": (
            "'Invited' means the invitation e-mail was queued. Report any other result "
            "to the user as it is; do not say an e-mail was sent for those rows."
        ),
    }


def register(mcp) -> None:
    """Register invite_candidates on the given FastMCP server."""

    @mcp.tool(annotations=INVITE_CANDIDATES_ANNOTATIONS, tags={"jobmojito", "jobmojito-api", "Interviews"})
    async def invite_candidates(
        interview_id: Annotated[
            str,
            Field(description="The interview (interview_def_set) or position (position_def_set) id, from list_interviews."),
        ],
        candidates: Annotated[
            list[CandidateToInvite],
            Field(min_length=1, max_length=MAX_CANDIDATES, description="The people to invite."),
        ],
        message_from_recruiter: Annotated[
            str | None,
            Field(description="Optional personal note shown above the invitation in the e-mail."),
        ] = None,
        language: Annotated[
            str | None,
            Field(description="Language code for new candidates (e.g. 'en', 'sk'); defaults to English."),
        ] = None,
        merchant_id: Annotated[
            str | None,
            Field(description="Only for an account acting for a sub-merchant; omit for your own company."),
        ] = None,
        send_email: Annotated[
            bool,
            Field(
                description=(
                    "true (default): e-mail each candidate their invitation. false: send NOTHING; "
                    "register them and return each one's personal interview_url to share yourself."
                )
            ),
        ] = True,
        is_test: Annotated[
            bool,
            Field(description="Only with send_email=false: test links that may take a draft interview; results are marked as tests."),
        ] = False,
    ) -> dict:
        """[Interviews] Invite candidates to an interview or position: e-mails each one their invitation.

        The same invitation the admin console's Add candidates sends: creates or
        finds the candidate, uses the company's invitation budget, and queues the
        invitation e-mail (with the optional recruiter message). Each row comes
        back with its outcome; only 'Invited' means an e-mail was queued.

        With send_email=false NOTHING is e-mailed: each candidate is registered and
        their personal interview_url comes back for you to share (also for a test
        with is_test=true). The result's email_sent says which happened.
        """
        if send_email:
            path = "/invite-users"
            body = build_invite_body(
                interview_id,
                candidates,
                message_from_recruiter=message_from_recruiter,
                language=language,
                merchant_id=merchant_id,
            )
        else:
            path = "/job-interview-register-users"
            body = build_register_body(interview_id, candidates, merchant_id=merchant_id, is_test=is_test)
        async with build_api_client() as client:
            try:
                resp = await client.post(path, json=body)
            except httpx2.HTTPError as exc:
                raise ToolError(f"Could not reach the JobMojito API: {exc}") from exc
        if resp.status_code >= 400:
            # the shape UpstreamErrorMiddleware turns into a cause + next step
            raise ToolError(f"HTTP error {resp.status_code}: {resp.reason_phrase} - {resp.text[:600]}")
        return summarize_results(resp.json()) if send_email else summarize_links(resp.json())

    logger.info("Candidate invitations registered (invite_candidates).")
