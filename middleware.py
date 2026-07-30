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

import json
import logging
import re
import time

import jsonschema
from jsonschema.validators import validator_for

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import ToolAnnotations

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


# ---------------------------------------------------------------------------
# Upstream error quality + result-size guard
#
# Both directory reviews test these directly. Anthropic's review criteria fail a
# server whose tools return generic "Internal Server Error" / "Bad Request" with
# no detail, and Claude truncates or rejects tool results over ~150,000
# characters (25,000 tokens in Claude Code) — a list endpoint that dumps every
# row will look broken rather than large.
# ---------------------------------------------------------------------------

# FastMCP surfaces upstream failures from OpenAPI-generated tools as a ToolError
# whose message starts with "HTTP error <status>: <reason> - <body>". We match on
# that shape rather than on httpx exception types, which never escape the tool.
_HTTP_ERROR_RE = re.compile(r"HTTP error (?P<status>\d{3})\b")

# How much of the upstream response body to keep in a rewritten error message.
_MAX_UPSTREAM_DETAIL_CHARS = 600

# status -> (what happened, what the caller should do about it)
_STATUS_GUIDANCE: dict[int, tuple[str, str]] = {
    400: (
        "the JobMojito API rejected the request as malformed",
        "Check the argument names and types against the tool schema. "
        "`search_documentation` can confirm what a field expects.",
    ),
    401: (
        "the JobMojito API rejected the credentials",
        "The connection is not authorized (or the session expired). Ask the user to "
        "reconnect/authorize this server, then retry. Do not retry with different "
        "arguments — this is not an input problem.",
    ),
    403: (
        "the signed-in user is not permitted to perform this action",
        "This usually means the wrong merchant is selected. Run "
        "`jobmojito_configuration` to pick a merchant the user owns, then pass that "
        "`merchant_id`. If the user genuinely lacks the permission, say so rather "
        "than retrying.",
    ),
    404: (
        "the requested record does not exist",
        "Verify the identifier. Ids are easy to confuse — an interview is "
        "`interview_def_set_id` on create but `position_id` on get/set-state, and "
        "results use `interview_result_id`, not the row's `id`. Use a `list_*` tool "
        "to find the correct id instead of guessing.",
    ),
    409: (
        "the request conflicts with the current state of the record",
        "Re-read the record with the matching `get_*` tool to see its current state "
        "before retrying.",
    ),
    413: (
        "the request payload was too large for the JobMojito API",
        "Split the input into smaller pieces (for example upload knowledge base "
        "documents one at a time).",
    ),
    422: (
        "the JobMojito API understood the request but rejected its contents",
        "The detail below names the offending field(s). Fix those specific values; "
        "do not resend the same payload.",
    ),
    429: (
        "the JobMojito API rate-limited this account",
        "Wait before retrying, and avoid issuing the same call in a loop.",
    ),
}

_SERVER_ERROR_GUIDANCE = (
    "This is a JobMojito-side failure, not a problem with the arguments. Retrying "
    "the identical call once is reasonable; if it fails again, report the failure "
    "to the user rather than trying variations."
)


class UpstreamErrorMiddleware(Middleware):
    """Rewrite raw upstream HTTP failures into errors an agent can act on.

    The generated API tools raise `ToolError("HTTP error 403: Forbidden - {...}")`.
    That tells a model *what* broke but not *what to do next*, so it tends to
    retry blindly with permuted arguments. We keep the upstream detail (capped),
    and prepend a plain statement of the cause plus the concrete next step.

    Errors we do not recognise are re-raised untouched — this never swallows or
    masks a failure.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            return await call_next(context)
        except ToolError as exc:
            rewritten = self._rewrite(getattr(getattr(context, "message", None), "name", None), str(exc))
            if rewritten is None:
                raise
            raise ToolError(rewritten) from exc

    @staticmethod
    def _rewrite(tool_name: str | None, message: str) -> str | None:
        match = _HTTP_ERROR_RE.search(message)
        if not match:
            return None
        status = int(match.group("status"))
        if status in _STATUS_GUIDANCE:
            cause, next_step = _STATUS_GUIDANCE[status]
        elif 500 <= status <= 599:
            cause = "the JobMojito API returned a server error"
            next_step = _SERVER_ERROR_GUIDANCE
        else:
            return None

        detail = message.split(" - ", 1)[1] if " - " in message else ""
        detail = detail.strip()
        if len(detail) > _MAX_UPSTREAM_DETAIL_CHARS:
            detail = detail[:_MAX_UPSTREAM_DETAIL_CHARS] + " …(truncated)"

        where = f"`{tool_name}` failed" if tool_name else "The call failed"
        parts = [f"{where} with HTTP {status}: {cause}.", f"What to do: {next_step}"]
        if detail:
            parts.append(f"Upstream detail: {detail}")
        return "\n\n".join(parts)


class ResultSizeGuardMiddleware(Middleware):
    """Fail loudly — and usefully — when a tool result is too big for the client.

    Claude caps tool results at roughly 150,000 characters on Claude.ai/Desktop
    (25,000 tokens in Claude Code) and other hosts have their own ceilings. An
    unbounded `list_*` call against a large merchant can sail past that, and the
    result is silently truncated or dropped somewhere downstream — which reads to
    the user as "the tool is broken".

    We check first and raise an actionable error instead, naming the pagination
    arguments to use. Deliberately an error rather than a silent truncation:
    truncating a structured result would corrupt it against its output schema,
    and a half-list that looks complete is worse than an explicit "narrow this".
    """

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if self.max_chars <= 0 or not isinstance(result, ToolResult):
            return result

        size = self._measure(result)
        if size <= self.max_chars:
            return result

        name = getattr(getattr(context, "message", None), "name", "the tool")
        logger.warning("tool call ✗ %s result too large (%d chars)", name, size)
        raise ToolError(
            f"`{name}` returned about {size:,} characters, which exceeds this "
            f"server's {self.max_chars:,}-character result limit and would be "
            "truncated by the client.\n\n"
            "What to do: request a smaller slice rather than retrying the same "
            "call. Most list tools accept `limit` and `offset` — start with "
            "`limit=25` and page through — and narrowing by `merchant_id`, a date "
            "range, or a status filter usually removes the need to page at all. "
            "For a single large record, request only the fields you need."
        )

    @staticmethod
    def _measure(result: ToolResult) -> int:
        """Approximate the serialized size of a tool result, cheaply."""
        total = 0
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                total += len(json.dumps(structured, default=str))
            except Exception:
                total += len(str(structured))
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                total += len(text)
        return total


# ---------------------------------------------------------------------------
# Annotation backfill
# ---------------------------------------------------------------------------


class ToolMetadataBackfillMiddleware(Middleware):
    """Guarantee every advertised tool carries a title and safety hints.

    The OpenAPI-generated tools get their metadata from ``naming.TOOL_META`` via
    ``server._customize_component``. Tools registered by other means — the docs
    tools, the merchant picker, anything a future MCP App provider adds — bypass
    that path entirely, and a single unannotated tool is enough to fail directory
    review.

    Rather than relying on every registration site remembering, this fills any
    gap at list time. The default for an unknown tool is deliberately the *safe*
    one (``destructiveHint=true``), so a newly added tool asks for confirmation
    until someone consciously marks it read-only.
    """

    def __init__(self, overrides: dict[str, dict] | None = None) -> None:
        self.overrides = overrides or {}

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        for tool in tools:
            self._backfill(tool)
        return tools

    def _backfill(self, tool) -> None:
        override = self.overrides.get(tool.name, {})
        annotations = _annotations_as_dict(getattr(tool, "annotations", None))
        changed = False

        title = (
            getattr(tool, "title", None)
            or annotations.get("title")
            or override.get("title")
            or _title_from_name(tool.name)
        )
        if not getattr(tool, "title", None):
            try:
                tool.title = title
            except Exception:  # pragma: no cover - frozen model
                pass
        if not annotations.get("title"):
            annotations["title"] = title
            changed = True

        has_hint = (
            annotations.get("readOnlyHint") is True
            or annotations.get("destructiveHint") is True
        )
        if not has_hint:
            read_only = override.get("readOnlyHint")
            if read_only is True:
                annotations["readOnlyHint"] = True
                annotations.setdefault("destructiveHint", False)
                annotations.setdefault("idempotentHint", True)
            else:
                # Unknown tool: assume it changes state so clients confirm first.
                annotations["readOnlyHint"] = False
                annotations["destructiveHint"] = True
                logger.warning(
                    "Tool %s had no readOnlyHint/destructiveHint; defaulting to "
                    "destructive. Add it to naming.TOOL_META or the backfill "
                    "overrides so the annotation is deliberate.",
                    tool.name,
                )
            annotations.setdefault("openWorldHint", override.get("openWorldHint", True))
            changed = True

        if changed:
            try:
                tool.annotations = ToolAnnotations(**annotations)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not backfill annotations for %s: %s", tool.name, exc)


def _annotations_as_dict(annotations) -> dict:
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return dict(annotations)
    dump = getattr(annotations, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {}


def _title_from_name(name: str) -> str:
    """`list_interview_results` -> `List interview results`."""
    words = name.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else name
