"""Smoke tests for the JobMojito MCP server.

Run with:  ENABLE_AUTH=false pytest -q

These avoid any network by pointing the OpenAPI URL at an unreachable host so the
loader falls back to the committed snapshot.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
# Keep tests hermetic/offline regardless of a local .env: disable the optional
# external doc sources and the Mintlify federation. (load_dotenv uses
# override=False, so these win.)
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

EXPECTED_API_TOOLS = {
    "generate_interview_report", "get_interview_definition", "set_interview_state",
    "request_another_interview_attempt", "generate_interview_url",
    "get_interview_result_details", "register_users_for_interview",
    "create_interview", "create_interview_from_questions", "update_interview",
    "create_catalogue_directory", "update_catalogue_directory",
    "list_catalogue_directories", "get_catalogue_directory",
    "upload_knowledge_base_document", "list_interviews", "list_candidates",
    "list_interview_results", "list_avatars", "list_sub_merchants",
    "get_merchant_analytics", "get_merchant_status",
}
EXPECTED_DOC_TOOLS = {"search_documentation", "get_documentation"}
# Endpoints intentionally excluded from the MCP (must NOT appear as tools).
IGNORED_TOOLS = {
    "invite_users", "create_interview_for_candidate",
    "upsert_pre_screening", "pre_screen_resume_text", "pre_screen_resume_binary",
}


async def _tool_names(mcp) -> set[str]:
    tools = await mcp.list_tools()
    return {t.name for t in tools}


@pytest.mark.asyncio
async def test_all_tools_present():
    import server

    names = await _tool_names(server.mcp)
    missing = (EXPECTED_API_TOOLS | EXPECTED_DOC_TOOLS) - names
    assert not missing, f"missing tools: {missing}"
    assert len(EXPECTED_API_TOOLS) == 22
    # Ignored endpoints must not be exposed.
    assert not (IGNORED_TOOLS & names), f"ignored tools leaked: {IGNORED_TOOLS & names}"


@pytest.mark.asyncio
async def test_no_admin_ui_link_tool():
    """The admin-link tool was removed; instructions point to the docs guide."""
    import server

    names = await _tool_names(server.mcp)
    assert "get_admin_ui_link" not in names
    # Instructions must link the identifiers/admin-links guide (which carries the
    # id-field map and the admin URL patterns) rather than embedding it inline.
    assert "mcp/identifiers" in server.INSTRUCTIONS


@pytest.mark.asyncio
async def test_merchant_selection_tools():
    import server

    names = await _tool_names(server.mcp)
    assert "list_my_merchants" in names
    assert "jobmojito_configuration" in names  # searchable merchant picker MCP App
    assert "setup" not in names and "choose" not in names  # renamed


# ---------------------------------------------------------------------------
# Curated page sizes
#
# `list_avatars` returned ~247,000 characters on a plain call — over twice
# MAX_TOOL_RESULT_CHARS — because avatar rows carry three long signed URLs and
# the API's default page size is 50. The tool therefore failed on its first call
# with default arguments, which reads to a user as "the tool is broken".
#
# Two traps these pin down:
#   1. An MCP result costs ~2x the API's JSON — the payload is sent in BOTH
#      `content` (as text) and `structuredContent`, and both count.
#   2. An OpenAPI `default` is advertised to the model but never sent:
#      FastMCP's RequestDirector only serialises arguments that were supplied.
#      So the schema default and CuratedDefaultsMiddleware must agree, or the
#      tool description promises a page size the server doesn't deliver.
# ---------------------------------------------------------------------------


def test_curated_default_is_both_advertised_and_sent():
    """The spec default and the middleware default must be the same number.

    They are written in one place (`TOOL_META.param_defaults`) and consumed by
    two — `openapi_loader.apply_param_defaults` writes the schema the model
    reads, `middleware.CuratedDefaultsMiddleware` puts the value on the wire.
    If they ever diverge, the tool advertises one page size and fetches another.
    """
    import json
    import pathlib

    from naming import curated_defaults
    from openapi_loader import apply_param_defaults

    spec = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text()
    )
    apply_param_defaults(spec)

    advertised = {
        p["name"]: p["schema"]["default"]
        for p in spec["paths"]["/merchant-avatar-list"]["get"]["parameters"]
        if p["name"] == "limit"
    }
    sent = curated_defaults()["list_avatars"]
    assert advertised["limit"] == sent["limit"]
    # 50 rows was ~247,000 chars; the replacement must leave real headroom.
    assert sent["limit"] <= 20


def test_param_default_outside_spec_bounds_is_refused():
    """A default the API would 422 on is worse than the one we're replacing."""
    from openapi_loader import apply_param_defaults

    spec = {
        "paths": {
            "/merchant-avatar-list": {
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                        }
                    ]
                }
            }
        }
    }
    # TOOL_META asks for 15, which exceeds this (hypothetical) maximum of 10.
    apply_param_defaults(spec)
    schema = spec["paths"]["/merchant-avatar-list"]["get"]["parameters"][0]["schema"]
    assert schema["default"] == 5, "out-of-range override must be ignored, not applied"


def test_curated_defaults_fill_only_absent_arguments():
    """An explicit page size from the model always wins over the default."""
    import asyncio
    import types

    from middleware import CuratedDefaultsMiddleware

    middleware = CuratedDefaultsMiddleware({"list_avatars": {"limit": 15}})

    async def run(arguments):
        context = types.SimpleNamespace(
            message=types.SimpleNamespace(name="list_avatars", arguments=arguments)
        )

        async def call_next(_):
            return None

        await middleware.on_call_tool(context, call_next)
        return arguments

    assert asyncio.run(run({}))["limit"] == 15
    assert asyncio.run(run({"limit": 100}))["limit"] == 100
    assert asyncio.run(run({"limit": None}))["limit"] == 15
    # Untouched tools keep their arguments exactly as sent.
    other = {"merchant_id": "x"}
    assert asyncio.run(run(other)) == {"merchant_id": "x", "limit": 15}


def test_oversize_guard_suggests_a_limit_that_actually_fits():
    """The old fixed advice ("try limit=25") also overflowed for avatars.

    25 avatar rows cost ~123,000 characters — still over the 120,000 budget — so
    following the guidance produced a second identical failure. The suggestion is
    now solved from the observed size instead of guessed.
    """
    from middleware import ResultSizeGuardMiddleware

    guard = ResultSizeGuardMiddleware(120_000)
    # 50 rows produced 246,931 chars => ~4,939 per row.
    suggested = guard._suggested_limit({"limit": 50}, 246_931)
    assert suggested is not None
    assert suggested * (246_931 / 50) <= 120_000, "the suggestion must fit the budget"
    assert suggested < 50, "must be smaller than the page size that just failed"

    # No `limit` argument to reason from -> generic advice, no bogus number.
    assert guard._suggested_limit({}, 246_931) is None
    assert guard._suggested_limit(None, 246_931) is None
    assert "limit=10" in guard._advice({}, 246_931)


def test_relax_nullable_schemas():
    import jsonschema

    from openapi_loader import relax_nullable_schemas

    spec = {
        "paths": {},
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                }
            }
        },
    }
    relaxed = relax_nullable_schemas(spec)
    item = relaxed["components"]["schemas"]["Item"]
    name_type = item["properties"]["name"]["type"]
    assert "null" in name_type and "string" in name_type
    # `required` is untouched (inputs still require their fields).
    assert item["required"] == ["name"]
    # The real-world failure ("None is not of type 'string'") now validates.
    jsonschema.validate(None, {"type": name_type})


def test_real_spec_nullable_string_fields():
    """Regression guard for the 3.0-`nullable` trap.

    The JobMojito spec is tagged OpenAPI 3.1.0 but declares fields like `emoji`
    and `billing_single_position_end_at` as `type: "string"` + `nullable: true`
    (a 3.0-ism that JSON Schema ignores under 3.1). The API returns null for
    them, which—without relaxing—raises "None is not of type 'string'". This
    asserts relax_nullable_schemas turns every occurrence into a `[..., "null"]`
    union on the committed snapshot, so the whole error class stays fixed.
    """
    import json
    from pathlib import Path

    from openapi_loader import relax_nullable_schemas

    snapshot = Path(__file__).resolve().parent.parent / "data" / "openapi.snapshot.json"
    if not snapshot.exists():
        pytest.skip("openapi snapshot not present")

    spec = json.loads(snapshot.read_text(encoding="utf-8"))
    targets = {"emoji", "billing_single_position_end_at"}

    def occurrences(node):
        found = []
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for name in targets:
                    ps = props.get(name)
                    if isinstance(ps, dict) and "type" in ps:
                        found.append((name, ps["type"]))
            for value in node.values():
                found += occurrences(value)
        elif isinstance(node, list):
            for value in node:
                found += occurrences(value)
        return found

    before = occurrences(spec)
    # Sanity: the snapshot really does contain these typed fields.
    assert {n for n, _ in before} == targets, f"snapshot changed: found {before}"

    relaxed = relax_nullable_schemas(spec)
    after = occurrences(relaxed)
    for name, type_ in after:
        assert isinstance(type_, list) and "null" in type_, (
            f"{name} not null-accepting after relax: {type_!r}"
        )


def test_relax_nullable_enum_allows_null():
    """A nullable enum field must accept null on BOTH type and enum checks.

    Widening `type` to include "null" isn't enough: jsonschema validates `enum`
    independently, so a null value fails ("None is not one of [...]") unless null
    is also added to the enum. Guards nullable enum RESPONSE fields.
    """
    import jsonschema

    from openapi_loader import relax_nullable_schemas

    spec = {
        "paths": {},
        "components": {
            "schemas": {
                "R": {
                    "type": "object",
                    "properties": {
                        "recommendation": {
                            "type": "string",
                            "enum": ["ai_accept", "ai_reject"],
                            "nullable": True,
                        }
                    },
                }
            }
        },
    }
    relaxed = relax_nullable_schemas(spec)
    field = relaxed["components"]["schemas"]["R"]["properties"]["recommendation"]
    assert "null" in field["type"]
    assert None in field["enum"]
    # The real-world failure ("None is not one of [...]") now validates.
    jsonschema.validate(None, {"type": field["type"], "enum": field["enum"]})


def test_llms_txt_parser():
    from docs_tools import _parse_llms_txt

    sample = """# JobMojito

## Docs
- [Welcome](https://developer.jobmojito.com/welcome-1018963m0.md):
- Webhooks [Creating webhooks](https://developer.jobmojito.com/creating-webhooks-1021007m0.md): how to

## API Docs
- Actions API [Create interview](https://developer.jobmojito.com/create-interview-16953824e0.md): Creates an interview
"""
    entries = _parse_llms_txt(sample)
    assert len(entries) == 3
    titles = {e.title for e in entries}
    assert "Welcome" in titles and "Creating webhooks" in titles
    assert all(e.source == "developer" for e in entries)


def test_help_html_parser():
    from docs_tools import _parse_help_html

    html = (
        '<a href="https://help.jobmojito.com/collections/9654934-recruiter">Recruiter</a>'
        '<a href="https://help.jobmojito.com/articles/4692316-avatars">Avatars</a>'
        '<a href="https://example.com/articles/x">External</a>'
    )
    entries = _parse_help_html(html, "https://help.jobmojito.com")
    urls = {e.url for e in entries}
    assert "https://help.jobmojito.com/collections/9654934-recruiter" in urls
    assert "https://help.jobmojito.com/articles/4692316-avatars" in urls
    assert not any("example.com" in u for u in urls)  # other domains excluded


def test_doc_search_scoring():
    from docs_tools import DocEntry, _score, _tokenize

    e = DocEntry(title="Creating webhooks", url="x", source="developer",
                 description="how to set up webhooks")
    assert _score(e, _tokenize("how do I create a webhook")) > 0


def test_featurebase_html_to_text():
    from featurebase import html_to_text

    body = "<h1>Title</h1><p>First para.</p><ul><li>one</li><li>two</li></ul>"
    text = html_to_text(body)
    assert "Title" in text and "First para." in text
    assert "- one" in text and "- two" in text
    assert "<" not in text  # tags stripped
    assert html_to_text(None) == ""


def test_featurebase_disabled_by_default():
    import featurebase

    # No API key in the test env → REST source disabled, HTML fallback used.
    assert featurebase.is_enabled() is False


def test_developer_docs_token_endpoint_derivation():
    from config import settings

    if settings.developer_docs_mcp_url:
        assert settings.developer_docs_token_endpoint == (
            settings.developer_docs_mcp_url.rstrip("/") + "/oauth/token"
        )
    else:
        assert settings.developer_docs_token_endpoint is None


def test_developer_docs_federation_off_when_url_empty():
    from config import settings

    # URL forced empty in the test env → federation disabled, no auth.
    assert settings.federate_developer_docs is False
    assert settings.developer_docs_uses_auth is False


def test_mintlify_parse_items():
    from mintlify import _parse_items

    text = (
        "Here are results:\n"
        "[Create interview](https://developer.jobmojito.com/create-interview)\n"
        "[Webhooks](https://developer.jobmojito.com/creating-webhooks)\n"
    )
    items = _parse_items(text, limit=8)
    urls = {i["url"] for i in items}
    assert "https://developer.jobmojito.com/create-interview" in urls
    assert all(i["source"] == "developer" for i in items)
    assert len(items) == 2


def test_docs_rank():
    from docs_tools import DocEntry, _rank

    entries = [
        DocEntry(title="Creating webhooks", url="u1", source="developer",
                 description="set up webhooks"),
        DocEntry(title="Avatars", url="u2", source="developer", description="templates"),
    ]
    ranked = _rank(entries, "how to create a webhook", limit=5)
    assert ranked and ranked[0]["url"] == "u1"


def test_mintlify_token_caching():
    import time

    from mintlify import MintlifyClientCredentialsAuth

    auth = MintlifyClientCredentialsAuth(
        token_url="https://developer.jobmojito.com/authed/mcp/oauth/token",
        client_id="cid",
        client_secret="secret",
    )
    assert auth._token_valid() is False  # no token yet

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "abc123", "expires_in": 1209600}

    auth._store_token(_Resp())
    assert auth._access_token == "abc123"
    assert auth._token_valid() is True
    assert auth._expiry > time.time()


# ---------------------------------------------------------------------------
# Response views + the protocol's duplicate payload copy
#
# `get_interview_result_details` returned 164,779 characters on a plain call and
# was refused. Two separate causes, and both are pinned here:
#
#   1. One interview result is a WIDE record — the per-answer
#      `answer_assessment_raw_data` blobs dominate it — and pagination cannot
#      narrow a single record. Hence the MCP-only `view` argument.
#   2. An MCP result carries the same JSON twice, as `content` text AND as
#      `structuredContent`, so ~82,000 characters of data costs ~164,000 on the
#      wire. The guard now drops the duplicate when that is the difference
#      between answering and failing.
# ---------------------------------------------------------------------------


def _snapshot_spec():
    import json
    import pathlib

    return json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "data/openapi.snapshot.json").read_text()
    )


def test_client_result_ceilings_are_documented_and_not_exceeded():
    """150,000 is Claude.ai's cap — the strictest published ceiling we target.

    Raising it past a real client limit doesn't buy anything: the host truncates
    or drops the result and the user sees a broken tool instead of a clear error.
    """
    from config import load_settings

    assert load_settings().max_tool_result_chars == 150_000


def test_view_argument_is_injected_into_the_schema():
    from naming import VIEW_PARAM_NAME
    from openapi_loader import inject_view_params

    spec = inject_view_params(_snapshot_spec())
    params = {
        p["name"]: p for p in spec["paths"]["/job-interview-details"]["get"]["parameters"]
    }
    assert VIEW_PARAM_NAME in params, "the model cannot ask for a view that isn't advertised"
    schema = params[VIEW_PARAM_NAME]["schema"]
    assert schema["enum"] == ["summary", "standard", "full"]
    assert schema["default"] == "standard"


def test_view_default_advertised_matches_the_default_applied():
    """The schema default and the runtime fallback must be the same projection.

    FastMCP never transmits an OpenAPI `default` the model omitted, so the
    advertised value is documentation only — the middleware's fallback is what
    actually decides. If they drift, the tool promises one shape and returns
    another.
    """
    from naming import TOOL_META, response_view_rules, view_parameter_schema

    meta = TOOL_META[("GET", "/job-interview-details")]
    advertised = view_parameter_schema(meta)["schema"]["default"]
    applied = response_view_rules()["get_interview_result_details"].default
    assert advertised == applied == "standard"


def test_view_argument_never_reaches_the_upstream_api():
    """`view` is ours. If it survives into the arguments, it goes on the wire.

    The Edge Functions' zod query schemas are non-strict and would drop it rather
    than 422, but relying on that would make this a silent contract with another
    repo. Strip it here.
    """
    import asyncio
    import types

    from middleware import ResponseViewMiddleware
    from naming import response_view_rules

    middleware = ResponseViewMiddleware(response_view_rules())
    seen = {}

    async def run(arguments):
        context = types.SimpleNamespace(
            message=types.SimpleNamespace(
                name="get_interview_result_details", arguments=arguments
            )
        )

        async def call_next(_):
            seen["arguments"] = dict(arguments)
            return None

        await middleware.on_call_tool(context, call_next)

    asyncio.run(run({"interview_result_id": "x", "view": "summary"}))
    assert seen["arguments"] == {"interview_result_id": "x"}

    # A tool with no views is passed through untouched.
    asyncio.run(run({"interview_result_id": "x"}))
    assert seen["arguments"] == {"interview_result_id": "x"}


def test_prune_fields_walks_arrays_and_ignores_missing_paths():
    from middleware import prune_fields

    data = {
        "score": 5,
        "transcript": [
            {"answer": "a", "answer_assessment_raw_data": {"big": "blob"}},
            {"answer": "b"},  # field legitimately absent
        ],
        "nested": {"keep": 1, "drop": 2},
    }
    prune_fields(data, ("transcript[].answer_assessment_raw_data", "nested.drop", "absent[].x"))
    assert data == {
        "score": 5,
        "transcript": [{"answer": "a"}, {"answer": "b"}],
        "nested": {"keep": 1},
    }


@pytest.mark.asyncio
async def test_every_view_still_validates_against_the_output_schema():
    """Pruning must never produce a result the SDK then rejects.

    Output validation stays ON, so a view that drops a field the schema marks
    required would turn a large-but-working call into a -32602. Build a response
    carrying every declared field, prune it through each view, and validate.
    """
    import jsonschema

    import server
    from middleware import prune_fields
    from naming import response_view_rules

    tools = await server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_interview_result_details")
    schema = tool.output_schema
    assert schema, "output validation is on; this test is meaningless without a schema"

    spec = _snapshot_spec()
    item_props = spec["components"]["schemas"]["JobInterviewDetailsResponse"][
        "properties"
    ]["transcript"]["items"]["properties"]
    response = {
        "score": 7.5,
        "score_text": "good",
        "status": "completed",
        "ai_analysis": "overall",
        "transcript": [{name: None for name in item_props}],
    }

    rules = response_view_rules()["get_interview_result_details"]
    for view in ("summary", "standard", "full"):
        import copy

        pruned = prune_fields(copy.deepcopy(response), rules.paths_for(view))
        jsonschema.validate(instance=pruned, schema=schema)

    # And the views actually differ, or none of this is doing anything.
    assert len(rules.paths_for("summary")) > len(rules.paths_for("standard")) > 0
    assert rules.paths_for("full") == ()
    # An unknown view narrows to the default rather than failing the call.
    assert rules.paths_for("nonsense") == rules.paths_for("standard")
    assert rules.paths_for(None) == rules.paths_for("standard")


@pytest.mark.asyncio
async def test_oversized_result_drops_the_duplicate_copy_instead_of_failing():
    """The protocol's second copy must not be what refuses a result that fits."""
    import types

    from fastmcp.tools.base import ToolResult

    from middleware import ResultSizeGuardMiddleware

    guard = ResultSizeGuardMiddleware(150_000)
    payload = {"result": "x" * 80_000}  # ~80k of data, ~160k once duplicated

    context = types.SimpleNamespace(
        message=types.SimpleNamespace(name="get_interview_result_details", arguments={})
    )

    async def call_next(_):
        return ToolResult(structured_content=payload)

    original = await call_next(None)
    structured_chars, content_chars = guard._measure_parts(original)
    assert structured_chars + content_chars > 150_000, "fixture must exceed the budget"
    assert structured_chars < 150_000, "fixture must fit once de-duplicated"

    result = await guard.on_call_tool(context, call_next)
    assert result.structured_content == payload, "no data may be removed"
    assert len(result.content) == 1
    assert "structuredContent" in result.content[0].text
    assert sum(guard._measure_parts(result)) <= 150_000


@pytest.mark.asyncio
async def test_result_over_budget_on_its_own_still_raises():
    """De-duplication is a last resort, not a way to hide an oversized payload."""
    import types

    from fastmcp.exceptions import ToolError
    from fastmcp.tools.base import ToolResult

    from middleware import ResultSizeGuardMiddleware

    guard = ResultSizeGuardMiddleware(150_000)

    context = types.SimpleNamespace(
        message=types.SimpleNamespace(name="list_avatars", arguments={"limit": 50})
    )

    async def call_next(_):
        return ToolResult(structured_content={"result": "x" * 200_000})

    with pytest.raises(ToolError) as excinfo:
        await guard.on_call_tool(context, call_next)
    message = str(excinfo.value)
    assert "exceeds this server's 150,000-character result limit" in message
    # The model is told the doubled figure isn't the whole story, and is given a
    # page size solved from the payload rather than from the doubled number.
    assert "sends the payload twice" in message
    assert "limit=" in message


@pytest.mark.asyncio
async def test_results_within_budget_keep_both_copies():
    """Nothing changes for the ordinary case — the text copy is still sent."""
    import types

    from fastmcp.tools.base import ToolResult

    from middleware import ResultSizeGuardMiddleware

    guard = ResultSizeGuardMiddleware(150_000)
    context = types.SimpleNamespace(
        message=types.SimpleNamespace(name="list_languages", arguments={})
    )

    async def call_next(_):
        return ToolResult(structured_content={"result": "small"})

    result = await guard.on_call_tool(context, call_next)
    assert "small" in result.content[0].text


@pytest.mark.asyncio
async def test_view_and_dedup_work_together_through_the_real_middleware_stack(monkeypatch):
    """The three fixes only work if they run in the right order.

    Registration order is execution order on the way IN, which reverses on the
    way out — so `ResponseViewMiddleware` must be registered LAST to be the first
    to touch the result, or the size guard measures a payload the client never
    receives. That ordering is invisible in unit tests; this exercises the built
    server end to end against a mocked upstream.
    """
    import httpx
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    blob = {"words": [{"w": f"w{i}", "phonemes": ["a"] * 20} for i in range(120)]}
    turn = {
        "id": "t",
        "is_answer": True,
        "question_asked": "Tell me about yourself.",
        "answer": "I have ten years of experience. " * 40,
        "ai_analysis": "Analysis. " * 60,
        "ai_analysis_recruiter": "Recruiter analysis. " * 60,
        "score": 7.0,
        "answer_assessment_raw_data": blob,
        "external_data": {"x": "y" * 500},
    }
    upstream = {
        "score": 7.0,
        "status": "completed",
        "transcript": [dict(turn, id=f"t{i}") for i in range(26)],
    }
    seen_urls = []

    async def fake_send(self, request, **kwargs):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=upstream, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    args = {"interview_result_id": "93c98d21-e04d-4a84-9afa-ed154cf73636"}

    async with Client(server.mcp) as client:
        # 1. Omitting `view` applies the default projection, and the argument is
        #    never sent upstream.
        default_result = await client.call_tool("get_interview_result_details", args)
        assert "view=" not in seen_urls[-1]
        first_turn = default_result.structured_content["transcript"][0]
        assert "answer_assessment_raw_data" not in first_turn
        assert "ai_analysis" in first_turn, "`standard` keeps per-answer analysis"

        # 2. The result now fits only because the duplicate copy was dropped.
        structured = len(json.dumps(default_result.structured_content))
        content = sum(len(b.text) for b in default_result.content if hasattr(b, "text"))
        assert structured * 2 > 150_000, "fixture must be big enough to need de-duplication"
        assert content < 1_000, "the duplicate text copy should have been replaced by a note"
        assert structured + content <= 150_000

        # 3. `summary` narrows further and needs no de-duplication.
        summary = await client.call_tool(
            "get_interview_result_details", {**args, "view": "summary"}
        )
        assert "view=" not in seen_urls[-1]
        assert "ai_analysis" not in summary.structured_content["transcript"][0]
        assert len(json.dumps(summary.structured_content)) < structured

        # 4. `full` is honestly refused rather than silently truncated.
        with pytest.raises(ToolError, match="exceeds this server's 150,000-character"):
            await client.call_tool("get_interview_result_details", {**args, "view": "full"})


def test_injected_view_param_is_refreshed_not_duplicated_from_cache():
    """The runtime cache stores the PREPARED spec, so injection re-runs over its own output.

    Two failure modes if this isn't handled: a duplicate `view` parameter, or —
    worse — a server booting from cache silently keeping the view definitions
    that were current when the cache was written.
    """
    from naming import VIEW_PARAM_NAME
    from openapi_loader import inject_view_params

    spec = inject_view_params(_snapshot_spec())
    # Simulate a cache written before the views were edited.
    cached = next(
        p
        for p in spec["paths"]["/job-interview-details"]["get"]["parameters"]
        if p["name"] == VIEW_PARAM_NAME
    )
    cached["schema"]["enum"] = ["stale"]
    cached["schema"]["default"] = "stale"

    inject_view_params(spec)
    params = [
        p
        for p in spec["paths"]["/job-interview-details"]["get"]["parameters"]
        if p["name"] == VIEW_PARAM_NAME
    ]
    assert len(params) == 1, "re-preparing a cached spec must not duplicate the parameter"
    assert params[0]["schema"]["enum"] == ["summary", "standard", "full"]


def test_a_real_api_view_parameter_wins_over_the_injected_one():
    """If JobMojito ever ships its own `view`, ours must get out of the way."""
    from naming import VIEW_PARAM_NAME
    from openapi_loader import inject_view_params

    spec = _snapshot_spec()
    api_param = {
        "name": VIEW_PARAM_NAME,
        "in": "query",
        "required": False,
        "schema": {"type": "string", "description": "The API's own parameter."},
    }
    spec["paths"]["/job-interview-details"]["get"]["parameters"].append(api_param)

    inject_view_params(spec)
    params = [
        p
        for p in spec["paths"]["/job-interview-details"]["get"]["parameters"]
        if p["name"] == VIEW_PARAM_NAME
    ]
    assert params == [api_param], "an API parameter must never be shadowed or removed"
