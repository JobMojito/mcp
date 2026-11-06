"""Load the JobMojito OpenAPI spec — live, with a cached fallback.

Strategy (matches the chosen "fetch live at startup + cached fallback"):

1. Try to fetch the spec live from ``JOBMOJITO_OPENAPI_URL``.
2. On success: inject curated operationIds, persist a fresh local cache, and use it.
3. On failure: fall back to the most recent cache, then to a committed snapshot.

This keeps tools in sync with the API automatically on every (re)deploy/restart,
while staying resilient if the spec endpoint is briefly unavailable at boot.

A committed ``data/openapi.snapshot.json`` is optional but recommended so the
very first cold start has a fallback. Generate it with
``python scripts/update_snapshot.py``.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from config import settings
from naming import (
    VIEW_PARAM_MARKER,
    VIEW_PARAM_NAME,
    meta_for,
    operation_id_for,
    view_parameter_schema,
)

logger = logging.getLogger("jobmojito_mcp.openapi")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
#: Added to the name of a component's response copy (see ``_response_side_components``).
RESPONSE_COPY_SUFFIX = "__nullable_response"


def inject_operation_ids(spec: dict[str, Any]) -> dict[str, Any]:
    """Add curated operationIds to operations that don't have one.

    Mutates and returns the spec. Unknown routes are left untouched (FastMCP
    will auto-name them), so newly added endpoints still surface automatically.
    """
    paths = spec.get("paths", {})
    injected, skipped = 0, 0
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("operationId"):
                continue  # respect any operationId the API may add later
            op_id = operation_id_for(method, path)
            if op_id:
                operation["operationId"] = op_id
                injected += 1
            else:
                skipped += 1
                logger.warning(
                    "No curated name for %s %s — FastMCP will auto-generate one.",
                    method.upper(),
                    path,
                )
    logger.info("Injected %d operationIds (%d routes auto-named).", injected, skipped)
    return spec


def apply_param_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """Override query-parameter defaults declared in ``naming.TOOL_META``.

    Used to shrink page sizes on endpoints whose rows are too large for the
    API's own default to fit in a tool result (see ``naming.PAGE_SIZE_WHY``).
    Applied at load time rather than committed into the snapshot, so it survives
    ``scripts/update_snapshot.py`` and stays visible next to the tool metadata.

    Only the ``default`` changes: ``minimum``/``maximum`` are the API's contract
    and are left alone, so the model can still ask for a bigger page. A default
    outside the spec's own bounds is refused — that would make every call 422.
    """
    applied = 0
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            meta = meta_for(method, path)
            if not meta or not meta.param_defaults:
                continue
            overrides = dict(meta.param_defaults)
            for parameter in operation.get("parameters", []):
                name = parameter.get("name")
                if name not in overrides:
                    continue
                schema = parameter.get("schema")
                if not isinstance(schema, dict):
                    continue
                value = overrides[name]
                low, high = schema.get("minimum"), schema.get("maximum")
                if isinstance(value, (int, float)) and (
                    (low is not None and value < low)
                    or (high is not None and value > high)
                ):
                    logger.warning(
                        "Ignoring default %s=%r for %s: outside the spec's %s..%s "
                        "range, which would make every call fail validation.",
                        name,
                        value,
                        meta.name,
                        low,
                        high,
                    )
                    continue
                if schema.get("default") == value:
                    continue
                logger.info(
                    "%s: default %s %r -> %r (%s)",
                    meta.name,
                    name,
                    schema.get("default"),
                    value,
                    "curated page size",
                )
                schema["default"] = value
                applied += 1
    if applied:
        logger.info("Applied %d curated parameter default(s).", applied)
    return spec


def inject_view_params(spec: dict[str, Any]) -> dict[str, Any]:
    """Add the MCP-only ``view`` argument to endpoints that declare projections.

    This is the half of the feature the model can see: without a parameter in the
    schema there is no way to ask for a narrower response. The other half is
    ``middleware.ResponseViewMiddleware``, which removes the argument before the
    upstream request is built and prunes the response on the way back.

    Declared as a query parameter because that is what FastMCP flattens into a
    plain tool argument for a GET. It is never transmitted — see
    ``naming.VIEW_PARAM_NAME``.

    An endpoint that already declares a real parameter of this name is left
    alone: an API parameter must win over ours, or we would silently swallow it.
    A parameter WE injected on an earlier load is replaced rather than kept —
    ``_write_cache`` persists the prepared spec, so a server booting from cache
    would otherwise be stuck with whatever views were defined when the cache was
    written. That is what ``VIEW_PARAM_MARKER`` distinguishes.
    """
    injected = 0
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            meta = meta_for(method, path)
            if not meta or not meta.views:
                continue
            parameters = operation.setdefault("parameters", [])
            existing = [
                p
                for p in parameters
                if isinstance(p, dict) and p.get("name") == VIEW_PARAM_NAME
            ]
            if any(not p.get(VIEW_PARAM_MARKER) for p in existing):
                logger.warning(
                    "%s %s declares its own %r parameter — not injecting the "
                    "response-view argument.",
                    method.upper(),
                    path,
                    VIEW_PARAM_NAME,
                )
                continue
            for stale in existing:  # a previously injected copy, from the cache
                parameters.remove(stale)
            parameter = view_parameter_schema(meta)
            if parameter is None:  # pragma: no cover - guarded by meta.views above
                continue
            parameters.append(parameter)
            injected += 1
    if injected:
        logger.info("Injected the %r argument on %d endpoint(s).", VIEW_PARAM_NAME, injected)
    return spec


def _json_body_properties(spec: dict[str, Any], operation: dict[str, Any]) -> list[dict[str, Any]]:
    """The top-level property schemas of an operation's JSON request body."""
    content = (operation.get("requestBody") or {}).get("content") or {}
    schema = (content.get("application/json") or {}).get("schema") or {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec.get("components", {}).get("schemas", {}).get(name, {})
    return [p for p in (schema.get("properties") or {}).values() if isinstance(p, dict)]


def apply_description_fixes(spec: dict[str, Any]) -> dict[str, Any]:
    """Apply ``ToolMeta.description_fixes`` to the text the model reads.

    The spec describes the HTTP API, where a null does arrive, so the correction
    cannot go upstream. Applied at load time rather than committed into the snapshot,
    so it survives ``scripts/update_snapshot.py``.

    Text already corrected is fine (the cached spec is prepared again on the next
    boot). Neither the old nor the new text means the API's wording changed, so the
    tool may be promising a null again: that is logged, and the snapshot test fails.
    """
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            meta = meta_for(method, path)
            if not meta or not meta.description_fixes:
                continue
            targets = [operation, *_json_body_properties(spec, operation)]
            for old, new in meta.description_fixes:
                seen = False
                for target in targets:
                    text = target.get("description")
                    if not isinstance(text, str):
                        continue
                    seen = seen or old in text or new in text
                    target["description"] = text.replace(old, new)
                if not seen:
                    logger.warning(
                        "%s: description text to correct was not found: %r",
                        meta.name,
                        old,
                    )
    return spec


def _allow_null(prop: dict[str, Any]) -> None:
    """Make a single property schema accept null (it stays the same type otherwise)."""
    t = prop.get("type")
    if isinstance(t, str):
        if t != "null":
            prop["type"] = [t, "null"]
    elif isinstance(t, list):
        if "null" not in t:
            prop["type"] = [*t, "null"]
    # If the field constrains values to an enum, null must also be a legal member —
    # otherwise widening `type` to allow null isn't enough and a null value still
    # fails the enum check ("None is not one of [...]"). Matters for nullable enum
    # RESPONSE fields (e.g. status / recommendation).
    enum = prop.get("enum")
    if isinstance(enum, list) and None not in enum:
        prop["enum"] = [*enum, None]
    # OpenAPI 3.0 sites also honor this; harmless under 3.1.
    if "type" in prop:
        prop["nullable"] = True


def _refs_under(node: Any) -> Iterator[str]:
    """Every ``$ref`` string anywhere under a node."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _refs_under(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs_under(value)


def _reachable_components(spec: dict[str, Any], roots: list[Any]) -> set[tuple[str, str]]:
    """(section, name) of every component the roots reach, following nested ``$ref``s."""
    components = spec.get("components") or {}
    seen: set[tuple[str, str]] = set()
    todo = list(roots)
    while todo:
        for ref in _refs_under(todo.pop()):
            parts = ref.split("/")
            if len(parts) < 4 or parts[:2] != ["#", "components"]:
                continue
            key = (parts[2], parts[3])
            if key not in seen and key[1] in (components.get(key[0]) or {}):
                seen.add(key)
                todo.append(components[key[0]][key[1]])
    return seen


def _operation_parts(spec: dict[str, Any]) -> tuple[list[Any], list[Any], list[Any]]:
    """Split ``paths`` into responses, request bodies and everything else."""
    responses: list[Any] = []
    bodies: list[Any] = []
    others: list[Any] = []
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for key, value in path_item.items():
            if key.lower() in _HTTP_METHODS and isinstance(value, dict):
                for part, content in value.items():
                    if part == "responses":
                        responses.append(content)
                    else:
                        (bodies if part == "requestBody" else others).append(content)
            else:
                others.append(value)  # path-level parameters
    return responses, bodies, others


def _point_refs_at(node: Any, copies: dict[str, str]) -> None:
    """Rewrite ``$ref``s to the copied components, sub-pointers included."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            base = "/".join(ref.split("/")[:4])
            if base in copies:
                node["$ref"] = copies[base] + ref[len(base) :]
        for value in node.values():
            _point_refs_at(value, copies)
    elif isinstance(node, list):
        for value in node:
            _point_refs_at(value, copies)


def _response_side_components(
    spec: dict[str, Any], responses: list[Any], inputs: list[Any]
) -> set[tuple[str, str]]:
    """The components only responses use, after separating them from the input side.

    A component both sides use gets a copy for the responses, so widening it cannot
    reach the input. The request keeps the original: it is the name the model reads.
    Nothing is widened in place, so a spec prepared earlier (the runtime cache) has no
    component left on both sides and is not copied twice.
    """
    response_side = _reachable_components(spec, responses)
    shared = response_side & _reachable_components(spec, inputs)
    components = spec.get("components") or {}
    copies = {}
    for section, name in shared:
        copy_name = f"{name}{RESPONSE_COPY_SUFFIX}"
        components[section][copy_name] = copy.deepcopy(components[section][name])
        copies[f"#/components/{section}/{name}"] = f"#/components/{section}/{copy_name}"
    result = (response_side - shared) | {
        (section, f"{name}{RESPONSE_COPY_SUFFIX}") for section, name in shared
    }
    if copies:
        _point_refs_at(responses, copies)
        for section, name in result:
            _point_refs_at(components[section][name], copies)
    return result


def _keep_declared_nullable(node: Any) -> None:
    """Express the spec's 3.0-style ``nullable: true`` in a form 3.1 tooling honours."""
    if isinstance(node, dict):
        if node.get("nullable") is True and "type" in node:
            _allow_null(node)
        for value in node.values():
            _keep_declared_nullable(value)
    elif isinstance(node, list):
        for value in node:
            _keep_declared_nullable(value)


def relax_nullable_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Allow null on every typed property of a response, and only what is declared elsewhere.

    The JobMojito API returns null for many fields the OpenAPI declares as
    non-nullable (e.g. external_id, emoji, slug). With output validation on, that
    triggers "None is not of type 'string'", so anything a response can reach accepts
    null whatever it declares.

    Input is the opposite. A tool's input schema is what the model reads, and a null it
    offers on a field the API refuses invites stray nulls, so input fields accept null
    only where the spec says ``nullable: true``. Parameters stay as declared: a query
    string cannot carry a null, so offering one there means nothing. See
    ``_response_side_components`` for a component both sides use. ``required`` arrays are
    left untouched everywhere.
    """
    responses, bodies, others = _operation_parts(spec)
    response_side = _response_side_components(spec, responses, [*bodies, *others])
    components = spec.get("components") or {}
    relaxed = 0

    def walk(node: Any) -> None:
        nonlocal relaxed
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for ps in props.values():
                    if isinstance(ps, dict) and ("type" in ps):
                        _allow_null(ps)
                        relaxed += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    _keep_declared_nullable([responses, bodies])
    for section, entries in components.items():
        if section != "parameters":
            _keep_declared_nullable(entries)
    walk(responses)
    for section, name in response_side:
        walk(components[section][name])
    logger.info("Relaxed %d response properties to be nullable.", relaxed)
    return spec


def prefer_json_request_bodies(spec: dict[str, Any]) -> dict[str, Any]:
    """Make JSON the body a tool sends when an endpoint accepts several.

    FastMCP sends the first content type an operation declares. A tool's arguments
    are JSON, so through multipart a file arrives as a text field (the API cannot read
    it) and a list as Python's ``str(list)``. The JSON form carries both as they are.
    """
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            content = (operation.get("requestBody") or {}).get("content") or {}
            if "application/json" in content and next(iter(content)) != "application/json":
                operation["requestBody"]["content"] = {
                    "application/json": content["application/json"],
                    **{k: v for k, v in content.items() if k != "application/json"},
                }
    return spec


def _prepare(spec: dict[str, Any]) -> dict[str, Any]:
    """Inject curated operationIds/defaults, relax response fields, keep input narrow.

    Order matters: ``apply_param_defaults`` and ``inject_view_params`` look
    endpoints up by (method, path) in ``TOOL_META``, so they are independent of
    the operationId pass, but all of them must run before
    ``relax_nullable_schemas`` walks the tree.

    ``relax_nullable_schemas`` widens responses only, so the injected ``view`` enum
    is left alone — which is what we want: it is an INPUT, and adding ``null`` to its
    enum would advertise a value that means nothing.
    """
    return relax_nullable_schemas(
        apply_description_fixes(
            prefer_json_request_bodies(
                inject_view_params(apply_param_defaults(inject_operation_ids(spec)))
            )
        )
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to read %s: %s", path, exc)
    return None


def _write_cache(spec: dict[str, Any]) -> None:
    try:
        settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
        settings.cache_path.write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Refreshed OpenAPI cache at %s", settings.cache_path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not write OpenAPI cache: %s", exc)


def _fetch_live(timeout: float = 15.0) -> dict[str, Any] | None:
    try:
        resp = httpx.get(settings.openapi_url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        spec = resp.json()
        if not isinstance(spec, dict) or "paths" not in spec:
            logger.warning("Live OpenAPI response missing 'paths'; ignoring.")
            return None
        logger.info(
            "Fetched live OpenAPI spec (%d paths).", len(spec.get("paths", {}))
        )
        return spec
    except Exception as exc:
        logger.warning("Live OpenAPI fetch failed (%s): %s", settings.openapi_url, exc)
        return None


def load_openapi_spec() -> dict[str, Any]:
    """Return a ready-to-use OpenAPI spec with curated operationIds injected.

    Order of preference: live fetch -> local cache -> committed snapshot.
    Raises RuntimeError only if every source fails.
    """
    spec = _fetch_live()
    if spec is not None:
        spec = _prepare(spec)
        _write_cache(spec)
        return spec

    cached = _read_json(settings.cache_path)
    if cached is not None:
        logger.warning("Using cached OpenAPI spec (live fetch unavailable).")
        return _prepare(cached)

    snapshot = _read_json(settings.snapshot_path)
    if snapshot is not None:
        logger.warning("Using committed OpenAPI snapshot (no live/cache available).")
        return _prepare(snapshot)

    raise RuntimeError(
        "Could not load the JobMojito OpenAPI spec from live URL, cache, or "
        "snapshot. Check JOBMOJITO_OPENAPI_URL / network, or generate a "
        "snapshot with `python scripts/update_snapshot.py`."
    )
