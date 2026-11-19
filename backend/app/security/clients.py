"""Who is asking: the client's IP address, for the per-IP limits.

The backend sits behind the app's own Next.js server (same container, on loopback), which passes
`X-Forwarded-For` through unchanged; on Render, Render's edge appends the address it saw. So the
client is the address `TRUSTED_PROXY_HOPS` entries from the right of `X-Forwarded-For`, and the
header is read only when the connection comes from loopback (the Next.js proxy) and at least one
hop is trusted. Anything to the left of that entry was written by the client and is ignored.
With no trusted hops (local development) the client is the connecting address.

An address that is loopback or private (a local request, or proxies set up differently than
configured) is not a public client: per-IP limits skip it, per-owner limits still apply.
"""

from __future__ import annotations

import ipaddress
from contextvars import ContextVar

from app.security.settings import proxy_hops

current_client: ContextVar[str | None] = ContextVar("current_client", default=None)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _header(scope: dict, name: bytes) -> str:
    return ", ".join(v.decode("latin-1") for k, v in scope.get("headers", []) if k == name)


def client_ip(scope: dict) -> str | None:
    """The client's IP address for an ASGI request `scope` (None if unknown)."""
    peer = (scope.get("client") or (None, None))[0]
    hops = proxy_hops()
    if hops > 0 and peer in _LOOPBACK:
        chain = [p.strip() for p in _header(scope, b"x-forwarded-for").split(",") if p.strip()]
        if len(chain) >= hops:
            candidate = chain[-hops]
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                return None
    return peer


def is_public(ip: str | None) -> bool:
    """Whether `ip` is a real client's address (not loopback, private or unknown)."""
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_global


__all__ = ["client_ip", "current_client", "is_public"]
