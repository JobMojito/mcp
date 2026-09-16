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
from mcp.types import TextContent, ToolAnnotations

# `naming` holds no imports of its own from this module, so this stays one-way.
from naming import VIEW_PARAM_NAME

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
        # Deliberately "retry the identical call once": that retry is what makes
        # the client meet RejectedTokenGateASGIMiddleware, get a real HTTP 401 +
        # WWW-Authenticate, and re-authenticate. Telling the model to ask the user
        # to reconnect instead leaves the session stuck — no client re-authorizes
        # on the strength of an error string. Do not retry with different
        # arguments; this is not an input problem.
        "The session is no longer valid. Retry this exact call once — that triggers "
        "the server's re-authentication challenge. If it fails a second time, tell "
        "the user to reconnect/authorize this server.",
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


def _remember_upstream_rejection() -> None:
    """Flag the current token so the *next* request gets a real 401 challenge.

    An upstream 401 reaches the client as HTTP 200 with ``isError: true``, which
    no MCP client treats as an auth failure — so nothing re-authenticates, and
    the session stays wedged until the user manually reconnects. Recording the
    token here lets ``lazy_auth.RejectedTokenGateASGIMiddleware`` answer the next
    request with ``401 + WWW-Authenticate`` instead.

    Best-effort by design: outside a request context (unit tests, background
    work) there is no token and this is a no-op. It must never turn an upstream
    error into a different error.
    """
    try:
        from session_verifier import mark_token_rejected
        from upstream import current_bearer_token

        mark_token_rejected(current_bearer_token())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record upstream token rejection: %s", exc)


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
        if status == 401:
            _remember_upstream_rejection()
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


class CuratedDefaultsMiddleware(Middleware):
    """Actually send the curated parameter defaults from ``naming.TOOL_META``.

    A default in an OpenAPI parameter schema is **advisory**: FastMCP's
    ``RequestDirector.build()`` iterates only the arguments the model supplied,
    so a `default` that the model omits is never put on the wire and the API
    applies its own. Lowering `default` in the spec therefore changes what the
    model *reads* but not what it *gets* — which is worse than useless, because
    the tool description then promises a page size the server doesn't deliver.

    This closes that gap: when a curated default exists and the caller omitted
    the argument, we fill it in before the request is built. The spec default and
    this middleware must carry the same value — ``openapi_loader`` writes the
    schema, this sends it, and a test pins them together.

    Only ever fills *absent* arguments. An explicit value from the model always
    wins, so the model can still ask for a bigger page.
    """

    def __init__(self, defaults: dict[str, dict[str, object]] | None = None) -> None:
        self.defaults = defaults or {}

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = getattr(context, "message", None)
        name = getattr(message, "name", None)
        wanted = self.defaults.get(name or "")
        if wanted:
            arguments = getattr(message, "arguments", None)
            if isinstance(arguments, dict):
                for key, value in wanted.items():
                    if arguments.get(key) is None:
                        arguments[key] = value
                        logger.debug("%s: applied default %s=%r", name, key, value)
        return await call_next(context)


# ---------------------------------------------------------------------------
# Response views
# ---------------------------------------------------------------------------


def prune_fields(data: dict, paths) -> dict:
    """Remove ``paths`` from ``data`` in place and return it.

    A path is dotted, with ``[]`` marking an array level:
    ``transcript[].ai_analysis`` drops that field from every transcript entry.
    Missing paths are ignored — the JobMojito response schemas are passthrough,
    so a field named here may legitimately be absent from a given record.
    """
    for path in paths:
        _prune(data, path.split("."))
    return data


def _prune(node, segments: list[str]) -> None:
    head, rest = segments[0], segments[1:]
    is_array = head.endswith("[]")
    key = head[:-2] if is_array else head
    if not isinstance(node, dict) or key not in node:
        return
    if not rest:
        node.pop(key, None)
        return
    child = node[key]
    if is_array:
        if isinstance(child, list):
            for item in child:
                _prune(item, rest)
    else:
        _prune(child, rest)


class ResponseViewMiddleware(Middleware):
    """Serve the MCP-only ``view`` argument: narrow a response the API can't.

    Pagination is the answer to a result that has too many rows. It is no answer
    at all to a single record that is too wide — and
    ``get_interview_result_details`` returns one interview whose per-answer raw
    assessment blobs dominate the payload and interest no agent. The JobMojito
    API has no field-selection parameter, so the projection happens here.

    Two halves, and both are required:

    * ``openapi_loader.inject_view_params`` puts ``view`` in the tool's input
      schema — otherwise the model has no way to ask.
    * this middleware removes it from the arguments *before* the upstream request
      is built (it is not an API parameter) and prunes the response afterwards.

    Pruning rebuilds the ``ToolResult`` from the pruned structured content, so
    the text copy MCP also sends is regenerated from the same data. Trimming only
    one of the two would leave the client holding two different answers.

    Every prunable field is optional in the response schema, so a pruned result
    still passes output validation. Register this middleware AFTER
    ``ResultSizeGuardMiddleware`` and ``OutputValidationErrorMiddleware``:
    registration order is execution order on the way in, which makes it the
    innermost of the three and therefore the first to touch the result on the way
    back out. Both of those must see what the client will actually receive.
    """

    def __init__(self, rules: dict | None = None) -> None:
        self.rules = rules or {}

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = getattr(context, "message", None)
        name = getattr(message, "name", None)
        rules = self.rules.get(name or "")
        if rules is None:
            return await call_next(context)

        requested = None
        arguments = getattr(message, "arguments", None)
        if isinstance(arguments, dict):
            # pop, not get: `view` is ours, and the request builder would either
            # warn about an unmapped argument or put it on the wire.
            requested = arguments.pop(rules.parameter, None)

        result = await call_next(context)

        paths = rules.paths_for(requested if isinstance(requested, str) else None)
        if not paths:
            return result
        if not isinstance(result, ToolResult) or result.structured_content is None:
            return result

        pruned = prune_fields(result.structured_content, paths)
        logger.debug(
            "%s: applied %s=%r (%d field path(s) pruned)",
            name,
            rules.parameter,
            requested or rules.default,
            len(paths),
        )
        return ToolResult(
            structured_content=pruned,
            meta=result.meta,
            is_error=result.is_error,
        )


class ResultSizeGuardMiddleware(Middleware):
    """Fail loudly — and usefully — when a tool result is too big for the client.

    Claude caps tool results at roughly 150,000 characters on Claude.ai/Desktop
    (25,000 tokens in Claude Code) and other hosts have their own ceilings. An
    unbounded `list_*` call against a large merchant can sail past that, and the
    result is silently truncated or dropped somewhere downstream — which reads to
    the user as "the tool is broken".

    We check first and raise an actionable error instead, naming the argument to
    narrow — `limit` for a list, `view` for a single wide record. Deliberately an
    error rather than a silent truncation: truncating a structured result would
    corrupt it against its output schema, and a half-list that looks complete is
    worse than an explicit "narrow this".

    For a **read-only paginated** tool it doesn't come to that: rather than hand
    back an error the model has to act on, the call is re-run once with a page
    size solved from what just overflowed, and the result carries a note saying
    so. See ``_retry_smaller_page`` for why that is safe for `limit` and is never
    done for `view`. ``AUTO_NARROW_OVERSIZED_RESULTS=false`` restores the
    error-only behaviour without a code change.

    Before refusing, it drops the DUPLICATE copy of the payload. An MCP tool
    result carries the same JSON twice — once as text in ``content``, once in
    ``structuredContent`` — because the spec says a tool with an output schema
    SHOULD also return equivalent unstructured content for older clients. FastMCP
    does that automatically (``ToolResult.__init__`` derives ``content`` from
    ``structured_content``), so an 82,000-character record costs 164,000 on the
    wire. When one copy fits and two do not, replacing the text copy with a short
    pointer is strictly better than failing the call.

    Only ever done as a last resort before an error, never routinely: the text
    copy is what clients that ignore ``structuredContent`` render, so dropping it
    trades breadth of client support for a result that at least arrives. The
    replacement is a human-readable note rather than an empty list, so such a
    client shows an explanation instead of silence.
    """

    def __init__(
        self,
        max_chars: int,
        *,
        views: dict | None = None,
        auto_narrow: bool = True,
        retryable: frozenset[str] | None = None,
    ) -> None:
        self.max_chars = max_chars
        #: ``naming.response_view_rules()`` — so the advice can name the `view`
        #: that would fit instead of a `limit` the tool may not even have.
        self.views = views or {}
        self.auto_narrow = auto_narrow
        #: Tool names safe to re-run (``naming.read_only_tool_names()``).
        self.retryable = retryable if retryable is not None else frozenset()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        msg = getattr(context, "message", None)
        name = getattr(msg, "name", "the tool")
        args = getattr(msg, "arguments", None)
        # Read `view` BEFORE the call: ResponseViewMiddleware runs inside this one
        # and pops the argument, so by the time the result comes back it is gone.
        requested_view = (
            args.get(VIEW_PARAM_NAME) if isinstance(args, dict) else None
        )

        result = await call_next(context)
        if self.max_chars <= 0 or not isinstance(result, ToolResult):
            return result

        fitted = self._fit(result, name)
        if fitted is not None:
            return fitted

        narrowed_to = None
        if self.auto_narrow:
            retried = await self._retry_smaller_page(
                context, call_next, name, args, result, requested_view
            )
            if isinstance(retried, ToolResult):
                fitted = self._fit(retried, name)
                if fitted is not None:
                    return self._note_narrowing(fitted, name, args)
                # The smaller page is still too big. Report on THAT one: its size
                # and page size are what the next suggestion should be solved
                # from, and re-reporting the original would send the model back
                # to a limit we just proved does not fit.
                result, narrowed_to = retried, args.get("limit")

        raise ToolError(
            self._too_large_message(name, args, result, requested_view, narrowed_to)
        )

    def _fit(self, result: ToolResult, name: str) -> ToolResult | None:
        """The result as the client should receive it, or None if it can't fit."""
        structured_chars, content_chars = self._measure_parts(result)
        size = structured_chars + content_chars
        if size <= self.max_chars:
            return result
        return self._drop_duplicate_copy(result, name, structured_chars, size)

    def _too_large_message(
        self,
        name: str,
        args,
        result: ToolResult,
        requested_view: str | None,
        narrowed_to: int | None,
    ) -> str:
        """The refusal, sized and worded against the result we actually measured."""
        structured_chars, content_chars = self._measure_parts(result)
        size = structured_chars + content_chars
        # Size the suggestion against the payload rather than the doubled figure —
        # the duplicate would have been dropped on the retry too.
        duplicated = self._has_duplicate(result)
        binding = structured_chars if duplicated else size
        detail = (
            " MCP sends the payload twice (as text and as structured content), so "
            f"the data itself is about {structured_chars:,} characters — still over "
            "the limit on its own."
            if duplicated
            else ""
        )
        attempted = (
            f" This server already retried automatically with `limit={narrowed_to}`, "
            "and that page was still too large, so the numbers below describe the "
            "retry."
            if narrowed_to is not None
            else ""
        )
        logger.warning("tool call ✗ %s result too large (%d chars)", name, size)
        return (
            f"`{name}` returned about {size:,} characters, which exceeds this "
            f"server's {self.max_chars:,}-character result limit and would be "
            f"truncated by the client.{detail}{attempted}\n\n"
            f"What to do: {self._advice(args, binding, name, requested_view)}"
        )

    async def _retry_smaller_page(
        self,
        context: MiddlewareContext,
        call_next,
        name: str,
        args,
        result: ToolResult,
        requested_view: str | None,
    ):
        """Re-run a read-only paginated call with a page size that fits.

        WHY THIS EXISTS
        An oversized result used to be a plain error. Production telemetry says
        the model usually recovers from it (29 Aug 2026: failure at 22:23:44,
        success on the suggested `limit` twelve seconds later; 3 Sep: nineteen
        seconds) — but not always, and on 26 Aug the session simply ended at the
        error. A second read costs ~1.2 s; a failed task costs the whole task.

        WHAT IT WILL NOT DO
        * Anything but a **read**: ``retryable`` holds the curated read-only tool
          names, so a write is never repeated.
        * **Narrow a `view`.** A shorter page is honest — the response envelope's
          ``pagination.has_more`` already says there is more, and the caller asked
          for rows, not for a specific row count. Dropping *fields* the caller
          explicitly asked for would be a silent lie about what a record
          contains, so an oversized `view="full"` is still refused outright.
        * Guess. Without a `limit` argument to solve from, ``_suggested_limit``
          returns None and this does nothing.

        Returns the retried ``ToolResult``, or None when no retry was made.
        """
        if name not in self.retryable or not isinstance(args, dict):
            return None
        structured_chars, content_chars = self._measure_parts(result)
        binding = (
            structured_chars
            if self._has_duplicate(result)
            else structured_chars + content_chars
        )
        suggested = self._suggested_limit(args, binding)
        if suggested is None or suggested == args.get("limit"):
            return None

        args["limit"] = suggested
        # ResponseViewMiddleware popped `view` on the way in. Put it back, or the
        # retry would quietly fall back to the default projection and return
        # different fields than the caller asked for.
        if requested_view is not None:
            args[VIEW_PARAM_NAME] = requested_view
        logger.info(
            "%s: result too large; retrying automatically with limit=%d",
            name,
            suggested,
        )
        try:
            return await call_next(context)
        except Exception:
            # The first call's size error is the more useful one to report — a
            # failure on the retry tells the model nothing about what to change.
            logger.warning("%s: automatic narrowed retry failed", name, exc_info=True)
            return None

    def _note_narrowing(self, result: ToolResult, name: str, args) -> ToolResult:
        """Say that the page was shrunk, so a short list isn't read as a full one."""
        limit = args.get("limit") if isinstance(args, dict) else None
        notice = TextContent(
            type="text",
            text=(
                f"Note: the full-size result of `{name}` exceeded this server's "
                f"{self.max_chars:,}-character limit, so it was re-fetched with "
                f"`limit={limit}`. This is ONE PAGE, not the whole list — check "
                "`pagination.has_more` and page with `offset` if you need the rest."
            ),
        )
        content = [notice, *(result.content or [])]
        structured_chars = self._measure_parts(result)[0]
        if structured_chars + sum(
            len(getattr(b, "text", "") or "") for b in content
        ) > self.max_chars:
            # The note itself tipped it over. Keep the note and drop the text copy
            # — the same trade `_drop_duplicate_copy` makes — and say where the
            # data went, so a client that ignores `structuredContent` isn't left
            # with a note and nothing else.
            content = [
                TextContent(
                    type="text",
                    text=(
                        notice.text
                        + "\n\nThe page itself is in this tool result's "
                        "`structuredContent`; the duplicate text copy MCP normally "
                        "repeats here was omitted to stay inside the limit."
                    ),
                )
            ]
        return result.model_copy(update={"content": content})

    @staticmethod
    def _has_duplicate(result: ToolResult) -> bool:
        """True when the same payload is present as both text and structured content."""
        return result.structured_content is not None and bool(result.content)

    def _drop_duplicate_copy(
        self, result: ToolResult, name: str, structured_chars: int, size: int
    ) -> ToolResult | None:
        """Send only ``structuredContent`` when that is what makes the result fit.

        Returns ``None`` when de-duplicating wouldn't help — no duplicate to drop,
        or the payload is over budget by itself — so the caller raises instead.
        """
        if not self._has_duplicate(result):
            return None
        notice = (
            f"The full result of `{name}` is in this tool result's "
            "`structuredContent`.\n\n"
            "MCP normally repeats the same JSON here as text. Both copies together "
            f"would be about {size:,} characters, past this server's "
            f"{self.max_chars:,}-character limit, so the duplicate was omitted. "
            "Nothing was removed from `structuredContent` — read the result from "
            "there."
        )
        if structured_chars + len(notice) > self.max_chars:
            return None
        logger.info(
            "%s: dropped the duplicate text copy of the result (%d -> %d chars)",
            name,
            size,
            structured_chars + len(notice),
        )
        return result.model_copy(
            update={"content": [TextContent(type="text", text=notice)]}
        )

    def _advice(
        self,
        arguments,
        size: int,
        name: str | None = None,
        requested_view: str | None = None,
    ) -> str:
        """Name the argument that will actually make this call fit.

        A fixed suggestion ("try limit=25") is a guess about row size, and it was
        wrong for exactly the tool that needed it most: avatar rows cost ~4,900
        characters each on the wire, so 25 of them overflow too. Two failed calls
        in a row reads as a broken tool. Since we know both the size that just
        overflowed and the page size that produced it, we can solve for one that
        fits instead of guessing.

        The same mistake in the other direction is naming `limit` for a tool that
        has no `limit`. ``get_interview_result_details`` overflowed in production
        on 10 Sep 2026 at 164,779 characters and was told to "try `limit=10` and
        page through with `offset`" — advice for a list, given to a tool that
        returns one record and carries a `view` argument built for precisely this
        situation. When the tool declares projections, name the narrower view.
        """
        narrower = None
        rules = self.views.get(name or "")
        if rules is not None:
            narrower = rules.narrower_than(requested_view)
        if narrower is not None:
            return (
                f"retry with `{rules.parameter}=\"{narrower}\"`. This tool returns "
                "ONE record, so there is nothing to page through — the "
                f"`{rules.parameter}` argument is how you ask for less of it, and "
                f"`{narrower}` drops the largest machine-only fields while keeping "
                "what a human reviewer reads. The tool's schema lists what each "
                "view returns."
            )

        suggested = self._suggested_limit(arguments, size)
        if suggested is None:
            return (
                "request a smaller slice rather than retrying the same call. Most "
                "list tools accept `limit` and `offset` — try `limit=10` and page "
                "through with `offset` — and narrowing by `merchant_id`, a date "
                "range, or a status filter usually removes the need to page at "
                "all. For a single large record, request only the fields you need."
            )
        return (
            f"retry with `limit={suggested}` (and page through with `offset`, "
            f"checking `pagination.has_more`). That size is calculated from what "
            "this call actually returned, so it should fit. Narrowing by "
            "`merchant_id`, a date range, or a status filter is often better "
            "still — it usually removes the need to page at all."
        )

    def _suggested_limit(self, arguments, size: int) -> int | None:
        """Largest page size that fits in the budget, with 20% headroom.

        Headroom because rows are not uniform — one long URL or free-text field
        in the next page would otherwise put us straight back over the limit.
        """
        if not isinstance(arguments, dict):
            return None
        current = arguments.get("limit")
        if not isinstance(current, int) or isinstance(current, bool) or current <= 0:
            return None
        if size <= 0:
            return None
        fitted = int(current * (self.max_chars / size) * 0.8)
        # Never suggest the caller's own failing value, or a nonsensical one.
        return max(1, min(fitted, current - 1))

    @staticmethod
    def _measure_parts(result: ToolResult) -> tuple[int, int]:
        """``(structuredContent chars, content chars)`` — both go over the wire.

        Kept separate so the guard can tell "the payload itself is too big" from
        "the payload fits but the protocol's duplicate copy pushes it over".
        """
        structured_chars = 0
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                structured_chars = len(json.dumps(structured, default=str))
            except Exception:
                structured_chars = len(str(structured))
        content_chars = 0
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                content_chars += len(text)
        return structured_chars, content_chars

    @classmethod
    def _measure(cls, result: ToolResult) -> int:
        """Approximate the serialized size of a tool result, cheaply."""
        return sum(cls._measure_parts(result))


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
