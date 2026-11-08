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
    assert "send_email=false" in tool.description  # the link-only way is the same tool


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
    assert (out["email_sent"], out["invited"], out["failed"], out["other"]) == (True, 1, 1, 1)
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
async def test_register_users_for_interview_is_merged_into_invite_candidates():
    tools = await _tools()
    assert "register_users_for_interview" not in tools
    params = tools["invite_candidates"].parameters["properties"]
    assert params["send_email"]["default"] is True
    assert "NOTHING" in params["send_email"]["description"]


def test_register_body_and_link_summary():
    from invitations import CandidateToInvite, build_register_body, summarize_links

    body = build_register_body(
        INTERVIEW,
        [CandidateToInvite(email=" jozo@jozo.sk ", name="Jozo", phone="+421903123456"), CandidateToInvite(email="a@b.co", name=" ")],
        merchant_id="bee53937-4593-4de0-a3cd-cb24036d1245",
        is_test=True,
    )
    assert body == {
        "interview_id": INTERVIEW,
        "users": [{"email": "jozo@jozo.sk", "name": "Jozo", "phone": "+421903123456"}, {"email": "a@b.co"}],
        "is_test": True,
        "merchant_id": "bee53937-4593-4de0-a3cd-cb24036d1245",
    }
    assert "is_test" not in build_register_body(INTERVIEW, [CandidateToInvite(email="a@b.co")])
    out = summarize_links([
        {"email": "a@b.co", "result": "ok", "profile_interview_id": "p1", "interview_url": "https://x/i?t=1"},
        {"email": "c@d.co", "result": "Error: invalid e-mail"},
        {"email": "e@f.co"},
        "junk",
    ])
    assert (out["email_sent"], out["registered"], out["failed"]) == (False, 1, 2)
    assert out["results"][0]["interview_url"] == "https://x/i?t=1"
    assert out["results"][2]["result"] == "Error: no result"
    assert "No e-mail was sent" in out["note"]
    assert summarize_links(None)["results"] == []


@pytest.mark.asyncio
async def test_send_email_false_registers_and_returns_links(monkeypatch):
    import invitations
    import server

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"email": "jozo@jozo.sk", "result": "ok", "profile_interview_id": "p1", "interview_url": "https://x/i"}])

    monkeypatch.setattr(invitations, "build_api_client", lambda: httpx.AsyncClient(base_url="https://api.test/functions/v1", transport=httpx.MockTransport(handler)))
    result = await server.mcp.call_tool(
        "invite_candidates",
        {"interview_id": INTERVIEW, "candidates": [{"email": "jozo@jozo.sk"}], "send_email": False, "message_from_recruiter": "ignored"},
    )
    assert seen["path"].endswith("/job-interview-register-users")
    assert seen["body"] == {"interview_id": INTERVIEW, "users": [{"email": "jozo@jozo.sk"}]}
    data = result.structured_content
    assert data["email_sent"] is False and data["registered"] == 1 and data["results"][0]["interview_url"] == "https://x/i"


def test_instructions_route_invites_to_invite_candidates():
    import server

    flat = " ".join(server.INSTRUCTIONS.split())  # line wrapping is not the point
    assert '"Invite candidates to an interview" → invite_candidates' in flat
    assert "send_email=false returns personal interview links only, NO e-mail" in flat
    assert "register_users_for_interview" not in server.INSTRUCTIONS


def test_justification_is_submittable():
    import naming

    assert len(naming.annotation_justifications()["invite_candidates"]) >= 40


def test_blank_phones_and_rows_without_a_result():
    from invitations import CandidateToInvite, build_invite_body, build_register_body, summarize_results

    blank = [CandidateToInvite(email="a@b.co", phone="   ")]
    assert build_invite_body(INTERVIEW, blank)["users"] == [{"email": "a@b.co", "type": "candidate", "interview_id": INTERVIEW}]
    assert build_register_body(INTERVIEW, blank)["users"] == [{"email": "a@b.co"}]
    out = summarize_results([{"email": "a@b.co"}])
    assert out["results"][0]["result"] == "Error: no result"
    assert (out["invited"], out["failed"]) == (0, 1)


@pytest.mark.parametrize("send_email", [True, False])
@pytest.mark.asyncio
async def test_an_unreachable_api_is_a_tool_error_in_both_modes(monkeypatch, send_email):
    from fastmcp.exceptions import ToolError

    import invitations
    import server

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(invitations, "build_api_client", lambda: httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(refuse)))
    with pytest.raises(ToolError, match="Could not reach the JobMojito API"):
        await server.mcp.call_tool(
            "invite_candidates", {"interview_id": INTERVIEW, "candidates": [{"email": "a@b.co"}], "send_email": send_email}
        )
