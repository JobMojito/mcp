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
