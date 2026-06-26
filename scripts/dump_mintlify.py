"""Diagnostic: dump the raw Mintlify developer-docs search result.

Run on a machine with internet access:
    python scripts/dump_mintlify.py "how do I create an interview"

Prints the tool name, the structured/data payload, and the raw text content so
we can write an accurate parser for `search_documentation`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(query: str) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    import mintlify
    from config import settings

    if not settings.developer_docs_mcp_url:
        print("DEVELOPER_DOCS_MCP_URL not set.")
        return

    auth = mintlify.build_auth() if settings.developer_docs_uses_auth else None
    transport = StreamableHttpTransport(url=settings.developer_docs_mcp_url, auth=auth)

    async with Client(transport) as client:
        tools = await client.list_tools()
        search = next(
            (t.name for t in tools if "search" in t.name.lower() and "filesystem" not in t.name.lower()),
            None,
        )
        print("=== tools ===", [t.name for t in tools])
        print("=== search tool ===", search)
        if not search:
            return
        result = await client.call_tool(search, {"query": query})

    print("\n=== result type ===", type(result).__name__)
    for attr in ("data", "structured_content"):
        val = getattr(result, attr, None)
        print(f"\n=== result.{attr} ===")
        try:
            print(json.dumps(val, indent=2)[:3000])
        except Exception:
            print(repr(val)[:3000])

    print("\n=== result.content (text blocks) ===")
    for i, block in enumerate(getattr(result, "content", []) or []):
        text = getattr(block, "text", None)
        print(f"--- block {i} ({type(block).__name__}) ---")
        print((text or repr(block))[:3000])


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "how do I create an interview"
    asyncio.run(main(q))
