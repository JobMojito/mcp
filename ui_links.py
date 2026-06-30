"""Admin UI deep-link tool.

Builds links into the JobMojito admin app for a candidate, interview, or result
so the user can click straight through to the relevant page.

URL routes are app-specific, so the base and per-entity path templates are
configurable via environment variables (defaults are best-guess and should be
confirmed against your actual admin routes):

    ADMIN_UI_BASE_URL        (default: SITE_URL, i.e. https://app.jobmojito.com)
    ADMIN_UI_CANDIDATE_PATH  (default: /candidates/{id})
    ADMIN_UI_INTERVIEW_PATH  (default: /interviews/{id})
    ADMIN_UI_RESULT_PATH     (default: /interview_results/result/{id})

`{id}` is replaced with the (URL-encoded) entity id.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from config import settings


def _base() -> str:
    return os.environ.get("ADMIN_UI_BASE_URL", settings.site_url).rstrip("/")


def _entity_paths() -> dict[str, str]:
    return {
        "candidate": os.environ.get("ADMIN_UI_CANDIDATE_PATH", "/candidates/{id}"),
        "interview": os.environ.get("ADMIN_UI_INTERVIEW_PATH", "/interviews/{id}"),
        "result": os.environ.get("ADMIN_UI_RESULT_PATH", "/interview_results/result/{id}"),
    }


def build_admin_url(entity_type: str, entity_id: str) -> str | None:
    """Build the admin UI URL for an entity, or None if the type is unknown."""
    template = _entity_paths().get((entity_type or "").lower().strip())
    if not template:
        return None
    return _base() + template.replace("{id}", quote(str(entity_id), safe=""))


def register(mcp) -> None:
    """Register the admin UI link tool on the given FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"jobmojito-ui"},
    )
    def get_admin_ui_link(entity_type: str, id: str) -> dict:
        """Get a JobMojito admin UI link to open a candidate, interview, or result.

        Use this when the user wants to view or open something in the JobMojito
        admin app — e.g. "show me the admin page for this candidate", "open that
        interview", or "give me a link to the result". If you only have a name,
        first look up the id with `list_candidates` / `list_interviews` /
        `list_interview_results`, then call this.

        Present the returned `markdown_link` to the user as a clickable link
        (it renders as a button/link in the chat).

        Args:
            entity_type: One of "candidate", "interview", or "result".
            id: The entity id.
        """
        et = (entity_type or "").lower().strip()
        url = build_admin_url(et, id)
        if not url:
            return {
                "error": (
                    f"Unknown entity_type '{entity_type}'. "
                    f"Use one of: {', '.join(sorted(_entity_paths()))}."
                ),
            }
        label = f"Open {et} in JobMojito admin"
        return {
            "entity_type": et,
            "id": id,
            "url": url,
            "label": label,
            "markdown_link": f"[{label}]({url})",
            "presentation": "Show the user the markdown_link as a clickable link/button.",
        }
