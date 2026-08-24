"""Smoke tests for the JobMojito MCP server.

Run with:  ENABLE_AUTH=false pytest -q

These avoid any network by pointing the OpenAPI URL at an unreachable host so the
loader falls back to the committed snapshot.
"""

from __future__ import annotations

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
