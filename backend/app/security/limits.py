"""Per-owner and per-IP rate limits (`docs/security.md`).

Each limit counts events in a sliding window, separately for the owner cookie (`app.owners`)
and for the client's IP address (`app.security.clients`; per-IP counting skips addresses that
aren't public). A request over either answers 429 with a plain message saying when it resets
and a `Retry-After` header. Counts live in memory: a restart starts them again.

| Limit | Setting (default) | Counts |
| --- | --- | --- |
| sessions | `SESSIONS_PER_DAY` (20) | `POST /api/sessions` that made a session |
| messages | `MESSAGES_PER_MINUTE` (10) | every `POST .../messages` (answers included) |
| runs | `RUNS_PER_HOUR` (30) | runs started: messages and loaded revisions that start Claude |
| writes | `WRITES_PER_MINUTE` (120) | every other state-changing request |

The friendly-limits work (`local/friendly-limits`) adds a general notice shape for these
messages; until it lands they are the `detail` of a plain 429 (`LimitReached`).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException

from app.owners import current_owner
from app.security import settings
from app.security.clients import current_client, is_public


@dataclass(frozen=True)
class Limit:
    name: str
    setting: str
    window: float  # seconds

    @property
    def size(self) -> int:
        return settings.whole(self.setting)


SESSIONS = Limit("sessions", "SESSIONS_PER_DAY", 24 * 3600)
MESSAGES = Limit("messages", "MESSAGES_PER_MINUTE", 60)
RUNS = Limit("runs", "RUNS_PER_HOUR", 3600)
WRITES = Limit("writes", "WRITES_PER_MINUTE", 60)

MAX_KEYS = 50_000  # counters kept before the idle ones are dropped


def clock() -> float:
    return time.time()


def when(seconds: float) -> str:
    """'in about 3 hours', in plain words."""
    if seconds < 60:
        return "in less than a minute"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"in about {hours} hour{'s' if hours != 1 else ''}"


def message(limit: Limit, wait: float) -> str:
    n = limit.size
    if limit is SESSIONS:
        return (
            f"You've started {n} chats today, the most this demo allows in a day. "
            f"You can start another {when(wait)}. Your existing chats are still open."
        )
    if limit is MESSAGES:
        return (
            f"You're sending messages faster than this demo allows ({n} a minute). "
            f"Please wait a moment and try again {when(wait)}."
        )
    if limit is RUNS:
        return (
            f"You've reached this demo's limit of {n} replies from Claude an hour. "
            f"You can send another {when(wait)}. Meanwhile you can still edit the design by hand."
        )
    return (
        f"That's a lot of changes in a short time ({n} a minute is the most this demo allows). "
        f"Please try again {when(wait)}."
    )


class LimitReached(HTTPException):
    def __init__(self, limit: Limit, wait: float):
        retry = max(1, math.ceil(wait))
        super().__init__(429, message(limit, wait), headers={"Retry-After": str(retry)})
        self.limit = limit
        self.wait = wait


class Counters:
    """Sliding-window counts by (limit, key)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _window(self, limit: Limit, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault((limit.name, key), deque())
        while hits and hits[0] <= now - limit.window:
            hits.popleft()
        return hits

    def wait(self, limit: Limit, keys: list[str]) -> float:
        """Seconds until one more event fits under `limit` for every key (0: it fits now)."""
        now, size, longest = clock(), limit.size, 0.0
        with self._lock:
            for key in keys:
                hits = self._window(limit, key, now)
                if len(hits) >= size:
                    longest = max(longest, hits[len(hits) - size] + limit.window - now)
        return longest

    def add(self, limit: Limit, keys: list[str]) -> None:
        now = clock()
        with self._lock:
            if len(self._hits) > MAX_KEYS:
                self._prune(now)
            for key in keys:
                self._window(limit, key, now).append(now)

    def _prune(self, now: float) -> None:
        longest = max(SESSIONS.window, RUNS.window)
        for k in [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - longest]:
            del self._hits[k]


COUNTERS = Counters()


def keys() -> list[str]:
    """The keys a request counts under: its owner and, when public, its IP address."""
    out = []
    owner = current_owner.get()
    if owner:
        out.append(f"owner:{owner}")
    ip = current_client.get()
    if is_public(ip):
        out.append(f"ip:{ip}")
    return out


def check(limit: Limit) -> None:
    """Raise `LimitReached` if one more `limit` event would go over it."""
    found = keys()
    if not found:
        return  # outside a request (scripts, tests calling functions directly)
    wait = COUNTERS.wait(limit, found)
    if wait > 0:
        raise LimitReached(limit, wait)


def record(limit: Limit) -> None:
    found = keys()
    if found:
        COUNTERS.add(limit, found)


def take(limit: Limit) -> None:
    """Check and count one `limit` event."""
    check(limit)
    record(limit)


__all__ = [
    "COUNTERS",
    "MESSAGES",
    "RUNS",
    "SESSIONS",
    "WRITES",
    "Limit",
    "LimitReached",
    "check",
    "message",
    "record",
    "take",
    "when",
]
