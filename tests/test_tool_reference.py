"""Short tool definitions in tools/list, the full ones from get_tool_reference.

Run with:  ENABLE_AUTH=false pytest -q tests/test_tool_reference.py

The trimming may only remove prose: every advertised schema must keep the full
schema's structure (types, enums, required, nesting), so validation and what a
client can send are unchanged. get_tool_reference returns the untouched definition
and runs without a token, like tools/list.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")

import tool_reference  # noqa: E402
from tool_reference import POINTER, TOOL_NAME, brief, strip_annotations, trim_input_schema  # noqa: E402

# The SDK's analytics arguments, added to every tool after capture.
ANALYTICS_ARGS = {"conversation_id", "llm_model", "context"}


async def _listed() -> dict:
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def _reference(tool: str, part: str = "arguments") -> dict:
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return (await client.call_tool(TOOL_NAME, {"tool": tool, "part": part})).structured_content


def _without_analytics(schema: dict) -> dict:
    schema = copy.deepcopy(schema)
    for name in ANALYTICS_ARGS:
        schema.get("properties", {}).pop(name, None)
        if name in schema.get("required", []):
            schema["required"].remove(name)
    return schema


def _nodes(schema, depth=0):
    """(node, depth) for every schema node; shared definitions count as nested."""
    if not isinstance(schema, dict):
        return
    yield schema, depth
    for key in ("$defs", "definitions"):
        for child in (schema.get(key) or {}).values():
            yield from _nodes(child, max(depth, 2))
    for child, deeper in tool_reference._children(schema):
        yield from _nodes(child, depth + 1 if deeper else depth)


def _resolved(schema: dict) -> dict:
    """The schema with every local $ref replaced by its definition, $defs dropped."""
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}

    def walk(node, seen=()):
        if isinstance(node, list):
            return [walk(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in defs and ref not in seen:
            merged = {**defs[ref.rsplit("/", 1)[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged, (*seen, ref))
        return {k: walk(v, seen) for k, v in node.items() if k not in ("$defs", "definitions")}

    return walk(schema)


# ---- what tools/list carries ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_generated_tool_is_short_and_points_to_the_reference():
    import server  # noqa: F401  (builds the server, which fills FULL)

    listed = await _listed()
    assert len(tool_reference.FULL) >= 60
    for name in tool_reference.FULL:
        tool = listed[name]
        assert tool.description.endswith(POINTER), name
        for node, depth in _nodes(tool.input_schema):
            assert "example" not in node and "examples" not in node, name
            if depth >= 2:
                assert "description" not in node, f"{name}: a nested description is still advertised"
            if depth == 1 and isinstance(node.get("description"), str):
                assert "Options — " not in node["description"], name
        for node, _ in _nodes(tool.output_schema or {}):
            assert not set(node) & {"description", "title", "example", "examples"}, f"{name}: output schema annotation"


@pytest.mark.asyncio
async def test_trimming_removes_prose_only_never_structure():
    """Same types, enums, required fields and nesting as the full definition."""
    listed = await _listed()
    for name, full in tool_reference.FULL.items():
        advertised = _resolved(_without_analytics(listed[name].input_schema))
        assert strip_annotations(advertised) == strip_annotations(_resolved(full["input_schema"])), name
        if full["output_schema"]:
            # tools/list keeps FastMCP's own x-fastmcp-* markers (FastMCP reads them); the
            # reference drops them
            advertised_out = tool_reference._without_overrides(copy.deepcopy(listed[name].output_schema))
            assert strip_annotations(_resolved(advertised_out)) == strip_annotations(
                _resolved(full["output_schema"])
            ), name


@pytest.mark.asyncio
async def test_the_model_visible_definitions_stay_within_budget():
    """Name + description + input schema of every tool: was ~286 KB (~73k tokens)."""
    listed = await _listed()
    size = sum(
        len(json.dumps({"name": t.name, "description": t.description, "input_schema": t.input_schema}, ensure_ascii=False))
        for t in listed.values()
    )
    assert size < 200_000, f"tools/list grew to {size:,} model-visible characters"


# ---- get_tool_reference ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reference_returns_the_full_definition():
    full = tool_reference.FULL["update_interview"]
    out = await _reference("update_interview")
    assert out["description"] == full["description"]
    assert out["arguments"] == full["input_schema"]
    assert "result" not in out
    # what tools/list dropped is back: an option essay, an example, a nested description
    arguments = json.dumps(out["arguments"], ensure_ascii=False)
    assert "Options — " in arguments and '"example"' in arguments
    question = _resolved(out["arguments"])["properties"]["questions"]["items"]["properties"]["question"]
    assert question["description"]
    # and the spec's own operation text that the short description leaves out
    assert "Only the fields present in the request body are written" in out["description"]


@pytest.mark.asyncio
async def test_the_reference_returns_what_a_tool_answers():
    out = await _reference("get_interview_result_details", "result")
    assert "arguments" not in out
    assert out["result"]["properties"]["recording_local_path"]["description"]
    both = await _reference("get_interview_result_details", "all")
    assert {"arguments", "result"} <= set(both)


@pytest.mark.asyncio
async def test_the_tool_argument_is_an_enum_of_the_captured_tools():
    listed = await _listed()
    schema = listed[TOOL_NAME].input_schema["properties"]["tool"]
    enum = schema.get("enum") or next(s["enum"] for s in schema.get("anyOf", []) if "enum" in s)
    assert sorted(enum) == sorted(tool_reference.FULL)
    assert TOOL_NAME not in enum
    assert listed[TOOL_NAME].annotations.read_only_hint is True


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_refused():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    async with Client(server.mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(TOOL_NAME, {"tool": "drop_database"})


# ---- the rules a model needs stay where it reads them ----------------------------------------


def test_brief_keeps_the_first_sentence_and_every_rule():
    text = (
        "Ask only a random subset of the questions. A fraction between 0.01 and 0.9. "
        "null asks all questions. Useful for long banks, e.g. 40 questions. "
        "Options — `x`: something."
    )
    assert brief(text) == "Ask only a random subset of the questions. null asks all questions."
    assert brief("One sentence only.") == "One sentence only."
    assert brief("Header.\nAn empty string clears it.\nTrivia.") == "Header. An empty string clears it."
    assert brief("") == ""


def test_an_enum_argument_keeps_what_each_option_means():
    """The three ways the spec lists options; the label that repeats a value goes."""
    essay = (
        "Who can see it. Options — `merchant_public`: Listed on the public list — anyone can start it. "
        "| `merchant_invite`: Invite-only — only invited candidates. Second sentence dropped."
    )
    assert brief(essay) == "Who can see it."
    assert brief(essay, options=True) == (
        "Who can see it. Values: `merchant_public`: Listed on the public list — anyone can start it | "
        "`merchant_invite`: only invited candidates."
    )
    lines = "How much to return.\n\n- `summary`: scores only.\n- `full`: every field. This is long."
    assert brief(lines, options=True) == "How much to return. Values: `summary`: scores only | `full`: every field."
    inline = "Pick deliberately. `interview`: a role-play. More. | `coaching` (DEFAULT when omitted): practice."
    assert brief(inline, options=True) == (
        "Pick deliberately. Values: `interview`: a role-play | `coaching` (DEFAULT when omitted): practice."
    )
    label = "Style. Options — `pre-screening-with-test-questions`: Pre-screening with test questions — plus a test."
    assert brief(label, options=True) == "Style. Values: `pre-screening-with-test-questions`: plus a test."


def test_a_sentence_never_ends_inside_a_code_span():
    text = "Every selector must include the class: `.jm-site h1 { ... }`. Trivia."
    assert brief(text) == "Every selector must include the class: `.jm-site h1 { ... }`."


@pytest.mark.asyncio
async def test_the_advertised_enums_carry_their_option_meanings():
    listed = await _listed()
    visibility = listed["create_interview"].input_schema["properties"]["visibility"]["description"]
    assert "`merchant_invite`: only candidates explicitly invited" in visibility
    view = listed["get_interview_result_details"].input_schema["properties"]["view"]["description"]
    assert "`summary`:" in view and "`full`:" in view and "This is the default" not in view


def test_trim_and_strip_work_in_place_on_nested_and_composed_schemas():
    schema = {
        "type": "object",
        "properties": {
            "a": {"description": "First. Second.", "example": 1, "anyOf": [{"type": "string", "description": "x"}]},
            "b": {"type": "object", "properties": {"c": {"description": "nested", "examples": [1]}}},
            "title": {"type": "string", "description": "A field called title. Must be set."},
        },
        "$defs": {"D": {"description": "def", "properties": {"e": {"description": "deep"}}}},
    }
    trim_input_schema(schema)
    assert schema["properties"]["a"] == {"description": "First.", "anyOf": [{"type": "string", "description": "x"}]}
    assert schema["properties"]["b"]["properties"]["c"] == {}
    assert schema["properties"]["title"]["description"] == "A field called title. Must be set."
    assert "description" not in schema["$defs"]["D"]["properties"]["e"]
    strip_annotations(schema)
    # a property NAMED title survives; only annotation keys go
    assert "title" in schema["properties"]
    assert "description" not in schema["properties"]["title"]


# ---- no token needed for it, and for nothing else --------------------------------------------


def _call(name: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}


@pytest.mark.parametrize(
    ("body", "public"),
    [
        ([_call(TOOL_NAME)], True),
        (_call(TOOL_NAME), True),
        ([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _call(TOOL_NAME)], True),
        (_call("list_candidates"), False),
        ([_call(TOOL_NAME), _call("list_candidates")], False),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {"name": TOOL_NAME}}}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [TOOL_NAME]}, False),
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"name": TOOL_NAME}}, False),
        ([], False),
        ([1], False),
    ],
)
def test_only_a_call_to_the_reference_tool_is_public(body, public):
    from lazy_auth import PUBLIC_METHODS, PUBLIC_TOOL_CALLS, _all_public

    assert _all_public(json.dumps(body).encode(), PUBLIC_METHODS, PUBLIC_TOOL_CALLS) is public


@pytest.mark.asyncio
async def test_the_middleware_serves_the_reference_without_a_token():
    from test_listing_readiness import _run_lazy_auth

    from lazy_auth import ANONYMOUS_CLIENT_ID

    seen = await _run_lazy_auth(json.dumps(_call(TOOL_NAME)).encode())
    assert seen["scope"]["user"].access_token.client_id == ANONYMOUS_CLIENT_ID
    seen = await _run_lazy_auth(json.dumps(_call("get_candidate")).encode())
    assert seen["scope"].get("user") is None


def test_the_public_tool_reads_nothing_upstream():
    """A public tool must not need the user's token: it is not a generated API tool."""
    import server  # noqa: F401

    from lazy_auth import PUBLIC_TOOL_CALLS

    assert PUBLIC_TOOL_CALLS == {TOOL_NAME}
    assert TOOL_NAME not in tool_reference.FULL


@pytest.mark.asyncio
async def test_result_tools_say_which_list_row_id_is_the_result():
    """Single-stage rows of list_interview_results carry the result as `id`, with
    interview_result_id null; telling a model to always use interview_result_id
    sent a null for every single-stage result."""
    import server

    listed = await _listed()
    for name in ("get_interview_result_details", "generate_interview_report", "request_another_interview_attempt"):
        assert "a single-stage row's `id`" in listed[name].description, name
    assert "NOT the result row's `id`" not in server.INSTRUCTIONS
    assert "on a single-stage row" in server.INSTRUCTIONS


def test_the_specs_own_short_text_wins_and_never_leaks():
    """x-mcp-description: the source's short version of a field, for where trimming reads badly."""
    from tool_reference import OVERRIDE, _without_overrides

    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "Long. Options — `x`: one.", OVERRIDE: "Short."},
            "b": {"type": "object", "properties": {"c": {"type": "string", "description": "nested", OVERRIDE: "n"}}},
            "flag": {"type": ["string", "null"], "enum": ["true", "false", None], "description": "When true, more."},
        },
    }
    full = _without_overrides(copy.deepcopy(schema))
    assert OVERRIDE not in json.dumps(full) and full["properties"]["a"]["description"] == "Long. Options — `x`: one."
    trim_input_schema(schema)
    assert schema["properties"]["a"] == {"type": "string", "description": "Short."}
    assert schema["properties"]["b"]["properties"]["c"] == {"type": "string"}
    # a nullable enum must not break the trimmer: FastMCP swallows the exception and
    # the tool is then advertised untrimmed
    assert schema["properties"]["flag"]["description"] == "When true, more. Send the string 'true' or 'false'."


@pytest.mark.asyncio
async def test_no_override_key_reaches_a_client():
    listed = await _listed()
    assert all(OVERRIDE_KEY not in json.dumps(t.input_schema) for t in listed.values())
    assert OVERRIDE_KEY not in json.dumps(await _reference("list_candidates", "all"))


OVERRIDE_KEY = "x-mcp-description"


@pytest.mark.asyncio
async def test_score_max_1_is_allowed_and_references_carry_no_fastmcp_markers():
    listed = await _listed()
    score_max = listed["set_assessment_result"].input_schema["properties"]["score_max"]
    assert score_max.get("exclusiveMinimum") == 0 and "minimum" not in score_max, score_max
    for name in tool_reference.FULL:
        assert "x-fastmcp" not in json.dumps(await _reference(name, "all")), name


def test_the_trimmers_skip_what_is_not_a_schema():
    """Odd specs: non-dict shared definitions, a root description, non-dict input."""
    from tool_reference import _without_overrides

    schema = {"description": "Root text. Kept.", "$defs": {"odd": "not a schema", "D": {"description": "deep"}}}
    trim_input_schema(schema)
    assert schema["description"] == "Root text. Kept." and schema["$defs"]["odd"] == "not a schema"
    assert "description" not in schema["$defs"]["D"]
    assert strip_annotations(schema)["$defs"]["odd"] == "not a schema"
    assert _without_overrides({"$defs": {"odd": 1}}) == {"$defs": {"odd": 1}}
    assert trim_input_schema("x") == "x" and strip_annotations(None) is None


def test_option_text_that_is_not_an_option_list_stays_out():
    # an entry without a `value`: and a bar that is not an option list
    assert brief("Pick one. Options — plain words | more words.", options=True) == "Pick one."
    assert brief("A | `b`:x", options=True) == "A | `b`:x"


def test_an_argument_without_text_borrows_it_from_its_part_or_its_definition():
    """zod puts a field's description in an allOf part or on the shared component."""
    from tool_reference import OVERRIDE

    schema = {
        "type": "object",
        "properties": {
            "inline": {"allOf": [{"type": "object"}, {"description": "From the part. More."}]},
            "ref": {"$ref": "#/$defs/Features"},
            "nullable": {"anyOf": [{"$ref": "#/$defs/Short"}, {"type": "null"}]},
            "unknown": {"$ref": "#/$defs/Missing"},
            "own": {"$ref": "#/$defs/Features", "description": "Its own. Kept first."},
        },
        "$defs": {
            "Features": {"type": "object", "description": "Which sections. Detail."},
            "Short": {"type": "object", "description": "Long text.", OVERRIDE: "Short text."},
            "Bare": {"type": "object"},
        },
    }
    trim_input_schema(schema)
    p = schema["properties"]
    assert p["inline"] == {"allOf": [{"type": "object"}, {}], "description": "From the part."}
    assert p["ref"]["description"] == "Which sections."
    assert p["nullable"]["description"] == "Short text."
    assert "description" not in p["unknown"]
    assert p["own"]["description"] == "Its own."
    # the definitions themselves are nested: trimmed as before
    assert "description" not in schema["$defs"]["Features"]


def test_a_long_option_meaning_is_cut_at_its_first_clause():
    from tool_reference import _first_clause

    assert _first_clause("short meaning") == "short meaning"
    long = "Audio + video cheating detection on every answer — the candidate is recorded for all answers"
    assert _first_clause(long) == "Audio + video cheating detection on every answer"
    assert _first_clause("a b c d e f g h i j k l m n") == "a b c d e f g h i j k l m n"  # no break: kept whole
    assert _first_clause("x; then a long tail of words that goes on and on and on") == "x; then a long tail of words that goes on and on and on"
