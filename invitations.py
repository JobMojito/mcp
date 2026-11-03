"""Candidate invitations — the same path as the admin console's Add candidates.

``invite_candidates`` calls ``POST /invite-users`` with every row typed
``candidate``, which is what the admin dialog sends: the API creates or finds the
candidate, applies the company's invitation budget and queues the invitation
e-mail (``merchant_send_interview_invitation``), with the recruiter's message.

``/invite-users`` itself stays out of the generated tools (``naming.IGNORED_PATHS``)
because its ``type`` field also creates staff accounts and grants access to other
companies. This tool never sends a ``type`` the caller chose: a prompt-injected
agent can invite candidates, never a team member.

``register_users_for_interview`` remains the tool for personal links only; it
sends no e-mail.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from upstream import build_api_client

logger = logging.getLogger("jobmojito_mcp.invitations")

#: One request at most; the admin dialog sends lists of this size too.
MAX_CANDIDATES = 50

INVITE_CANDIDATES_ANNOTATIONS = {
    "title": "Invite candidates to an interview or position",
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

INVITE_CANDIDATES_JUSTIFICATION = (
    "readOnlyHint=false / destructiveHint=true: e-mails an interview invitation to "
    "real people and uses the company's invitation budget, which cannot be undone. "
    "openWorldHint=true: the e-mail leaves the platform."
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
    ) -> dict:
        """[Interviews] Invite candidates to an interview or position: e-mails each one their invitation.

        The same invitation the admin console's Add candidates sends: creates or
        finds the candidate, uses the company's invitation budget, and queues the
        invitation e-mail (with the optional recruiter message). Each row comes
        back with its outcome; only 'Invited' means an e-mail was queued.

        To get a personal interview link WITHOUT sending an e-mail (to share it
        yourself, or for a test), use register_users_for_interview instead.
        """
        body = build_invite_body(
            interview_id,
            candidates,
            message_from_recruiter=message_from_recruiter,
            language=language,
            merchant_id=merchant_id,
        )
        async with build_api_client() as client:
            try:
                resp = await client.post("/invite-users", json=body)
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not reach the JobMojito API: {exc}") from exc
        if resp.status_code >= 400:
            # the shape UpstreamErrorMiddleware turns into a cause + next step
            raise ToolError(f"HTTP error {resp.status_code}: {resp.reason_phrase} - {resp.text[:600]}")
        return summarize_results(resp.json())

    logger.info("Candidate invitations registered (invite_candidates).")
