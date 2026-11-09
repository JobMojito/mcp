"""Values the API accepts that the MCP tools do not offer (naming.WITHHELD_VALUES).

request_candidate_documents does not replace a position's national ID question: OpenAI's
plugin scan held the tool on 9 Nov 2026 because `form_national_id` in the accepted
values of documents[].source_field_id read as soliciting a government identifier. The
value leaves the advertised schema (openapi_loader.withhold_values) and a call that
sends it anyway is refused before the API (middleware.WithheldValuesMiddleware).

Run with:  ENABLE_AUTH=false pytest -q tests/test_withheld_values.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import types

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")

from fastmcp.exceptions import ToolError  # noqa: E402

import naming  # noqa: E402
import openapi_loader as loader  # noqa: E402
from middleware import WithheldValuesMiddleware  # noqa: E402

KEY = ("POST", "/merchant-document-request-create")
API_PATTERN = "^(form_cover_letter|form_national_id|custom_[A-Za-z0-9_]{1,60})$"


def _spec(pattern: str | None = API_PATTERN, enum: list[str] | None = None, body_ref: bool = True) -> dict:
    """The document request's shape as the API publishes it: body and item schemas are components."""
    field: dict = {"type": "string", "description": "the API's own text, naming form_national_id"}
    if pattern is not None:
        field["pattern"] = pattern
    if enum is not None:
        field["enum"] = enum
    item = {"type": "object", "properties": {"title": {"type": "string"}, "source_field_id": field}}
    body = {"type": "object", "properties": {"documents": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}
    schemas = {"Item": item, "Body": body}
    return {
        "paths": {KEY[1]: {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"} if body_ref else body}}}}}},
        "components": {"schemas": schemas},
    }


def _field(spec: dict) -> dict:
    return spec["components"]["schemas"]["Item"]["properties"]["source_field_id"]


# ---- the table -----------------------------------------------------------------------------


def test_the_table_names_a_curated_tool_and_its_text():
    assert naming.TOOL_META[KEY].name == "request_candidate_documents"
    assert naming.withheld_tool_values() == {"request_candidate_documents": {"documents[].source_field_id": ("form_national_id",)}}
    assert "national" not in naming.WITHHELD_FIELD_TEXT[KEY]["documents[].source_field_id"]


# ---- withhold_values -------------------------------------------------------------------------


def test_the_value_leaves_the_pattern_through_component_refs_and_the_text_is_restated():
    spec = loader.withhold_values(_spec())
    assert _field(spec)["pattern"] == "^(form_cover_letter|custom_[A-Za-z0-9_]{1,60})$"
    assert _field(spec)["description"] == naming.WITHHELD_FIELD_TEXT[KEY]["documents[].source_field_id"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("^(form_national_id|form_cover_letter)$", "^(form_cover_letter)$"),
        ("^(form_cover_letter|form_national_id)$", "^(form_cover_letter)$"),
        ("^(a|form_national_id|b)$", "^(a|b)$"),
        # a longer name that only starts with the value is another value
        ("^(form_national_id_scan|b)$", "^(form_national_id_scan|b)$"),
    ],
)
def test_the_value_is_one_alternative_wherever_it_stands(pattern, expected):
    assert _field(loader.withhold_values(_spec(pattern)))["pattern"] == expected


def test_an_enum_loses_the_value_and_an_inline_body_is_reached_too():
    spec = loader.withhold_values(_spec(pattern=None, enum=["form_cover_letter", "form_national_id"], body_ref=False))
    field = spec["paths"][KEY[1]]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["documents"]["items"]
    assert loader._resolved(spec, field)["properties"]["source_field_id"]["enum"] == ["form_cover_letter"]


def test_a_value_the_api_no_longer_lists_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="jobmojito_mcp.openapi"):
        spec = loader.withhold_values(_spec("^(form_cover_letter|custom_x)$"))
    assert _field(spec)["pattern"] == "^(form_cover_letter|custom_x)$"
    assert "no longer lists 'form_national_id'" in caplog.text


def test_a_missing_field_is_logged_and_a_missing_operation_skipped(caplog):
    spec = _spec()
    del spec["components"]["schemas"]["Item"]["properties"]["source_field_id"]
    with caplog.at_level(logging.WARNING, logger="jobmojito_mcp.openapi"):
        loader.withhold_values(spec)
    assert "has no body field documents[].source_field_id" in caplog.text
    empty = {"paths": {}}
    assert loader.withhold_values(copy.deepcopy(empty)) == empty
    assert loader._body_field({}, {}, "documents[].x") is None


def test_the_committed_snapshot_is_narrowed_when_loaded():
    with open(os.path.join(os.path.dirname(loader.__file__), "data", "openapi.snapshot.json")) as fh:
        spec = loader._prepare(json.load(fh))
    field = loader._body_field(spec, spec["paths"][KEY[1]]["post"], "documents[].source_field_id")
    assert "form_national_id" not in field["pattern"]
    assert "form_cover_letter" in field["pattern"] and "custom_" in field["pattern"]


# ---- the tool as clients see it ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_and_the_tool_reference_carry_no_national_id():
    import server
    import tool_reference

    tools = {t.name: t for t in await server.mcp.list_tools()}
    tool = tools["request_candidate_documents"]
    listed = json.dumps({"description": tool.description, "input": tool.parameters}).lower()
    assert "national" not in listed and "passport" not in listed
    reference = json.dumps(tool_reference.FULL["request_candidate_documents"]).lower()
    assert "source_field_id" in reference
    assert "national" not in reference and "passport" not in reference


# ---- WithheldValuesMiddleware -------------------------------------------------------------


def _context(name: str, arguments) -> types.SimpleNamespace:
    return types.SimpleNamespace(message=types.SimpleNamespace(name=name, arguments=arguments))


async def _call(name: str, arguments):
    reached = []

    async def call_next(context):
        reached.append(context.message.arguments)
        return "ok"

    middleware = WithheldValuesMiddleware(naming.withheld_tool_values())
    result = await middleware.on_call_tool(_context(name, arguments), call_next)
    return result, reached


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "documents",
    [
        [{"title": "ID", "source_field_id": "form_national_id"}],
        [{"title": "Letter", "source_field_id": "form_cover_letter"}, {"title": "ID", "source_field_id": "form_national_id"}],
        json.dumps([{"title": "ID", "source_field_id": "form_national_id"}]),
    ],
)
async def test_a_withheld_value_is_refused_before_the_api(documents):
    with pytest.raises(ToolError) as caught:
        await _call("request_candidate_documents", {"candidate_id": "c", "documents": documents})
    assert "documents[].source_field_id form_national_id is not available through this server" in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("request_candidate_documents", {"documents": [{"title": "Letter", "source_field_id": "form_cover_letter"}, {"title": "Work sample"}]}),
        ("request_candidate_documents", {"documents": "not json"}),
        ("request_candidate_documents", {"documents": {"source_field_id": "form_national_id"}}),
        ("request_candidate_documents", {"candidate_id": "c"}),
        ("request_candidate_documents", None),
        ("update_position_step", {"documents": [{"source_field_id": "form_national_id"}]}),
    ],
)
async def test_everything_else_goes_through(name, arguments):
    result, reached = await _call(name, arguments)
    assert result == "ok" and reached == [arguments]
