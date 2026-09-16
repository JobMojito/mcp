"""Which upstream failures reach Error Tracking, and how they are labelled.

The MCP SDK promotes every failed tool call to an `$exception` alongside its
`$mcp_tool_call`. Only **401** is dropped: an unauthenticated `tools/call` is how
a client learns it must run the OAuth flow, and lazy_auth answers it before any
tool executes, so reporting those would mean an exception for every client's
first call. Every other status — 4xx and 5xx alike — is reported and stamped
with `upstream_status` / `error_class`, so a status that turns out to be noisy
can be suppressed with a filter instead of a redeploy.

PAYLOADS ARE BUILT BY THE SDK, NOT HAND-WRITTEN
`_exception_payload` calls `posthog.mcp._exceptions.capture_exception`, the same
function the SDK uses. This matters: an earlier version of the filter read
`$mcp_error_message`, which exists only on the `$mcp_tool_call` sibling and never
on the `$exception`. Every hand-written test passed and the filter matched
nothing in production.
"""

import pytest

from posthog.mcp._exceptions import capture_exception

from posthog_analytics import _classify_upstream_exception, _exception_messages, _http_status

REWRITTEN_422 = (
    "`get_interview_definition` failed with HTTP 422: the JobMojito API understood the "
    "request but rejected its contents.\n\nWhat to do: The detail below names the "
    "offending field(s).\n\nUpstream detail: {'error': 'Field is required.', 'name': 'position_id'}"
)
REWRITTEN_404 = "`get_interview_definition` failed with HTTP 404: the requested record does not exist."
REWRITTEN_502 = "`generate_interview_report` failed with HTTP 502: the JobMojito API returned a server error."
RAW_403 = "HTTP error 403: Forbidden - the signed-in user is not permitted to perform this action"
RAW_401 = "HTTP error 401: Unauthorized - the request carried no valid token"


def _exception_payload(message):
    """An `$exception` event shaped exactly as the SDK builds it."""
    properties = {"$mcp_tool_name": "get_interview_definition", "service": "mcp", "tier": "backend"}
    properties.update(capture_exception(message))
    return {"event": "$exception", "distinct_id": "u", "properties": properties}


def test_sdk_payload_has_no_mcp_error_message():
    """Guards the assumption that broke the first version of this filter."""
    properties = _exception_payload(REWRITTEN_422)["properties"]
    assert "$mcp_error_message" not in properties
    assert properties["$exception_list"], "the message must live in $exception_list"


def test_messages_are_recovered_from_the_real_payload():
    assert any(REWRITTEN_422 in m for m in _exception_messages(_exception_payload(REWRITTEN_422)["properties"]))


@pytest.mark.parametrize(
    "message,expected",
    [(REWRITTEN_422, 422), (REWRITTEN_404, 404), (REWRITTEN_502, 502),
     (RAW_403, 403), (RAW_401, 401), ("no status here", None), ("", None)],
)
def test_http_status_parses_both_message_shapes(message, expected):
    assert _http_status(message) == expected


@pytest.mark.parametrize("message", [RAW_401, "`x` failed with HTTP 401: not authenticated."])
def test_401_is_dropped(message):
    """Part of the OAuth handshake, not a fault."""
    assert _classify_upstream_exception(_exception_payload(message)) is None


@pytest.mark.parametrize(
    "message,status,klass",
    [
        (REWRITTEN_422, 422, "client"),
        (REWRITTEN_404, 404, "client"),
        (RAW_403, 403, "client"),
        (REWRITTEN_502, 502, "server"),
    ],
)
def test_other_statuses_are_reported_and_labelled(message, status, klass):
    result = _classify_upstream_exception(_exception_payload(message))
    assert result is not None
    assert result["properties"]["upstream_status"] == status
    assert result["properties"]["error_class"] == klass


@pytest.mark.parametrize("message", ["upstream exploded", ""])
def test_unrecognised_errors_are_reported_without_a_status(message):
    """Never hide something we have not seen before."""
    result = _classify_upstream_exception(_exception_payload(message))
    assert result is not None
    assert "upstream_status" not in result["properties"]


def test_real_exception_object_is_reported():
    payload = {"event": "$exception", "properties": dict(capture_exception(ValueError("boom")))}
    result = _classify_upstream_exception(payload)
    assert result is not None
    assert "upstream_status" not in result["properties"]


@pytest.mark.parametrize(
    "event",
    [
        # The counting signal must survive regardless of what happens to the
        # $exception: a 401 tool call still belongs on the MCP dashboard.
        {"event": "$mcp_tool_call", "properties": {"$mcp_error_message": RAW_401, "$mcp_is_error": True}},
        {"event": "$mcp_tool_call", "properties": {}},
        {"event": "$mcp_initialize", "properties": {}},
        {"event": "$mcp_tools_list", "properties": {}},
    ],
)
def test_non_exception_events_are_never_dropped(event):
    assert _classify_upstream_exception(event) is not None


def test_malformed_event_is_kept_not_raised():
    """A filter that throws would cost us the report it was meant to triage."""
    assert _classify_upstream_exception({"event": "$exception"}) is not None
    assert _classify_upstream_exception({}) is not None


# ---------------------------------------------------------------------------
# Transport-level rejections
#
# The layer no tool metric can see: MCP Streamable HTTP answers a stale
# `Mcp-Session-Id` with a 400/404 before dispatch, so a client stuck in a
# reconnect loop shows up as a flat zero in the tool error rate.
# ---------------------------------------------------------------------------


class _RecordingClient:
    def __init__(self):
        self.events = []

    def capture(self, event, **kwargs):
        self.events.append((event, kwargs))


@pytest.fixture
def recording_client(monkeypatch):
    import posthog_analytics

    client = _RecordingClient()
    monkeypatch.setattr(posthog_analytics, "_client", client)
    monkeypatch.setattr(
        posthog_analytics, "_event_tags", {"service": "mcp", "tier": "backend"}
    )
    return client


def test_transport_rejection_is_reported_as_an_exception(recording_client):
    from posthog_analytics import capture_transport_rejection

    capture_transport_rejection(status=400, method="POST", had_session_id=True)

    (event, kwargs), = recording_client.events
    assert event == "$exception"
    properties = kwargs["properties"]
    assert properties["$exception_list"][0]["type"] == "MCPTransportRejection"
    assert properties["transport_status"] == 400
    assert properties["transport_had_session_id"] is True
    assert properties["error_class"] == "client"
    # Same discriminators as every other event this module sends.
    assert properties["service"] == "mcp" and properties["tier"] == "backend"
    # These requests have no verified user; a reconnect loop must not mint one
    # person per attempt.
    assert properties["$process_person_profile"] is False


def test_transport_rejection_is_not_mistaken_for_an_upstream_status(recording_client):
    """`_classify_upstream_exception` parses "HTTP error <n>" out of messages.

    A transport rejection is this server answering, not JobMojito — if its
    wording matched that pattern, the two would group into one issue and a dead
    session would read as an API failure.
    """
    from posthog_analytics import capture_transport_rejection, _http_status

    capture_transport_rejection(status=404, method="POST", had_session_id=False)
    value = recording_client.events[0][1]["properties"]["$exception_list"][0]["value"]
    assert _http_status(value) is None


def test_transport_reporting_is_inert_without_analytics(monkeypatch):
    """No POSTHOG_API_KEY means no client, and this must stay a no-op."""
    import posthog_analytics

    monkeypatch.setattr(posthog_analytics, "_client", None)
    posthog_analytics.capture_transport_rejection(status=400)  # must not raise


def test_a_failing_reporter_cannot_break_the_response(monkeypatch):
    import posthog_analytics

    class _Boom:
        def capture(self, *args, **kwargs):
            raise RuntimeError("posthog is down")

    monkeypatch.setattr(posthog_analytics, "_client", _Boom())
    posthog_analytics.capture_transport_rejection(status=500)  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reported",
    [
        (200, False),
        # The whole point of lazy auth: an unauthenticated tools/call is the
        # OAuth handshake, not a fault. Same carve-out as the $exception filter.
        (401, False),
        (400, True),   # stale Mcp-Session-Id
        (404, True),   # terminated session
        (500, True),
    ],
)
async def test_only_unexpected_transport_statuses_are_reported(status, reported):
    from lazy_auth import TransportRejectionASGIMiddleware

    seen = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = TransportRejectionASGIMiddleware(
        app, mcp_path="/mcp", report=lambda **kw: seen.append(kw)
    )
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [(b"mcp-session-id", b"dead"), (b"user-agent", b"claude-code/2.1")],
    }
    await middleware(scope, None, send)

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [status]
    assert bool(seen) is reported
    if reported:
        assert seen[0]["had_session_id"] is True
        assert seen[0]["user_agent"] == "claude-code/2.1"
        assert "dead" not in str(seen[0]), "the session id itself is never recorded"


@pytest.mark.asyncio
async def test_other_routes_are_left_alone():
    """Health probes and .well-known documents are not MCP traffic."""
    from lazy_auth import TransportRejectionASGIMiddleware

    seen = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})

    middleware = TransportRejectionASGIMiddleware(
        app, mcp_path="/mcp", report=lambda **kw: seen.append(kw)
    )
    async def send(message):
        return None

    await middleware(
        {"type": "http", "path": "/healthz", "method": "GET", "headers": []},
        None,
        send,
    )
    assert seen == []
