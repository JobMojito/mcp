"""What every tool sends upstream, and what it does with a refusal.

Run with:  ENABLE_AUTH=false pytest -q tests/test_tool_security.py

Each tool is called through FastMCP the way a client calls it, with the caller's
token in the MCP auth context and a minimal valid argument set built from the
tool's own input schema. The network is replaced at the transport, below the
httpx auth flow, so the request checked here is the one that would leave the
process: the caller's bearer exactly, ``X-JobMojito-Client: mcp``, no other
credential, the mapped endpoint and method. A 401 or 403 must come back as a tool
error carrying no data.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
from urllib.parse import urlsplit

import httpx2
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

from naming import TOOL_META, is_ignored  # noqa: E402

CALLER = "eyJcaller.token-of-the-signed-in-user.sig"
DEV_TOKEN = "dev-fallback-token-must-not-be-sent"
EVIL = "https://evil.test/steal?x=../../admin"

# (name, method, path) of every tool generated from the API
API_TOOLS = sorted(
    (meta.name, method, path) for (method, path), meta in TOOL_META.items() if not is_ignored(method, path)
)
# Headers a request to the API may carry; anything else is a credential or a leak.
ALLOWED_HEADERS = {
    "accept", "accept-encoding", "apikey", "authorization", "connection", "content-length",
    "content-type", "host", "user-agent", "x-jobmojito-client",
}
# Argument names that would let a caller pick the request target or credentials.
TARGET_PARAMS = {
    "url", "uri", "path", "method", "http_method", "verb", "endpoint", "base_url", "host",
    "api_path", "route", "headers", "authorization", "bearer", "apikey", "api_key",
}
# Writes the annotations must never let a client run without asking.
IRREVERSIBLE = {
    "merge_candidates", "manage_candidate_tag", "manage_segment", "manage_meeting_slot",
    "request_candidate_documents", "update_document_request", "manage_media_file",
    "set_candidate_tags", "add_candidate_note", "save_task", "update_position_step",
    "set_position_process", "invite_candidates",
}


# ---- the fake network ---------------------------------------------------------------------


class Upstream:
    """Answers every request at the transport; records what would have left the process."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = 200
        self.body: object = {}
        self.by_url: dict[str, tuple[int, object]] = {}

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        for fragment, (status, body) in self.by_url.items():
            if fragment in str(request.url):
                return httpx2.Response(status, json=body, request=request)
        return httpx2.Response(self.status, json=self.body, request=request)


@pytest.fixture
def upstream(monkeypatch):
    """The caller is signed in; the dev fallback token is set and must lose to them."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    import upstream as upstream_module
    from fastmcp.server.auth.auth import AccessToken
    from session_verifier import rejected_tokens

    fake = Upstream()
    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    monkeypatch.setattr(
        upstream_module, "settings", dataclasses.replace(upstream_module.settings, dev_bearer_token=DEV_TOKEN)
    )
    reset = auth_context_var.set(AuthenticatedUser(AccessToken(token=CALLER, client_id="test-client", scopes=[])))
    rejected_tokens.clear()
    yield fake
    auth_context_var.reset(reset)
    rejected_tokens.clear()


async def _listed():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


async def _call(name: str, arguments: dict):
    from fastmcp import Client

    import server

    async with Client(server.mcp) as client:
        return await client.call_tool(name, arguments, raise_on_error=False)


# ---- a minimal valid argument set from the schema -----------------------------------------


def _resolve(schema: dict, defs: dict) -> dict:
    while isinstance(schema, dict) and "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _value(schema: dict, defs: dict):
    schema = _resolve(schema, defs)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, defs).get("type") != "null"]
            return _value(options[0], defs)
    if "allOf" in schema:
        return _value(schema["allOf"][0], defs)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return next(v for v in schema["enum"] if v is not None)
    kind = schema.get("type", "string")
    if isinstance(kind, list):
        kind = next(k for k in kind if k != "null")
    if kind == "string":
        return {
            "uuid": "11111111-1111-4111-8111-111111111111",
            "email": "a@b.co",
            "date-time": "2026-11-08T10:00:00Z",
            "date": "2026-11-08",
            "uri": "https://example.org/x",
        }.get(schema.get("format"), "x" * max(1, schema.get("minLength", 1)))
    if kind in ("integer", "number"):
        return schema.get("minimum", 1)
    if kind == "boolean":
        return True
    if kind == "array":
        return [_value(schema.get("items", {}), defs) for _ in range(max(1, schema.get("minItems", 1)))]
    if kind == "object":
        required = schema.get("required", [])
        return {k: _value(v, defs) for k, v in schema.get("properties", {}).items() if k in required}
    return "x"


def _minimal_arguments(parameters: dict) -> dict:
    defs = parameters.get("$defs", {})
    return {name: _value(parameters["properties"][name], defs) for name in parameters.get("required", [])}


# ---- what a request may carry -------------------------------------------------------------


def _secret_settings() -> dict[str, str]:
    """Every configured credential except the public anon key: none may reach the API."""
    from config import settings

    out = {}
    for field in dataclasses.fields(settings):
        value = getattr(settings, field.name)
        if field.name == "supabase_anon_key" or not isinstance(value, str) or len(value) < 8:
            continue
        if any(word in field.name for word in ("key", "secret", "token", "password")):
            out[field.name] = value
    return out


def _jwt_role(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    return payload.get("role") if isinstance(payload, dict) else None


def _assert_carries_only_the_caller(request: httpx2.Request, where: str) -> None:
    from config import settings

    headers = request.headers
    assert headers.get("authorization") == f"Bearer {CALLER}", f"{where}: not the caller's bearer"
    assert len(headers.get_list("authorization")) == 1, f"{where}: more than one Authorization"
    assert headers.get("x-jobmojito-client") == "mcp", f"{where}: X-JobMojito-Client is not mcp"
    extra = set(headers.keys()) - ALLOWED_HEADERS
    assert not extra, f"{where}: unexpected headers {sorted(extra)}"
    apikey = headers.get("apikey")
    if apikey is not None:
        # only the public anon key, never a service-role or secret key (value not printed)
        assert apikey == settings.supabase_anon_key, f"{where}: apikey is not the configured anon key"
        assert not apikey.startswith("sb_secret_"), f"{where}: apikey is a secret key"
        assert _jwt_role(apikey) != "service_role", f"{where}: apikey is the service-role key"
    wire = " ".join([str(request.url), request.content.decode("utf-8", "replace"), *headers.values()])
    assert DEV_TOKEN not in wire, f"{where}: the dev fallback token was sent"
    for name, secret in _secret_settings().items():
        assert secret not in wire, f"{where}: settings.{name} reached the request"


def _assert_mapped_endpoint(request: httpx2.Request, method: str, path: str, where: str) -> None:
    from config import settings

    base = urlsplit(settings.api_base_url)
    url = request.url
    assert request.method == method, f"{where}: {request.method} instead of {method}"
    assert (url.scheme, url.host) == (base.scheme, base.hostname), f"{where}: sent to {url.host}"
    assert url.path == base.path.rstrip("/") + path, f"{where}: sent to {url.path}"
    assert url.fragment == "", where
    assert not url.username and not url.password, where


# ---- the sweep over every generated tool --------------------------------------------------


def test_the_sweep_covers_every_generated_tool():
    assert len(API_TOOLS) == 67
    assert {"merge_candidates", "get_candidate", "run_report", "manage_media_file"} <= {n for n, _, _ in API_TOOLS}


@pytest.mark.parametrize(("name", "method", "path"), API_TOOLS, ids=[n for n, _, _ in API_TOOLS])
@pytest.mark.asyncio
async def test_a_call_sends_only_the_callers_token_to_its_endpoint(upstream, name, method, path):
    tool = (await _listed())[name]
    await _call(name, _minimal_arguments(tool.parameters))  # {} does not validate everywhere: not under test
    assert len(upstream.requests) == 1, f"{name}: {len(upstream.requests)} requests"
    request = upstream.requests[0]
    _assert_mapped_endpoint(request, method, path, name)
    _assert_carries_only_the_caller(request, name)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(("name", "method", "path"), API_TOOLS, ids=[n for n, _, _ in API_TOOLS])
@pytest.mark.asyncio
async def test_a_refusal_is_a_tool_error_with_no_data(upstream, name, method, path, status):
    from session_verifier import rejected_tokens, token_fingerprint

    upstream.status = status
    upstream.body = {"error": "Only staff of this company can do this.", "data": [{"email": "leak@example.org"}]}
    tool = (await _listed())[name]
    result = await _call(name, _minimal_arguments(tool.parameters))
    assert result.is_error, f"{name}: a {status} came back as a success"
    assert result.structured_content is None, f"{name}: data came back with the {status}"
    assert result.data is None
    assert str(status) in result.content[0].text
    # a 401 is what makes the client re-authenticate (lazy_auth gate); a 403 is not
    assert (token_fingerprint(CALLER) in rejected_tokens) is (status == 401)


@pytest.mark.asyncio
async def test_an_address_in_an_argument_never_moves_the_request(upstream):
    """Every argument whose name says url/path/uri gets an attacker address: the target stays."""
    tools = await _listed()
    checked = 0
    for name, method, path in API_TOOLS:
        properties = tools[name].parameters["properties"]
        addressy = [p for p in properties if p.endswith(("url", "_path", "uri", "host", "endpoint"))]
        if not addressy:
            continue
        arguments = {**_minimal_arguments(tools[name].parameters), **{p: EVIL for p in addressy}}
        upstream.requests.clear()
        await _call(name, arguments)
        for request in upstream.requests:
            _assert_mapped_endpoint(request, method, path, f"{name} {addressy}")
            _assert_carries_only_the_caller(request, name)
        checked += 1
    assert checked >= 4  # cover_image_url, poster_url, meeting_url, ...


@pytest.mark.asyncio
async def test_no_caller_and_no_dev_token_sends_no_credential(upstream, monkeypatch):
    from mcp.server.auth.middleware.auth_context import auth_context_var

    import upstream as upstream_module

    monkeypatch.setattr(upstream_module, "settings", dataclasses.replace(upstream_module.settings, dev_bearer_token=None))
    token = auth_context_var.set(None)
    try:
        await _call("list_tasks", {})
    finally:
        auth_context_var.reset(token)
    assert "authorization" not in upstream.requests[0].headers


@pytest.mark.asyncio
async def test_the_dev_token_is_only_a_fallback_without_a_caller(upstream, monkeypatch):
    """Local development only (JOBMOJITO_DEV_BEARER_TOKEN): used when nobody is signed in."""
    from mcp.server.auth.middleware.auth_context import auth_context_var

    token = auth_context_var.set(None)
    try:
        await _call("list_tasks", {})
    finally:
        auth_context_var.reset(token)
    assert upstream.requests[0].headers["authorization"] == f"Bearer {DEV_TOKEN}"


def test_a_broken_auth_context_forwards_nothing(monkeypatch):
    import fastmcp.server.dependencies as dependencies

    import upstream as upstream_module

    def broken():
        raise RuntimeError("no context")

    monkeypatch.setattr(dependencies, "get_access_token", broken)
    monkeypatch.setattr(upstream_module, "settings", dataclasses.replace(upstream_module.settings, dev_bearer_token=None))
    assert upstream_module.current_bearer_token() is None
    # a token object with no raw token is no caller either
    monkeypatch.setattr(dependencies, "get_access_token", lambda: dataclasses.make_dataclass("T", [("token", str)])(""))
    assert upstream_module.current_bearer_token() is None


@pytest.mark.parametrize("anon_key", [None, "anon-public-key"])
def test_the_project_key_header_is_the_anon_key_or_nothing(monkeypatch, anon_key):
    import upstream as upstream_module

    monkeypatch.setattr(upstream_module, "settings", dataclasses.replace(upstream_module.settings, supabase_anon_key=anon_key))
    client = upstream_module.build_api_client()
    assert client.headers.get("apikey") == anon_key
    assert client.headers["x-jobmojito-client"] == "mcp"
    assert "authorization" not in client.headers  # added per request, from the caller
    assert isinstance(client.auth, upstream_module.SupabaseTokenForwardAuth)


# ---- what a tool can be asked to do -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tool_takes_a_target_or_credential_argument():
    tools = await _listed()
    for name, tool in tools.items():
        clash = TARGET_PARAMS & set(tool.parameters.get("properties", {}))
        if name == "get_documentation":
            assert clash == {"url"}  # restricted to the docs hosts, see below
            continue
        assert not clash, f"{name} takes {clash}"


def test_no_exposed_endpoint_has_a_path_template():
    """With no {param} in a path, no argument can reach the URL path."""
    for _name, _method, path in API_TOOLS:
        assert "{" not in path and "}" not in path, path


@pytest.mark.asyncio
async def test_read_and_write_follow_the_method_with_named_exceptions():
    tools = await _listed()
    read_only_posts = set()
    for name, method, _path in API_TOOLS:
        meta = next(m for m in TOOL_META.values() if m.name == name)
        annotations = tools[name].annotations
        if method == "GET":
            assert meta.read_only and annotations.read_only_hint is True, f"{name} is a GET but not read-only"
            assert annotations.destructive_hint is False, name
        elif meta.read_only:
            read_only_posts.add(name)
        else:
            assert annotations.read_only_hint is False, name
            assert annotations.destructive_hint is True, f"{name} writes but is not destructive"
    # POSTs only because the input is a JSON body; both store nothing (see their justifications)
    assert read_only_posts == {"run_report", "generate_component_styles"}


@pytest.mark.asyncio
async def test_irreversible_tools_always_ask():
    tools = await _listed()
    for name in IRREVERSIBLE:
        annotations = tools[name].annotations
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is True, name
    assert tools["merge_candidates"].annotations.idempotent_hint is False


def test_no_write_is_retried_by_the_size_guard():
    import naming

    retried = naming.read_only_tool_names()
    assert retried.isdisjoint(IRREVERSIBLE)
    assert "run_report" in retried


# ---- the tools written by hand ------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "path", "answer"),
    [
        ({}, "/invite-users", [{"email": "a@b.co", "result": "Invited"}]),
        ({"send_email": False}, "/job-interview-register-users", [{"email": "a@b.co", "result": "ok"}]),
    ],
)
@pytest.mark.asyncio
async def test_invite_candidates_sends_only_the_callers_token(upstream, arguments, path, answer):
    upstream.body = answer
    result = await _call("invite_candidates", {"interview_id": "i", "candidates": [{"email": "a@b.co"}], **arguments})
    assert not result.is_error
    (request,) = upstream.requests
    _assert_mapped_endpoint(request, "POST", path, "invite_candidates")
    _assert_carries_only_the_caller(request, "invite_candidates")


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("send_email", [True, False])
@pytest.mark.asyncio
async def test_invite_candidates_refused_is_an_error_with_no_data(upstream, status, send_email):
    upstream.status = status
    upstream.body = {"error": "no", "data": [{"email": "leak@example.org"}]}
    result = await _call("invite_candidates", {"interview_id": "i", "candidates": [{"email": "a@b.co"}], "send_email": send_email})
    assert result.is_error and result.structured_content is None


@pytest.mark.asyncio
async def test_list_my_merchants_sends_only_the_callers_token(upstream):
    upstream.body = {"data": [], "pagination": {"has_more": False}}
    await _call("list_my_merchants", {})
    assert upstream.requests
    for request in upstream.requests:
        _assert_mapped_endpoint(request, "GET", "/merchant-sub-merchant-list", "list_my_merchants")
        _assert_carries_only_the_caller(request, "list_my_merchants")


@pytest.mark.asyncio
async def test_upload_media_file_gives_the_token_to_the_api_only(upstream):
    signed = "https://storage.test/storage/v1/object/upload/sign/public/m/a.png?token=t"
    upstream.by_url = {
        "/media-upload-start": (200, {"media_id": "m1", "upload_url": signed, "token": "t", "path": "m/a.png", "cache_control": 60}),
        "/upload/sign/": (200, {"Key": "k"}),
        "/media-upload-finish": (200, {"file": {}, "replaced": False, "drafts_updated": 0, "warnings": []}),
    }
    file = base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode()
    await _call("upload_media_file", {"file": file, "file_name": "a.png", "content_type": "image/png"})
    start, put, finish = upstream.requests
    _assert_mapped_endpoint(start, "POST", "/media-upload-start", "upload start")
    _assert_carries_only_the_caller(start, "upload start")
    _assert_mapped_endpoint(finish, "POST", "/media-upload-finish", "upload finish")
    _assert_carries_only_the_caller(finish, "upload finish")
    # the signed address is storage, not the API: no user token, no project key
    assert str(put.url) == signed
    assert "authorization" not in put.headers and "apikey" not in put.headers
    assert CALLER not in " ".join(put.headers.values())


@pytest.mark.asyncio
async def test_the_documentation_tools_never_send_the_callers_token(upstream):
    import docs_tools
    from config import settings

    upstream.body = {}
    await _call("search_documentation", {"query": "invite"})
    page = settings.developer_docs_base_url.rstrip("/") + "/some-page"
    await _call("get_documentation", {"url": page})
    for request in upstream.requests:
        wire = " ".join([str(request.url), *request.headers.values()])
        assert CALLER not in wire, request.url
        assert "apikey" not in request.headers, request.url
    # an address off the docs hosts is refused before any request
    host = urlsplit(settings.developer_docs_base_url).hostname
    for url in (EVIL, f"https://{host}.evil.test/x", f"https://user@{host}/x"):
        upstream.requests.clear()
        result = await _call("get_documentation", {"url": url})
        assert not upstream.requests, url
        assert "not allowed" in result.structured_content["error"]
    assert docs_tools  # imported for the registry side effects only


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.asyncio
async def test_a_boolean_query_flag_reaches_the_api_as_the_string_it_reads(upstream, value):
    """extended / include_archived / ... were 'true'/'false' string enums; a model sends a boolean."""
    tool = (await _listed())["list_interview_results"]
    assert tool.parameters["properties"]["extended"]["type"] == "boolean"
    await _call("list_interview_results", {"extended": value})
    assert upstream.requests[0].url.params["extended"] == ("true" if value else "false")


@pytest.mark.asyncio
async def test_the_other_id_name_reaches_the_api_under_the_name_the_tool_takes(upstream):
    """position_id and interview_id name the same record on different endpoints."""
    pid = "00000000-0000-4000-8000-0000000000aa"
    result = await _call("set_interview_state", {"interview_id": pid, "status": "active"})
    sent = json.loads(upstream.requests[0].content)
    assert sent["position_id"] == pid and "interview_id" not in sent, (result, sent)
    await _call("generate_interview_url", {"type": "interview-results-for-position", "position_id": pid})
    sent = json.loads(upstream.requests[1].content)
    assert sent["interview_id"] == pid and "position_id" not in sent


@pytest.mark.asyncio
async def test_rules_sent_as_an_object_reach_the_api_as_its_json_text(upstream):
    rules = {"match": "all", "rules": [{"field": "tag", "op": "any", "values": ["t1"]}]}
    await _call("list_candidates", {"rules": rules})
    assert json.loads(upstream.requests[0].url.params["rules"]) == rules
    await _call("list_candidates", {"rules": json.dumps(rules)})
    assert json.loads(upstream.requests[1].url.params["rules"]) == rules


@pytest.mark.asyncio
async def test_argument_shape_leaves_what_it_does_not_own():
    from middleware import ArgumentShapeMiddleware

    seen = {}

    async def call_next(context):
        seen.update(context.message.arguments or {})
        return "ok"

    class Message:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class Context:
        def __init__(self, message):
            self.message = message

    shape = ArgumentShapeMiddleware({"t": {"position_id", "status"}, "both": {"position_id", "interview_id"}}, {"t": {"rules"}})
    # both names sent: neither is moved; a string stays a string
    await shape.on_call_tool(Context(Message("t", {"interview_id": "i", "position_id": "p", "rules": "{}"})), call_next)
    assert seen == {"interview_id": "i", "position_id": "p", "rules": "{}"}
    seen.clear()
    await shape.on_call_tool(Context(Message("both", {"interview_id": "i"})), call_next)
    assert seen == {"interview_id": "i"}
    seen.clear()
    await shape.on_call_tool(Context(Message("t", {"rules": [1]})), call_next)
    assert seen == {"rules": "[1]"}
    assert await shape.on_call_tool(Context(Message("t", None)), call_next) == "ok"


@pytest.mark.asyncio
async def test_argument_shape_drops_client_markers_that_no_tool_declares():
    """`_as_extra: true` reached get_tool_reference on 9 Nov 2026 and the call was refused."""
    from middleware import ArgumentShapeMiddleware

    seen = {}

    async def call_next(context):
        seen.clear()
        seen.update(context.message.arguments or {})
        return "ok"

    class Message:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class Context:
        def __init__(self, message):
            self.message = message

    shape = ArgumentShapeMiddleware({"t": {"status", "_declared"}}, {})
    await shape.on_call_tool(Context(Message("t", {"status": "a", "_as_extra": True, "_declared": 1})), call_next)
    assert seen == {"status": "a", "_declared": 1}
    # a local tool (no spec entry) loses the marker too
    await shape.on_call_tool(Context(Message("get_tool_reference", {"tool": "x", "_as_extra": True})), call_next)
    assert seen == {"tool": "x"}


@pytest.mark.asyncio
async def test_location_reaches_a_tool_that_calls_it_interview_location(upstream):
    tool = (await _listed())["create_persona"]
    assert "interview_location" in tool.parameters["properties"] and "location" not in tool.parameters["properties"]
    await _call("create_persona", {**_minimal_arguments(tool.parameters), "location": "Vienna"})
    sent = json.loads(upstream.requests[0].content)
    assert sent["interview_location"] == "Vienna" and "location" not in sent


@pytest.mark.asyncio
async def test_segment_rules_sent_as_an_object_or_a_string_reach_the_api_as_its_json_text(upstream):
    rules = {"match": "any", "rules": [{"field": "tag", "op": "any", "values": ["t1"]}]}
    for value in (rules, json.dumps(rules)):
        await _call("manage_segment", {"action": "save", "name": "Nurses", "kind": "dynamic", "rules": value})
    for request in upstream.requests:
        assert json.loads(json.loads(request.content)["rules"]) == rules
