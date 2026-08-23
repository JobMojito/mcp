"""Which tool failures reach Error Tracking, and which stay analytics-only.

The MCP SDK promotes every failed tool call to an `$exception` alongside its
`$mcp_tool_call`. For this server most failures are 4xx — an agent omitting a
required argument, or a user lacking a permission — which are normal operation
rather than defects. `posthog_analytics` drops just the `$exception` sibling for
those, and this suite pins that boundary.

The 4xx messages below are verbatim from production; the first three tool errors
this server ever recorded were all the same missing `position_id`, which is what
motivated the filter.
"""

import pytest

from posthog_analytics import _drop_client_error_exceptions, _http_status

# Rewritten by middleware.UpstreamErrorMiddleware into agent-facing guidance.
REWRITTEN_422 = (
    "`get_interview_definition` failed with HTTP 422: the JobMojito API understood the "
    "request but rejected its contents.\n\nWhat to do: The detail below names the "
    "offending field(s). Fix those specific values; do not resend the same payload.\n\n"
    "Upstream detail: {'error': 'Field is required.', 'name': 'position_id'}"
)
REWRITTEN_502 = (
    "`generate_interview_report` failed with HTTP 502: the JobMojito API returned a "
    "server error.\n\nWhat to do: retry shortly."
)
# Raw FastMCP ToolError, i.e. a status the rewriter has no guidance entry for.
RAW_403 = "HTTP error 403: Forbidden - the signed-in user is not permitted to perform this action"


def _exception(message):
    return {"event": "$exception", "properties": {"$mcp_error_message": message}}


@pytest.mark.parametrize(
    "message,expected",
    [(REWRITTEN_422, 422), (REWRITTEN_502, 502), (RAW_403, 403), ("no status here", None), ("", None)],
)
def test_http_status_parses_both_message_shapes(message, expected):
    """Both the rewritten and the raw upstream formats must be readable."""
    assert _http_status(message) == expected


@pytest.mark.parametrize("message", [REWRITTEN_422, RAW_403])
def test_client_errors_are_dropped(message):
    """4xx is the caller's mistake; it must not land in Error Tracking."""
    assert _drop_client_error_exceptions(_exception(message)) is None


@pytest.mark.parametrize(
    "message",
    [
        REWRITTEN_502,               # ours to fix
        "upstream exploded",         # no status: never hide the unrecognised
        "",
    ],
)
def test_server_and_unrecognised_errors_are_kept(message):
    assert _drop_client_error_exceptions(_exception(message)) is not None


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
