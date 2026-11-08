"""list_knowledge_base_documents and set_knowledge_base_document_interviews.

Run with:  ENABLE_AUTH=false pytest -q tests/test_knowledge_base_document_limit.py

The three meanings of the list have to survive the trip: a list, an empty list (none)
and null (every interview). FastMCP drops a None argument, and the API requires the
field so that leaving it out can never open a document, so without KeepNullBodyArgs a
document could not be opened again through the MCP at all.
"""

from __future__ import annotations

import json
import os
import pathlib

import httpx2
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

SET_TOOL = "set_knowledge_base_document_interviews"
LIST_TOOL = "list_knowledge_base_documents"
DOCUMENT = "44444444-4444-4444-8444-444444444444"
STORE = "33333333-3333-4333-8333-333333333333"
INTERVIEW = "de300000-0000-4000-8001-000000000007"
OPEN = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": False}


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


def _fake_upstream(monkeypatch, answer: dict, status: int = 200) -> list[httpx2.Request]:
    seen: list[httpx2.Request] = []

    async def fake_send(self, request, **kwargs):
        seen.append(request)
        return httpx2.Response(status, json=answer, request=request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", fake_send)
    return seen


async def _call(tool: str, arguments: dict):
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments)


# ---- the tools' contract ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_ids_is_required_and_may_be_a_list_or_null():
    schema = (await _tools())[SET_TOOL].parameters
    assert set(schema["required"]) == {"knowledge_base_id", "interview_ids"}
    assert schema["properties"]["interview_ids"]["type"] == ["array", "null"]
    description = schema["properties"]["interview_ids"]["description"]
    assert "null opens it to every interview" in description


@pytest.mark.asyncio
async def test_the_hint_says_what_each_value_does():
    description = (await _tools())[SET_TOOL].description
    assert "[] to none" in description.split("\n")[0]
    assert "null opens it to every interview" in description.split("\n")[0]


@pytest.mark.asyncio
async def test_annotations():
    tools = await _tools()
    write, read = tools[SET_TOOL].annotations, tools[LIST_TOOL].annotations
    assert (write.read_only_hint, write.destructive_hint, write.idempotent_hint) == (False, True, True)
    assert (read.read_only_hint, read.destructive_hint) == (True, False)


@pytest.mark.asyncio
async def test_the_list_tool_offers_its_filters():
    properties = (await _tools())[LIST_TOOL].parameters["properties"]
    assert {"knowledge_base_id", "knowledge_base_store_id", "merchant_id", "limit", "offset"} <= set(properties)


@pytest.mark.asyncio
async def test_output_reports_the_limit():
    properties = (await _tools())[SET_TOOL].output_schema["properties"]
    assert {"knowledge_base_id", "limit_to_interviews", "interview_ids"} <= set(properties)


def test_the_fallback_spec_declares_both_endpoints_under_their_curated_names():
    spec = json.loads((pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text())
    write = spec["paths"]["/knowledge-base-document-set-interviews"]["post"]
    read = spec["paths"]["/knowledge-base-document-list"]["get"]
    assert write["operationId"] == SET_TOOL
    assert read["operationId"] == LIST_TOOL
    assert {"200", "401", "403", "404", "422", "500"} <= set(write["responses"]) & set(read["responses"])
    schema = spec["components"]["schemas"]["KnowledgeBaseDocumentSetInterviewsRequest"]
    assert schema["required"] == ["knowledge_base_id", "interview_ids"]
    assert schema["properties"]["interview_ids"]["nullable"] is True
    assert "KnowledgeBaseDocumentListResponse" in spec["components"]["schemas"]


# ---- what reaches the API -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_list_is_sent_as_a_json_list_and_the_limit_comes_back(monkeypatch):
    answer = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": True, "interview_ids": [INTERVIEW]}
    seen = _fake_upstream(monkeypatch, answer)
    result = await _call(SET_TOOL, {"knowledge_base_id": DOCUMENT, "interview_ids": [INTERVIEW]})
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/knowledge-base-document-set-interviews")
    assert seen[0].headers["content-type"].startswith("application/json")
    assert json.loads(seen[0].content) == {"knowledge_base_id": DOCUMENT, "interview_ids": [INTERVIEW]}
    assert result.structured_content == answer


@pytest.mark.asyncio
async def test_an_empty_list_stays_an_empty_list(monkeypatch):
    """[] limits the document to no interview; losing it would be a refused call, not none."""
    answer = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": True, "interview_ids": []}
    seen = _fake_upstream(monkeypatch, answer)
    result = await _call(SET_TOOL, {"knowledge_base_id": DOCUMENT, "interview_ids": []})
    assert json.loads(seen[0].content)["interview_ids"] == []
    assert result.structured_content["interview_ids"] == []


@pytest.mark.asyncio
async def test_null_is_sent_as_null(monkeypatch):
    seen = _fake_upstream(monkeypatch, OPEN)
    result = await _call(SET_TOOL, {"knowledge_base_id": DOCUMENT, "interview_ids": None})
    assert seen[0].headers["content-type"].startswith("application/json")
    assert json.loads(seen[0].content) == {"knowledge_base_id": DOCUMENT, "interview_ids": None}
    assert int(seen[0].headers["content-length"]) == len(seen[0].content)
    assert result.structured_content == OPEN


@pytest.mark.asyncio
async def test_leaving_the_list_out_never_becomes_null(monkeypatch):
    """FastMCP or the API refuses it: either way no request says 'every interview'."""
    from fastmcp.exceptions import ToolError

    seen = _fake_upstream(monkeypatch, {"error": "Field interview_ids is required", "name": "interview_ids"}, status=422)
    with pytest.raises(ToolError, match="interview_ids"):
        await _call(SET_TOOL, {"knowledge_base_id": DOCUMENT})
    assert all("interview_ids" not in json.loads(request.content) for request in seen)


@pytest.mark.asyncio
async def test_a_null_on_other_tools_is_still_left_out(monkeypatch):
    """The opt-in stays an opt-in: a null a model passes elsewhere must not clear a value."""
    seen = _fake_upstream(monkeypatch, {"knowledge_base_id": DOCUMENT, "limit_to_interviews": False})
    await _call(
        "upload_knowledge_base_document",
        {"knowledge_base_store_id": STORE, "file": "aGVsbG8=", "interview_ids": None},
    )
    assert "interview_ids" not in json.loads(seen[0].content)


@pytest.mark.parametrize(
    ("status", "answer", "match"),
    [
        (422, {"error": "Every interview in interview_ids must belong to this company", "name": "interview_ids"}, "interview_ids"),
        (404, {"error": f"Unable to find document {DOCUMENT}: not found or not permitted.", "name": "knowledge_base_id"}, "not permitted"),
        (403, {"error": "Only a company owner, recruiter or admin can change a document"}, "company owner"),
    ],
)
@pytest.mark.asyncio
async def test_a_refusal_reaches_the_caller_as_an_error(monkeypatch, status, answer, match):
    from fastmcp.exceptions import ToolError

    _fake_upstream(monkeypatch, answer, status=status)
    with pytest.raises(ToolError, match=match):
        await _call(SET_TOOL, {"knowledge_base_id": DOCUMENT, "interview_ids": [INTERVIEW]})


@pytest.mark.asyncio
async def test_reading_one_document_sends_its_id_and_returns_its_limit(monkeypatch):
    answer = {
        "data": [{
            "knowledge_base_id": DOCUMENT,
            "knowledge_base_store_id": STORE,
            "merchant_id": None,
            "name": "guide.pdf",
            "type": "file",
            "status": "active",
            "processing_status": "completed",
            "created_at": "2026-11-06T10:00:00Z",
            "limit_to_interviews": True,
            "interview_ids": [INTERVIEW],
        }],
        "pagination": {"total": 1, "limit": 50, "offset": 0, "has_more": False},
    }
    seen = _fake_upstream(monkeypatch, answer)
    result = await _call(LIST_TOOL, {"knowledge_base_id": DOCUMENT})
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/knowledge-base-document-list")
    assert seen[0].url.params["knowledge_base_id"] == DOCUMENT
    assert result.structured_content == answer


@pytest.mark.asyncio
async def test_listing_a_store_and_an_open_document_validate(monkeypatch):
    """An open document has no interview_ids key; the output schema must accept that."""
    answer = {
        "data": [{
            "knowledge_base_id": DOCUMENT,
            "knowledge_base_store_id": STORE,
            "merchant_id": "m",
            "name": None,
            "type": "markdown",
            "status": "active",
            "processing_status": None,
            "created_at": None,
            "limit_to_interviews": False,
        }],
        "pagination": {"total": 1, "limit": 50, "offset": 0, "has_more": False},
    }
    seen = _fake_upstream(monkeypatch, answer)
    result = await _call(LIST_TOOL, {"knowledge_base_store_id": STORE})
    assert seen[0].url.params["knowledge_base_store_id"] == STORE
    assert result.structured_content["data"][0]["limit_to_interviews"] is False


# ---- KeepNullBodyArgs ---------------------------------------------------------------------


class _Director:
    def __init__(self, request: httpx2.Request) -> None:
        self.request = request
        self.calls: list[tuple] = []

    def build(self, route, flat_args, base_url):
        self.calls.append((route, flat_args, base_url))
        return self.request


def _wrapped(request: httpx2.Request):
    from null_body_args import KeepNullBodyArgs

    return KeepNullBodyArgs(_Director(request), ("ids", "other"))


def test_without_a_null_the_request_is_untouched():
    request = httpx2.Request("POST", "http://api.test/x", json={"ids": ["a"]})
    wrapped = _wrapped(request)
    assert wrapped.build("route", {"ids": ["a"]}) is request
    assert wrapped._director.calls == [("route", {"ids": ["a"]}, "http://localhost")]


def test_a_named_argument_that_was_not_passed_is_not_invented():
    request = httpx2.Request("POST", "http://api.test/x", json={"name": "n"})
    assert _wrapped(request).build("route", {"name": "n"}) is request


def test_a_null_for_an_argument_that_is_not_named_stays_out():
    request = httpx2.Request("POST", "http://api.test/x", json={"name": "n"})
    assert _wrapped(request).build("route", {"name": "n", "skip": None}) is request


def test_a_null_joins_the_body_and_the_rest_survives():
    request = httpx2.Request("POST", "http://api.test/x?a=1", json={"name": "n"}, headers={"x-keep": "1"})
    out = _wrapped(request).build("route", {"name": "n", "ids": None}, "http://b.test")
    assert json.loads(out.content) == {"name": "n", "ids": None}
    assert (out.method, str(out.url), out.headers["x-keep"]) == ("POST", "http://api.test/x?a=1", "1")
    assert out.headers["content-type"] == "application/json"
    assert int(out.headers["content-length"]) == len(out.content)


def test_a_null_when_nothing_else_was_sent_is_still_a_body():
    request = httpx2.Request("POST", "http://api.test/x")
    out = _wrapped(request).build("route", {"ids": None, "other": None})
    assert json.loads(out.content) == {"ids": None, "other": None}
