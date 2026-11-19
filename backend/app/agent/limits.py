"""Limits and failures, said plainly: what happened, whether anything was lost, what to do.

Every limit or failure a user can hit while Claude works reaches the chat as one of two events
(`app.contracts.session`): a `notice` while the run goes on (`retrying`, `waiting`, `warning`)
or an `error` when it stops (with `retryable` for the Retry control). Both carry a `reason`
code (`LimitReason`) next to the text. The texts here are the only user-facing words for them:
no status codes or internal names. The raw details go to the server log instead.

```python
failure = classify_exception(TimeoutError("read timed out"))  # Failure("network", transient)
stopped_text(failure.reason, tries=3)  # "The connection to Claude kept dropping, so ..."
```

Temporary API errors (rate limits, overload, network) are retried with backoff. The Claude Code
CLI retries a failed API call itself, honouring `retry-after`, and reports each retry as a
`system`/`api_retry` message, which becomes a `retrying` notice (`retry_notice`). A run the CLI
couldn't keep going (its process died, or it stopped on a temporary error without retrying) is
resumed by `app.agent.run` under `RetryPolicy`: a new attempt picks up from the workspace, so
the rounds and builds so far are kept.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.contracts.session import LimitReason

# --- What went wrong ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """Why a run stopped. `transient` failures are retried automatically; `detail` is the raw
    error, for the log only."""

    reason: LimitReason
    transient: bool = False
    retry_after: float | None = None
    detail: str = ""


TRANSIENT: frozenset[str] = frozenset({"rate_limited", "overloaded", "network"})

# The SDK's `AssistantMessage.error` categories.
_SDK_ERRORS: dict[str, LimitReason] = {
    "rate_limit": "rate_limited",
    "server_error": "overloaded",
    "billing_error": "billing",
    "authentication_failed": "not_configured",
}

_TEXT_PATTERNS: list[tuple[str, LimitReason]] = [
    # A model the key can't use: not found, or no permission for that model.
    (
        r"not_found_error|model[^a-z]{0,3}not found|permission_error.{0,200}model"
        r"|access to (?:the )?model|model\b.{0,60}\b(?:not available|unavailable)",
        "model_unavailable",
    ),
    # Most specific first: a billing error also says "invalid_request_error".
    (r"credit balance|billing|purchase credits|insufficient[_ ]credits?", "billing"),
    (
        r"x-api-key|api[_ ]key|authentication_error|unauthori[sz]ed|\b401\b|permission_error"
        r"|\b403\b|not logged in|/login",
        "not_configured",
    ),
    (
        r"prompt is too long|too many tokens|context (?:window|length)|request_too_large"
        r"|request too large|\b413\b|maximum context",
        "too_large",
    ),
    (r"rate[_ ]limit|\b429\b|too many requests", "rate_limited"),
    (
        r"overloaded|\b529\b|\b50[0234]\b|internal server error|service unavailable|api_error",
        "overloaded",
    ),
    (
        r"timed? ?out|timeout|connection|network|econnreset|econnrefused|socket|enotfound"
        r"|fetch failed|stream (?:ended|closed)|broken pipe",
        "network",
    ),
]


def reason_for_status(status: int | None) -> LimitReason | None:
    """The reason for an HTTP status of a failed API call (`ResultMessage.api_error_status`)."""
    if status is None:
        return None
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "not_configured"
    if status == 402:
        return "billing"
    if status == 413:
        return "too_large"
    if status == 404:
        return "model_unavailable"
    if status == 408:
        return "network"
    if status >= 500:
        return "overloaded"
    return None


def classify_text(text: str) -> LimitReason | None:
    """The reason an error text names, if it names one."""
    lowered = text.lower()
    for pattern, reason in _TEXT_PATTERNS:
        if re.search(pattern, lowered):
            return reason
    return None


def retry_after_in(text: str) -> float | None:
    """A `retry-after` the error text carries, in seconds."""
    match = re.search(r"retry[-_ ]after\"?\s*[:=]?\s*\"?(\d+(?:\.\d+)?)", text, re.I)
    return float(match.group(1)) if match else None


def failure(reason: LimitReason | None, detail: str = "") -> Failure:
    reason = reason or "unknown"
    return Failure(reason, reason in TRANSIENT, retry_after_in(detail), detail)


def classify_message(message: Any) -> Failure | None:
    """The failure an SDK message reports: an assistant message with an `error` (the CLI's
    API error, after its own retries), or an error result. None for anything else."""
    error = getattr(message, "error", None)
    texts = [getattr(b, "text", "") for b in getattr(message, "content", None) or []]
    if isinstance(error, str) and hasattr(message, "content"):
        detail = " ".join(t for t in texts if t) or error
        return failure(_SDK_ERRORS.get(error) or classify_text(detail), detail)
    if getattr(message, "is_error", False) and hasattr(message, "subtype"):
        detail = "; ".join(getattr(message, "errors", None) or []) or (
            getattr(message, "result", None) or message.subtype
        )
        status = getattr(message, "api_error_status", None)
        return failure(reason_for_status(status) or classify_text(detail), detail)
    return None


def classify_exception(exc: BaseException) -> Failure:
    """The failure an exception from the driver means. The CLI dying or its connection
    dropping is a network failure: worth another try."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return failure("network", detail)
    name = type(exc).__name__
    if name in ("CLIConnectionError", "ProcessError", "CLIJSONDecodeError"):
        return failure(classify_text(str(exc)) or "network", detail)
    return failure(classify_text(str(exc)), detail)


# --- What the chat says -------------------------------------------------------------------------

KEPT = "Your design and builds so far are kept."

_BUSY = {
    "rate_limited": "Claude is getting a lot of requests right now",
    "overloaded": "Claude is overloaded right now",
    "network": "The connection to Claude dropped",
}


def retrying_text(reason: LimitReason, attempt: int, attempts: int) -> str:
    """A `retrying` notice: the run is paused and tries again by itself."""
    lead = _BUSY.get(reason, "Something went wrong reaching Claude")
    return (
        f"{lead}, so this reply is paused. It tries again automatically "
        f"(try {attempt} of {attempts}). Nothing is lost."
    )


def stopped_text(reason: LimitReason, tries: int = 0) -> str:
    """The `error` that ends a run, by reason."""
    after = f" after {tries} tries" if tries > 1 else ""
    return {
        "rate_limited": f"Claude is still too busy{after}, so this reply stopped. {KEPT} "
        "Wait a minute, then press Retry.",
        "overloaded": f"Claude is still overloaded{after}, so this reply stopped. {KEPT} "
        "Wait a minute, then press Retry.",
        "network": f"The connection to Claude kept dropping{after}, so this reply stopped. "
        f"{KEPT} Press Retry to pick up where it left off.",
        "billing": "Claude can't reply right now: this app's Anthropic account has run out of "
        "credits. Your design and builds are kept. Let whoever runs this app know, then press "
        "Retry once it's topped up.",
        "not_configured": "Claude isn't set up on this server yet, so it can't reply. Your "
        "design and builds are kept. Let whoever runs this app know.",
        "too_large": "This chat has grown too long for Claude to read in one go, so this reply "
        f"stopped. {KEPT} Try a shorter message or fewer attachments, or start a new chat for "
        "the next change.",
        "preempted": "Claude stopped waiting for your answer so that someone else's request "
        f"could go ahead. {KEPT} Press Retry, or send a message, to carry on.",
        "stopped": f"You stopped this reply. {KEPT}",
        "interrupted": "This reply was interrupted because the server restarted while Claude "
        f"was working. {KEPT} Press Retry to pick up where it left off.",
    }.get(
        reason,
        f"Something went wrong on our side, so this reply stopped. {KEPT} Press Retry to pick "
        "up where it left off.",
    )


def model_unavailable_text(default: str) -> str:
    """The `error` of a run whose model the server's key can't use (`default`: the name of the
    model Retry goes on with)."""
    return (
        f"That model isn't available right now, so this reply stopped. {KEPT} Press Retry to "
        f"carry on with {default}; the model picker now offers only the models that work."
    )


def retryable(reason: LimitReason) -> bool:
    """Whether Retry can help: not for a server that isn't set up or a chat that's too long."""
    return reason not in ("not_configured", "too_large")


def waiting_text(place: int) -> str:
    """A `waiting` notice for a run in line behind other people's runs (1: next)."""
    if place <= 1:
        return (
            "You're next. Claude starts on this as soon as a reply that's already running finishes."
        )
    ahead = place - 1
    requests = "request" if ahead == 1 else "requests"
    return (
        f"Waiting for {ahead} {requests} ahead of you. Claude starts on this when it's your "
        "turn; you move up as they finish."
    )


BUILD_QUEUE = "Waiting for another build to finish. This one starts right after."

# A build that hit a limit (`geometry.sandbox.BuildError` kinds): a `warning`, the run goes on.
BUILD_LIMITS: dict[str, tuple[LimitReason, str]] = {
    "memory": (
        "build_memory",
        "A build needed more memory than the server allows, so it stopped. Your earlier builds "
        "are kept, and Claude has been told so it can simplify the design.",
    ),
    "timeout": (
        "build_time",
        "A build took longer than the time limit allows, so it stopped. Your earlier builds "
        "are kept, and Claude has been told so it can simplify the design.",
    ),
    "crash": (
        "build_crash",
        "A build stopped unexpectedly. Your earlier builds are kept, and Claude has been told "
        "so it can try again.",
    ),
}

# An export that failed (`POST .../exports`), by the build error's kind.
_EXPORT_FIX = "Your design is unchanged: export fewer parts, or one format at a time."
EXPORT_FAILED: dict[str, str] = {
    "output": f"This export is too large for the server to write. {_EXPORT_FIX}",
    "disk": f"This export is too large for the server to write. {_EXPORT_FIX}",
    "memory": f"Exporting needed more memory than the server allows. {_EXPORT_FIX}",
    "timeout": f"Exporting took longer than the server allows. {_EXPORT_FIX}",
}
EXPORT_UNKNOWN = "The export couldn't be written just now. Your design is unchanged; try again."
ATTACHMENT_TOO_LARGE = (
    "{name} is larger than {mb} MB, the most an attached file can be. Nothing was sent: attach "
    "a smaller file, or only the pages or parts Claude needs."
)

SAVE_FAILED = (
    "This chat couldn't be saved just now because the server is out of space. Claude keeps "
    "working and everything stays open here; it saves again as soon as it can."
)

# --- Retrying -----------------------------------------------------------------------------------

Sleep = Callable[[float], Awaitable[Any]]


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except ValueError:
        return default


@dataclass
class RetryPolicy:
    """How a run retries temporary failures: `attempts` retries after the first try, waiting
    `base_s` doubled each time up to `cap_s`, or the server's `retry-after` when it is longer.
    `cli_retries` caps the CLI's own retries of a failed API call (`CLAUDE_CODE_MAX_RETRIES`).
    Tests swap `sleep` for a fake clock."""

    attempts: int = field(default_factory=lambda: _env_int("AGENT_RUN_RETRIES", 2))
    cli_retries: int = field(default_factory=lambda: _env_int("AGENT_API_RETRIES", 4))
    base_s: float = field(default_factory=lambda: float(_env_int("AGENT_RETRY_BASE_S", 10)))
    cap_s: float = 60.0
    sleep: Sleep = asyncio.sleep

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        backoff = min(self.cap_s, self.base_s * 2 ** (attempt - 1))
        return max(backoff, retry_after or 0.0)


POLICY = RetryPolicy()


def retry_notice(data: dict[str, Any]) -> dict[str, Any] | None:
    """The `retrying` notice for the CLI's `system`/`api_retry` message (its own retry of a
    failed API call, after `retry_delay_ms`)."""
    attempt = int(data.get("attempt") or 1)
    attempts = max(attempt, int(data.get("max_retries") or attempt))
    reason = (
        reason_for_status(data.get("error_status"))
        or _SDK_ERRORS.get(str(data.get("error") or ""))
        or classify_text(str(data.get("error") or ""))
        or "network"
    )
    if reason not in TRANSIENT:
        return None
    seconds = max(0.0, float(data.get("retry_delay_ms") or 0) / 1000)
    return {
        "reason": reason,
        "text": retrying_text(reason, attempt, attempts),
        "retry_in_s": round(seconds, 1),
        "attempt": attempt,
        "attempts": attempts,
    }
