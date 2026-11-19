"""Files attached to chat messages: uploaded to a session, then sent with a message by URL.

The session API (`app.session_api`) and the mock (`app.mock_api`) both keep them here, in
memory, per session. The routes are `POST /api/sessions/{id}/attachments` (multipart, field
`file`) and `GET` on the URL the upload returns.
"""

from __future__ import annotations

import mimetypes
import secrets
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import Response

from app.agent.limits import ATTACHMENT_TOO_LARGE
from app.contracts.session import Attachment, ErrorBody

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# What can be uploaded for now: a STEP file to import, or an image for reference (a photo to
# turn into a part). Anything else is refused with UNSUPPORTED_FILE.
STEP_SUFFIXES = (".step", ".stp")
UNSUPPORTED_FILE = "{name} isn't supported yet. Upload a STEP file (.step, .stp) or an image."


def supported_file(name: str, media_type: str) -> bool:
    """A STEP file, by its extension, or an image, by its media type."""
    return name.lower().endswith(STEP_SUFFIXES) or media_type.startswith("image/")


UPLOAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody},
    413: {"model": ErrorBody},
    415: {"model": ErrorBody},
}
FILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "The attached file", "content": {"*/*": {}}},
    404: {"model": ErrorBody},
}
UploadField = Annotated[
    UploadFile, File(description="The file. Its filename (a relative path for a folder) names it")
]


async def read_upload(upload: UploadFile, limit: int) -> tuple[str, str, bytes]:
    """An uploaded file's name, media type and at most `limit` bytes of it."""
    data = await upload.read(limit)
    name = (upload.filename or "").strip() or "attachment"
    media_type = upload.content_type or ""
    if not media_type or media_type == "application/octet-stream":
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return name, media_type, data


@dataclass
class Attachments:
    """One session's uploaded files, by attachment ID."""

    files: dict[str, tuple[Attachment, bytes]] = field(default_factory=dict)

    async def add(self, base_url: str, upload: UploadFile) -> Attachment:
        name, media_type, data = await read_upload(upload, MAX_ATTACHMENT_BYTES + 1)
        if not supported_file(name, media_type):
            raise HTTPException(415, UNSUPPORTED_FILE.format(name=name))
        if len(data) > MAX_ATTACHMENT_BYTES:
            mb = MAX_ATTACHMENT_BYTES // 1024**2
            raise HTTPException(413, ATTACHMENT_TOO_LARGE.format(name=name, mb=mb))
        return self.store(base_url, name, media_type, data)

    def store(self, base_url: str, name: str, media_type: str, data: bytes) -> Attachment:
        """Keep a file under a new random attachment ID, served at `base_url/<id>`."""
        aid = f"attachment-{secrets.token_hex(8)}"
        while aid in self.files:
            aid = f"attachment-{secrets.token_hex(8)}"
        attachment = Attachment(name=name, media_type=media_type, url=f"{base_url}/{aid}")
        self.files[aid] = (attachment, data)
        return attachment

    def find(self, attachment: Attachment) -> bytes | None:
        """The contents of an attachment a message sends, if it was uploaded here."""
        found = self.files.get(attachment.url.rsplit("/", 1)[-1])
        if found is None or found[0].url != attachment.url:
            return None
        return found[1]

    def response(self, attachment_id: str) -> Response:
        found = self.files.get(attachment_id)
        if found is None:
            raise HTTPException(404, f"no attachment {attachment_id}")
        attachment, data = found
        # The base name only, whichever separator the name used: never a path to save to.
        base = attachment.name.replace("\\", "/").rsplit("/", 1)[-1].lstrip(".") or "attachment"
        filename = quote(base)
        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
        return Response(data, media_type=attachment.media_type, headers=headers)
