"""The two portal configuration tools as the built server lists them.

Run with:  ENABLE_AUTH=false pytest -q tests/test_portal_configuration_tools.py

The curated entries in ``naming.TOOL_META`` are data; what matters is that they
reach the listed tools (FastMCP swallows a failing ``_customize_component`` and
ships the tool uncustomized), so these tests assert the built server: names,
titles, annotations, the description copy the model reads (read first, dry run,
publish only on request), the input the API takes, and the server instructions.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

COOKBOOKS = (
    "https://developer.jobmojito.com/cookbooks/portal-branding/branding",
    "https://developer.jobmojito.com/cookbooks/portal-branding/interview-portal",
    "https://developer.jobmojito.com/cookbooks/portal-branding/coaching-portal",
    "https://developer.jobmojito.com/cookbooks/portal-branding/portal-pages",
)


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True)


async def _tool(name: str):
    import server

    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert name in tools, f"{name} is not listed"
    return tools[name]


@pytest.mark.asyncio
async def test_get_portal_configuration_is_a_read_only_lookup():
    tool = await _tool("get_portal_configuration")
    assert tool.title == "Get portal configuration"
    assert _annotations(tool) == {
        "title": "Get portal configuration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    assert tool.meta["jobmojito"] == {"category": "Admin", "http_method": "GET"}


@pytest.mark.asyncio
async def test_update_portal_configuration_is_destructive_and_not_idempotent():
    tool = await _tool("update_portal_configuration")
    assert tool.title == "Update portal configuration"
    assert _annotations(tool) == {
        "title": "Update portal configuration",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tool.meta["jobmojito"] == {"category": "Admin", "http_method": "POST"}


@pytest.mark.asyncio
async def test_descriptions_carry_the_category_the_flow_and_the_field_reference():
    get = await _tool("get_portal_configuration")
    update = await _tool("update_portal_configuration")

    assert get.description.startswith("[Admin] Read the company's portal configuration")
    assert "sections" in get.description
    assert "update_portal_configuration" in get.description

    assert update.description.startswith("[Admin] Import portal configuration documents")
    # read first, preview with dry_run, then apply; publish only when asked
    assert "get_portal_configuration" in update.description
    assert "`dry_run: true`" in update.description
    assert "repeat without `dry_run` to apply" in update.description
    assert "`publish: true` only when the user asked" in update.description
    assert "nothing is written" in update.description

    for description in (get.description, update.description):
        for url in COOKBOOKS:
            assert url in description


@pytest.mark.asyncio
async def test_inputs_match_the_api():
    get = (await _tool("get_portal_configuration")).parameters["properties"]
    assert {"merchant_id", "sections"} <= set(get)

    update = (await _tool("update_portal_configuration")).parameters
    assert {"merchant_id", "documents", "kind", "portal", "publish", "dry_run"} <= set(update["properties"])
    assert "documents" in update.get("required", [])


def test_curated_entries_and_their_justifications():
    import naming

    get = naming.meta_for("get", "/portal-configuration-get")
    update = naming.meta_for("POST", "/portal-configuration-update")
    assert (get.name, get.read_only, get.destructive) == ("get_portal_configuration", True, False)
    assert (update.name, update.read_only, update.destructive, update.idempotent) == (
        "update_portal_configuration",
        False,
        True,
        False,
    )
    assert "readOnlyHint=true" in get.justification
    assert "destructiveHint=true" in update.justification
    assert "`dry_run: true` writes nothing" in update.justification
    assert "idempotentHint=false" in update.justification

    justifications = naming.annotation_justifications()
    assert justifications["get_portal_configuration"] == get.justification
    assert justifications["update_portal_configuration"] == update.justification


def test_instructions_list_the_tools_and_the_flow():
    import server

    text = server.INSTRUCTIONS
    assert "• Portal configuration: get_portal_configuration, update_portal_configuration" in text
    flow = text[text.index('"Rebrand the portal or edit its pages"'):]
    assert "get_portal_configuration (only the\n  sections involved)" in flow
    assert "update_portal_configuration\n  with `dry_run: true` to preview" in flow
    assert "`publish: true` only\n  when the user asked to publish" in flow


@pytest.mark.asyncio
async def test_the_descriptions_name_brands_styles_and_their_switches():
    get = await _tool("get_portal_configuration")
    update = await _tool("update_portal_configuration")
    assert "`brands`" in get.description
    assert "`style_settings`" in get.description
    assert "`css_files`" in get.description
    assert "brands" in get.parameters["properties"]["sections"]["description"]
    assert "brands" in update.description
    assert "style settings, CSS files" in update.description
    assert "builds and publishes the stylesheet" in update.description
    assert "refused with 422" in update.description
    assert "`version`" in update.description


@pytest.mark.asyncio
async def test_a_brands_document_passes_the_input_schema():
    update = (await _tool("update_portal_configuration")).parameters
    kinds = str(update["properties"]["kind"])
    assert "'brands'" in kinds
