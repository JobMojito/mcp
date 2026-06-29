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
        Card,
        CardContent,
        CardHeader,
        Column,
        Combobox,
        ComboboxOption,
        Field,
        FieldContent,
        FieldDescription,
        FieldTitle,
        Muted,
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

    class JobMojitoConfiguration(FastMCPApp):
        """MCP App exposing a `jobmojito_configuration` tool (settings form)."""

        def __init__(self) -> None:
            super().__init__("JobMojito configuration")

            @self.ui()
            async def jobmojito_configuration() -> PrefabApp:
                """Configure JobMojito: choose which merchant to act as.

                Run this FIRST whenever a tool needs a `merchant_id` and none has
                been selected yet, or when the user wants to switch merchants. The
                user picks a sub-merchant from a searchable list; afterwards, pass
                `merchant_id=<chosen id>` on every JobMojito call (omit it for the
                user's own account). After calling this, STOP and wait for the
                user's selection.
                """
                try:
                    subs = await _fetch_sub_merchants()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("jobmojito_configuration: merchant fetch failed: %s", exc)
                    subs = []

                # The selected combobox value lands in STATE.sub_merchant ("own"
                # for the user's own account, otherwise the merchant_id). onChange
                # sends that value back so the model knows what to use.
                on_select = [
                    SendMessage(
                        "JobMojito merchant selected (sub_merchant='"
                        + STATE.sub_merchant
                        + "'). Use merchant_id=<that value> on every subsequent "
                        "JobMojito call. If the value is 'own', use my own account "
                        "and OMIT merchant_id."
                    ),
                    SetState("decided", True),
                ]

                with Card(css_class="max-w-lg mx-auto") as view:
                    with CardHeader():
                        H3("JobMojito configuration")
                    with CardContent():
                        # One labeled field per setting. Add more Field blocks
                        # below as configuration grows (e.g. default language).
                        with Column(gap=4, css_class="w-full"):
                            # --- Field: Sub merchant (searchable) ---
                            with Field():
                                FieldTitle("Sub merchant")
                                FieldDescription(
                                    "Search and choose which merchant to act as."
                                )
                                with FieldContent():
                                    if not subs:
                                        Muted(
                                            "Only your own account is available — "
                                            "nothing to select."
                                        )
                                    else:
                                        with Combobox(
                                            name="sub_merchant",
                                            placeholder="Select a merchant…",
                                            searchPlaceholder="Search merchants…",
                                            onChange=on_select,
                                        ):
                                            ComboboxOption(
                                                "Your own account (default)",
                                                value="own",
                                            )
                                            for m in subs:
                                                ComboboxOption(
                                                    m["name"], value=m["merchant_id"]
                                                )
                                        with If(STATE.decided):
                                            Muted("Merchant selected.")
                            # --- Future fields go here ---

                return PrefabApp(view=view, state={"decided": False, "sub_merchant": ""})


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
            subs = await _fetch_sub_merchants()
        except Exception as exc:
            logger.warning("list_my_merchants failed: %s", exc)
            return {"error": f"Could not list merchants: {exc}"}

        q = (search or "").strip().lower()
        if q:
            subs = [m for m in subs if q in m["name"].lower()]

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
