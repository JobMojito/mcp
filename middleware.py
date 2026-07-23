"""Server-side request logging + friendlier output-validation errors for tool calls.

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

import jsonschema
from jsonschema.validators import validator_for

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

logger = logging.getLogger("jobmojito_mcp.requests")

# Cap how many field paths we list, so a list endpoint where every row trips the
# same field doesn't produce a multi-KB error blob.
_MAX_REPORTED_FIELDS = 8


def _format_output_validation_error(errors: list[jsonschema.ValidationError]) -> str:
    """Turn raw jsonschema errors into a message that names the offending field(s).

    The MCP SDK's built-in output validation reports only `e.message`
    (e.g. "0.29 is not of type 'boolean', 'null'") with no path, so an agent
    can't tell WHICH field to fix. We prepend `e.json_path` (e.g.
    `$.result[1].is_default`) to every reported error.

    Kept starting with "Output validation error" so anything matching on that
    prefix (logs, clients) keeps working.
    """
    # Stable order (by path) and de-dup identical path+message pairs — a paged
    # list can surface the same field failure on many rows.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for e in sorted(errors, key=lambda err: err.json_path):
        key = (e.json_path or "$", e.message)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    lines = [f"  • {path}: {msg}" for path, msg in unique[:_MAX_REPORTED_FIELDS]]
    more = len(unique) - _MAX_REPORTED_FIELDS
    if more > 0:
        lines.append(f"  … and {more} more field(s)")

    count = len(unique)
    noun = "field" if count == 1 else "fields"
    return (
        f"Output validation error in {count} {noun} "
        f"(the response did not match the tool's output schema):\n"
        + "\n".join(lines)
    )


class OutputValidationErrorMiddleware(Middleware):
    """Re-raise output-schema validation failures with the offending field path.

    The MCP SDK validates a tool's structured result against its output schema
    and, on failure, returns a bare "Output validation error: <message>" with no
    indication of WHICH field is wrong. We pre-validate here (identical schema +
    instance) and, if it fails, raise a ToolError naming the field path(s). That
    ToolError propagates past the SDK's own validation (the result becomes an
    error before the SDK re-checks it), so the client sees only our richer
    message. On success this is a cheap no-op that the SDK then re-confirms.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)

        # Only structured results are output-validated by the SDK.
        if not isinstance(result, ToolResult) or result.structured_content is None:
            return result

        fastmcp_ctx = getattr(context, "fastmcp_context", None)
        server = getattr(fastmcp_ctx, "fastmcp", None)
        name = getattr(getattr(context, "message", None), "name", None)
        if server is None or not name:
            return result

        try:
            tool = await server.get_tool(name)
        except Exception:  # tool lookup is best-effort; never block a good result
            return result
        schema = getattr(tool, "output_schema", None) if tool else None
        if not schema:
            return result

        try:
            validator = validator_for(schema)(schema)
            errors = list(validator.iter_errors(result.structured_content))
        except Exception as exc:  # malformed schema etc. — don't mask the result
            logger.warning("Output validation skipped for %s: %s", name, exc)
            return result

        if errors:
            message = _format_output_validation_error(errors)
            logger.warning("tool call ✗ %s output invalid:\n%s", name, message)
            raise ToolError(message)

        return result


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
