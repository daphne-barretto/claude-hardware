"""The app-wide daily spend cap (`DAILY_BUDGET_USD`, default $50 a UTC day).

Every run adds what it cost to the day's total in the session store's database (`usage` in
`DATA_DIR/sessions.db`), from the SDK's `ResultMessage` (`total_cost_usd`, else its token usage
at a conservative price). Once the day's total reaches the budget, new runs are refused with a
plain message (`BudgetReached`, a 503 with `Retry-After` until midnight UTC); runs already going
finish. Crossing `BUDGET_WARN_FRACTION` (80%) of the budget logs a warning once a day.
`GET /api/health` reports the day's usage (`status()`), never anything secret.

Each run is also capped on its own (`RUN_BUDGET_USD`, the SDK's `max_budget_usd`), so the most
a day can overshoot is one run's cap per run slot (`MAX_AGENT_RUNS`).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.security import settings
from app.security.limits import when

log = logging.getLogger(__name__)

# US dollars per million tokens when a result reports tokens but no cost: the highest list
# prices, so an estimate never undercounts.
FALLBACK_INPUT_PER_MTOK = 15.0
FALLBACK_OUTPUT_PER_MTOK = 75.0

_warned: set[str] = set()


def now() -> datetime:
    return datetime.now(UTC)


def today() -> str:
    return now().date().isoformat()


def seconds_to_reset() -> float:
    current = now()
    midnight = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), UTC)
    return (midnight - current).total_seconds()


def _store():
    from app.session_store import session_store

    return session_store()


def budget_usd() -> float:
    return settings.number("DAILY_BUDGET_USD")


def spent() -> dict[str, Any]:
    return _store().usage(today())


def reached() -> bool:
    return spent()["cost_usd"] >= budget_usd()


def limit_message() -> str:
    return (
        "The demo has reached today's limit for Claude, so it can't take new requests right "
        f"now. It resets at midnight UTC ({when(seconds_to_reset())}). Meanwhile you can still "
        "open your chats and the demos, and edit designs by hand."
    )


class BudgetReached(HTTPException):
    def __init__(self) -> None:
        retry = max(1, math.ceil(seconds_to_reset()))
        super().__init__(503, limit_message(), headers={"Retry-After": str(retry)})


def check() -> None:
    """Raise `BudgetReached` once the day's spend has reached the budget."""
    if reached():
        raise BudgetReached()


def cost_of(cost_usd: float | None, input_tokens: int | None, output_tokens: int | None) -> float:
    if cost_usd is not None:
        return max(0.0, float(cost_usd))
    return (
        (input_tokens or 0) * FALLBACK_INPUT_PER_MTOK
        + (output_tokens or 0) * FALLBACK_OUTPUT_PER_MTOK
    ) / 1_000_000


def record(cost_usd: float | None, input_tokens: int | None, output_tokens: int | None) -> None:
    """Add one run's usage to today's total, and warn once the day passes the warning line."""
    day = today()
    cost = cost_of(cost_usd, input_tokens, output_tokens)
    try:
        store = _store()
        store.add_usage(day, cost, int(input_tokens or 0), int(output_tokens or 0))
        total = store.usage(day)["cost_usd"]
    except Exception:  # never lose the run over its bookkeeping
        log.exception("can't record a run's usage")
        return
    budget = budget_usd()
    line = budget * settings.number("BUDGET_WARN_FRACTION")
    if total >= line and day not in _warned:
        _warned.add(day)
        log.warning(
            "Daily Claude spend is $%.2f, %.0f%% of the $%.2f budget (DAILY_BUDGET_USD)",
            total,
            100 * total / budget if budget else 100,
            budget,
        )


_cache: dict[str, Any] = {}
CACHE_SECONDS = 1.0


def cached_status() -> dict[str, Any]:
    """`status()`, read again at most every `CACHE_SECONDS` (for the health check)."""
    key = (str(_store_root()), budget_usd())
    now_s = time.monotonic()
    if _cache.get("key") != key or now_s - _cache.get("at", -1e9) > CACHE_SECONDS:
        _cache.update(key=key, at=now_s, value=status())
    return dict(_cache["value"])


def _store_root():
    from app.runtime import data_dir

    return data_dir()


def status() -> dict[str, Any]:
    """Today's usage for `/api/health`: no secrets, only totals."""
    used = spent()
    budget = budget_usd()
    return {
        "day": today(),
        "spent_usd": round(used["cost_usd"], 4),
        "budget_usd": budget,
        "runs": used["runs"],
        "input_tokens": used["input_tokens"],
        "output_tokens": used["output_tokens"],
        "limit_reached": used["cost_usd"] >= budget,
    }


__all__ = ["BudgetReached", "budget_usd", "check", "limit_message", "record", "reached", "status"]
