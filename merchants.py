"""Merchant selection / agent setup.

Many JobMojito endpoints accept a `merchant_id`. For users who can act across
multiple merchants (an account with sub-merchants), the agent should be
"configured" with a merchant before making calls:

* ``setup`` — an MCP App (UI) that renders a clickable merchant picker. Run it
  first when a tool needs a `merchant_id` and none has been selected, or whenever
  the user wants to switch merchants. Clicking a merchant tells the model which
  `merchant_id` to use.
* ``list_my_merchants`` — returns the same options as plain structured data, for
  clients without UI support.

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


# --- Interactive `setup` picker (MCP App) ------------------------------------
# Imported at module level so the `-> PrefabApp` annotation resolves; requires
# fastmcp[apps]. If unavailable, `list_my_merchants` still covers the flow.
try:
    from fastmcp.apps.app import FastMCPApp
    from prefab_ui.actions import SetState
    from prefab_ui.actions.mcp import SendMessage
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        H3,
        Button,
        Card,
        CardContent,
        CardFooter,
        CardHeader,
        Column,
        Muted,
        Text,
    )
    from prefab_ui.components.control_flow import If
    from prefab_ui.rx import STATE

    _APPS_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - fastmcp[apps] not installed
    _APPS_AVAILABLE = False
    logger.warning(
        "`setup` picker unavailable (install fastmcp[apps]); "
        "list_my_merchants still works. %s",
        _exc,
    )


if _APPS_AVAILABLE:

    class MerchantSetup(FastMCPApp):
        """MCP App exposing a `setup` tool that renders a merchant picker."""

        def __init__(self) -> None:
            super().__init__("Setup")

            @self.ui()
            async def setup() -> PrefabApp:
                """Configure the agent: choose which JobMojito merchant to act as.

                Run this FIRST whenever a tool needs a `merchant_id` and none has
                been selected yet, or when the user wants to switch merchants. The
                user picks a merchant; afterwards, pass `merchant_id=<chosen id>`
                on every JobMojito call (omit it for the user's own account).
                After calling this, STOP and wait for the user's selection.
                """
                try:
                    subs = await _fetch_sub_merchants()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("setup: merchant fetch failed: %s", exc)
                    subs = []

                options = [("Your own account (default)", None)] + [
                    (m["name"], m["merchant_id"]) for m in subs
                ]

                with Card(css_class="max-w-lg mx-auto") as view:
                    with CardHeader():
                        H3("Setup")
                    with CardContent():
                        # One labeled field per setting. Today: "Sub merchant".
                        # Add more fields below as additional labeled Columns.
                        with Column(gap=4, css_class="w-full"):
                            # --- Field: Sub merchant ---
                            with Column(gap=2, css_class="w-full"):
                                Text("Sub merchant", css_class="font-medium")
                                if not subs:
                                    Muted(
                                        "Only your own account is available — "
                                        "nothing to select."
                                    )
                                else:
                                    with If(STATE.decided):
                                        Muted("Sub merchant selected.")
                                    with If(~STATE.decided):  # noqa: SIM117
                                        with Column(gap=2, css_class="w-full"):
                                            for name, mid in options:
                                                msg = (
                                                    "Use my own JobMojito account — "
                                                    "omit merchant_id on calls."
                                                    if mid is None
                                                    else f'Use JobMojito merchant '
                                                    f'"{name}" (merchant_id={mid}) '
                                                    f"on all calls."
                                                )
                                                Button(
                                                    name,
                                                    variant="outline",
                                                    css_class="w-full justify-start",
                                                    on_click=[
                                                        SendMessage(msg),
                                                        SetState("decided", True),
                                                    ],
                                                )
                            # --- Future fields go here (e.g. default language) ---

                return PrefabApp(view=view, state={"decided": False})


def register(mcp) -> None:
    """Register merchant-selection tooling on the given FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"jobmojito"},
    )
    async def list_my_merchants(ctx: Context) -> dict:
        """List the merchants the signed-in user can act as (text equivalent of `setup`).

        Use this when an action needs a merchant and the user may have more than
        one (e.g. sub-merchants), and the client has no UI picker. Returns the
        user's own account plus any sub-merchants. After the user picks one, pass
        `merchant_id=<chosen id>` on every subsequent call; OMIT `merchant_id` to
        use the user's own account. For UI clients, prefer running `setup`.
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
        mcp.add_provider(MerchantSetup())
        logger.info(
            "Merchant setup registered (app tool: setup; text: list_my_merchants)."
        )
    else:
        logger.info("Merchant setup: list_my_merchants only (fastmcp[apps] missing).")
