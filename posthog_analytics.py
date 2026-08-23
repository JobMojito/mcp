"""PostHog instrumentation for the JobMojito MCP server.

Covers both requirements with one integration:

  * **MCP analytics** — tool calls, tool listings, handshakes and resource/prompt
    access, captured as the ``$mcp_*`` events PostHog's MCP Analytics views read.
  * **Exception reporting** — tool failures additionally land as ``$exception``,
    so they group into issues in Error Tracking beside the rest of the backend.

We use PostHog's own adapter rather than hand-rolled middleware. ``instrument()``
supports jlowin's FastMCP directly (``posthog.mcp._compatibility.is_fastmcp_v2``
is a plain isinstance check against whatever ``fastmcp`` is installed), verified
against this server on fastmcp 3.4.7.

UPGRADE RISK, AND WHY IT IS ACCEPTED
``instrument()`` hooks private FastMCP internals — ``_tool_manager``,
``_mcp_server``, the request-handler registries. That is the same class of
coupling ``lazy_auth.py`` already takes on, which is why requirements.txt pins
``fastmcp<4`` and tests/test_listing_readiness.py gates upgrades. Re-run that
suite plus a live tool call after any fastmcp bump; a broken adapter degrades to
a no-op handle (analytics stop) rather than breaking the server.

Nothing here is load-bearing for serving traffic. With POSTHOG_API_KEY unset the
whole module is inert and the server behaves exactly as it did before.
"""

from __future__ import annotations

import atexit
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Matches both message shapes an upstream failure can arrive in: FastMCP's raw
# `ToolError("HTTP error 403: ...")` and the agent-facing rewrite produced by
# middleware.UpstreamErrorMiddleware ("`tool` failed with HTTP 422: ..."). Both
# formats are ours to keep stable — _HTTP_ERROR_RE in middleware.py parses the
# first and line ~262 builds the second — so anchoring on them is safe in a way
# that matching arbitrary error prose would not be.
_HTTP_STATUS_RE = re.compile(r"(?:HTTP error|with HTTP)\s+(\d{3})\b")


def _http_status(message: str) -> int | None:
    """The upstream HTTP status named in an error message, if it names one."""
    match = _HTTP_STATUS_RE.search(message or "")
    return int(match.group(1)) if match else None


def _drop_client_error_exceptions(event: Any) -> Any:
    """Keep 4xx tool failures out of Error Tracking, without hiding them.

    A 4xx from the JobMojito API means the CALLER got it wrong — a missing
    required argument, or a permission the signed-in user does not have. For an
    MCP server that is normal operation, not a fault: the model is expected to
    occasionally send a bad payload, and our own error text is written to tell it
    how to correct the call. Left alone, every such mistake also raises an
    `$exception`, and since agents make them constantly they would quickly
    outnumber the real failures in Error Tracking — the first three tool errors
    this server recorded in production were all one missing `position_id`.

    Only the `$exception` sibling is dropped. The `$mcp_tool_call` event still
    carries `$mcp_is_error=True` and the full message, so failed calls stay
    visible and countable on the MCP dashboard, where they are a useful signal
    about tool-schema clarity rather than a page-worthy defect.

    5xx and anything without a recognisable status still raise `$exception`:
    those are ours to fix, and an unparseable message is exactly the case where
    dropping the report would hide something we have not seen before.
    """
    try:
        if event.get("event") != "$exception":
            return event
        properties = event.get("properties") or {}
        status = _http_status(str(properties.get("$mcp_error_message") or ""))
        if status is not None and 400 <= status < 500:
            return None
    except Exception:
        # A filter that throws must not cost us the error report.
        logger.debug("posthog.mcp before_send filter failed", exc_info=True)
    return event


def _build_client(api_key: str | None, host: str, *, debug: bool) -> Any | None:
    """Construct the PostHog client, or None when analytics is not configured.

    Returns None rather than raising on a missing key or a missing ``posthog``
    package: analytics is optional infrastructure, and a server that refuses to
    boot because a telemetry dependency is absent is worse than one that runs
    without telemetry.
    """
    if not api_key:
        logger.info("PostHog analytics disabled (no POSTHOG_API_KEY).")
        return None

    try:
        from posthog import Posthog
    except ImportError:
        logger.warning(
            "POSTHOG_API_KEY is set but the `posthog` package is not installed; "
            "MCP analytics and exception reporting are disabled."
        )
        return None

    try:
        return Posthog(api_key, host=host, debug=debug)
    except Exception:
        logger.warning("Failed to construct PostHog client; analytics disabled.", exc_info=True)
        return None


def _register_shutdown(client: Any, analytics: Any) -> None:
    """Flush on process exit.

    The SDK batches in a background thread, so without this the last few events
    are lost on restart. Neither the local ``mcp.run()`` path nor Horizon's runner
    gives us a lifespan hook, so ``atexit`` is what we have.

    ``analytics.flush()`` is a coroutine that drains in-flight auto-captures, and
    at ``atexit`` time there is usually no running loop left to await it on. We
    try it, and fall back to ``client.shutdown()`` alone — which still flushes
    everything already queued. The gap is only captures still mid-flight at the
    instant the process exits.
    """

    def _shutdown() -> None:
        try:
            import asyncio

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(analytics.flush())
        except Exception:
            logger.debug("PostHog MCP analytics flush skipped", exc_info=True)
        try:
            client.shutdown()
        except Exception:
            logger.debug("PostHog shutdown failed", exc_info=True)

    atexit.register(_shutdown)


def install(
    server: Any,
    *,
    api_key: str | None,
    host: str,
    debug: bool = False,
    enable_intent: bool = False,
    service: str = "mcp",
    tier: str = "backend",
) -> Any | None:
    """Instrument ``server`` with PostHog MCP analytics. No-op when unconfigured.

    Returns the ``McpAnalytics`` handle (usable for custom events via
    ``await handle.capture(...)``), or None if analytics is off or setup failed.

    Call this AFTER every tool is registered, so the adapter wraps the final tool
    manager rather than a partially-built one.

    :param enable_intent: Capture *why* the agent called a tool, which powers the
        Intent clustering view. This is off by default deliberately: PostHog's own
        default is on, and turning it on injects a **required** ``context`` string
        argument into every tool's JSON schema. That is a visible change to this
        server's public tool contract — the schemas directory reviewers read — so
        it is opt-in via POSTHOG_ENABLE_INTENT rather than something that arrives
        silently with a dependency bump. The SDK strips the argument before our
        handlers run, so tool implementations are unaffected either way.
    :param service: Stamped onto every captured event, including ``$exception``.
        This project also holds browser errors from app.jobmojito.com and will
        hold edge-function and digital-recruiter errors, so without a
        discriminator every backend failure lands in one undifferentiated issue
        list. The SDK's own ``$mcp_*`` properties identify MCP tool failures, but
        only MCP ones — ``service`` is the convention that spans all sources.
    :param tier: Coarse frontend/backend split, stamped alongside ``service``.
        PostHog's own ``$lib`` already separates browser (``web``) from server
        (``posthog-python``), but that is SDK-derived rather than semantic — add a
        Node service and the split becomes an ever-growing list of library names.
        An explicit tag says what we mean and survives an SDK change.
    """
    client = _build_client(api_key, host, debug=debug)
    if client is None:
        return None

    try:
        from posthog.mcp import instrument
        from posthog.mcp.types import MCPAnalyticsOptions
    except ImportError:
        logger.warning(
            "posthog is installed but posthog.mcp is unavailable; "
            "MCP analytics disabled. Requires posthog>=6.0."
        )
        return None

    try:
        analytics = instrument(
            server,
            client,
            MCPAnalyticsOptions(
                # Surface the SDK's own warnings (unsupported versions, failed
                # instrumentation) through our logger instead of its no-op sink.
                logger=lambda message: logger.warning("posthog.mcp: %s", message),
                context=enable_intent,
                # Merged onto every auto-captured event, $exception included.
                # Signature is (request, extra) -> dict | None; we ignore both
                # because the tag is constant for this process.
                event_properties=lambda *_args, **_kwargs: {
                    "service": service,
                    "tier": tier,
                },
                # Tool failures become $exception events in Error Tracking, not
                # just an error flag on $mcp_tool_call. This is the whole reason
                # exception reporting needs no separate integration.
                enable_exception_autocapture=True,
                # ...but 4xx means the caller erred, not the server. Runs after
                # the $exception payload is built and can drop it; see the
                # function's docstring for why only that sibling goes.
                before_send=_drop_client_error_exceptions,
            ),
        )
    except Exception:
        logger.warning("PostHog MCP instrumentation failed; analytics disabled.", exc_info=True)
        return None

    # instrument() degrades to a no-op handle rather than raising when it cannot
    # hook the server. Detect that so a silent analytics outage is visible in logs.
    if type(analytics).__name__ == "_NoopAnalytics":
        logger.warning(
            "PostHog MCP instrumentation returned a no-op handle — the FastMCP "
            "internals it hooks may have changed. Analytics will not be captured."
        )
        return None

    _register_shutdown(client, analytics)
    logger.info(
        "PostHog MCP analytics enabled (host=%s, service=%s, tier=%s, intent capture=%s).",
        host,
        service,
        tier,
        "on" if enable_intent else "off",
    )
    return analytics
