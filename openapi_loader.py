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

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from config import settings
from naming import operation_id_for

logger = logging.getLogger("jobmojito_mcp.openapi")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


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
        spec = inject_operation_ids(spec)
        _write_cache(spec)
        return spec

    cached = _read_json(settings.cache_path)
    if cached is not None:
        logger.warning("Using cached OpenAPI spec (live fetch unavailable).")
        return inject_operation_ids(cached)

    snapshot = _read_json(settings.snapshot_path)
    if snapshot is not None:
        logger.warning("Using committed OpenAPI snapshot (no live/cache available).")
        return inject_operation_ids(snapshot)

    raise RuntimeError(
        "Could not load the JobMojito OpenAPI spec from live URL, cache, or "
        "snapshot. Check JOBMOJITO_OPENAPI_URL / network, or generate a "
        "snapshot with `python scripts/update_snapshot.py`."
    )
