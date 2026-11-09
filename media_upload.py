"""upload_media_file: put a file in the media library in one call.

The API uploads in three steps: ``/media-upload-start`` signs an address,
the file goes there directly, and ``/media-upload-finish`` checks it. A model has no
way to send bytes to an address, so like ``upload_knowledge_base_document`` this tool
takes the file as base64 and does the three steps itself.

Base64 and not a public URL: fetching an address a model chose would make this server
reach whatever it can reach. A client that can send a file itself, as a coding agent
can, uses start_media_upload and finish_media_upload instead, which also suits a video
too large to pass as an argument.

The signed address carries its own permission, so the upload goes out without the
user's token.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Annotated, Any, Literal

import httpx2
from fastmcp.exceptions import ToolError
from pydantic import Field

from upstream import build_api_client

logger = logging.getLogger("jobmojito_mcp.media_upload")

UPLOAD_MEDIA_FILE_ANNOTATIONS = {
    "title": "Upload a file to the media library",
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

UPLOAD_MEDIA_FILE_JUSTIFICATION = (
    "readOnlyHint=false / destructiveHint=true: stores a new file in the company's public "
    "media library, or with replace_media_id swaps an existing file and moves page drafts "
    "to it. idempotentHint=false: each call adds a file or a version. "
    "openWorldHint=true: the file goes to the JobMojito API and its storage."
)


def decode_file(file: str) -> bytes:
    """The bytes of a base64 argument. A data: URL prefix is forgiven; bad base64 is not."""
    text = file.split(",", 1)[1] if file.startswith("data:") and "," in file else file
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("`file` is not valid base64. Send the file's bytes base64-encoded.") from exc


def _raise_for_status(resp: httpx2.Response) -> None:
    if resp.status_code >= 400:
        # the shape UpstreamErrorMiddleware turns into a cause + next step
        raise ToolError(f"HTTP error {resp.status_code}: {resp.reason_phrase} - {resp.text[:600]}")


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


async def upload_to_library(
    data: bytes,
    file_name: str,
    content_type: str,
    *,
    folder: str | None = None,
    replace_media_id: str | None = None,
    merchant_id: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_seconds: float | None = None,
    poster_url: str | None = None,
) -> dict[str, Any]:
    """Start, send, finish. What the API refuses comes back as its own message."""
    start_body = _without_none(
        {
            "file_name": file_name,
            "content_type": content_type,
            "size_bytes": len(data),
            "folder": folder,
            "replace_media_id": replace_media_id,
            "merchant_id": merchant_id,
        }
    )
    async with build_api_client() as client:
        try:
            started = await client.post("/media-upload-start", json=start_body)
            _raise_for_status(started)
            ticket = started.json()

            async with httpx2.AsyncClient(timeout=httpx2.Timeout(180.0, connect=15.0)) as storage:
                sent = await storage.put(
                    ticket["upload_url"],
                    content=data,
                    headers={
                        "content-type": content_type,
                        "cache-control": f"max-age={ticket['cache_control']}",
                        "x-upsert": "false",
                    },
                )
            _raise_for_status(sent)

            finish_body = _without_none(
                {
                    "media_id": ticket["media_id"],
                    "width": width,
                    "height": height,
                    "duration_seconds": duration_seconds,
                    "poster_url": poster_url,
                }
            )
            finished = await client.post("/media-upload-finish", json=finish_body)
        except httpx2.HTTPError as exc:
            raise ToolError(f"Could not reach the JobMojito API: {exc}") from exc
    _raise_for_status(finished)
    return finished.json()


def register(mcp) -> None:
    """Register upload_media_file on the given FastMCP server."""

    @mcp.tool(annotations=UPLOAD_MEDIA_FILE_ANNOTATIONS, tags={"jobmojito", "jobmojito-api", "Admin"})
    async def upload_media_file(
        file: Annotated[str, Field(description="The file's bytes, base64-encoded. At most 50 MB once decoded.")],
        file_name: Annotated[str, Field(description="Display name in the library, such as team-photo.jpg. No slashes.")],
        content_type: Annotated[
            Literal[
                "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "image/svg+xml",
                "video/mp4", "audio/mpeg", "audio/mp4", "audio/x-m4a", "font/woff2",
            ],
            Field(
                description=(
                    "image/jpeg, image/png, image/webp, image/gif, image/avif, image/svg+xml, video/mp4 "
                    "(H.264 only), audio/mpeg, audio/mp4, audio/x-m4a or font/woff2."
                )
            ),
        ],
        folder: Annotated[
            str | None,
            Field(description="Library folder, one level, such as Crew life (list_media_files include_folders=true gives the names). Default: the top. A new name makes the folder."),
        ] = None,
        replace_media_id: Annotated[
            str | None,
            Field(description="Replace this file instead of adding one. It must be the same kind, and page drafts move to the new address."),
        ] = None,
        width: Annotated[int | None, Field(description="Width in pixels of an image or video.")] = None,
        height: Annotated[int | None, Field(description="Height in pixels of an image or video.")] = None,
        duration_seconds: Annotated[float | None, Field(description="Length of a video or audio file in seconds.")] = None,
        poster_url: Annotated[
            str | None,
            Field(description="For a video: the url of an image already in this company's library, shown before it plays."),
        ] = None,
        merchant_id: Annotated[
            str | None,
            Field(description="Only for an account acting for a sub-merchant; omit for your own company."),
        ] = None,
    ) -> dict:
        """[Admin] Upload a file to the company's media library and return it, ready to use.

        Sends the file (base64) and has it checked: its real type, the codec of a
        video, and anything active in an SVG, which is refused rather than cleaned.
        The returned `file.url` is what page blocks, brand logos and font settings take.
        The library is public: a file can be reached by anyone with its address.
        Switched on per company by JobMojito; while it is off this is refused with 403.

        A file too large to pass as an argument goes through start_media_upload and
        finish_media_upload, for a client that can send a file to an address.
        """
        return await upload_to_library(
            decode_file(file),
            file_name,
            content_type,
            folder=folder,
            replace_media_id=replace_media_id,
            merchant_id=merchant_id,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            poster_url=poster_url,
        )

    logger.info("Media upload registered (upload_media_file).")
