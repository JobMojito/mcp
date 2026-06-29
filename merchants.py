"""Merchant selection / agent setup.

Many JobMojito endpoints accept a `merchant_id`. For users who can act across
multiple merchants (an account with sub-merchants), the agent should be
"configured" with a merchant before making calls:

* ``jobmojito_configuration`` — an MCP App (UI) with a settings form. Today it has
  one field, a searchable "Sub merchant" picker (more fields can be added later).
  Run it first when a tool needs a `merchant_id` and none has been selected, or
  whenever the user wants to switch merchants. Selecting a merchant tells the model
  which `merchant_id` to use.
* ``list_my_merchants`` — returns the same options as plain structured data (with
  an optional name filter), for clients without UI support.

After a merchant is chosen, the model passes ``merchant_id=<chosen id>`` on every
subsequent call (omit it for the user's own account, which the API derives from
the JWT).
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


async def _fetch_sub_merchants(
    search: str = "", page_size: int = 1000, max_total: int = 5000
) -> list[dict]:
    """Fetch sub-merchants via the JobMojito API (forwards the user's token).

    Paginates through ALL sub-merchants (the endpoint defaults to ~50/page), up to
    ``max_total``, so the picker can search the full list — not just the first
    page. ``search`` is sent server-side (best effort, as ``filter_text``) and also
    applied client-side as a fallback in case the endpoint ignores it.
    """
    q = (search or "").strip()
    out: list[dict] = []
    offset = 0
    async with build_api_client() as client:
        while len(out) < max_total:
            params: dict = {"limit": page_size, "offset": offset}
            if q:
                # Send both names: `filter_text` (OpenAPI) and `in_filter_text`
                # (Supabase RPC convention) so server-side filtering works
                # regardless of which the endpoint expects. A client-side filter
                # below is the fallback if neither is honored.
                params["filter_text"] = q
                params["in_filter_text"] = q
            resp = await client.get("/merchant-sub-merchant-list", params=params)
            resp.raise_for_status()
            payload = resp.json()
            data = (payload.get("data") if isinstance(payload, dict) else payload) or []
            for item in data:
                if not isinstance(item, dict):
                    continue
                mid = _pick(item, _ID_KEYS)
                if mid:
                    out.append({"merchant_id": mid, "name": _pick(item, _NAME_KEYS) or mid})

            page = payload.get("pagination") if isinstance(payload, dict) else None
            if page is not None:
                if not page.get("has_more"):
                    break
                offset = (page.get("offset", offset) or offset) + (
                    page.get("limit", page_size) or page_size
                )
            elif len(data) < page_size:
                break
            else:
                offset += page_size

    if q:  # client-side fallback filter
        ql = q.lower()
        out = [m for m in out if ql in m["name"].lower()]
    return out


# --- Interactive `setup` picker (MCP App) ------------------------------------
# Imported at module level so the `-> PrefabApp` annotation resolves; requires
# fastmcp[apps]. If unavailable, `list_my_merchants` still covers the flow.
try:
    from fastmcp.apps.app import FastMCPApp
    from prefab_ui.actions import SetState
    from prefab_ui.actions.mcp import CallTool, SendMessage
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        H3,
        Button,
        Card,
        CardContent,
        CardHeader,
        Column,
        Field,
        FieldContent,
        FieldDescription,
        FieldTitle,
        Input,
        Muted,
    )
    from prefab_ui.components.control_flow import ForEach, If
    from prefab_ui.rx import RESULT, STATE

    _APPS_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - fastmcp[apps] not installed
    _APPS_AVAILABLE = False
    logger.warning(
        "`setup` picker unavailable (install fastmcp[apps]); "
        "list_my_merchants still works. %s",
        _exc,
    )


if _APPS_AVAILABLE:

    class JobMojitoConfiguration(FastMCPApp):
        """MCP App exposing a `jobmojito_configuration` tool (settings form).

        The "Sub merchant" field uses type-and-click server search: the user types
        a name, clicks Search, and the UI calls a backend tool that filters
        server-side (so it scales past one page); matches render as clickable rows.
        """

        def __init__(self) -> None:
            super().__init__("JobMojito configuration")

            @self.tool()
            async def search_merchants(query: str = "") -> list[dict]:
                """Backend: server-side sub-merchant search (called by the UI's Search button)."""
                subs = await _fetch_sub_merchants(search=query)
                return [
                    {"merchant_id": m["merchant_id"], "name": m["name"]}
                    for m in subs[:50]
                ]

            @self.ui()
            async def jobmojito_configuration() -> PrefabApp:
                """Configure JobMojito: choose which merchant to act as.

                Run this FIRST whenever a tool needs a `merchant_id` and none has
                been selected yet, or when the user wants to switch merchants. The
                user searches sub-merchants by name and picks one; afterwards, pass
                `merchant_id=<chosen id>` on every JobMojito call (omit it for the
                user's own account). After calling this, STOP and wait for the
                user's selection.
                """
                with Card(css_class="max-w-lg mx-auto") as view:
                    with CardHeader():
                        H3("JobMojito configuration")
                    with CardContent():
                        # One labeled field per setting. Add more Field blocks
                        # below as configuration grows (e.g. default language).
                        with Column(gap=4, css_class="w-full"):
                            # --- Field: Sub merchant (type-and-click search) ---
                            with Field():
                                FieldTitle("Sub merchant")
                                FieldDescription(
                                    "Use your own account, or search by name and "
                                    "pick a sub-merchant."
                                )
                                with FieldContent():
                                    with Column(gap=2, css_class="w-full"):
                                        Button(
                                            "Your own account (default)",
                                            variant="outline",
                                            css_class="w-full justify-start",
                                            on_click=[
                                                SendMessage(
                                                    "Use my own JobMojito account — "
                                                    "omit merchant_id on calls."
                                                ),
                                                SetState("decided", True),
                                            ],
                                        )
                                        Input(
                                            name="q",
                                            placeholder="Search merchants by name…",
                                        )
                                        Button(
                                            "Search",
                                            variant="default",
                                            on_click=[
                                                CallTool(
                                                    search_merchants,
                                                    arguments={"query": STATE.q},
                                                    onSuccess=[
                                                        SetState("results", RESULT)
                                                    ],
                                                ),
                                            ],
                                        )
                                        with ForEach("results") as item:
                                            Button(
                                                item.name,
                                                variant="outline",
                                                css_class="w-full justify-start",
                                                on_click=[
                                                    SendMessage(
                                                        "Use JobMojito merchant_id="
                                                        + item.merchant_id
                                                        + " on all calls."
                                                    ),
                                                    SetState("decided", True),
                                                ],
                                            )
                                        with If(STATE.decided):
                                            Muted("Merchant selected.")
                            # --- Future fields go here ---

                return PrefabApp(
                    view=view, state={"decided": False, "q": "", "results": []}
                )


def register(mcp) -> None:
    """Register merchant-selection tooling on the given FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"jobmojito"},
    )
    async def list_my_merchants(ctx: Context, search: str = "") -> dict:
        """List the merchants the signed-in user can act as (text equivalent of config).

        Use this when an action needs a merchant and the user may have more than
        one (e.g. sub-merchants), and the client has no UI picker. Returns the
        user's own account plus any sub-merchants. After the user picks one, pass
        `merchant_id=<chosen id>` on every subsequent call; OMIT `merchant_id` to
        use the user's own account. For UI clients, prefer running
        `jobmojito_configuration` (a searchable picker).

        Args:
            search: Optional case-insensitive filter on sub-merchant name.
        """
        try:
            subs = await _fetch_sub_merchants(search=search)
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
                    "your login, so no setup is needed."
                ),
            }
        return {
            "merchant_count": len(choices),
            "ui_setup_available": ctx.client_supports_extension(UI_EXTENSION_ID),
            "choices": choices,
            "next_step": (
                "Ask the user to choose a merchant (or run `setup` for a picker), then "
                "pass merchant_id=<chosen id> on every subsequent call. OMIT merchant_id "
                "for 'Your own account'."
            ),
        }

    if _APPS_AVAILABLE:
        mcp.add_provider(JobMojitoConfiguration())
        logger.info(
            "JobMojito configuration registered (app tool: jobmojito_configuration; "
            "text: list_my_merchants)."
        )
    else:
        logger.info(
            "JobMojito configuration: list_my_merchants only (fastmcp[apps] missing)."
        )
