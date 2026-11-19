"""A hand edit that breaks a team standard is the user's decision, never a silent failure.

Locked values are the user's choices; standards are the team's rules; only the user changes
either. When the rebuild after a hand edit makes a check against a standard fail that passed
before, the session asks (a `question` event with the `conflict` and the spot on the model):

- `keep`: keep the value and accept that the standard fails. Later runs are told.
- `adjust`: Claude reworks the rest of the design to meet the standard, keeping the value.
- `revert`: put the value back as it was before the edit.

The value is never reverted unless the user picks `revert`. Nothing here knows a part or a
parameter: any parameter, any standard with a `min` or `max` limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.agent.events import EventSink
from app.agent.services import CheckItem
from app.contracts.common import Location
from app.contracts.session import (
    ConflictConstraint,
    ConstraintConflict,
    QuestionEvent,
    QuestionOption,
)
from app.standards import StandardsFile

KEEP, ADJUST, REVERT = "keep", "adjust", "revert"


@dataclass
class EditDecision:
    """An open decision on a hand edit: the edit, the standards it broke, the message asking."""

    question_id: str
    sink: EventSink  # the open message the question is in
    name: str
    value: Any
    previous: Any  # the value before the edit
    was_locked: bool
    broken: list[CheckItem] = field(default_factory=list)
    before: set[str] = field(default_factory=set)  # the checks failing before the edit


def failing_ids(items: list[CheckItem]) -> set[str]:
    return {i.id for i in items if i.status == "fail"}


def failing_in_report(report: dict) -> set[str]:
    """The failing check IDs of a check report (the `checks.json` format)."""
    return {r["id"] for r in report.get("results", []) if r.get("status") == "fail"}


def broken_standards(before_failing: set[str], items: list[CheckItem]) -> list[CheckItem]:
    """The checks against a standard that fail now and didn't before."""
    return [i for i in items if i.status == "fail" and i.standard and i.id not in before_failing]


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _title(item: CheckItem, standards: StandardsFile) -> str:
    try:
        return f"{standards.get(item.standard).title} ({item.standard})"
    except (KeyError, LookupError):
        return str(item.standard)


def describe(item: CheckItem, standards: StandardsFile) -> str:
    """One broken standard in words: which, measured against required, where."""
    unit = f" {item.unit}" if item.unit else ""
    numbers = ""
    if item.measured is not None and item.required is not None:
        numbers = f": measured {item.measured}{unit}, required {item.required}{unit}"
    where = item.feature or item.component or item.part
    return f"{_title(item, standards)}{numbers}" + (f", on {where}" if where else "")


def decision_text(d: EditDecision, standards: StandardsFile) -> str:
    broken = "; ".join(describe(i, standards) for i in d.broken)
    return (
        f"Your edit sets {d.name} to {d.value}, and the design no longer meets the "
        f"constraint{'s' if len(d.broken) > 1 else ''}: {broken}. Only you can trade a constraint "
        "away. How should I go on?"
    )


def options(d: EditDecision, standards: StandardsFile) -> list[QuestionOption]:
    which = ", ".join(_title(i, standards) for i in d.broken)
    return [
        QuestionOption(
            id=KEEP,
            label="Keep my value",
            description=f"Keep {d.name} at {d.value} and accept that {which} fails",
        ),
        QuestionOption(
            id=ADJUST,
            label="Let Claude adjust the rest",
            description=f"Claude changes other values to meet {which}, keeping {d.name}",
        ),
        QuestionOption(
            id=REVERT,
            label="Revert my edit",
            description=f"Put {d.name} back to {d.previous}",
        ),
    ]


def location(item: CheckItem) -> Location | None:
    if not item.point or len(item.point) != 3:
        return None
    return Location(point=tuple(item.point), part=item.part)  # type: ignore[arg-type]


def conflict(d: EditDecision, standards: StandardsFile) -> ConstraintConflict | None:
    """The edit against the first broken standard, in the conflict format, when both are
    numbers and the standard is a `min` or `max` limit."""
    item = d.broken[0]
    where = location(item)
    value, measured, required = _num(d.value), _num(item.measured), _num(item.required)
    try:
        standard = standards.get(item.standard)
    except (KeyError, LookupError):
        return None
    relation = {"min": ">=", "max": "<="}.get(standard.kind)
    if where is None or None in (value, measured, required) or relation is None:
        return None
    unit = standard.unit if standard.unit in ("mm", "deg") else "mm"
    return ConstraintConflict(
        id=f"edit:{d.name}:{item.standard}",
        message=f"{d.name} = {d.value} breaks {describe(item, standards)}",
        over_by=abs(required - measured),  # type: ignore[operator]
        constraints=[
            ConflictConstraint(
                kind="locked",
                id=d.name,
                label=f"Your value for {d.name}",
                relation="=",
                value=value,  # type: ignore[arg-type]
                unit=unit,
                best=_num(d.previous),
                locations=[where],
            ),
            ConflictConstraint(
                kind="standard",
                id=item.standard,  # type: ignore[arg-type]
                label=standard.title,
                relation=relation,  # type: ignore[arg-type]
                value=required,  # type: ignore[arg-type]
                unit=unit,
                best=measured,
                locations=[where],
            ),
        ],
        location=where,
    )


def ask(d: EditDecision, standards: StandardsFile) -> None:
    """Open the decision's message: what broke, then the question."""
    d.sink.text(decision_text(d, standards))
    d.sink.emit(
        QuestionEvent(
            type="question",
            message_id=d.sink.message_id,
            question_id=d.question_id,
            text=f"Keep {d.name} at {d.value}?",
            options=options(d, standards),
            location=location(d.broken[0]),
            conflict=conflict(d, standards),
        )
    )


def adjust_prompt(d: EditDecision, standards: StandardsFile) -> str:
    """What Claude is asked when the user lets it adjust the rest."""
    broken = "; ".join(describe(i, standards) for i in d.broken)
    return (
        f"I set {d.name} to {d.value} by hand; it is locked. With it the design fails: "
        f"{broken}. Rework the other values so the design meets the constraint, keeping "
        f"{d.name} at {d.value}. If the constraint can't be met without changing my value, ask "
        "me (ask_user) with the trade-offs."
    )


def accepted(d: EditDecision) -> list[dict[str, Any]]:
    """The failures the user accepted by keeping the value, for later runs."""
    at = datetime.now(UTC).isoformat()
    return [
        {"parameter": d.name, "value": d.value, "check": i.id, "standard": i.standard, "at": at}
        for i in d.broken
    ]


def accepted_note(rows: list[dict[str, Any]]) -> str | None:
    """What Claude is told on every later run of the failures the user accepted."""
    if not rows:
        return None
    lines = [
        f"- {r['check']} ({r['standard']}) fails with {r['parameter']} = {r['value']} "
        f"(accepted {r['at']})"
        for r in rows
    ]
    return (
        "The user accepted these failing constraints by keeping their value. Keep their value; "
        "don't trade it or the constraint away to make the check pass unless the user asks:\n"
        + "\n".join(lines)
    )


__all__ = [
    "ADJUST",
    "KEEP",
    "REVERT",
    "EditDecision",
    "accepted",
    "accepted_note",
    "adjust_prompt",
    "ask",
    "broken_standards",
    "failing_ids",
    "failing_in_report",
]
