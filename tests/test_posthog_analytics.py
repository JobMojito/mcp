"""Which tool failures reach Error Tracking, and which stay analytics-only.

The MCP SDK promotes every failed tool call to an `$exception` alongside its
`$mcp_tool_call`. For this server most failures are 4xx — an agent omitting a
required argument, passing an id that does not resolve, or a user lacking a
permission — which are normal operation rather than defects. `posthog_analytics`
drops just the `$exception` sibling for those, and this suite pins that boundary.

PAYLOADS ARE BUILT BY THE SDK, NOT HAND-WRITTEN
`_exception_payload` calls `posthog.mcp._exceptions.capture_exception`, the same
function the SDK uses, so these tests assert against the real property shape.
This matters: the first version of this filter read `$mcp_error_message`, which
exists only on the `$mcp_tool_call` sibling and never on the `$exception`. Every
hand-written test passed, and the filter matched nothing in production. If the
SDK changes the payload shape, these tests fail rather than quietly going green.

The 4xx messages below are verbatim from production.
"""

import pytest

from posthog.mcp._exceptions import capture_exception

from posthog_analytics import _drop_client_error_exceptions, _exception_messages, _http_status

# Rewritten by middleware.UpstreamErrorMiddleware into agent-facing guidance.
REWRITTEN_422 = (
    "`get_interview_definition` failed with HTTP 422: the JobMojito API understood the "
    "request but rejected its contents.\n\nWhat to do: The detail below names the "
    "offending field(s). Fix those specific values; do not resend the same payload.\n\n"
    "Upstream detail: {'error': 'Field is required.', 'name': 'position_id'}"
)
REWRITTEN_404 = (
    "`get_interview_definition` failed with HTTP 404: the requested record does not "
    "exist.\n\nWhat to do: Verify the identifier."
)
REWRITTEN_502 = (
    "`generate_interview_report` failed with HTTP 502: the JobMojito API returned a "
    "server error.\n\nWhat to do: retry shortly."
)
# Raw FastMCP ToolError, i.e. a status the rewriter has no guidance entry for.
RAW_403 = "HTTP error 403: Forbidden - the signed-in user is not permitted to perform this action"


def _exception_payload(message):
    """An `$exception` event shaped exactly as the SDK builds it."""
    properties = {"$mcp_tool_name": "get_interview_definition", "service": "mcp", "tier": "backend"}
    properties.update(capture_exception(message))
    return {"event": "$exception", "distinct_id": "u", "properties": properties}


def test_sdk_payload_has_no_mcp_error_message():
    """Guards the assumption that broke the first version of this filter."""
    properties = _exception_payload(REWRITTEN_422)["properties"]
    assert "$mcp_error_message" not in properties
    assert properties["$exception_list"], "message must live in $exception_list"


def test_messages_are_recovered_from_the_real_payload():
    assert any(REWRITTEN_422 in m for m in _exception_messages(_exception_payload(REWRITTEN_422)["properties"]))


@pytest.mark.parametrize(
    "message,expected",
    [(REWRITTEN_422, 422), (REWRITTEN_404, 404), (REWRITTEN_502, 502), (RAW_403, 403),
     ("no status here", None), ("", None)],
)
def test_http_status_parses_both_message_shapes(message, expected):
    """Both the rewritten and the raw upstream formats must be readable."""
    assert _http_status(message) == expected


@pytest.mark.parametrize("message", [REWRITTEN_422, REWRITTEN_404, RAW_403])
def test_client_errors_are_dropped(message):
    """4xx is the caller's mistake; it must not land in Error Tracking."""
    assert _drop_client_error_exceptions(_exception_payload(message)) is None


@pytest.mark.parametrize(
    "message",
    [
        REWRITTEN_502,        # ours to fix
        "upstream exploded",  # no status: never hide the unrecognised
        "",
    ],
)
def test_server_and_unrecognised_errors_are_kept(message):
    assert _drop_client_error_exceptions(_exception_payload(message)) is not None


def test_real_exception_object_is_kept():
    """A genuine crash carries no HTTP status and must always be reported."""
    payload = {"event": "$exception", "properties": dict(capture_exception(ValueError("boom")))}
    assert _drop_client_error_exceptions(payload) is not None


@pytest.mark.parametrize(
    "event",
    [
        # The counting signal: a failed call stays visible on the MCP dashboard
        # even when its $exception sibling is dropped. Regressing this would make
        # 4xx failures invisible rather than merely un-paged.
        {"event": "$mcp_tool_call", "properties": {"$mcp_error_message": REWRITTEN_422, "$mcp_is_error": True}},
        {"event": "$mcp_tool_call", "properties": {}},
        {"event": "$mcp_initialize", "properties": {}},
        {"event": "$mcp_tools_list", "properties": {}},
    ],
)
def test_non_exception_events_are_never_dropped(event):
    assert _drop_client_error_exceptions(event) is not None


def test_malformed_event_is_kept_not_raised():
    """A filter that throws would cost us the report it was meant to triage."""
    assert _drop_client_error_exceptions({"event": "$exception"}) is not None
    assert _drop_client_error_exceptions({}) is not None
