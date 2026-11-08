"""The media library and stylesheet tools.

Run with:  ENABLE_AUTH=false pytest -q tests/test_media_tools.py

The generated tools are checked as the built server lists them, and on what reaches the
API. upload_media_file is three requests in one call, so its tests follow the bytes.
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

os.environ.setdefault("ENABLE_AUTH", "false")
os.environ.setdefault("JOBMOJITO_OPENAPI_URL", "http://127.0.0.1:1/unreachable")
os.environ.setdefault("FEATUREBASE_API_KEY", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_URL", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_ID", "")
os.environ.setdefault("DEVELOPER_DOCS_MCP_CLIENT_SECRET", "")

MEDIA = "33333333-3333-4333-8333-333333333333"
PNG = b"\x89PNG\r\n\x1a\nbytes"
SIGNED = "https://storage.test/storage/v1/object/upload/sign/public/m/a-v1.png?token=tok"
FILE = {
    "id": MEDIA, "merchant_id": "m", "kind": "image", "file_name": "a.png", "folder": "", "path": "m/a-v1.png",
    "url": "https://cdn.test/a.png", "content_type": "image/png", "size_bytes": len(PNG), "width": None, "height": None,
    "duration_seconds": None, "poster_url": None, "alt": {}, "focal_x": None, "focal_y": None, "status": "ready",
    "version": 1, "read_only": False, "created_at": "2026-11-06", "updated_at": "2026-11-06",
}
FINISHED = {"file": FILE, "replaced": False, "drafts_updated": 0, "warnings": []}
TICKET = {"media_id": MEDIA, "upload_url": SIGNED, "token": "tok", "path": "m/a-v1.png", "cache_control": 31536000}


def _annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return {}
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(exclude_none=True)


async def _tools():
    import server

    return {t.name: t for t in await server.mcp.list_tools()}


def _route(monkeypatch, answers: dict[str, tuple[int, object]]) -> list[httpx.Request]:
    """Answers by path ending; a request nothing answers fails the test."""
    seen: list[httpx.Request] = []

    async def fake_send(self, request, **kwargs):
        seen.append(request)
        for ending, (status, body) in answers.items():
            if ending in str(request.url):
                if isinstance(body, Exception):
                    raise body
                return httpx.Response(status, json=body, request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return seen


HAPPY = {"/media-upload-start": (200, TICKET), "/upload/sign/": (200, {"Key": "k"}), "/media-upload-finish": (200, FINISHED)}


# ---- how they are listed -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listed_with_the_right_annotations():
    tools = await _tools()
    assert _annotations(tools["list_media_files"]) == {
        "title": "List media library files", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True,
    }
    # merged: folders come from list_media_files include_folders=true, describe and delete
    # are manage_media_file (idempotentHint=false: a second delete is refused)
    for gone in ("list_media_folders", "update_media_file", "delete_media_file"):
        assert gone not in tools, gone
    for name, idempotent in [
        ("start_media_upload", False), ("finish_media_upload", True), ("manage_media_file", False),
        ("publish_portal_stylesheet", False), ("upload_media_file", False),
        ("manage_media_folder", False),
    ]:
        annotations = _annotations(tools[name])
        assert annotations["readOnlyHint"] is False, name
        assert annotations["destructiveHint"] is True, name
        assert annotations["idempotentHint"] is idempotent, name
        assert annotations["openWorldHint"] is True, name
        assert tools[name].title, name


def test_the_purge_worker_is_not_a_tool():
    import naming

    assert not any(path == "/media-purge-queue" for _, path in naming.TOOL_META)


def test_every_tool_has_a_written_justification():
    import naming

    written = naming.annotation_justifications()
    for name in ("list_media_files", "manage_media_folder", "start_media_upload", "finish_media_upload", "manage_media_file", "publish_portal_stylesheet", "upload_media_file"):
        assert written[name], name
    for gone in ("list_media_folders", "update_media_file", "delete_media_file"):
        assert gone not in written, gone
    assert "destructiveHint=true" in written["manage_media_file"]
    assert "for good" in written["manage_media_file"]  # delete cannot be undone
    assert "readOnlyHint=true" in written["list_media_files"]
    assert "destructiveHint=true" in written["manage_media_folder"]
    assert "new address" in written["manage_media_folder"]


@pytest.mark.asyncio
async def test_the_descriptions_steer_the_model():
    tools = await _tools()
    assert "upload_media_file" in tools["start_media_upload"].description
    assert "finish_media_upload" in tools["start_media_upload"].description
    assert "start_media_upload" in tools["upload_media_file"].description
    assert "read_only" in tools["list_media_files"].description
    assert "only when the user asked" in tools["publish_portal_stylesheet"].description.lower()
    assert "update_portal_configuration" in tools["publish_portal_stylesheet"].description
    assert "confirmed" in tools["manage_media_file"].description
    # SQL 313: every company has the library, folders are storage directories, and more things use a file
    for name in ("list_media_files", "start_media_upload", "manage_media_file", "manage_media_folder"):
        assert "switched" not in tools[name].description, name
        assert "career-media" not in tools[name].description, name
    assert "include_folders=true" in tools["list_media_files"].description
    assert "profile" in tools["manage_media_folder"].description
    assert "folder" in tools["manage_media_file"].description
    assert "logo" in tools["manage_media_file"].description
    assert "action delete" in tools["manage_media_file"].description
    folder_help = tools["upload_media_file"].parameters["properties"]["folder"]["description"]
    assert "list_media_files include_folders=true" in folder_help
    assert "list_media_folders" not in folder_help


@pytest.mark.asyncio
async def test_inputs_match_the_api():
    tools = await _tools()
    assert {"merchant_id", "kind", "folder", "search", "include_parent", "include_folders", "limit", "offset"} <= set(tools["list_media_files"].parameters["properties"])
    start = tools["start_media_upload"].parameters
    assert set(start["required"]) == {"file_name", "content_type", "size_bytes"}
    assert "video/webm" not in json.dumps(start)
    assert "video/mp4" in json.dumps(start)
    assert "text/css" not in json.dumps(start["properties"]["content_type"])
    assert set(tools["finish_media_upload"].parameters["required"]) == {"media_id"}
    # what the upload functions own is not there to set
    manage_file = tools["manage_media_file"].parameters
    assert set(manage_file["properties"]) == {"media_id", "action", "file_name", "folder", "alt", "focal_x", "focal_y", "poster_url"}
    assert set(manage_file["required"]) == {"media_id"}
    assert manage_file["properties"]["action"]["enum"] == ["update", "delete"]
    manage = tools["manage_media_folder"].parameters
    assert set(manage["properties"]) == {"merchant_id", "action", "folder", "to"}
    assert set(manage["required"]) == {"action", "folder"}
    assert manage["properties"]["action"]["enum"] == ["create", "rename", "delete"]
    assert set(tools["publish_portal_stylesheet"].parameters["required"]) == {"portal"}
    upload = tools["upload_media_file"].parameters
    assert set(upload["required"]) == {"file", "file_name", "content_type"}
    assert "base64" in upload["properties"]["file"]["description"]


def test_instructions_list_the_tools():
    import server

    for name in ("publish_portal_stylesheet", "list_media_files", "upload_media_file", "start_media_upload", "manage_media_file", "manage_media_folder"):
        assert name in server.INSTRUCTIONS, name
    for gone in ("list_media_folders", "update_media_file", "delete_media_file"):
        assert gone not in server.INSTRUCTIONS, gone
    assert "list_media_files include_folders=true" in server.INSTRUCTIONS


# ---- what reaches the API ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_null_focal_point_is_sent_and_a_field_left_out_is_not(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, {"/media-update": (200, {**FILE, "drafts_updated": 0, "warnings": []})})
    async with Client(server.mcp) as client:
        await client.call_tool("manage_media_file", {"media_id": MEDIA, "focal_x": None, "alt": {"en": "Deck"}})
    assert json.loads(seen[0].content) == {"media_id": MEDIA, "focal_x": None, "alt": {"en": "Deck"}}


@pytest.mark.asyncio
async def test_a_move_answers_the_file_with_its_new_address_and_the_drafts(monkeypatch):
    from fastmcp import Client

    import server

    moved = {**FILE, "folder": "Crew life", "path": "m/Crew life/a-v1.png", "drafts_updated": 2, "warnings": []}
    seen = _route(monkeypatch, {"/media-update": (200, moved)})
    async with Client(server.mcp) as client:
        result = await client.call_tool("manage_media_file", {"action": "update", "media_id": MEDIA, "folder": "Crew life"})
    assert json.loads(seen[0].content) == {"action": "update", "media_id": MEDIA, "folder": "Crew life"}
    # update or delete answer with one object schema, so the update fields stay at the top level
    assert "result" not in result.structured_content
    assert result.structured_content["drafts_updated"] == 2
    assert result.structured_content["width"] is None


@pytest.mark.asyncio
async def test_the_folders_are_read_and_managed(monkeypatch):
    from fastmcp import Client

    import server

    listing = {
        "data": [], "pagination": {"total": 0, "limit": 50, "offset": 0, "has_more": False},
        "folders": [{"name": "Crew life", "files": 2}],
    }
    seen = _route(monkeypatch, {"/media-list": (200, listing)})
    async with Client(server.mcp) as client:
        listed = await client.call_tool("list_media_files", {"include_folders": True})
    assert seen[-1].method == "GET"
    assert seen[-1].url.path.endswith("/media-list")
    assert seen[-1].url.params["include_folders"] == "true"
    assert listed.structured_content["folders"] == [{"name": "Crew life", "files": 2}]
    renamed = {"folder": "Fleet", "moved": 2, "drafts_updated": 1, "warnings": []}
    seen = _route(monkeypatch, {"/media-folders": (200, renamed)})
    async with Client(server.mcp) as client:
        result = await client.call_tool("manage_media_folder", {"action": "rename", "folder": "Crew life", "to": "Fleet"})
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content) == {"action": "rename", "folder": "Crew life", "to": "Fleet"}
    assert result.structured_content == renamed
    seen = _route(monkeypatch, {"/media-folders": (200, {"folder": "Empty"})})
    async with Client(server.mcp) as client:
        created = await client.call_tool("manage_media_folder", {"action": "create", "folder": "Empty"})
    assert created.structured_content == {"folder": "Empty"}


@pytest.mark.asyncio
async def test_a_folder_that_still_holds_files_comes_back_as_an_error(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    _route(monkeypatch, {"/media-folders": (409, {"error": "This folder still holds files. Move or delete them first.", "name": "folder"})})
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="still holds files"):
            await client.call_tool("manage_media_folder", {"action": "delete", "folder": "Crew life"})


@pytest.mark.asyncio
async def test_the_list_validates_a_file_with_nulls(monkeypatch):
    from fastmcp import Client

    import server

    answer = {"data": [FILE], "pagination": {"total": 1, "limit": 50, "offset": 0, "has_more": False}}
    seen = _route(monkeypatch, {"/media-list": (200, answer)})
    async with Client(server.mcp) as client:
        result = await client.call_tool("list_media_files", {"kind": "image"})
    assert seen[0].url.params["kind"] == "image"
    assert result.structured_content["data"][0]["read_only"] is False


@pytest.mark.asyncio
async def test_in_use_comes_back_with_where(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    seen = _route(monkeypatch, {"/media-update": (409, {"error": "This file is in use in 1 place(s).", "usage": {"count": 1, "items": [{"type": "page", "slug": "about"}], "other_companies": 0}})})
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="in use"):
            await client.call_tool("manage_media_file", {"action": "delete", "media_id": MEDIA})
    assert json.loads(seen[0].content) == {"action": "delete", "media_id": MEDIA}


@pytest.mark.asyncio
async def test_a_delete_goes_through_media_update_and_answers_the_deleted_id(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, {"/media-update": (200, {"id": MEDIA, "status": "deleted"})})
    async with Client(server.mcp) as client:
        result = await client.call_tool("manage_media_file", {"action": "delete", "media_id": MEDIA})
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/media-update")  # /media-delete is hidden, never called
    assert json.loads(seen[0].content) == {"action": "delete", "media_id": MEDIA}
    assert result.structured_content == {"id": MEDIA, "status": "deleted"}


@pytest.mark.asyncio
async def test_publish_returns_the_warnings(monkeypatch):
    from fastmcp import Client

    import server

    answer = {"url": "https://cdn.test/a.css", "hash": "9f86d081884c7d65", "size_bytes": 10, "warnings": ["1 rule(s) sit outside .jm-site"]}
    seen = _route(monkeypatch, {"/portal-css-publish": (200, answer)})
    async with Client(server.mcp) as client:
        result = await client.call_tool("publish_portal_stylesheet", {"portal": "interview"})
    assert json.loads(seen[0].content) == {"portal": "interview"}
    assert result.structured_content["warnings"] == answer["warnings"]


# ---- upload_media_file ----------------------------------------------------------------------


def _args(**extra) -> dict:
    return {"file": base64.b64encode(PNG).decode(), "file_name": "a.png", "content_type": "image/png", **extra}


@pytest.mark.asyncio
async def test_the_three_steps_with_the_real_bytes(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, HAPPY)
    async with Client(server.mcp) as client:
        result = await client.call_tool("upload_media_file", _args(folder="team", width=10, height=20))
    start, put, finish = seen
    assert json.loads(start.content) == {"file_name": "a.png", "content_type": "image/png", "size_bytes": len(PNG), "folder": "team"}
    assert put.method == "PUT"
    assert str(put.url) == SIGNED
    assert put.content == PNG
    assert put.headers["content-type"] == "image/png"
    assert put.headers["cache-control"] == "max-age=31536000"
    assert put.headers["x-upsert"] == "false"
    assert json.loads(finish.content) == {"media_id": MEDIA, "width": 10, "height": 20}
    assert result.structured_content == FINISHED


@pytest.mark.asyncio
async def test_the_signed_address_gets_no_user_token(monkeypatch):
    from fastmcp import Client

    import server
    import upstream

    monkeypatch.setattr(upstream, "current_bearer_token", lambda: "user-token")
    seen = _route(monkeypatch, HAPPY)
    clients: list[httpx.AsyncClient] = []
    real_put = httpx.AsyncClient.put

    async def spying_put(self, *args, **kwargs):
        clients.append(self)
        return await real_put(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "put", spying_put)

    async with Client(server.mcp) as client:
        await client.call_tool("upload_media_file", _args())
    # the client that sent the file holds no auth flow and no project key
    assert clients[0].auth is None
    assert "apikey" not in clients[0].headers
    assert "authorization" not in seen[1].headers


@pytest.mark.asyncio
async def test_replacing_and_acting_for_a_company_reach_the_right_step(monkeypatch):
    from fastmcp import Client

    import server

    seen = _route(monkeypatch, HAPPY)
    async with Client(server.mcp) as client:
        await client.call_tool(
            "upload_media_file",
            _args(replace_media_id=MEDIA, merchant_id="m2", duration_seconds=1.5, poster_url="https://cdn.test/p.png"),
        )
    start = json.loads(seen[0].content)
    assert (start["replace_media_id"], start["merchant_id"]) == (MEDIA, "m2")
    finish = json.loads(seen[2].content)
    assert (finish["duration_seconds"], finish["poster_url"]) == (1.5, "https://cdn.test/p.png")


def test_a_data_url_is_forgiven_and_bad_base64_is_refused():
    from fastmcp.exceptions import ToolError

    from media_upload import decode_file

    encoded = base64.b64encode(PNG).decode()
    assert decode_file(encoded) == PNG
    assert decode_file(f"data:image/png;base64,{encoded}") == PNG
    for bad in ("not base64!", "data:no-comma"):
        with pytest.raises(ToolError, match="not valid base64"):
            decode_file(bad)


@pytest.mark.asyncio
async def test_bad_base64_sends_nothing(monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    seen = _route(monkeypatch, HAPPY)
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match="not valid base64"):
            await client.call_tool("upload_media_file", _args(file="@@@"))
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "match", "requests"),
    [
        # refused before any address exists: switch off, oversize, wrong type
        ({"/media-upload-start": (403, {"error": "The media library is not switched on for this company."})}, "not switched on", 1),
        ({"/media-upload-start": (413, {"error": "A file can be at most 50 MB."})}, "50 MB", 1),
        ({"/media-upload-start": (422, {"error": "Field content_type must be one of"})}, "content_type", 1),
        # the upload itself fails: finish is not called
        ({"/media-upload-start": (200, TICKET), "/upload/sign/": (400, {"message": "mime type not supported"})}, "mime type", 2),
        # refused by the checks on the real bytes
        ({**HAPPY, "/media-upload-finish": (422, {"error": "This MP4 uses the hvc1 codec.", "name": "file"})}, "hvc1", 3),
        # nothing listening
        ({"/media-upload-start": (200, httpx.ConnectError("refused"))}, "Could not reach", 1),
        ({"/media-upload-start": (200, TICKET), "/upload/sign/": (200, httpx.ReadTimeout("slow"))}, "Could not reach", 2),
    ],
)
async def test_a_refusal_stops_there_and_says_why(monkeypatch, answers, match, requests):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    import server

    seen = _route(monkeypatch, answers)
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool("upload_media_file", _args())
    assert len(seen) == requests
