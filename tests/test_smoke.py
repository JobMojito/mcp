"""Smoke tests for the JobMojito MCP server.

Run with:  ENABLE_AUTH=false pytest -q

These avoid any network by pointing the OpenAPI URL at an unreachable host so the
loader falls back to the committed snapshot.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
# Keep tests hermetic/offline regardless of a local .env: disable the optional
# external doc sources and the Mintlify federation. (load_dotenv uses
# override=False, so these win.)
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

EXPECTED_API_TOOLS = {
    "generate_interview_report", "get_interview_definition", "set_interview_state",
    "request_another_interview_attempt", "generate_interview_url",
    "get_interview_result_details", "invite_users", "register_users_for_interview",
    "create_interview", "create_interview_from_questions",
    "create_interview_for_candidate", "upsert_pre_screening",
    "pre_screen_resume_text", "pre_screen_resume_binary",
    "upload_knowledge_base_document", "list_interviews", "list_candidates",
    "list_interview_results", "list_avatars", "list_sub_merchants",
    "get_merchant_analytics",
}
EXPECTED_DOC_TOOLS = {"search_documentation", "get_documentation"}


async def _tool_names(mcp) -> set[str]:
    tools = await mcp.list_tools()
    return {t.name for t in tools}


@pytest.mark.asyncio
async def test_all_tools_present():
    import server

    names = await _tool_names(server.mcp)
    missing = (EXPECTED_API_TOOLS | EXPECTED_DOC_TOOLS) - names
    assert not missing, f"missing tools: {missing}"
    assert len(EXPECTED_API_TOOLS) == 21


def test_llms_txt_parser():
    from docs_tools import _parse_llms_txt

    sample = """# JobMojito

## Docs
- [Welcome](https://developer.jobmojito.com/welcome-1018963m0.md):
- Webhooks [Creating webhooks](https://developer.jobmojito.com/creating-webhooks-1021007m0.md): how to

## API Docs
- Actions API [Create interview](https://developer.jobmojito.com/create-interview-16953824e0.md): Creates an interview
"""
    entries = _parse_llms_txt(sample)
    assert len(entries) == 3
    titles = {e.title for e in entries}
    assert "Welcome" in titles and "Creating webhooks" in titles
    assert all(e.source == "developer" for e in entries)


def test_help_html_parser():
    from docs_tools import _parse_help_html

    html = (
        '<a href="https://help.jobmojito.com/collections/9654934-recruiter">Recruiter</a>'
        '<a href="https://help.jobmojito.com/articles/4692316-avatars">Avatars</a>'
        '<a href="https://example.com/articles/x">External</a>'
    )
    entries = _parse_help_html(html, "https://help.jobmojito.com")
    urls = {e.url for e in entries}
    assert "https://help.jobmojito.com/collections/9654934-recruiter" in urls
    assert "https://help.jobmojito.com/articles/4692316-avatars" in urls
    assert not any("example.com" in u for u in urls)  # other domains excluded


def test_doc_search_scoring():
    from docs_tools import DocEntry, _score, _tokenize

    e = DocEntry(title="Creating webhooks", url="x", source="developer",
                 description="how to set up webhooks")
    assert _score(e, _tokenize("how do I create a webhook")) > 0


def test_featurebase_html_to_text():
    from featurebase import html_to_text

    body = "<h1>Title</h1><p>First para.</p><ul><li>one</li><li>two</li></ul>"
    text = html_to_text(body)
    assert "Title" in text and "First para." in text
    assert "- one" in text and "- two" in text
    assert "<" not in text  # tags stripped
    assert html_to_text(None) == ""


def test_featurebase_disabled_by_default():
    import featurebase

    # No API key in the test env → REST source disabled, HTML fallback used.
    assert featurebase.is_enabled() is False


def test_developer_docs_token_endpoint_derivation():
    from config import settings

    if settings.developer_docs_mcp_url:
        assert settings.developer_docs_token_endpoint == (
            settings.developer_docs_mcp_url.rstrip("/") + "/oauth/token"
        )
    else:
        assert settings.developer_docs_token_endpoint is None


def test_developer_docs_federation_off_when_url_empty():
    from config import settings

    # URL forced empty in the test env → federation disabled, no auth.
    assert settings.federate_developer_docs is False
    assert settings.developer_docs_uses_auth is False


def test_mintlify_token_caching():
    import time

    from mintlify import MintlifyClientCredentialsAuth

    auth = MintlifyClientCredentialsAuth(
        token_url="https://developer.jobmojito.com/authed/mcp/oauth/token",
        client_id="cid",
        client_secret="secret",
    )
    assert auth._token_valid() is False  # no token yet

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "abc123", "expires_in": 1209600}

    auth._store_token(_Resp())
    assert auth._access_token == "abc123"
    assert auth._token_valid() is True
    assert auth._expiry > time.time()
