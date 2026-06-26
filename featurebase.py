"""Featurebase REST API client — read-only Help Center access.

Used as the preferred source for help-center documentation when a
``FEATUREBASE_API_KEY`` is configured. Auth is a single API key
(``Authorization: Bearer <key>``); base URL defaults to https://do.featurebase.app.

Docs: https://docs.featurebase.app/rest-api/help-centers
Generate a key at https://auth.featurebase.app/settings/api

Note: Featurebase's *MCP* (mcp-read.featurebase.app) uses interactive OAuth
(authorization code + PKCE, no client secret) and is meant for end-user AI tools,
not server-to-server use — so we use the REST API here instead.
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from config import settings

logger = logging.getLogger("jobmojito_mcp.featurebase")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n\s*\n\s*\n+")


def is_enabled() -> bool:
    return bool(settings.featurebase_api_key)


def html_to_text(body: str | None) -> str:
    """Cheap HTML→text for article bodies (no extra dependencies)."""
    if not body:
        return ""
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub("\n\n", text).strip()


def _headers() -> dict[str, str]:
    # Featurebase accepts the key two ways depending on API version/docs:
    #   - v2 (nova): Authorization: Bearer <key>
    #   - legacy/quickstart: X-API-Key: <key>
    # Sending both is harmless and works across versions.
    key = settings.featurebase_api_key or ""
    return {
        "Authorization": f"Bearer {key}",
        "X-API-Key": key,
        "Featurebase-Version": settings.featurebase_api_version,
        "Accept": "application/json",
    }


async def list_articles(state: str = "live", page_limit: int = 100, max_pages: int = 20) -> list[dict]:
    """Return all help-center articles (paginated via cursor)."""
    if not is_enabled():
        return []
    url = f"{settings.featurebase_api_base_url}/v2/help_center/articles"
    out: list[dict] = []
    cursor: str | None = None
    async with httpx.AsyncClient(headers=_headers(), timeout=30.0) as client:
        for _ in range(max_pages):
            params: dict[str, str | int] = {"state": state, "limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning("Featurebase list_articles failed: %s", exc)
                break
            out.extend(payload.get("data", []))
            cursor = payload.get("nextCursor")
            if not cursor:
                break
    logger.info("Fetched %d Featurebase help-center articles.", len(out))
    return out


async def get_article(article_id: str, state: str = "live") -> dict | None:
    """Fetch a single article (including body HTML) by id."""
    if not is_enabled():
        return None
    url = f"{settings.featurebase_api_base_url}/v2/help_center/articles/{article_id}"
    async with httpx.AsyncClient(headers=_headers(), timeout=30.0) as client:
        try:
            resp = await client.get(url, params={"state": state})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Featurebase get_article(%s) failed: %s", article_id, exc)
            return None
