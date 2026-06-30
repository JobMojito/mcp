"""Server-side request logging for tool calls.

Adds visibility into what the agent actually invokes and why a call failed —
including FastMCP output-schema validation errors (JSON-RPC -32602), which are
raised while serializing a tool's result and are otherwise opaque on the client.

IMPORTANT — what this CANNOT see: MCP Streamable HTTP rejects requests with a
missing/invalid `Mcp-Session-Id` (HTTP 400) or a terminated session (HTTP 404)
at the transport layer, BEFORE any tool runs. Those never reach this middleware,
so a bare 400/404 with no entry here means the client is reusing a dead session
and must re-`initialize` — it is not an application/tool error.
"""

from __future__ import annotations

import logging
import time

from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger("jobmojito_mcp.requests")


class ToolCallLoggingMiddleware(Middleware):
    """Log each tool call: name + argument keys on entry, outcome + timing on exit."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        msg = getattr(context, "message", None)
        name = getattr(msg, "name", "<unknown>")
        args = getattr(msg, "arguments", None) or {}
        # Log argument KEYS only (values may be large; tokens are never in args,
        # but this keeps logs tidy and avoids echoing free-text filters).
        arg_keys = ",".join(sorted(args.keys())) if isinstance(args, dict) else ""
        started = time.monotonic()
        logger.info("tool call → %s(%s)", name, arg_keys)
        try:
            result = await call_next(context)
        except Exception as exc:  # log and re-raise (don't swallow)
            ms = (time.monotonic() - started) * 1000
            logger.warning(
                "tool call ✗ %s failed after %.0fms: %s: %s",
                name,
                ms,
                type(exc).__name__,
                exc,
            )
            raise
        ms = (time.monotonic() - started) * 1000
        logger.info("tool call ✓ %s (%.0fms)", name, ms)
        return result
