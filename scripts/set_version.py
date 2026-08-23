#!/usr/bin/env python3
"""Set the server version everywhere it has to appear.

    python scripts/set_version.py 1.0.3
    python scripts/set_version.py --check

`server.py:SERVER_VERSION` is the single source of truth. `pyproject.toml`
derives from it automatically (`[tool.setuptools.dynamic]`), so it is never
touched here. `server.json` cannot derive from anything — `mcp-publisher` reads
that file directly and JSON has no way to reference another file — so this
script writes it, and `--check` is what CI and the test suite use to prove the
two have not drifted.

Registry versions are immutable, so a version that has already been published
cannot be reused; bump rather than amend.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "server.py"
SERVER_JSON = ROOT / "server.json"

_VERSION_RE = re.compile(r'^SERVER_VERSION = "([^"]+)"', re.M)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def read_code_version() -> str:
    match = _VERSION_RE.search(SERVER_PY.read_text(encoding="utf-8"))
    if not match:
        sys.exit("server.py: could not find `SERVER_VERSION = \"...\"`")
    return match.group(1)


def read_json_version() -> str:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))["version"]


def write_versions(version: str) -> None:
    source = SERVER_PY.read_text(encoding="utf-8")
    updated, count = _VERSION_RE.subn(f'SERVER_VERSION = "{version}"', source, count=1)
    if count != 1:
        sys.exit("server.py: SERVER_VERSION assignment not found")
    SERVER_PY.write_text(updated, encoding="utf-8")

    # Rewritten as text, not json.dump: server.json is hand-maintained and
    # reformatting it would bury the version change in an unreadable diff.
    raw = SERVER_JSON.read_text(encoding="utf-8")
    updated_json, count = re.subn(
        r'^(\s*)"version": "[^"]+"', rf'\g<1>"version": "{version}"', raw, count=1, flags=re.M
    )
    if count != 1:
        sys.exit('server.json: top-level "version" not found')
    SERVER_JSON.write_text(updated_json, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", nargs="?", help="new version, e.g. 1.0.3")
    parser.add_argument("--check", action="store_true", help="verify server.py and server.json agree")
    args = parser.parse_args()

    if args.check:
        code, manifest = read_code_version(), read_json_version()
        if code != manifest:
            print(f"version mismatch: server.py={code} server.json={manifest}", file=sys.stderr)
            print("run: python scripts/set_version.py " + code, file=sys.stderr)
            return 1
        print(f"version consistent: {code}")
        return 0

    if not args.version:
        print(f"current version: {read_code_version()}")
        print("pass a version to change it, or --check to verify consistency")
        return 0

    if not _SEMVER_RE.match(args.version):
        print(f"not a valid semver: {args.version}", file=sys.stderr)
        return 1

    previous = read_code_version()
    write_versions(args.version)
    print(f"{previous} -> {args.version}  (server.py, server.json; pyproject derives automatically)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
