"""get_interview_result_details and the earlier sessions of a restarted interview.

Run with:  ENABLE_AUTH=false pytest -q tests/test_interview_result_recording_history.py

recording_local_path is the latest recording. The API also answers with the earlier ones
(SQL 299 and 300), and output validation is on, so these pin that the tool's schema takes
every shape it can answer with and that a real call hands the values back.
"""

from __future__ import annotations

import os

import httpx2
import jsonschema
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

TOOL = "get_interview_result_details"
RESULT = "99e3cbd5-0000-4000-8000-000000000001"


async def _output_schema() -> dict:
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    return tools[TOOL].output_schema


@pytest.mark.asyncio
async def test_the_tool_documents_the_earlier_sessions():
    properties = (await _output_schema())["properties"]
    assert "earlier sessions" in properties["recording_local_path_history"]["description"]
    assert "earlier sessions" in properties["recording_url_history"]["description"]
    assert "latest session" in properties["recording_local_path"]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"recording_local_path_history": None},
        {"recording_local_path_history": []},
        {"recording_local_path_history": ["m/c/r/composite-1.mp4", "m/c/r/composite-2.mp4"]},
        {"recording_url_history": ["https://signed/1", None]},
        {"recording_local_path_history": ["m/c/r/composite-1.mp4"], "recording_url_history": ["https://signed/1"]},
    ],
)
async def test_output_validation_takes_every_shape_the_api_answers_with(answer):
    schema = await _output_schema()
    jsonschema.validate({"id": RESULT, **answer}, schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["summary", "standard", "full"])
async def test_a_call_hands_back_the_earlier_sessions_in_every_view(monkeypatch, view):
    from fastmcp import Client

    import server

    answer = {
        "id": RESULT,
        "recording_local_path": "m/c/r/composite-2.mp4",
        "recording_local_path_history": ["m/c/r/composite-1.mp4"],
        "recording_url": "https://signed/2",
        "recording_url_history": ["https://signed/1"],
        "transcript": [],
    }

    async def _send(self, request, **_kwargs):
        return httpx2.Response(200, json=answer, request=request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", _send)
    async with Client(server.mcp) as client:
        result = await client.call_tool(TOOL, {"interview_result_id": RESULT, "get_signed_recordings": True, "view": view})
    assert result.structured_content["recording_local_path"] == "m/c/r/composite-2.mp4"
    assert result.structured_content["recording_local_path_history"] == ["m/c/r/composite-1.mp4"]
    assert result.structured_content["recording_url_history"] == ["https://signed/1"]
