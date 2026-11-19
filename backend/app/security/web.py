"""The HTTP layer's protections, as one pure ASGI middleware on `/api/*` (`docs/security.md`).

- Knows the client: puts its IP address (`app.security.clients`) in `current_client`.
- CSRF: a state-changing request (POST, PUT, PATCH, DELETE) whose `Origin` (or, without one,
  `Referer`) isn't the app's own origin is refused (403), as is one a browser marks
  `Sec-Fetch-Site: cross-site`. The app's own origin is the host the browser used (the Next.js
  proxy's `X-Forwarded-Host`, else `Host`), `ALLOWED_ORIGINS` and Render's public URL. A request
  with neither header is not from a browser page, so it can't be a forged one; scripts work.
- JSON only: a state-changing request with a body must be `application/json`, except the upload
  routes (`/attachments`, `/imports`), which take `multipart/form-data` (415 otherwise).
- Body size: JSON bodies up to `MAX_JSON_BODY_KB` (512), uploads up to `MAX_UPLOAD_MB` (50); a
  body over it is refused (413) by its `Content-Length` or as soon as it passes the limit.
- Slow uploads: the whole body must arrive within `UPLOAD_TIMEOUT_SECONDS` (120), else 408.
- Writes: every state-changing request counts against `WRITES_PER_MINUTE` (`app.security.limits`).
- Event streams: at most `MAX_EVENT_STREAMS_PER_OWNER` (6) open `/events` streams per owner and
  `MAX_EVENT_STREAMS` (200) in all; one more is refused (429).
- Security headers on every API response: `X-Content-Type-Options: nosniff`, `X-Frame-Options:
  DENY`, `Referrer-Policy: same-origin` and a `Content-Security-Policy` that lets nothing load or
  frame it. The pages' own policy is set by Next.js (`frontend/next.config.ts`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.owners import current_owner
from app.security import limits, settings
from app.security.clients import client_ip, current_client

log = logging.getLogger(__name__)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
UPLOAD_SUFFIXES = ("/attachments", "/imports")
API_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; sandbox"),
]

FORGED = (
    "This request came from another website, so it was blocked. Open the app directly and try "
    "again."
)
JSON_ONLY = "This request needs a JSON body (Content-Type: application/json)."
TOO_MANY_STREAMS = (
    "You have too many tabs following chats live. Close a few tabs and reload this one."
)
BUSY_STREAMS = "The demo is busy right now. Please reload in a minute."


# Room in an upload's body for the form around the file, over the file's own limit.
FORM_SLACK = 64 * 1024


def too_big(limit: int, upload: bool) -> str:
    size = f"{round(limit / 1024**2, 2):g} MB" if upload else f"{limit // 1024} KB"
    what = "This file is" if upload else "This request is"
    return f"{what} bigger than the {size} this demo accepts. Try a smaller one."


def too_slow(seconds: float) -> str:
    return (
        f"The upload took longer than {seconds:g} seconds, so it was stopped. Check your "
        "connection and try again, or try a smaller file."
    )


def _headers(scope: dict) -> dict[bytes, str]:
    out: dict[bytes, str] = {}
    for k, v in scope.get("headers", []):
        out[k] = v.decode("latin-1") if k not in out else out[k] + ", " + v.decode("latin-1")
    return out


def _from_proxy(scope: dict) -> bool:
    return (scope.get("client") or (None,))[0] in ("127.0.0.1", "::1")


def is_https(scope: dict) -> bool:
    """Whether the browser's connection is HTTPS: directly, or as the proxy in front says."""
    if scope.get("scheme") == "https" or settings.on_render():
        return True
    proto = _headers(scope).get(b"x-forwarded-proto", "")
    return _from_proxy(scope) and proto.split(",")[0].strip().lower() == "https"


def _own_hosts(scope: dict) -> set[str]:
    headers = _headers(scope)
    hosts = {headers.get(b"host", "").lower()}
    if _from_proxy(scope):
        hosts |= {h.strip().lower() for h in headers.get(b"x-forwarded-host", "").split(",")}
    return {h for h in hosts if h}


def forged(scope: dict) -> bool:
    """Whether a state-changing request comes from a page on another origin."""
    headers = _headers(scope)
    if headers.get(b"sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = headers.get(b"origin") or headers.get(b"referer")
    if origin is None:
        return False  # not sent by a browser page
    if origin.strip().lower() == "null":
        return True
    parts = urlsplit(origin.strip())
    netloc, full = parts.netloc.lower(), f"{parts.scheme}://{parts.netloc}".lower()
    return netloc not in _own_hosts(scope) and full not in settings.allowed_origins()


def _is_stream(scope: dict) -> bool:
    if scope["method"] != "GET" or not scope["path"].endswith("/events"):
        return False
    query = scope.get("query_string", b"").decode("latin-1").lower()
    return "follow=false" not in query and "follow=0" not in query


class Streams:
    """Open event streams, per owner and in all."""

    def __init__(self) -> None:
        self.by_owner: dict[str, int] = {}
        self.total = 0

    def open(self, owner: str | None) -> str | None:
        """Count one more open stream, or say why it can't open."""
        if self.total >= settings.whole("MAX_EVENT_STREAMS"):
            return BUSY_STREAMS
        key = owner or ""
        if self.by_owner.get(key, 0) >= settings.whole("MAX_EVENT_STREAMS_PER_OWNER"):
            return TOO_MANY_STREAMS
        self.by_owner[key] = self.by_owner.get(key, 0) + 1
        self.total += 1
        return None

    def close(self, owner: str | None) -> None:
        key = owner or ""
        self.by_owner[key] = max(0, self.by_owner.get(key, 0) - 1)
        if not self.by_owner[key]:
            del self.by_owner[key]
        self.total = max(0, self.total - 1)


STREAMS = Streams()


async def _send_error(send: Any, status: int, detail: str, extra: list | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *API_HEADERS,
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    """Pure ASGI, so streamed responses pass through untouched. Runs inside `OwnerMiddleware`,
    so the owner is known."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        token = current_client.set(client_ip(scope))
        try:
            await self._handle(scope, receive, send)
        finally:
            current_client.reset(token)

    async def _handle(self, scope: dict, receive: Any, send: Any) -> None:
        method = scope["method"]

        async def send_secure(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0] not in dict(API_HEADERS)]
                message = {**message, "headers": headers + API_HEADERS}
            await send(message)

        if method in UNSAFE:
            refused = self._refuse_write(scope)
            if refused is not None:
                await _send_error(send, *refused)
                return
            receive = self._limited(scope, receive)
        if _is_stream(scope):
            owner = current_owner.get()
            why = STREAMS.open(owner)
            if why is not None:
                await _send_error(send, 429, why, [(b"retry-after", b"30")])
                return
            try:
                await self.app(scope, receive, send_secure)
            finally:
                STREAMS.close(owner)
            return
        await self.app(scope, receive, send_secure)

    def _refuse_write(self, scope: dict) -> tuple[int, str, list] | None:
        if forged(scope):
            log.warning("refused a cross-origin %s %s", scope["method"], scope["path"])
            return 403, FORGED, []
        headers = _headers(scope)
        length = headers.get(b"content-length")
        has_body = (length is not None and length.strip() not in ("", "0")) or (
            b"transfer-encoding" in headers
        )
        kind = headers.get(b"content-type", "").split(";")[0].strip().lower()
        upload = scope["path"].endswith(UPLOAD_SUFFIXES)
        if has_body:
            allowed = {"multipart/form-data"} if upload else {"application/json"}
            if kind not in allowed:
                return 415, JSON_ONLY if not upload else "Upload the file as a form.", []
        limit, shown = self._limit(upload, scope["path"])
        if length is not None:
            try:
                if int(length) > limit:
                    return 413, too_big(shown, upload), []
            except ValueError:
                return 400, "The request's Content-Length isn't a number.", []
        try:
            limits.take(limits.WRITES)
        except limits.LimitReached as reached:
            retry = str(max(1, int(reached.wait))).encode()
            return 429, str(reached.detail), [(b"retry-after", retry)]
        return None

    @staticmethod
    def _limit(upload: bool, path: str = "") -> tuple[int, int]:
        """The most a body may be, and the limit to name: an import names the STEP size
        limit (`app.imports.MAX_IMPORT_BYTES`) when it is lower than the uploads' cap, so a
        refused file is told the limit it must meet."""
        if not upload:
            size = int(settings.number("MAX_JSON_BODY_KB") * 1024)
            return size, size
        size = int(settings.number("MAX_UPLOAD_MB") * 1024 * 1024)
        if path.endswith("/imports"):
            from app.imports import MAX_IMPORT_BYTES

            if MAX_IMPORT_BYTES < size:
                return MAX_IMPORT_BYTES + FORM_SLACK, MAX_IMPORT_BYTES
        return size, size

    def _limited(self, scope: dict, receive: Any) -> Any:
        """`receive`, cut off once the body passes its size limit or takes too long."""
        upload = scope["path"].endswith(UPLOAD_SUFFIXES)
        limit, shown = self._limit(upload, scope["path"])
        timeout = settings.number("UPLOAD_TIMEOUT_SECONDS")
        deadline = time.monotonic() + timeout
        state = {"read": 0, "done": False}

        async def wrapped() -> dict:
            if state["done"]:
                return await receive()
            remaining = deadline - time.monotonic()
            try:
                message = await asyncio.wait_for(receive(), max(remaining, 0.001))
            except TimeoutError:
                raise HTTPException(408, too_slow(timeout)) from None
            if message["type"] == "http.request":
                state["read"] += len(message.get("body", b""))
                if state["read"] > limit:
                    raise HTTPException(413, too_big(shown, upload))
                if not message.get("more_body", False):
                    state["done"] = True
            return message

        return wrapped


__all__ = ["STREAMS", "SecurityMiddleware", "forged", "is_https"]
