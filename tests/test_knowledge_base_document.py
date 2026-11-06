"""upload_knowledge_base_document and the interview limit on a document.

Run with:  ENABLE_AUTH=false pytest -q tests/test_knowledge_base_document.py

The endpoint accepts multipart and JSON. FastMCP sends the first content type the
spec declares, which was multipart: the base64 file arrived as a text field, which the
API cannot read, and a list would have arrived as Python's str(list). These pin the tool
to the JSON body, and that the three meanings of the limit survive the trip: left out
(every interview), a list, and an empty list (none).
"""

from __future__ import annotations

import copy
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

STORE = "33333333-3333-4333-8333-333333333333"
INTERVIEW = "de300000-0000-4000-8001-000000000007"
DOCUMENT = "44444444-4444-4444-8444-444444444444"
BASE = {"knowledge_base_store_id": STORE, "name": "guide.pdf", "file": "aGVsbG8="}


async def _tool():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}["upload_knowledge_base_document"]


def _fake_upstream(monkeypatch, answer: dict, status: int = 200) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def fake_send(self, request, **kwargs):
        seen.append(request)
        return httpx.Response(status, json=answer, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return seen


# ---- the tool's contract ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_ids_is_an_optional_list():
    schema = (await _tool()).parameters
    assert "interview_ids" in schema["properties"]
    assert "array" in json.dumps(schema["properties"]["interview_ids"])
    assert "interview_ids" not in schema["required"]
    assert "every interview" in schema["properties"]["interview_ids"]["description"]


@pytest.mark.asyncio
async def test_file_is_the_base64_string_of_the_json_body():
    file_schema = (await _tool()).parameters["properties"]["file"]
    assert "base64" in file_schema["description"]
    assert file_schema.get("format") != "binary"


@pytest.mark.asyncio
async def test_description_says_how_to_limit_a_document():
    description = (await _tool()).description
    assert "interview_ids" in description
    assert "multipart" not in description.split("\n")[0]


@pytest.mark.asyncio
async def test_output_reports_the_limit():
    properties = (await _tool()).output_schema["properties"]
    assert {"knowledge_base_id", "limit_to_interviews", "interview_ids"} <= set(properties)


# ---- what reaches the API -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_list_is_sent_as_a_json_list_and_the_limit_comes_back(monkeypatch):
    from fastmcp import Client

    import server

    answer = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": True, "interview_ids": [INTERVIEW]}
    seen = _fake_upstream(monkeypatch, answer)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "upload_knowledge_base_document", {**BASE, "interview_ids": [INTERVIEW]}
        )
    assert seen[0].headers["content-type"].startswith("application/json")
    assert json.loads(seen[0].content) == {**BASE, "interview_ids": [INTERVIEW]}
    assert result.structured_content == answer


@pytest.mark.asyncio
async def test_an_empty_list_stays_an_empty_list(monkeypatch):
    """[] limits the document to nobody; losing it on the way would open it to everyone."""
    from fastmcp import Client

    import server

    answer = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": True, "interview_ids": []}
    seen = _fake_upstream(monkeypatch, answer)
    async with Client(server.mcp) as client:
        result = await client.call_tool("upload_knowledge_base_document", {**BASE, "interview_ids": []})
    assert json.loads(seen[0].content)["interview_ids"] == []
    assert result.structured_content["interview_ids"] == []


@pytest.mark.asyncio
async def test_left_out_sends_nothing_and_an_open_document_validates(monkeypatch):
    from fastmcp import Client

    import server

    answer = {"knowledge_base_id": DOCUMENT, "limit_to_interviews": False}
    seen = _fake_upstream(monkeypatch, answer)
    async with Client(server.mcp) as client:
        result = await client.call_tool("upload_knowledge_base_document", BASE)
    assert "interview_ids" not in json.loads(seen[0].content)
    assert result.structured_content["limit_to_interviews"] is False


@pytest.mark.asyncio
async def test_a_refused_list_reaches_the_caller_as_an_error(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    _fake_upstream(
        monkeypatch,
        {"error": "Every interview in interview_ids must belong to this company and use this knowledge base.", "name": "interview_ids"},
        status=422,
    )
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="interview_ids"):
            await client.call_tool("upload_knowledge_base_document", {**BASE, "interview_ids": [INTERVIEW]})


# ---- prefer_json_request_bodies -----------------------------------------------------------


def _operation(content):
    return {"paths": {"/x": {"post": {"requestBody": {"content": content}}}}}


def _order(spec) -> list[str]:
    return list(spec["paths"]["/x"]["post"]["requestBody"]["content"])


def test_json_moves_first_and_nothing_is_lost():
    from openapi_loader import prefer_json_request_bodies

    multipart, plain_json = {"schema": {"title": "m"}}, {"schema": {"title": "j"}}
    spec = prefer_json_request_bodies(
        _operation({"multipart/form-data": multipart, "application/json": plain_json})
    )
    content = spec["paths"]["/x"]["post"]["requestBody"]["content"]
    assert list(content) == ["application/json", "multipart/form-data"]
    assert content["application/json"] is plain_json
    assert content["multipart/form-data"] is multipart


@pytest.mark.parametrize(
    "content",
    [
        {"application/json": {}, "multipart/form-data": {}},  # already first
        {"application/json": {}},
        {"multipart/form-data": {}},  # nothing to prefer: binary-only endpoints stay as they are
        {},
    ],
)
def test_other_bodies_are_left_alone(content):
    from openapi_loader import prefer_json_request_bodies

    before = list(content)
    assert _order(prefer_json_request_bodies(_operation(copy.copy(content)))) == before


def test_operations_without_a_body_and_non_operations_are_skipped():
    from openapi_loader import prefer_json_request_bodies

    spec = {
        "paths": {
            "/get": {"get": {"responses": {}}, "parameters": [{"name": "a"}], "summary": "text"},
            "/null-body": {"post": {"requestBody": None}},
            "/odd": "not a path item",
        }
    }
    assert prefer_json_request_bodies(copy.deepcopy(spec)) == spec
    assert prefer_json_request_bodies({}) == {}


def test_the_loader_applies_it():
    from openapi_loader import _prepare

    spec = _prepare(
        {"paths": {"/knowledge-base-document-upload": {"post": {"requestBody": {"content": {
            "multipart/form-data": {"schema": {"type": "object"}},
            "application/json": {"schema": {"type": "object"}},
        }}}}}}
    )
    body = spec["paths"]["/knowledge-base-document-upload"]["post"]["requestBody"]
    assert next(iter(body["content"])) == "application/json"


def test_the_fallback_spec_declares_the_limit():
    import pathlib

    spec = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text()
    )
    schemas = spec["components"]["schemas"]
    assert "interview_ids" in schemas["KnowledgeBaseDocumentUploadJson"]["properties"]
    assert "interview_ids" in schemas["KnowledgeBaseDocumentUploadMultipart"]["properties"]
    assert schemas["KnowledgeBaseDocumentUploadResponse"]["required"] == ["knowledge_base_id", "limit_to_interviews"]


def test_the_hint_explains_all_three_meanings():
    from naming import TOOL_META

    hint = TOOL_META[("POST", "/knowledge-base-document-upload")].hint
    assert "interview_ids" in hint and "every interview" in hint and "[] for none" in hint
