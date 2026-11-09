"""Which nulls reach the API through a generated tool, and what the tools say about it.

Run with:  ENABLE_AUTH=false pytest -q tests/test_null_body_args.py

FastMCP leaves every top-level None out of the request. A few fields opt back in with
``ToolMeta.null_body_args``; everywhere else a null means "not sent", and the tool's text
has to say so instead of repeating the API's "null clears it".
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re

import httpx2
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

from naming import TOOL_META, is_ignored  # noqa: E402
from openapi_loader import apply_description_fixes  # noqa: E402

INTERVIEW = "de300000-0000-4000-8001-000000000007"
DIRECTORY = "sales-coaching-en"
UPDATE_INTERVIEW = "update_interview"
# One tool creates and updates a directory (POST /catalogue-tag-create, action update);
# the partial update is what the null rules are about.
UPDATE_DIRECTORY = "save_catalogue_directory"
DUPLICATE_POSITION = "duplicate_position"
POSITION = "de300000-0000-4000-8001-000000000008"
TARGET = {
    UPDATE_INTERVIEW: {"position_id": INTERVIEW},
    UPDATE_DIRECTORY: {"action": "update", "id": DIRECTORY},
    DUPLICATE_POSITION: {"position_id": POSITION},
}
ANSWER = {
    UPDATE_INTERVIEW: {"position_id": INTERVIEW, "is_multistage": False, "updated_fields": []},
    UPDATE_DIRECTORY: {"id": DIRECTORY, "merchant_id": "m", "parent_tag": None, "updated_fields": [], "catalogue_url": None},
    DUPLICATE_POSITION: {"position_id": INTERVIEW, "source_position_id": POSITION},
}
ENABLED = [
    # the live retest of 9 Nov: a null closing date was dropped and the copy kept a 2025 one
    (DUPLICATE_POSITION, "interview_available_till", "2026-12-31T23:59:59Z"),
    (DUPLICATE_POSITION, "openings_total", 3),
    (UPDATE_DIRECTORY, "content_md", "## Sales\n"),
    (UPDATE_INTERVIEW, "candidate_notification_channel", "reminders"),
    (UPDATE_INTERVIEW, "questions_random_subset", 0.5),
    (UPDATE_INTERVIEW, "pdf_export_auto_config", {"transcript": True}),
]

SNAPSHOT = json.loads((pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text())


def _body_properties(path: str) -> dict:
    schema = SNAPSHOT["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    name = schema.get("$ref", "").rsplit("/", 1)[-1]
    return (SNAPSHOT["components"]["schemas"][name] if name else schema)["properties"]


def _nullable_body_fields() -> list[tuple[str, str]]:
    """(tool, field) for every top-level nullable body field of a curated, exposed POST."""
    out = []
    for (method, path), meta in TOOL_META.items():
        if method != "POST" or is_ignored(method, path):
            continue
        operation = SNAPSHOT["paths"][path]["post"]
        if "application/json" not in operation.get("requestBody", {}).get("content", {}):
            continue
        for field, schema in _body_properties(path).items():
            if schema.get("nullable"):
                out.append((meta.name, field))
    return out


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


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


# ---- the fields that opted in -------------------------------------------------------------


def test_the_opt_in_list_is_exactly_this():
    """An addition has to pass the test in null_body_args.py and be listed here."""
    enabled = {meta.name: set(meta.null_body_args) for meta in TOOL_META.values() if meta.null_body_args}
    assert enabled == {
        "set_knowledge_base_document_interviews": {"interview_ids"},
        UPDATE_DIRECTORY: {"content_md"},
        UPDATE_INTERVIEW: {"candidate_notification_channel", "questions_random_subset", "pdf_export_auto_config"},
        "manage_media_file": {"focal_x", "focal_y", "poster_url"},
        DUPLICATE_POSITION: {"interview_available_till", "openings_total"},
    }


@pytest.mark.parametrize(("key", "meta"), [(k, m) for k, m in TOOL_META.items() if m.null_body_args])
def test_every_opted_in_field_is_real_and_named_in_the_hint(key, meta):
    properties = _body_properties(key[1])
    for name in meta.null_body_args:
        assert properties[name]["nullable"] is True, f"{meta.name}.{name} is not nullable in the API"
        assert name in meta.hint, f"{meta.name}: the hint must say what null does to {name}"


@pytest.mark.parametrize(("tool", "field", "value"), ENABLED)
@pytest.mark.asyncio
async def test_null_reaches_the_api(monkeypatch, tool, field, value):
    seen = _fake_upstream(monkeypatch, ANSWER[tool])
    result = await _call(tool, {**TARGET[tool], field: None})
    assert seen[0].method == "POST"
    assert seen[0].headers["content-type"].startswith("application/json")
    assert int(seen[0].headers["content-length"]) == len(seen[0].content)
    assert json.loads(seen[0].content) == {**TARGET[tool], field: None}
    assert result.structured_content == ANSWER[tool]


@pytest.mark.parametrize(("tool", "field", "value"), ENABLED)
@pytest.mark.asyncio
async def test_null_beside_another_field_keeps_both(monkeypatch, tool, field, value):
    seen = _fake_upstream(monkeypatch, ANSWER[tool])
    await _call(tool, {**TARGET[tool], "name": "Renamed", field: None})
    assert json.loads(seen[0].content) == {**TARGET[tool], "name": "Renamed", field: None}


@pytest.mark.parametrize(("tool", "field", "value"), ENABLED)
@pytest.mark.asyncio
async def test_a_value_is_sent_as_it_is(monkeypatch, tool, field, value):
    seen = _fake_upstream(monkeypatch, ANSWER[tool])
    await _call(tool, {**TARGET[tool], field: value})
    assert json.loads(seen[0].content) == {**TARGET[tool], field: value}


@pytest.mark.parametrize(("tool", "field", "value"), ENABLED)
@pytest.mark.asyncio
async def test_leaving_it_out_never_becomes_null(monkeypatch, tool, field, value):
    """The whole point of the partial update: an untouched field stays out of the body."""
    seen = _fake_upstream(monkeypatch, ANSWER[tool])
    await _call(tool, {**TARGET[tool], "name": "Renamed"})
    assert field not in json.loads(seen[0].content)


# ---- everything else stays left out -------------------------------------------------------


def _filler(schema: dict):
    """A value for a required argument, so that the call gets as far as a request."""
    if schema.get("enum"):
        return schema["enum"][0]
    kinds = schema.get("type", "string")
    kind = kinds if isinstance(kinds, str) else next(k for k in kinds if k != "null")
    return {"string": "x", "array": ["x"], "object": {}, "number": 1, "integer": 1, "boolean": True}[kind]


def _other_nullable_fields() -> list[tuple[str, str]]:
    enabled = {meta.name: meta.null_body_args for meta in TOOL_META.values()}
    return [(tool, field) for tool, field in _nullable_body_fields() if field not in enabled[tool]]


def test_the_sweep_covers_the_tools_that_matter():
    tools = {tool for tool, _ in _other_nullable_fields()}
    assert {UPDATE_INTERVIEW, UPDATE_DIRECTORY, "set_interview_state", "create_interview"} <= tools


@pytest.mark.parametrize(("tool", "field"), _other_nullable_fields())
@pytest.mark.asyncio
async def test_any_other_null_is_left_out(monkeypatch, tool, field):
    """A model skipping a field with null must not clear what is stored."""
    from fastmcp.exceptions import ToolError

    seen = _fake_upstream(monkeypatch, {})
    schema = (await _tools())[tool].parameters
    required = {name: _filler(schema["properties"][name]) for name in schema.get("required", []) if name != field}
    try:
        await _call(tool, {**required, field: None})
    except ToolError:
        pass  # the fake answer does not validate; only the request is under test
    assert seen, f"{tool}: no request was built, so this test proved nothing"
    assert field not in json.loads(seen[0].content or b"{}")


# ---- the other way to reach each state ----------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "field", "value"),
    [
        (UPDATE_INTERVIEW, "recruiter_profile_id", ""),
        (UPDATE_INTERVIEW, "knowledge_base_store_id", ""),
        (UPDATE_INTERVIEW, "interview_available_till", ""),
        (UPDATE_INTERVIEW, "country_availability", {}),
        (UPDATE_INTERVIEW, "hiring_for_company", {"name": None}),
        (UPDATE_INTERVIEW, "candidate_video_introduction", "hidden"),
        (UPDATE_INTERVIEW, "max_followups", 2),
        (UPDATE_INTERVIEW, "interview_conversation_speed", "normal"),
        (UPDATE_INTERVIEW, "coach_plan", "other"),
        (UPDATE_INTERVIEW, "custom_fields", {"cost_centre": None, "contract_type": "permanent"}),
        (UPDATE_DIRECTORY, "cover_image_url", ""),
        (UPDATE_DIRECTORY, "coach_plan", "other"),
        (UPDATE_DIRECTORY, "translations", {"sk": None, "de": {"name": "Verkauf", "content_md": None}}),
    ],
)
@pytest.mark.asyncio
async def test_the_alternative_a_description_names_reaches_the_api(monkeypatch, tool, field, value):
    """Blank strings, empty objects and nulls nested in an object all arrive untouched."""
    seen = _fake_upstream(monkeypatch, ANSWER[tool])
    await _call(tool, {**TARGET[tool], field: value})
    assert json.loads(seen[0].content) == {**TARGET[tool], field: value}


@pytest.mark.asyncio
async def test_a_null_inside_a_portal_document_reaches_the_api(monkeypatch):
    seen = _fake_upstream(monkeypatch, {})
    document = {"schema": "jobmojito.branding", "version": None}
    try:
        await _call("update_portal_configuration", {"documents": [document]})
    except Exception:
        pass  # the fake answer does not validate; only the request is under test
    assert json.loads(seen[0].content) == {"documents": [document]}


# ---- what the tools say -------------------------------------------------------------------

PROMISE = re.compile(
    r"\bnull (clears|unlinks|keeps|uses|disables|switches|removes|asks|is treated|to )"
    r"|Omit/null|Null = |sending null|Sending null|`translations: null`",
)


@pytest.mark.parametrize("tool", [UPDATE_INTERVIEW, UPDATE_DIRECTORY])
@pytest.mark.asyncio
async def test_only_an_opted_in_field_still_promises_a_null(tool):
    meta = next(m for m in TOOL_META.values() if m.name == tool)
    parameters = (await _tools())[tool].parameters["properties"]
    promising = {name for name, schema in parameters.items() if PROMISE.search(schema.get("description", ""))}
    # translations promises a null per language, and that one is nested, so it arrives
    assert promising - {"translations"} == set(meta.null_body_args)


@pytest.mark.parametrize("tool", [UPDATE_INTERVIEW, UPDATE_DIRECTORY])
@pytest.mark.asyncio
async def test_the_tool_text_stops_saying_null_clears_any_nullable_field(tool):
    description = (await _tools())[tool].description
    assert "sending null clears a nullable field" not in description
    assert "counts as not sent" in description


@pytest.mark.parametrize(
    ("tool", "field", "says"),
    [
        (UPDATE_INTERVIEW, "recruiter_profile_id", "An empty string clears it."),
        (UPDATE_INTERVIEW, "knowledge_base_store_id", "An empty string unlinks it."),
        (UPDATE_INTERVIEW, "interview_available_till", "An empty string keeps it always available."),
        (UPDATE_INTERVIEW, "country_availability", "An empty object {} = not used"),
        (UPDATE_INTERVIEW, "hiring_for_company", "An object with name null/blank means hiring for yourself"),
        (UPDATE_INTERVIEW, "candidate_video_introduction", "Send `hidden` to stop asking."),
        (UPDATE_INTERVIEW, "max_followups", "2 is the template default (Normal)."),
        (UPDATE_INTERVIEW, "interview_conversation_speed", "Omit to keep the current pace"),
        (UPDATE_INTERVIEW, "recording", "`none` switches it off; a null is ignored."),
        (UPDATE_INTERVIEW, "recording_full_session", "`none` switches it off; a null is ignored."),
        (UPDATE_INTERVIEW, "candidate_expectations_json", "regenerate_candidate_expectations"),
        (UPDATE_INTERVIEW, "coach_plan", "use `other` when no stage fits"),
        (UPDATE_INTERVIEW, "candidate_notification_channel", "null switches them off"),
        (UPDATE_INTERVIEW, "questions_random_subset", "null asks all questions"),
        (UPDATE_INTERVIEW, "pdf_export_auto_config", "null disables auto-export"),
        (UPDATE_DIRECTORY, "cover_image_url", "An empty string clears it."),
        (UPDATE_DIRECTORY, "coach_plan", "use `other` when no stage fits"),
        (UPDATE_DIRECTORY, "translations", "send null for each language"),
        (UPDATE_DIRECTORY, "content_md", "Sending null removes the custom page"),
    ],
)
@pytest.mark.asyncio
async def test_each_field_says_what_really_happens(tool, field, says):
    assert says in (await _tools())[tool].parameters["properties"][field]["description"]


@pytest.mark.asyncio
async def test_the_create_tools_keep_their_text():
    """On a create, null and omit are the same thing, so "Omit/null to disable" is true."""
    tools = await _tools()
    assert "Omit/null to disable." in tools["create_interview"].parameters["properties"]["recording"]["description"]
    # the explicit-questions create is create_interview with `questions` now
    assert "create_interview_from_questions" not in tools
    assert "questions" in tools["create_interview"].parameters["properties"]


def test_a_correction_never_contains_what_it_replaces():
    """The cached spec is prepared a second time: this would grow the text on every boot."""
    for meta in TOOL_META.values():
        for old, new in meta.description_fixes:
            assert old not in new, f"{meta.name}: {old!r}"


@pytest.mark.parametrize(("key", "meta"), [(k, m) for k, m in TOOL_META.items() if m.description_fixes])
def test_every_correction_finds_its_text_in_the_snapshot(key, meta):
    """Fails when the API rewords a sentence, which would bring the promise back."""
    operation = SNAPSHOT["paths"][key[1]]["post"]
    texts = [operation["description"], *(p.get("description", "") for p in _body_properties(key[1]).values())]
    for old, _new in meta.description_fixes:
        assert any(old in text for text in texts), f"{meta.name}: {old!r}"


# ---- apply_description_fixes --------------------------------------------------------------

# save_catalogue_directory: the create endpoint, whose action update replaced
# /catalogue-tag-update (hidden now, and without corrections of its own).
FIX_PATH = "/catalogue-tag-create"
PROMISE_TEXT = "Update: (null clears a nullable field; in `translations` null removes a language)."
COVER = "Cover image URL shown on the directory card."


def _spec(schema: dict, components: dict | None = None) -> dict:
    return {
        "paths": {
            FIX_PATH: {
                "parameters": [],
                "post": {
                    "description": PROMISE_TEXT,
                    "requestBody": {"content": {"application/json": {"schema": schema}}},
                },
            },
            "/not-a-path-item": "ignored",
            "/job-interview-get": {"get": {"description": "null clears it."}},
        },
        "components": {"schemas": components or {}},
    }


def test_a_correction_reaches_the_operation_and_a_field_behind_a_ref():
    schema = {"properties": {"cover_image_url": {"description": f"Cover. {COVER}"}, "x": {}, "y": True}}
    spec = _spec({"$ref": "#/components/schemas/Body"}, {"Body": schema})
    apply_description_fixes(spec)
    assert "counts as not sent" in spec["paths"][FIX_PATH]["post"]["description"]
    assert "null clears" not in spec["paths"][FIX_PATH]["post"]["description"]
    assert schema["properties"]["cover_image_url"]["description"] == (
        "Cover. Cover image URL for the directory card. An empty string clears it."
    )
    # the hidden update endpoint is not a tool, so nothing corrects its text
    hidden = {"paths": {"/catalogue-tag-update": {"post": {"description": PROMISE_TEXT}}}}
    apply_description_fixes(hidden)
    assert hidden["paths"]["/catalogue-tag-update"]["post"]["description"] == PROMISE_TEXT
    assert spec["paths"]["/job-interview-get"]["get"]["description"] == "null clears it."


def test_an_inline_body_is_corrected_too():
    schema = {"properties": {"cover_image_url": {"description": COVER}}}
    apply_description_fixes(_spec(schema))
    assert schema["properties"]["cover_image_url"]["description"].endswith("An empty string clears it.")


def test_preparing_twice_changes_nothing_and_warns_about_nothing_new(caplog):
    """A boot from the cache prepares a spec that is already corrected."""
    spec = json.loads(json.dumps(SNAPSHOT))
    with caplog.at_level(logging.WARNING, logger="jobmojito_mcp.openapi"):
        once = json.dumps(apply_description_fixes(spec))
        caplog.clear()
        twice = json.dumps(apply_description_fixes(spec))
    assert once == twice
    assert not caplog.records


def test_wording_the_api_changed_is_logged(caplog):
    spec = _spec({"properties": {"x": {"description": "Reworded."}}})
    with caplog.at_level(logging.WARNING, logger="jobmojito_mcp.openapi"):
        apply_description_fixes(spec)
    assert "save_catalogue_directory" in caplog.text
    assert COVER in caplog.text


def test_an_operation_without_a_json_body_is_only_checked_for_its_own_text(caplog):
    spec = {"paths": {FIX_PATH: {"post": {"description": "Plain."}}}}
    with caplog.at_level(logging.WARNING, logger="jobmojito_mcp.openapi"):
        apply_description_fixes(spec)
    assert spec["paths"][FIX_PATH]["post"]["description"] == "Plain."
    assert "not found" in caplog.text


def test_a_body_reference_to_nothing_is_not_an_error():
    spec = _spec({"$ref": "#/components/schemas/Gone"})
    spec.pop("components")
    apply_description_fixes(spec)
    assert "counts as not sent" in spec["paths"][FIX_PATH]["post"]["description"]


def test_a_spec_without_a_description_on_a_target_is_skipped():
    spec = _spec({"properties": {"x": {"description": 3}}})
    spec["paths"][FIX_PATH]["post"].pop("description")
    apply_description_fixes(spec)
    assert "description" not in spec["paths"][FIX_PATH]["post"]
