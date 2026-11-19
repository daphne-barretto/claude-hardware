"""Every protection's setting, read from the environment each time it is used (so tests and a
restart with new values need no code change). `docs/security.md` lists them with defaults."""

from __future__ import annotations

import os

# name -> default. A missing, empty or unreadable value means the default.
DEFAULTS: dict[str, float] = {
    # Cost (per owner cookie and per client IP).
    "RUNS_PER_HOUR": 30,
    "SESSIONS_PER_DAY": 20,
    "MESSAGES_PER_MINUTE": 10,
    # Any other state-changing request (rebuilds, edits, exports, uploads), per owner and IP.
    "WRITES_PER_MINUTE": 120,
    # App-wide spend per UTC day, in US dollars; the warning is logged at this fraction.
    "DAILY_BUDGET_USD": 50.0,
    "BUDGET_WARN_FRACTION": 0.8,
    # One run (one message's reply).
    "AGENT_MAX_TURNS": 30,
    "RUN_BUDGET_USD": 2.0,
    # Denial of service.
    "MAX_EVENT_STREAMS_PER_OWNER": 6,
    "MAX_EVENT_STREAMS": 200,
    "MAX_JSON_BODY_KB": 512,
    "MAX_UPLOAD_MB": 50,
    "UPLOAD_TIMEOUT_SECONDS": 120,
    "OWNER_DISK_QUOTA_MB": 500,
    "EXPORT_MAX_AGE_HOURS": 24,
    "MAX_IMAGE_PIXELS": 40_000_000,
    # Proxies: how many trusted proxies append to X-Forwarded-For in front of the backend.
    # Unset: 1 on Render (Render's edge appends the client; the app's own Next.js proxy passes
    # the header through unchanged), else 0 (trust none: use the connecting address).
    "TRUSTED_PROXY_HOPS": -1,
}


def number(name: str) -> float:
    """The setting `name`: a number from the environment, else its default."""
    default = DEFAULTS[name]
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value >= 0 else default


def whole(name: str) -> int:
    return int(number(name))


def on_render() -> bool:
    return bool(os.environ.get("RENDER", "").strip())


def is_production() -> bool:
    """Running for the public: on Render (it sets `RENDER`) or with `APP_ENV=production`."""
    return on_render() or os.environ.get("APP_ENV", "").strip().lower() == "production"


def proxy_hops() -> int:
    raw = os.environ.get("TRUSTED_PROXY_HOPS", "").strip()
    try:
        hops = int(raw) if raw else -1
    except ValueError:
        hops = -1
    if hops < 0:
        return 1 if on_render() else 0
    return hops


def allowed_origins() -> set[str]:
    """Origins that may send state-changing requests, besides the request's own host:
    `ALLOWED_ORIGINS` (comma-separated) and Render's public URL (`RENDER_EXTERNAL_URL`)."""
    names = os.environ.get("ALLOWED_ORIGINS", "").split(",")
    names.append(os.environ.get("RENDER_EXTERNAL_URL", ""))
    return {n.strip().rstrip("/").lower() for n in names if n.strip()}


class UnsafeConfiguration(RuntimeError):
    pass


def check_startup() -> None:
    """Refuse to start in production with the build sandbox's kernel limits turned off
    (`GEOMETRY_SANDBOX_KERNEL_LIMITS=optional` is for local macOS development only)."""
    limits = os.environ.get("GEOMETRY_SANDBOX_KERNEL_LIMITS", "").strip().lower()
    if is_production() and limits == "optional":
        raise UnsafeConfiguration(
            "GEOMETRY_SANDBOX_KERNEL_LIMITS=optional turns off the build sandbox's kernel limits; "
            "it is for local development only and is refused in production (RENDER or "
            "APP_ENV=production). Unset it."
        )


__all__ = [
    "DEFAULTS",
    "UnsafeConfiguration",
    "allowed_origins",
    "check_startup",
    "is_production",
    "number",
    "on_render",
    "proxy_hops",
    "whole",
]
