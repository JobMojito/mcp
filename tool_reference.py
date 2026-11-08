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
_ANNOTATIONS = ("description", "example", "examples", "title")


def capture(name: str, *, title: str | None, description: str, input_schema: dict | None, output_schema: dict | None) -> None:
    FULL[name] = {
        "title": title,
        "description": description,
        "input_schema": copy.deepcopy(input_schema or {}),
        "output_schema": copy.deepcopy(output_schema) if output_schema else None,
    }


def brief(text: str) -> str:
    """The first sentence and every sentence that states a behaviour."""
    head = text.split("Options — ", 1)[0].strip()
    sentences = [s.strip() for s in _SENTENCE_END.split(head) if s and s.strip()]
    if not sentences:
        return head
    return " ".join([sentences[0], *(s for s in sentences[1:] if _RULE.search(s))])


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


def trim_input_schema(schema: dict) -> dict:
    """Top-level arguments brief, nested descriptions and every example gone. In place."""

    def walk(node: dict, depth: int) -> None:
        # Shared definitions are always nested: FastMCP inlines them where they are used.
        for key in ("$defs", "definitions"):
            for child in (node.get(key) or {}).values():
                if isinstance(child, dict):
                    walk(child, 2)
        node.pop("example", None)
        node.pop("examples", None)
        text = node.get("description")
        if isinstance(text, str):
            if depth >= 2:
                del node["description"]
            elif depth == 1:
                node["description"] = brief(text)
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
        annotations={"title": "Get a tool's full reference", "readOnlyHint": True, "openWorldHint": False},
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
