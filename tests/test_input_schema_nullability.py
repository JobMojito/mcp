"""A tool's input schema offers null only where the API declares it; responses stay relaxed.

Run with:  ENABLE_AUTH=false pytest -q tests/test_input_schema_nullability.py

The model reads the input schema, and a null offered on a field the API refuses invites
stray nulls. Output validation is on and the API returns null for fields it declares as
non-nullable, so response schemas accept null everywhere. The two meet on components both
a request and a response use, which is where most of these tests look.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib

import httpx2
import jsonschema
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

from naming import TOOL_META, is_ignored  # noqa: E402
from openapi_loader import RESPONSE_COPY_SUFFIX, _prepare, relax_nullable_schemas  # noqa: E402

INTERVIEW = "de300000-0000-4000-8001-000000000007"
SNAPSHOT_PATH = pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json"
RAW = json.loads(SNAPSHOT_PATH.read_text())

#: Not nullable in the API. Every one of these used to be advertised with null.
NOT_NULLABLE_ON_UPDATE = (
    "position_id",
    "status",
    "visibility",
    "environment",
    "custom_fields",
    "questions",
    "regenerate_candidate_expectations",
    "type",
    "interview_template_id",
    "result_view",
    "interview_attempts",
)
UPDATE_BODY = RAW["components"]["schemas"]["JobInterviewUpdateRequest"]["properties"]


def _offers_null(schema: dict) -> bool:
    kinds = schema.get("type")
    return (
        kinds == "null"
        or (isinstance(kinds, list) and "null" in kinds)
        or None in (schema.get("enum") or [])
        or any(_offers_null(option) for option in schema.get("anyOf") or [])
    )


def _accepts(schema: dict, value) -> bool:
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError:
        return False
    return True


def _fake_upstream(monkeypatch, answer: dict) -> list[httpx2.Request]:
    seen: list[httpx2.Request] = []

    async def fake_send(self, request, **kwargs):
        seen.append(request)
        return httpx2.Response(200, json=answer, request=request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", fake_send)
    return seen


async def _listed_tools() -> dict:
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def _call(tool: str, arguments: dict):
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return await client.call_tool(tool, arguments)


# ---- update_interview, as a client sees it --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", NOT_NULLABLE_ON_UPDATE)
async def test_update_interview_does_not_offer_null_the_api_refuses(field):
    # A guard on the test itself: the premise is the API's own declaration.
    assert field == "position_id" or not UPDATE_BODY[field].get("nullable")
    schema = (await _listed_tools())["update_interview"].input_schema["properties"][field]
    assert not _offers_null(schema), schema
    assert "nullable" not in schema
    assert not _accepts({k: v for k, v in schema.items() if k != "description"}, None)


@pytest.mark.asyncio
async def test_update_interview_enums_are_the_apis_values():
    properties = (await _listed_tools())["update_interview"].input_schema["properties"]
    for field in ("status", "visibility", "environment", "type", "result_view"):
        assert properties[field]["enum"] == UPDATE_BODY[field]["enum"]
        assert None not in properties[field]["enum"]


@pytest.mark.asyncio
async def test_update_interview_keeps_every_null_the_api_declares():
    properties = (await _listed_tools())["update_interview"].input_schema["properties"]
    declared = [
        name for name, schema in UPDATE_BODY.items() if schema.get("nullable") and "type" in schema
    ]
    assert "candidate_notification_channel" in declared  # the list is the real one
    for name in declared:
        assert _offers_null(properties[name]), name


@pytest.mark.asyncio
async def test_a_declared_nullable_enum_offers_null_on_both_checks():
    field = (await _listed_tools())["update_interview"].input_schema["properties"][
        "candidate_notification_channel"
    ]
    assert field["type"] == ["string", "null"]
    assert None in field["enum"]
    assert _accepts({"type": field["type"], "enum": field["enum"]}, None)


@pytest.mark.asyncio
async def test_a_null_response_still_validates(monkeypatch):
    """None of these is nullable in the spec, and the API can still return null for them."""
    answer = {"position_id": None, "is_multistage": None, "updated_fields": None}
    declared = RAW["components"]["schemas"]["JobInterviewUpdateResponse"]["properties"]
    assert not any(declared[field].get("nullable") for field in answer)

    _fake_upstream(monkeypatch, answer)
    result = await _call("update_interview", {"position_id": INTERVIEW, "name": "Renamed"})
    assert result.structured_content == answer


@pytest.mark.asyncio
async def test_output_validation_is_still_on(monkeypatch):
    """A relaxed schema still refuses the wrong type, so the null test above means something."""
    from fastmcp.exceptions import ToolError

    _fake_upstream(monkeypatch, {"position_id": 5, "is_multistage": False, "updated_fields": []})
    with pytest.raises(ToolError, match="position_id"):
        await _call("update_interview", {"position_id": INTERVIEW, "name": "Renamed"})


@pytest.mark.asyncio
async def test_a_stray_null_on_a_narrowed_field_stays_out_of_the_request(monkeypatch):
    """The schema no longer invites it, and a client that sends one anyway still isn't refused."""
    seen = _fake_upstream(monkeypatch, {"position_id": INTERVIEW, "is_multistage": False, "updated_fields": []})
    await _call(
        "update_interview",
        {"position_id": INTERVIEW, "name": "Renamed", "status": None, "visibility": None},
    )
    assert json.loads(seen[0].content) == {"position_id": INTERVIEW, "name": "Renamed"}


# ---- components both sides use --------------------------------------------------------------


@pytest.mark.asyncio
async def test_interview_question_is_narrow_as_input_and_relaxed_as_output():
    """JobInterviewCreateFromArrayQuestion: update_interview sends it, get_interview_definition returns it."""
    tools = await _listed_tools()
    sent = tools["update_interview"].input_schema["properties"]["questions"]["items"]["properties"]
    assert sent["question"]["type"] == "string"
    assert sent["duration"]["type"] == ["number", "null"]  # declared: stays

    returned = tools["get_interview_definition"].output_schema["properties"]["questions"]["items"]["properties"]
    assert "null" in returned["question"]["type"]
    assert "null" in returned["duration"]["type"]


@pytest.mark.asyncio
async def test_portal_document_is_narrow_as_input_and_relaxed_as_output(monkeypatch):
    tools = await _listed_tools()
    sent = tools["update_portal_configuration"].input_schema["properties"]["documents"]["items"]["properties"]
    assert sent["kind"]["type"] == "string"
    assert None not in sent["kind"]["enum"]
    assert sent["format"]["type"] == "string"

    returned = tools["get_portal_configuration"].output_schema["properties"]["documents"]["items"]["properties"]
    assert "null" in returned["kind"]["type"]
    assert None in returned["kind"]["enum"]

    answer = {"merchant_id": "m", "documents": [{"kind": None, "format": None, "version": None}]}
    _fake_upstream(monkeypatch, answer)
    result = await _call("get_portal_configuration", {})
    assert result.structured_content["documents"][0]["kind"] is None


@pytest.mark.asyncio
async def test_no_input_schema_carries_a_response_copy():
    for name, tool in (await _listed_tools()).items():
        assert RESPONSE_COPY_SUFFIX not in json.dumps(tool.input_schema), name


# ---- every tool -----------------------------------------------------------------------------


def _resolved(schema: dict) -> dict:
    while "$ref" in schema:
        schema = RAW["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _paired_nodes(tool_node: dict, raw_node: dict, where: str):
    """Walk an input schema next to what the spec declares for the same field."""
    raw_node = _resolved(raw_node)
    yield where, tool_node, raw_node
    for name, child in (tool_node.get("properties") or {}).items():
        yield from _paired_nodes(child, (raw_node.get("properties") or {}).get(name, {}), f"{where}.{name}")
    for key in ("items", "additionalProperties"):
        child, raw_child = tool_node.get(key), raw_node.get(key)
        if isinstance(child, dict) and isinstance(raw_child, dict):
            yield from _paired_nodes(child, raw_child, f"{where}.{key}")
    for key in ("anyOf", "oneOf"):
        for index, (child, raw_child) in enumerate(zip(tool_node.get(key) or [], raw_node.get(key) or [])):
            yield from _paired_nodes(child, raw_child, f"{where}.{key}[{index}]")


def _generated_tools_with_a_body():
    for (method, path), meta in TOOL_META.items():
        if is_ignored(method, path):
            continue
        operation = RAW["paths"][path][method.lower()]
        content = operation.get("requestBody", {}).get("content", {})
        if "application/json" in content:
            yield meta.name, content["application/json"]["schema"]


@pytest.mark.asyncio
async def test_every_tool_offers_null_only_where_the_spec_declares_it():
    tools = await _listed_tools()
    checked = 0
    for name, body in _generated_tools_with_a_body():
        schema = tools[name].input_schema
        for where, node, raw in _paired_nodes(schema, body, name):
            checked += 1
            kinds = node.get("type")
            if isinstance(kinds, list) and "null" in kinds:
                declared = raw.get("nullable") is True or "null" in (raw.get("type") or [])
                assert declared, f"{where} offers null the spec does not declare: {raw}"
            if raw.get("nullable") is True and isinstance(raw.get("type"), str):
                assert "null" in kinds, f"{where} lost a null the spec declares: {node}"
    assert checked > 300  # really walked the nested schemas


@pytest.mark.asyncio
async def test_query_parameters_are_not_offered_null():
    """Declared `nullable` on `offset`, but a query string has no way to carry a null."""
    tools = await _listed_tools()
    parameters = RAW["paths"]["/merchant-interview-list"]["get"]["parameters"]
    assert next(p for p in parameters if p["name"] == "offset")["schema"]["nullable"] is True
    for name in ("list_interviews", "list_candidates"):
        assert tools[name].input_schema["properties"]["offset"]["type"] == "integer"


def _typed_output_properties(tool_node: dict, raw_node: dict):
    """Output properties the spec types directly (``$ref`` ones are CLAUDE.md's known gap)."""
    raw_node = _resolved(raw_node)
    for name, child in (tool_node.get("properties") or {}).items():
        raw_child = (raw_node.get("properties") or {}).get(name, {})
        if "type" in raw_child:
            yield name, child
        yield from _typed_output_properties(child, raw_child)
    for key in ("items", "additionalProperties"):
        child, raw_child = tool_node.get(key), raw_node.get(key)
        if isinstance(child, dict) and isinstance(raw_child, dict):
            yield from _typed_output_properties(child, raw_child)
    for key in ("anyOf", "oneOf"):
        for child, raw_child in zip(tool_node.get(key) or [], raw_node.get(key) or []):
            yield from _typed_output_properties(child, raw_child)


@pytest.mark.asyncio
async def test_every_typed_output_property_accepts_null():
    """The rule output validation depends on, across every generated tool."""
    tools = await _listed_tools()
    checked = 0
    for (method, path), meta in TOOL_META.items():
        if is_ignored(method, path) or not tools[meta.name].output_schema:
            continue
        responses = RAW["paths"][path][method.lower()]["responses"]
        ok = next(content for status, content in responses.items() if status.startswith("2"))
        raw = ok.get("content", {}).get("application/json", {}).get("schema", {})
        for field, schema in _typed_output_properties(tools[meta.name].output_schema, raw):
            checked += 1
            assert _offers_null(schema), f"{meta.name}: {field} refuses null: {schema}"
    assert checked > 300  # really walked the nested schemas


# ---- the relaxer ----------------------------------------------------------------------------


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _json_content(schema: dict) -> dict:
    return {"content": {"application/json": {"schema": schema}}}


def _spec(request: dict, response: dict, schemas: dict) -> dict:
    return {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": _json_content(request),
                    "responses": {"200": _json_content(response)},
                }
            }
        },
        "components": {"schemas": schemas},
    }


def _object(**properties) -> dict:
    return {"type": "object", "properties": properties}


def test_a_component_only_a_request_uses_is_left_alone():
    spec = _spec(_ref("In"), _ref("Out"), {
        "In": _object(a={"type": "string", "enum": ["x"]}, b={"type": "string", "nullable": True}),
        "Out": _object(c={"type": "string", "enum": ["x"]}),
    })
    schemas = relax_nullable_schemas(spec)["components"]["schemas"]
    assert schemas["In"]["properties"]["a"] == {"type": "string", "enum": ["x"]}
    assert schemas["In"]["properties"]["b"]["type"] == ["string", "null"]
    assert schemas["Out"]["properties"]["c"] == {
        "type": ["string", "null"],
        "enum": ["x", None],
        "nullable": True,
    }


def test_inline_schemas_follow_the_same_rule():
    spec = _spec(_object(a={"type": "string"}), _object(b={"type": "string"}), {})
    operation = relax_nullable_schemas(spec)["paths"]["/x"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["a"] == {"type": "string"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["b"]["type"] == [
        "string",
        "null",
    ]


def test_a_shared_component_is_split_along_with_what_it_nests():
    spec = _spec(_object(item=_ref("Outer")), _object(item=_ref("Outer")), {
        "Outer": _object(inner=_ref("Inner"), name={"type": "string"}),
        "Inner": _object(value={"type": "string"}),
    })
    relaxed = relax_nullable_schemas(spec)
    schemas = relaxed["components"]["schemas"]
    outer_copy, inner_copy = f"Outer{RESPONSE_COPY_SUFFIX}", f"Inner{RESPONSE_COPY_SUFFIX}"

    # The request keeps the originals, untouched.
    request = relaxed["paths"]["/x"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert request["properties"]["item"] == _ref("Outer")
    assert schemas["Outer"]["properties"]["inner"] == _ref("Inner")
    assert schemas["Outer"]["properties"]["name"] == {"type": "string"}
    assert schemas["Inner"]["properties"]["value"] == {"type": "string"}

    # The response reaches only the copies, all the way down.
    response = relaxed["paths"]["/x"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["properties"]["item"] == _ref(outer_copy)
    assert schemas[outer_copy]["properties"]["inner"] == _ref(inner_copy)
    assert schemas[outer_copy]["properties"]["name"]["type"] == ["string", "null"]
    assert schemas[inner_copy]["properties"]["value"]["type"] == ["string", "null"]


def test_a_response_only_component_nesting_a_shared_one_follows_the_copy():
    spec = _spec(_ref("Shared"), _ref("Wrapper"), {
        "Wrapper": _object(shared=_ref("Shared")),
        "Shared": _object(a={"type": "string"}),
    })
    schemas = relax_nullable_schemas(spec)["components"]["schemas"]
    assert schemas["Wrapper"]["properties"]["shared"] == _ref(f"Shared{RESPONSE_COPY_SUFFIX}")
    assert schemas["Shared"]["properties"]["a"] == {"type": "string"}


def test_a_component_that_refers_to_itself_terminates():
    spec = _spec(_ref("Tree"), _ref("Tree"), {"Tree": _object(child=_ref("Tree"), a={"type": "string"})})
    schemas = relax_nullable_schemas(spec)["components"]["schemas"]
    assert schemas["Tree"]["properties"]["child"] == _ref("Tree")
    copy_name = f"Tree{RESPONSE_COPY_SUFFIX}"
    assert schemas[copy_name]["properties"]["child"] == _ref(copy_name)


def test_a_reference_into_a_shared_component_follows_the_copy():
    spec = _spec(_ref("Shared"), {"$ref": "#/components/schemas/Shared/properties/a"}, {
        "Shared": _object(a=_object(b={"type": "string"})),
    })
    relaxed = relax_nullable_schemas(spec)
    schema = relaxed["paths"]["/x"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"] == f"#/components/schemas/Shared{RESPONSE_COPY_SUFFIX}/properties/a"
    assert relaxed["components"]["schemas"]["Shared"]["properties"]["a"]["properties"]["b"] == {"type": "string"}


def test_the_snapshot_splits_the_components_both_sides_use():
    copies = {
        name.removesuffix(RESPONSE_COPY_SUFFIX)
        for name in _prepare(copy.deepcopy(RAW))["components"]["schemas"]
        if name.endswith(RESPONSE_COPY_SUFFIX)
    }
    assert {"JobInterviewCreateFromArrayQuestion", "PortalConfigurationDocument"} <= copies


def test_a_cached_spec_prepares_to_the_same_thing():
    """The runtime cache holds the prepared spec, and it is prepared again on the next boot."""
    once = _prepare(copy.deepcopy(RAW))
    assert _prepare(json.loads(json.dumps(once))) == once


def test_the_default_cache_does_not_read_a_file_an_older_build_wrote(monkeypatch):
    """Those files offer null on every input field, and the loader can't tell which were declared."""
    from config import settings

    monkeypatch.delenv("OPENAPI_CACHE_PATH", raising=False)
    assert settings.cache_path.name != "jobmojito_openapi.cache.json"
