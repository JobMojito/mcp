"""Documentation tools — read JobMojito docs live, single entry point.

Two tools are exposed:

* ``search_documentation(query, ...)`` — ONE call searches both doc sources in
  parallel and returns merged, source-labeled results:
    - developer docs (developer.jobmojito.com) via the Mintlify MCP's semantic
      search when configured, else a keyword index built from its ``llms.txt``;
    - help center (help.jobmojito.com) via the Featurebase REST API when a key is
      set, else public-HTML scraping.
* ``get_documentation(url)`` — fetches the clean content of a single doc page on
  demand (the ``.md`` variant for developer docs; Featurebase article body for help).

Nothing is copied into this repo: docs are authored once on the two platforms and
read live here, with a short in-memory TTL cache to keep things fast.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

import featurebase
import mintlify
from config import settings

logger = logging.getLogger("jobmojito_mcp.docs")

# --- markdown / html link patterns -------------------------------------------
# llms.txt lines look like:  - Section [Title](https://.../page.md): description
_LLMS_LINE = re.compile(r"\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)(?::\s*(?P<desc>.*))?")
_HTML_ANCHOR = re.compile(
    r'<a[^>]+href="(?P<url>https?://[^"#?]+)"[^>]*>(?P<title>[^<]{2,160})</a>',
    re.IGNORECASE,
)
_MD_LINK = re.compile(r"\[(?P<title>[^\]]{2,160})\]\((?P<url>https?://[^)\s]+)\)")
_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "and", "or", "how", "do", "i", "is",
    "in", "on", "with", "my", "me", "can", "what", "are", "this", "that",
}


@dataclass
class DocEntry:
    title: str
    url: str
    source: str  # "developer" | "help"
    description: str = ""
    section: str = ""
    article_id: str | None = None  # set for Featurebase REST help articles

    def haystack(self) -> str:
        return " ".join([self.title, self.description, self.section, self.url]).lower()


@dataclass
class _Cache:
    entries: list[DocEntry] = field(default_factory=list)
    fetched_at: float = 0.0


_index_cache = _Cache()
_page_cache: dict[str, tuple[float, str]] = {}


def _ttl_seconds() -> int:
    return max(60, settings.docs_cache_ttl_minutes * 60)


async def _get(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.warning("Doc fetch failed for %s: %s", url, exc)
        return None


def _parse_llms_txt(text: str) -> list[DocEntry]:
    """Parse a developer-docs llms.txt index into DocEntry objects."""
    entries: list[DocEntry] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("# ").strip()
            continue
        if not stripped.startswith("-"):
            continue
        m = _LLMS_LINE.search(stripped)
        if not m:
            continue
        title = m.group("title").strip()
        url = m.group("url").strip()
        desc = (m.group("desc") or "").strip()
        # The bit before the [title] often carries a category label (e.g. "Webhooks").
        prefix = stripped.split("[", 1)[0].lstrip("- ").strip()
        full_section = " / ".join(p for p in (section, prefix) if p)
        entries.append(
            DocEntry(title=title, url=url, source="developer", description=desc, section=full_section)
        )
    return entries


def _parse_help_html(text: str, base: str) -> list[DocEntry]:
    """Extract article/collection links from the Featurebase help center."""
    entries: list[DocEntry] = []
    seen: set[str] = set()
    host = urlparse(base).netloc
    for pattern in (_HTML_ANCHOR, _MD_LINK):
        for m in pattern.finditer(text):
            url = m.group("url").strip()
            title = re.sub(r"\s+", " ", m.group("title")).strip()
            if urlparse(url).netloc != host:
                continue
            if not re.search(r"/(articles|collections)/", url):
                continue
            if url in seen or not title:
                continue
            seen.add(url)
            entries.append(DocEntry(title=title, url=url, source="help", section="Help center"))
    return entries


async def _build_index(force: bool = False) -> list[DocEntry]:
    now = time.time()
    if not force and _index_cache.entries and (now - _index_cache.fetched_at) < _ttl_seconds():
        return _index_cache.entries

    entries: list[DocEntry] = []
    async with httpx.AsyncClient(headers={"User-Agent": "jobmojito-mcp/0.1"}) as client:
        # Developer docs: public llms.txt index (no credentials needed).
        # Skipped when the Mintlify developer-docs MCP is federated — those tools
        # cover developer docs natively, so indexing llms.txt here would duplicate.
        if not settings.federate_developer_docs:
            dev = await _get(client, settings.developer_docs_llms_url)
            if dev:
                entries.extend(_parse_llms_txt(dev))

        # Help center: prefer the Featurebase REST API (robust, structured);
        # fall back to scraping the public help-center HTML.
        if featurebase.is_enabled():
            for a in await featurebase.list_articles():
                entries.append(
                    DocEntry(
                        title=a.get("title", "") or "",
                        url=a.get("featurebaseUrl") or a.get("externalUrl") or "",
                        source="help",
                        description=a.get("description", "") or "",
                        section="Help center",
                        article_id=a.get("id"),
                    )
                )
        else:
            help_html = await _get(client, settings.help_docs_base_url + "/")
            if help_html:
                entries.extend(_parse_help_html(help_html, settings.help_docs_base_url))

    if entries:
        _index_cache.entries = entries
        _index_cache.fetched_at = now
        logger.info("Built docs index: %d entries.", len(entries))
    elif _index_cache.entries:
        logger.warning("Doc index refresh failed; serving stale cache.")
    return _index_cache.entries


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _score(entry: DocEntry, query_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    title = entry.title.lower()
    hay = entry.haystack()
    score = 0.0
    for tok in query_tokens:
        if tok in title:
            score += 3.0
        if tok in hay:
            score += 1.0
    # Small boost for whole-phrase title hits.
    phrase = " ".join(query_tokens)
    if phrase and phrase in title:
        score += 2.0
    return score


def _rank(entries: list[DocEntry], query: str, limit: int) -> list[dict]:
    """Keyword-rank index entries and return result dicts."""
    query_tokens = _tokenize(query)
    scored = [(e, _score(e, query_tokens)) for e in entries]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda p: p[1], reverse=True)
    return [
        {
            "title": e.title,
            "url": e.url,
            "source": e.source,
            "section": e.section,
            "description": e.description,
        }
        for e, _ in scored[:limit]
    ]


async def _noop_list() -> list[dict]:
    return []


async def _noop_dict() -> dict:
    return {"items": [], "text": ""}


async def _search_help(query: str, limit: int) -> list[dict]:
    entries = [e for e in await _build_index() if e.source == "help"]
    return _rank(entries, query, limit)


async def _search_developer(query: str, limit: int) -> dict:
    """Developer-docs search: live Mintlify semantic search, or llms.txt fallback."""
    if mintlify.is_enabled():
        return await mintlify.search_developer_docs(query, limit)
    entries = [e for e in await _build_index() if e.source == "developer"]
    return {"items": _rank(entries, query, limit), "text": ""}


def _allowed_doc_host(url: str) -> bool:
    host = urlparse(url).netloc
    allowed = {
        urlparse(settings.developer_docs_base_url).netloc,
        urlparse(settings.help_docs_base_url).netloc,
    }
    return host in allowed


async def _find_help_article_id(url: str) -> str | None:
    """Resolve a help-center URL to its Featurebase article id via the index."""
    target = url.rstrip("/")
    for entry in await _build_index():
        if entry.source == "help" and entry.article_id and entry.url.rstrip("/") == target:
            return entry.article_id
    return None


def register(mcp) -> None:
    """Register documentation tools on the given FastMCP server."""

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"documentation"},
    )
    async def search_documentation(query: str, source: str = "all", limit: int = 8) -> dict:
        """Search ALL JobMojito documentation. This is the single entry point.

        One call searches both documentation sources in parallel and returns a
        merged, source-labeled list — you do not need to choose a source or call
        a separate tool:
          • "developer" — developer.jobmojito.com: API reference, request/response
            schemas, tables, webhooks, code examples, integration guides.
          • "help" — help.jobmojito.com: recruiter, candidate, and administrator
            product guides (how the platform behaves for end users).

        Use this whenever you need to understand how a feature, endpoint, field,
        or workflow works — including before calling an action tool you're unsure
        about. Then call `get_documentation(url)` with a returned URL to read the
        full page.

        Args:
            query: Natural-language search query or keywords.
            source: "all" (default), "developer", or "help" to restrict the search.
            limit: Max results per source (1-25).
        """
        limit = max(1, min(int(limit), 25))
        src = (source or "all").lower()
        want_help = src in {"all", "help"}
        want_dev = src in {"all", "developer"}

        help_task = _search_help(query, limit) if want_help else _noop_list()
        dev_task = _search_developer(query, limit) if want_dev else _noop_dict()
        help_results, dev_result = await asyncio.gather(help_task, dev_task)

        results: list[dict] = []
        for item in dev_result.get("items", []):
            results.append({**item, "source": "developer"})
        results.extend(help_results)

        out: dict = {
            "query": query,
            "source": src,
            "result_count": len(results),
            "results": results,
            "next_step": "Call get_documentation(url) with a result URL to read full content.",
        }
        # Include the raw developer-search snippet when structured items weren't
        # parseable, so the model still gets the content.
        dev_text = dev_result.get("text") or ""
        if dev_text and not dev_result.get("items"):
            out["developer_docs_snippet"] = dev_text[:4000]
        return out

    @mcp.tool(
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"documentation"},
    )
    async def get_documentation(url: str, max_chars: int = 20000) -> dict:
        """Fetch the full content of a single JobMojito documentation page.

        Accepts a URL returned by `search_documentation`. For developer docs the
        clean Markdown (.md) variant is fetched automatically. Only
        developer.jobmojito.com and help.jobmojito.com URLs are allowed.

        Args:
            url: The documentation page URL.
            max_chars: Truncate content to this many characters (default 20000).
        """
        url = url.strip()
        max_chars = max(500, min(int(max_chars), 80000))

        # Help center via Featurebase REST API: resolve the article id from the
        # index by URL (works regardless of the host — Featurebase canonical URLs
        # use feedback.jobmojito.com, not help.jobmojito.com).
        if featurebase.is_enabled():
            article_id = await _find_help_article_id(url)
            if article_id:
                article = await featurebase.get_article(article_id)
                if article:
                    text = featurebase.html_to_text(article.get("body"))
                    return {
                        "url": url,
                        "source": "featurebase-api",
                        "title": article.get("title"),
                        "truncated": len(text) > max_chars,
                        "content": text[:max_chars],
                    }

        if not _allowed_doc_host(url):
            return {
                "error": "URL not allowed and not a known help-center article. Use a "
                "URL returned by search_documentation.",
                "url": url,
            }

        # Prefer the clean markdown variant for developer docs.
        fetch_url = url
        dev_host = urlparse(settings.developer_docs_base_url).netloc
        if urlparse(url).netloc == dev_host and not url.endswith(".md"):
            fetch_url = url.rstrip("/") + ".md"
        now = time.time()
        cached = _page_cache.get(fetch_url)
        if cached and (now - cached[0]) < _ttl_seconds():
            content = cached[1]
        else:
            async with httpx.AsyncClient(headers={"User-Agent": "jobmojito-mcp/0.1"}) as client:
                content = await _get(client, fetch_url)
                if content is None and fetch_url != url:
                    content = await _get(client, url)  # fallback to original URL
                if content is None:
                    return {"error": "Could not fetch documentation page.", "url": url}
                _page_cache[fetch_url] = (now, content)

        truncated = len(content) > max_chars
        return {
            "url": url,
            "fetched_url": fetch_url,
            "truncated": truncated,
            "content": content[:max_chars],
        }
