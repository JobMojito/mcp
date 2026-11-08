"""The component style tools: the guide an agent designs from, and one named style as a draft.

Run with:  ENABLE_AUTH=false pytest -q tests/test_component_style_tools.py

Checked as the built server lists them, and on what reaches the API.
"""

from __future__ import annotations

import json
import os

import httpx2
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

BRAND = "44444444-4444-4444-8444-444444444444"
GUIDE = {
    "portal": "interview",
    "brand": None,
    "file_rule": "Write only components/<block type>/<style>.css",
    "how_to": ["Pick the block type"],
    "shared_parts": [{"selector": ".jm-button", "what": "a button"}],
    "tokens": [{"selector": "--jm-color-text", "what": "text colour"}],
    "tokens_css": {"main": "", "brand": None},
    "components": [
        {
            "type": "hero",
            "summary": "The page opener.",
            "root": ".jm-site .jm-block--hero",
            "parts": [{"selector": "h1", "what": "the title"}],
            "variants": [],
            "styles": [
                {"name": "Dark band", "class": "jm-style-dark-band", "file": "components/hero/dark-band.css", "line": 1, "brand_id": None, "written_by": "agent", "live": False, "pages": []},
            ],
        }
    ],
}
SAVED = {
    "action": "created", "block_type": "hero", "name": "Dark band", "class": "jm-style-dark-band", "style_name": "Dark band",
    "file": "components/hero/dark-band.css", "file_id": None, "live": False, "warnings": [],
}


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True, by_alias=True)


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


def _route(monkeypatch, status: int, body: object) -> list[httpx2.Request]:
    seen: list[httpx2.Request] = []

    async def fake_send(self, request, **kwargs):
        seen.append(request)
        if "/portal-component-styles" not in str(request.url):
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return httpx2.Response(status, json=body, request=request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", fake_send)
    return seen


@pytest.mark.asyncio
async def test_listed_read_and_write_apart():
    tools = await _tools()
    assert _annotations(tools["get_component_style_guide"]) == {
        "title": "Get the component style guide", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True,
    }
    assert _annotations(tools["generate_component_styles"]) == {
        "title": "Generate component style alternatives", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True,
    }
    assert _annotations(tools["save_component_style"]) == {
        "title": "Save a component style", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True,
    }


@pytest.mark.asyncio
async def test_descriptions_say_draft_own_file_and_how_to_apply():
    tools = await _tools()
    save = tools["save_component_style"].description
    for words in ("DRAFT", "components/<block_type>/<style>.css", "never changed", "styleName", "publish_portal_stylesheet", "remove: true", "which pages use it"):
        assert words in save, words
    assert "root" in save


@pytest.mark.asyncio
async def test_inputs_match_the_api():
    tools = await _tools()
    guide = tools["get_component_style_guide"].parameters
    assert set(guide["properties"]) == {"merchant_id", "portal", "brand_id"}
    assert guide["required"] == ["portal"]
    save = tools["save_component_style"].parameters
    assert set(save["properties"]) == {"merchant_id", "portal", "brand_id", "block_type", "name", "css", "description", "remove"}
    assert set(save["required"]) == {"portal", "block_type", "name"}
    assert "hero" in json.dumps(save["properties"]["block_type"])
    # the API refuses a null here, so the schema does not offer one
    assert "null" not in json.dumps(save["properties"]["css"])


@pytest.mark.asyncio
async def test_generate_takes_the_block_and_says_nothing_is_stored():
    tools = await _tools()
    generate = tools["generate_component_styles"]
    assert set(generate.parameters["properties"]) == {"merchant_id", "portal", "brand_id", "block_type", "block", "instruction", "count"}
    assert set(generate.parameters["required"]) == {"portal", "block_type"}
    assert "Nothing is stored" in generate.description
    assert "save_component_style" in generate.description


@pytest.mark.asyncio
async def test_generate_posts_the_block(monkeypatch):
    from fastmcp import Client

    import server

    answer = {"block_type": "hero", "alternatives": [{"name": "Airy", "class": "jm-style-airy", "description": "More space.", "css": ".jm-site .jm-block--hero.jm-style-airy{padding:96px 0}"}], "warnings": []}
    seen = _route(monkeypatch, 200, answer)
    body = {"portal": "interview", "block_type": "hero", "block": {"title": "Sail with us"}, "count": 1}
    async with Client(server.mcp) as client:
        result = await client.call_tool("generate_component_styles", body)
    assert seen[0].method == "POST" and seen[0].url.path.endswith("/portal-component-styles-generate")
    assert json.loads(seen[0].content) == body
    assert result.structured_content["alternatives"][0]["name"] == "Airy"


def test_instructions_list_the_tools():
    import server

    for name in ("get_component_style_guide", "generate_component_styles", "save_component_style"):
        assert name in server.INSTRUCTIONS, name


@pytest.mark.asyncio
async def test_the_guide_is_a_get_with_the_query(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, 200, GUIDE)
    async with Client(server.mcp) as client:
        result = await client.call_tool("get_component_style_guide", {"portal": "interview", "brand_id": BRAND})
    assert seen[0].method == "GET"
    assert dict(seen[0].url.params) == {"portal": "interview", "brand_id": BRAND}
    assert result.structured_content["components"][0]["styles"][0]["written_by"] == "agent"


@pytest.mark.asyncio
async def test_a_save_posts_the_body_and_validates_a_null_file_id(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, 200, SAVED)
    body = {"portal": "interview", "block_type": "hero", "name": "Dark band", "css": ".jm-site .jm-block--hero.jm-style-dark-band h1{color:red}"}
    async with Client(server.mcp) as client:
        result = await client.call_tool("save_component_style", body)
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == body
    assert result.structured_content["file_id"] is None


@pytest.mark.asyncio
async def test_a_refusal_reaches_the_model(monkeypatch):
    from fastmcp import Client

    import server

    _route(monkeypatch, 409, {"error": '"Dark band" is already a hero style of the company, in brand/hero.css.'})
    async with Client(server.mcp) as client:
        result = await client.call_tool("save_component_style", {"portal": "interview", "block_type": "hero", "name": "Dark band", "remove": True}, raise_on_error=False)
    assert result.is_error
    assert "brand/hero.css" in result.content[0].text
