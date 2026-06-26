"""Refresh the committed OpenAPI snapshot used as an offline fallback.

Run locally / in CI whenever you want to update the bundled fallback spec:

    python scripts/update_snapshot.py

The running server does NOT need this — it fetches live at startup and writes a
runtime cache. This snapshot only guarantees a fallback on a cold start when the
live endpoint is unreachable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naming import operation_id_for  # noqa: E402  (after sys.path tweak)

OPENAPI_URL = os.environ.get(
    "JOBMOJITO_OPENAPI_URL", "https://cool.jobmojito.com/functions/v1/openapi"
)
SNAPSHOT_PATH = ROOT / "data" / "openapi.snapshot.json"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def main() -> int:
    print(f"Fetching {OPENAPI_URL} ...")
    resp = httpx.get(OPENAPI_URL, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    spec = resp.json()

    # Inject curated operationIds so the snapshot matches runtime behavior.
    injected = 0
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            if not op.get("operationId"):
                op_id = operation_id_for(method, path)
                if op_id:
                    op["operationId"] = op_id
                    injected += 1

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Wrote {SNAPSHOT_PATH} — {len(spec.get('paths', {}))} paths, "
        f"{injected} operationIds injected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
