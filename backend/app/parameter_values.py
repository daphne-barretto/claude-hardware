"""What a hand edit may set a parameter to (`PATCH .../parameters/{name}`).

A number stays a finite number within reach of a real part (`MAX_MAGNITUDE`, in the
parameter's unit) and within the parameter's `min` and `max`; text stays non-empty text. A
value of the wrong kind is refused with a message naming the parameter, before anything
changes. Whether a size of 0 or below builds is the rebuild's to say: a position can be 0 or
negative, so the value alone can't tell.
"""

from __future__ import annotations

import math

from app.contracts.session import Parameter

MAX_MAGNITUDE = 100_000.0  # 100 m (or 100,000 degrees): far past any part


def value_error(param: Parameter, value: object) -> str | None:
    """Why `value` can't be `param`'s new value, or None if it can."""
    name = param.name
    unit = f" {param.unit}" if param.unit else ""
    if isinstance(param.value, bool) or not isinstance(param.value, int | float):
        if not isinstance(value, str) or not value.strip():
            return f"{name} is text: enter a non-empty value"
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"{name} is a number{unit}: {value!r} is not a number"
    if not math.isfinite(value):
        return f"{name} must be a finite number{unit}, not {value}"
    if abs(value) > MAX_MAGNITUDE:
        return (
            f"{name} = {value:g}{unit} is out of range: at most {MAX_MAGNITUDE:g}{unit} either way"
        )
    if param.min is not None and value < param.min:
        return f"{name} must be at least {param.min:g}{unit}"
    if param.max is not None and value > param.max:
        return f"{name} must be at most {param.max:g}{unit}"
    return None


__all__ = ["MAX_MAGNITUDE", "value_error"]
