"""Short tool definitions in tools/list, the full ones on demand.

A client hands every tool's name, description and input schema to the model before
the first call. Generated straight from the OpenAPI spec, that was about 286 KB
(~73k tokens) for this server, most of it prose: per-option essays, examples, the
descriptions of nested fields, and the spec's own operation text repeated under the
curated hint. The output schemas (another ~230 KB) are for validation and are not
shown to the model at all.

So, per generated tool (``server._customize_component`` calls ``capture`` and then
``trim``):

* the full definition is kept here, exactly as it was generated;
* the advertised description is the "[Category] hint" line, where the curated
  rules live, plus a pointer to ``get_tool_reference``;
* each top-level argument keeps its first sentence and every sentence that states a
  behaviour (null, an empty value, omit, only, never, must, replace, default,
  e-mail, dry_run, a status code...), so the rules a model needs to call the tool
  correctly stay where it reads them; the "Options —" essays, examples and the
  descriptions of nested fields go;
* a field whose spec carries ``x-mcp-description`` shows that text instead of
  the trimmed one: the source's own short version, for where trimming reads badly;
* the output schema keeps its structure (validation is unchanged) and loses its
  annotations.

``get_tool_reference`` returns the full definition. It reads nothing but this
module's copy of the spec, so it needs no token: ``lazy_auth`` serves a call to it
anonymously, like ``tools/list``. Its ``tool`` argument is an enum of the captured
tool names.
"""

# No `from __future__ import annotations`: FastMCP reads get_tool_reference's
# signature at runtime, and the enum is a local built from the captured names.
import copy
import re
from typing import Annotated, Any, Literal

from pydantic import Field

TOOL_NAME = "get_tool_reference"

#: The full definitions, by tool name, captured before trimming.
FULL: dict[str, dict[str, Any]] = {}

POINTER = f"Every argument in full, with examples: {TOOL_NAME}."

# A sentence that states how a value behaves is kept even when it is not the first.
_RULE = re.compile(
    r"\bnull\b|empty (string|object|array|list)|\bomit|left out|\bonly\b|\bnever\b|\bmust\b"
    r"|\bwhole\b|replace|required|cannot|refused|\b4\d\d\b|not sent|default|e-?mail"
    r"|irreversible|dry_run|\bsend `|\buse `|\bkeep",
    re.IGNORECASE,
)
# Sentence ends: . ! ? then whitespace (a rule often starts lower case: "null asks
# all questions."); not after "e.g." / "i.e.", and not inside a number (0.9). A line
# break also ends one.
_SENTENCE_END = re.compile(r"(?<=[.!?])(?<!e\.g\.)(?<!i\.e\.)\s+(?=\S)|\n+")
#: A field's own short text, written in the spec next to its description. tools/list
#: shows it instead of the trimmed description; the docs (Mintlify) ignore x- keys.
OVERRIDE = "x-mcp-description"
_ANNOTATIONS = ("description", "example", "examples", "title", OVERRIDE)


def capture(name: str, *, title: str | None, description: str, input_schema: dict | None, output_schema: dict | None) -> None:
    FULL[name] = {
        "title": title,
        "description": description,
        "input_schema": _without_overrides(copy.deepcopy(input_schema or {})),
        "output_schema": _without_overrides(copy.deepcopy(output_schema)) if output_schema else None,
    }


def _without_overrides(schema: dict) -> dict:
    """The full definition has the full texts: the short ones are tools/list's. FastMCP's
    own markers (x-fastmcp-top-level-schema) mean nothing to a model either."""

    def walk(node: dict) -> None:
        node.pop(OVERRIDE, None)
        for key in [k for k in node if isinstance(k, str) and k.startswith("x-fastmcp")]:
            del node[key]
        for key in ("$defs", "definitions"):
            for child in (node.get(key) or {}).values():
                if isinstance(child, dict):
                    walk(child)
        for child, _ in _children(node):
            walk(child)

    walk(schema)
    return schema


_CODE = re.compile(r"`[^`\n]*`")
# An option entry: `value`, an optional "(DEFAULT when omitted)", a colon.
_OPTION = re.compile(r"`([^`\n]+)`(\s*\([^)\n]*\))?:\s*(.*)", re.S)
_OPTION_BAR = re.compile(r"\s+\|\s+(?=`[^`\n]+`(?:\s*\([^)\n]*\))?:)")
_OPTION_LINE = re.compile(r"^\s*- (?=`[^`\n]+`(?:\s*\([^)\n]*\))?:)", re.M)
_INLINE_OPTIONS = re.compile(r"`[^`\n]+`(?:\s*\([^)\n]*\))?:\s")


def _sentences(text: str) -> list[str]:
    """Split into sentences, never inside a `code span` (".jm-site h1 { ... }")."""
    spans: list[str] = []

    def hide(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    hidden = _CODE.sub(hide, text)
    out = []
    for s in _SENTENCE_END.split(hidden):
        if s and s.strip():
            out.append(re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], s.strip()))
    return out


def _split_options(text: str) -> tuple[str, list[str]]:
    """(the text before the options, one entry per option) for the three ways the
    spec lists options: "Options — `a`: … | `b`: …", "- `a`: …" lines, and
    "`a`: … | `b`: …" inline after the field's own sentences."""
    if "Options — " in text:
        head, rest = text.split("Options — ", 1)
        return head, _OPTION_BAR.split(rest.strip())
    lines = list(_OPTION_LINE.finditer(text))
    if lines:
        head = text[: lines[0].start()]
        bounds = [m.end() for m in lines] + [len(text)]
        return head, [text[bounds[i] : lines[i + 1].start() if i + 1 < len(lines) else len(text)] for i in range(len(lines))]
    if _OPTION_BAR.search(text):
        first = _INLINE_OPTIONS.search(text)
        if first:
            return text[: first.start()], _OPTION_BAR.split(text[first.start() :].strip())
    return text, []


def _option_gist(entry: str) -> str | None:
    """`value`: the first sentence of what it means, without a label that repeats it."""
    m = _OPTION.match(entry.strip())
    if not m:
        return None
    name, qualifier, body = m.group(1), (m.group(2) or "").strip(), m.group(3)
    sentences = _sentences(body)
    gist = sentences[0] if sentences else body.strip()
    label, dash, rest = gist.partition(" — ")
    # "Pre-screening — quick qualification check…": the label repeats the value.
    same = label.lower().replace("-", " ") == name.lower().replace("-", " ").replace("_", " ")
    if dash and (same or len(label.split()) <= 3):
        gist = rest
    gist = _first_clause(gist.rstrip(". "))
    return f"`{name}`{' ' + qualifier if qualifier else ''}: {gist}"


_CLAUSE_END = re.compile(r" — |; |: | \(")
_GIST_WORDS = 10


def _first_clause(text: str) -> str:
    """A long option meaning cut at its first clause break after a few words: enough to
    choose by; get_tool_reference has the rest."""
    if len(text.split()) <= _GIST_WORDS:
        return text
    for m in _CLAUSE_END.finditer(text):
        if len(text[: m.start()].split()) >= 3:
            return text[: m.start()]
    return text


def brief(text: str, *, options: bool = False) -> str:
    """The first sentence and every sentence that states a behaviour; with
    ``options`` (an enum argument) also one line per option: what it means."""
    head, entries = _split_options(text)
    sentences = _sentences(head.strip())
    out = " ".join([sentences[0], *(s for s in sentences[1:] if _RULE.search(s))]) if sentences else head.strip()
    if options and entries:
        gists = [g for g in (_option_gist(e) for e in entries) if g]
        if gists:
            out = f"{out} Values: {' | '.join(gists)}.".strip()
    return out


def _is_string_flag(node: dict) -> bool:
    """A query flag typed as the strings 'true' / 'false', which a model sends as a boolean."""
    enum = node.get("enum")
    return isinstance(enum, list) and set(enum) - {None} == {"true", "false"}


def _has_enum(node: dict) -> bool:
    if "enum" in node:
        return True
    return any(isinstance(c, dict) and "enum" in c for c in [*(node.get("anyOf") or []), *(node.get("oneOf") or []), *(node.get("allOf") or [])])


def _children(node: dict) -> list[tuple[dict, bool]]:
    """(child schema, one level deeper?) for every schema nested in this one."""
    out: list[tuple[dict, bool]] = []
    for key in ("properties", "patternProperties"):
        if isinstance(node.get(key), dict):
            out += [(v, True) for v in node[key].values() if isinstance(v, dict)]
    for key in ("items", "additionalProperties", "not", "contains"):
        if isinstance(node.get(key), dict):
            out.append((node[key], True))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(node.get(key), list):
            out += [(v, False) for v in node[key] if isinstance(v, dict)]
    return out


def _borrowed_text(node: dict, shared: dict[str, str]) -> str | None:
    """The text of an argument that has none of its own, taken (and removed) from an
    allOf/anyOf/oneOf part, else from the shared definition it references: zod puts a
    field's description there, where the trimmer would drop it as nested."""
    parts = [p for key in ("allOf", "anyOf", "oneOf") for p in (node.get(key) or []) if isinstance(p, dict)]
    for part in parts:
        if isinstance(part.get("description"), str):
            return part.pop("description")
    for candidate in (node, *parts):
        ref = candidate.get("$ref")
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in shared:
            return shared[ref.rsplit("/", 1)[-1]]
    return None


def trim_input_schema(schema: dict) -> dict:
    """Top-level arguments brief, nested descriptions and every example gone. In place."""
    # Read before the walk below trims them: an argument that only references one borrows its text.
    shared = {
        name: (d.get(OVERRIDE) or d.get("description"))
        for key in ("$defs", "definitions")
        for name, d in ((schema.get(key) or {}).items() if isinstance(schema, dict) else ())
        if isinstance(d, dict) and isinstance(d.get(OVERRIDE) or d.get("description"), str)
    }

    def walk(node: dict, depth: int) -> None:
        # Shared definitions are always nested: FastMCP inlines them where they are used.
        for key in ("$defs", "definitions"):
            for child in (node.get(key) or {}).values():
                if isinstance(child, dict):
                    walk(child, 2)
        node.pop("example", None)
        node.pop("examples", None)
        short = node.pop(OVERRIDE, None)
        text = node.get("description")
        if depth == 1 and not isinstance(text, str) and not isinstance(short, str):
            text = _borrowed_text(node, shared)
            if text is not None:
                node["description"] = text
        if depth == 1 and isinstance(short, str) and short.strip():
            node["description"] = short.strip()
        elif isinstance(text, str):
            if depth >= 2:
                del node["description"]
            elif depth == 1:
                node["description"] = brief(text, options=_has_enum(node))
        if depth == 1 and _is_string_flag(node) and "'true'" not in node.get("description", ""):
            node["description"] = (node.get("description", "") + " Send the string 'true' or 'false'.").strip()
        for child, deeper in _children(node):
            walk(child, depth + 1 if deeper else depth)

    if isinstance(schema, dict):
        walk(schema, 0)
    return schema


def strip_annotations(schema: dict) -> dict:
    """Remove description, title and examples from every schema node; keep the structure. In place."""

    def walk(node: dict) -> None:
        for key in _ANNOTATIONS:
            node.pop(key, None)
        for key in ("$defs", "definitions"):
            for child in (node.get(key) or {}).values():
                if isinstance(child, dict):
                    walk(child)
        for child, _ in _children(node):
            walk(child)

    if isinstance(schema, dict):
        walk(schema)
    return schema


def register(mcp) -> None:
    """Add get_tool_reference, its ``tool`` an enum of the captured tool names."""
    names = tuple(sorted(FULL))
    if not names:  # pragma: no cover - a spec without endpoints
        return
    ToolName = Literal[names]  # type: ignore[valid-type]

    @mcp.tool(
        name=TOOL_NAME,
        annotations={
            "title": "Get a tool's full reference",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def get_tool_reference(
        tool: Annotated[ToolName, Field(description="The tool to describe.")],  # type: ignore[valid-type]
        part: Annotated[
            Literal["arguments", "result", "all"],
            Field(description="arguments (default): every argument in full; result: what the tool returns; all: both."),
        ] = "arguments",
    ) -> dict:
        """The full definition of one JobMojito tool: its complete description, and
        every argument with all its options and examples (part=result: every field it
        returns). tools/list carries short versions; call this when you need the
        options of an argument, its exact format or an example."""
        full = FULL[tool]
        out: dict[str, Any] = {"tool": tool, "title": full["title"], "description": full["description"]}
        if part in ("arguments", "all"):
            out["arguments"] = full["input_schema"]
        if part in ("result", "all"):
            out["result"] = full["output_schema"]
        return out
