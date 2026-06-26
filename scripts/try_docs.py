"""Local smoke test for the JobMojito MCP server (macOS/dev).

Connects to the server in-process (no HTTP, no Supabase login) and exercises the
documentation tools, including the federated Mintlify developer-docs search.

Usage:
    # from the repo root, with your .env populated:
    ENABLE_AUTH=false python scripts/try_docs.py "how do I create an interview"

What it checks:
  - the server builds and lists its tools
  - the Featurebase help search (search_documentation) returns results
  - the Mintlify developer-docs search returns results (if creds are set)

Note: this makes real network calls to developer.jobmojito.com and Featurebase,
so run it from a machine with internet access (not a locked-down CI sandbox).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("ENABLE_AUTH", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _text(result) -> str:
    # FastMCP call results expose content blocks; join any text we find.
    blocks = getattr(result, "content", None) or []
    parts = [getattr(b, "text", "") for b in blocks]
    out = "\n".join(p for p in parts if p)
    return out or str(getattr(result, "structured_content", result))


async def main(query: str) -> None:
    from fastmcp import Client

    import server  # builds server.mcp

    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print(f"\n{len(names)} tools exposed:")
        for n in names:
            print("  -", n)

        # 1) Help-center / built-in documentation search.
        print(f"\n--- search_documentation({query!r}) ---")
        res = await client.call_tool("search_documentation", {"query": query})
        print(_text(res)[:1500])

        # 2) Mintlify developer-docs search (tool name is site-specific).
        dev_tool = next(
            (n for n in names if n.startswith("search_") and "developer" in n.lower()),
            None,
        )
        if dev_tool:
            print(f"\n--- {dev_tool}({query!r}) ---")
            res = await client.call_tool(dev_tool, {"query": query})
            print(_text(res)[:1500])
        else:
            print(
                "\n(no Mintlify developer-docs tool mounted — set "
                "DEVELOPER_DOCS_MCP_CLIENT_ID/SECRET in .env to enable)"
            )


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "how do I create an interview"
    asyncio.run(main(q))
