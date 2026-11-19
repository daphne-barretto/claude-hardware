"""Which browser a session belongs to, so people using the app at once never see each
other's sessions, builds or history.

Each browser gets an owner token in a cookie (`OWNER_COOKIE`) on its first request to `/api`.
A session made with it belongs to it: the session list shows only that browser's sessions, and
every other route answers 404 for a session the browser doesn't own, as if it didn't exist.
Sessions saved before owners existed have none; the first browser to open one claims it.

The token is a random secret, not an identity: it keeps people apart, nothing more. A
client without a cookie jar (a script, a webhook) sends the cookie it got back with the first
response. `OwnerMiddleware` reads the cookie, or makes a token and sets it on the response, and
puts the token in `current_owner` for the request; outside a request it is None and nothing is
checked. The same token is the request's `geometry.slots.slot_owner`, so its builds share the
build slots fairly with other browsers'.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from http.cookies import SimpleCookie
from typing import Any

from geometry.slots import slot_owner

OWNER_COOKIE = "hardware_owner"
MAX_AGE = 400 * 24 * 3600  # the longest a browser keeps a cookie
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

current_owner: ContextVar[str | None] = ContextVar("current_owner", default=None)


def new_owner() -> str:
    return secrets.token_urlsafe(18)


def owner_from(cookie_header: str) -> str | None:
    """The owner token in a `Cookie` header, if there is a valid one."""
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:  # a malformed header: treat it as no cookie
        return None
    morsel = jar.get(OWNER_COOKIE)
    return morsel.value if morsel is not None and _TOKEN.match(morsel.value) else None


def set_cookie_header(owner: str, secure: bool = False) -> bytes:
    """The owner cookie. `Secure` when the browser's connection is HTTPS (directly or behind
    Render's proxy), so it never travels over plain HTTP there."""
    flags = "; Secure" if secure else ""
    return (
        f"{OWNER_COOKIE}={owner}; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Lax{flags}"
    ).encode()


class OwnerMiddleware:
    """Pure ASGI, so streamed responses (`/events`) pass through untouched."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        header = b"; ".join(v for k, v in scope.get("headers", []) if k == b"cookie")
        owner = owner_from(header.decode("latin-1"))
        fresh = owner is None
        if owner is None:
            owner = new_owner()
        from app.security.web import is_https

        secure = is_https(scope)

        async def send_with_cookie(message: dict) -> None:
            if fresh and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", set_cookie_header(owner, secure)))
                message = {**message, "headers": headers}
            await send(message)

        token = current_owner.set(owner)
        # Builds started for this request (and the runs it spawns) share the build slots
        # fairly with other browsers' (`geometry.slots`).
        slot_token = slot_owner.set(owner)
        try:
            await self.app(scope, receive, send_with_cookie)
        finally:
            slot_owner.reset(slot_token)
            current_owner.reset(token)


__all__ = ["OWNER_COOKIE", "OwnerMiddleware", "current_owner", "new_owner", "owner_from"]
