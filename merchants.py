"""Merchant selection.

Many JobMojito endpoints accept a `merchant_id`. For users who can act across
multiple merchants (e.g. an account with sub-merchants), `list_my_merchants`
returns the user's own account plus any sub-merchants so the user can pick one.
The chosen `merchant_id` is then passed by the model on subsequent tool calls
(omit it to use the user's own account, which the API derives from the JWT).

UI-capable clients (MCP Apps, `io.modelcontextprotocol/ui`) also get a first-party
clickable picker via the `Choice` app, added as a provider. Text-only clients use
the structured list returned by `list_my_merchants`.
"""

from __future__ import annotations

import logging

from fastmcp import Context
from fastmcp.apps.config import UI_EXTENSION_ID

from upstream import build_api_client

logger = logging.getLogger("jobmojito_mcp.merchants")

_ID_KEYS = ("merchant_id", "id", "sub_merchant_id")
_NAME_KEYS = ("name", "merchant_name", "display_name", "displayName", "title")


def _pick(d: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


async def _fetch_sub_merchants() -> list[dict]:
    """Fetch sub-merchants via the JobMojito API (forwards the user's token)."""
    async with build_api_client() as client:
        resp = await client.get("/merchant-sub-merchant-list")
        resp.raise_for_status()
        payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        mid = _pick(item, _ID_KEYS)
        if not mid:
            continue
        out.append({"merchant_id": mid, "name": _pick(item, _NAME_KEYS) or mid})
    return out


def register(mcp) -> None:
    """Register merchant-selection tooling on the given FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"jobmojito"},
    )
    async def list_my_merchants(ctx: Context) -> dict:
        """List the merchants the signed-in user can act as (to choose a merchant_id).

        Use this when an action needs a merchant and the user may have more than
        one (e.g. sub-merchants). Returns the user's own account plus any
        sub-merchants. After the user picks one, pass `merchant_id=<chosen id>` on
        every subsequent JobMojito tool call; OMIT `merchant_id` to use the user's
        own account (the API derives it from the login). If a clickable picker is
        available, present the names with the `choose` tool.
        """
        try:
            subs = await _fetch_sub_merchants()
        except Exception as exc:
            logger.warning("list_my_merchants failed: %s", exc)
            return {"error": f"Could not list merchants: {exc}"}

        choices = [{"merchant_id": None, "name": "Your own account (default)"}] + subs

        if not subs:
            return {
                "merchant_count": 1,
                "choices": choices,
                "note": (
                    "Only your own account is available — merchant_id is taken from "
                    "your login, so no selection is needed."
                ),
            }

        return {
            "merchant_count": len(choices),
            "ui_picker_available": ctx.client_supports_extension(UI_EXTENSION_ID),
            "choices": choices,
            "next_step": (
                "Ask the user to choose a merchant, then pass merchant_id=<chosen id> "
                "on every subsequent JobMojito tool call. OMIT merchant_id for "
                "'Your own account'. If a clickable picker is available, render the "
                "merchant names via the `choose` tool."
            ),
        }

    # First-party clickable picker for UI-capable clients (text clients ignore it
    # and use list_my_merchants' structured list instead).
    try:
        from fastmcp.apps.choice import Choice

        mcp.add_provider(Choice(title="Select a merchant"))
        logger.info("Merchant selection: list_my_merchants + Choice picker registered.")
    except Exception as exc:  # fastmcp[apps] not installed
        logger.warning(
            "Choice picker unavailable (install fastmcp[apps]); "
            "list_my_merchants still works. %s",
            exc,
        )
