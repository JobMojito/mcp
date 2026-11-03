"""invite_candidates and the link-only register_users_for_interview.

Run with:  ENABLE_AUTH=false pytest -q tests/test_invite_candidates.py

An agent asked to "invite" a candidate used register_users_for_interview with
send_email=true. That endpoint never sent anything, and the agent told the user an
e-mail had gone out. These pin the fix: invite_candidates goes through
/invite-users with candidate rows only (the endpoint also creates staff accounts,
so the tool must never pass a caller-chosen `type`), and the register tool no
longer advertises an e-mail.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

INTERVIEW = "de300000-0000-0000-0001-000000000007"


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True)


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


@pytest.mark.asyncio
async def test_invite_candidates_is_listed_as_a_destructive_write():
    tool = (await _tools())["invite_candidates"]
    assert _annotations(tool) == {
        "title": "Invite candidates to an interview or position",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert "register_users_for_interview" in tool.description  # points to the link-only tool


@pytest.mark.asyncio
async def test_invite_candidates_takes_no_account_type():
    """The staff-account half of /invite-users must not be reachable from the tool."""
    schema = (await _tools())["invite_candidates"].parameters
    assert set(schema["required"]) == {"interview_id", "candidates"}
    assert "type" not in schema["properties"]
    item = schema["properties"]["candidates"]["items"]
    item = schema.get("$defs", {}).get(item["$ref"].split("/")[-1], item) if "$ref" in item else item
    assert set(item["properties"]) == {"email", "name", "phone"}
    blob = json.dumps(schema)
    for word in ("merchant_owner", "merchant_selection", "mentor"):
        assert word not in blob


def test_body_has_candidate_rows_only():
    from invitations import CandidateToInvite, build_invite_body

    body = build_invite_body(
        INTERVIEW,
        [CandidateToInvite(email=" jozo@jozo.sk ", name="Jozo", phone="+421903123456"), CandidateToInvite(email="a@b.co", name="  ")],
        message_from_recruiter="  Hi!  ",
        language="sk",
        merchant_id="bee53937-4593-4de0-a3cd-cb24036d1245",
    )
    assert body == {
        "force_invite": True,
        "users": [
            {"email": "jozo@jozo.sk", "type": "candidate", "interview_id": INTERVIEW, "name": "Jozo", "phone": "+421903123456"},
            {"email": "a@b.co", "type": "candidate", "interview_id": INTERVIEW},
        ],
        "message_from_recruiter": "Hi!",
        "mojito_language_code": "sk",
        "merchant_id": "bee53937-4593-4de0-a3cd-cb24036d1245",
    }
    assert "message_from_recruiter" not in build_invite_body(INTERVIEW, [CandidateToInvite(email="a@b.co")], message_from_recruiter=" ")


def test_summary_counts_only_invited_as_sent():
    from invitations import summarize_results

    out = summarize_results([
        {"email": "a@b.co", "result": "Invited", "profile_interview_id": "p1"},
        {"email": "c@d.co", "result": "Error: no invitations left"},
        {"email": "e@f.co", "result": "Activated"},
        "junk",
    ])
    assert (out["invited"], out["failed"], out["other"]) == (1, 1, 1)
    assert out["results"][1] == {"email": "c@d.co", "result": "Error: no invitations left", "profile_interview_id": None}
    assert summarize_results(None)["results"] == []


@pytest.mark.asyncio
async def test_call_posts_candidate_rows_to_invite_users(monkeypatch):
    import invitations
    import server

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"email": "jozo@jozo.sk", "result": "Invited", "profile_interview_id": "p1"}])

    monkeypatch.setattr(invitations, "build_api_client", lambda: httpx.AsyncClient(base_url="https://api.test/functions/v1", transport=httpx.MockTransport(handler)))
    result = await server.mcp.call_tool("invite_candidates", {"interview_id": INTERVIEW, "candidates": [{"email": "jozo@jozo.sk"}]})
    assert seen["path"].endswith("/invite-users")
    assert seen["body"]["users"] == [{"email": "jozo@jozo.sk", "type": "candidate", "interview_id": INTERVIEW}]
    data = result.structured_content
    assert data["invited"] == 1 and data["results"][0]["result"] == "Invited"


@pytest.mark.asyncio
async def test_upstream_failure_is_a_tool_error_the_middleware_can_rewrite(monkeypatch):
    from fastmcp.exceptions import ToolError

    import invitations
    import server

    monkeypatch.setattr(invitations, "build_api_client", lambda: httpx.AsyncClient(
        base_url="https://api.test", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "Only merchant can create invitations"}))))
    with pytest.raises(ToolError) as exc:
        await server.mcp.call_tool("invite_candidates", {"interview_id": INTERVIEW, "candidates": [{"email": "a@b.co"}]})
    assert "403" in str(exc.value)


@pytest.mark.asyncio
async def test_register_users_for_interview_promises_no_email():
    tool = (await _tools())["register_users_for_interview"]
    assert "send_email" not in tool.parameters["properties"]
    assert "NO e-mail" in tool.description
    assert "invite_candidates" in tool.description


def test_instructions_route_invites_to_invite_candidates():
    import server

    assert '"Invite candidates to an interview" → invite_candidates' in server.INSTRUCTIONS
    assert "register_users_for_interview (personal interview\n  links only, NO e-mail)" in server.INSTRUCTIONS


def test_justification_is_submittable():
    import naming

    assert len(naming.annotation_justifications()["invite_candidates"]) >= 40
