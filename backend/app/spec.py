"""The design's spec: the constraints shown as chips above the chat input.

The user describes the part or change in plain language. `extract()` finds the team
standards the description names, by each standard's `aliases` in `standards.yaml`, and the
value the user gives for one ("2.5 mm walls", "no taller than 14 mm"). `Spec` keeps the
constraints of one session:

- Every standard is a constraint, so each one has a check that enforces it. Its value is the
  standard's own (`source: standard`) until the user's words give one (`user`, with the
  words in `quote`) or the chip is edited (`edit`).
- Removing a chip drops the user's value: the team standard applies again. A team standard
  itself can't be removed, only edited, because every standard must stay checked.
- `effective()` is the standards with the spec's values, which the checks and Claude use.

The chips appear once the user has described something (`active`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.contracts.session import Constraint, ConstraintSource
from app.standards import Standard, StandardsFile

UNITS = {
    "mm": r"mm|millimet(?:er|re)s?",
    "deg": r"°|deg(?:ree)?s?",
}
OPERATORS = {"min": "≥", "max": "≤", "tolerance": "±"}
# A clause ends at punctuation (but not a decimal point) or at "and".
_CLAUSES = re.compile(r"[;,\n!?]|\.(?!\d)|\band\b", re.IGNORECASE)
_NUMBER = r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:{unit})(?![A-Za-z])"


@dataclass(frozen=True)
class Mention:
    """A standard the user's words name, with the value they gave, if any."""

    standard: str
    value: float | None
    quote: str


def _alias_at(alias: str, clause: str) -> int | None:
    match = re.search(r"(?<![A-Za-z])" + re.escape(alias), clause, re.IGNORECASE)
    return match.start() if match else None


def extract(text: str, standards: StandardsFile) -> list[Mention]:
    """The standards a description names, in the order they appear, with the values given."""
    mentions: dict[str, tuple[int, Mention]] = {}
    offset = 0
    for clause in _CLAUSES.split(text):
        offset += 1
        quote = " ".join(clause.split())
        if not quote:
            continue
        found = []
        for s in standards.enabled:  # a rule turned off is not a constraint
            hits = [p for p in (_alias_at(a, clause) for a in s.aliases) if p is not None]
            if hits:
                found.append((min(hits), s))
        claimed: set[int] = set()
        for at, s in sorted(found, key=lambda f: f[0]):
            value = None
            if s.unit is not None:
                numbers = [
                    m
                    for m in re.finditer(_NUMBER.format(unit=UNITS[s.unit]), clause, re.I)
                    if m.start() not in claimed
                ]
                if numbers:
                    near = min(numbers, key=lambda m: abs(m.start() - at))
                    claimed.add(near.start())
                    value = float(near.group(1))
            previous = mentions.get(s.id)
            # A later value wins; a later bare mention doesn't undo an earlier value.
            if previous is None or value is not None:
                mentions[s.id] = (
                    previous[0] if previous else offset * 10_000 + at,
                    Mention(s.id, value, quote),
                )
    return [m for _, m in sorted(mentions.values(), key=lambda t: t[0])]


# Words that make a clause a rule ("every wall at least 3 mm", "no taller than 14 mm") ...
_RULE = re.compile(
    r"\b(?:at least|at most|minimum|maximum|min|max|no (?:more|less|\w+er) than|"
    r"not (?:more|less|\w+er) than|under|below|over|above|within|every|all|any)\b|[≥≤]|>=|<=",
    re.IGNORECASE,
)
# ... and words that make it a design value: locked, kept or set (a hand edit put in words).
_DESIGN_VALUE = re.compile(
    r"\b(?:lock(?:s|ed|ing)?|unlock(?:s|ed|ing)?|pin(?:s|ned)?|keep(?:s|ing)?|kept|"
    r"set(?:s|ting)?|hold(?:s|ing)?|held|fix(?:es|ed)?)\b",
    re.IGNORECASE,
)


def _words(path: str) -> set[str]:
    """The words of a parameter path's last part: `boss.insert_diameter` -> insert, diameter."""
    return {w for w in re.split(r"[_\W\d]+", path.rsplit(".", 1)[-1].lower()) if w}


def _names_locked_value(clause: str, value: float, locked: dict[str, Any]) -> bool:
    """Whether the clause gives a hand-locked parameter's value and names that parameter."""
    words = {w.rstrip("s") for w in re.findall(r"[a-z]+", clause.lower())}
    for path, held in locked.items():
        if isinstance(held, bool) or not isinstance(held, int | float):
            continue
        if abs(float(held) - value) <= 1e-9 and {w.rstrip("s") for w in _words(path)} & words:
            return True
    return False


def states_a_rule(quote: str, value: float, locked: dict[str, Any] | None = None) -> bool:
    """Whether a value in the user's words is a rule for the design (a standard's new value)
    rather than a design value: one to lock, keep or set, or a value already locked by hand.
    Rule words ("at least", "every", "no taller than") always make it a rule."""
    if _RULE.search(quote):
        return True
    if _DESIGN_VALUE.search(quote):
        return False
    return not _names_locked_value(quote, value, locked or {})


def _number(value: float | str) -> str:
    return f"{float(value):g}"


def label(standard: Standard, value: float | str) -> str:
    """The chip text: "Minimum wall thickness ≥ 2.5 mm", "Molded ABS"."""
    if standard.kind == "spec":
        text = str(value).strip()
        return text[:1].upper() + text[1:]
    unit = "°" if standard.unit == "deg" else f" {standard.unit}"
    op = OPERATORS[standard.kind]
    sep = "" if standard.kind == "tolerance" else " "
    return f"{standard.title} {op}{sep}{_number(value)}{unit}"


@dataclass
class Override:
    value: float | str
    source: ConstraintSource
    quote: str | None = None
    message_id: str | None = None


class SpecError(ValueError):
    """A constraint edit the spec refuses."""


class Spec:
    def __init__(self, standards: StandardsFile):
        self.standards = standards
        self.overrides: dict[str, Override] = {}
        self.active = False
        # Whether the design has a board: the chips list only the standards that apply to it
        # (`Standard.applies`, as the check report does), and those the user gave in words.
        self.has_board: Callable[[], bool] = lambda: True
        self.has_object: Callable[[], bool] = lambda: True  # fits a photographed object

    def describe(
        self, text: str, message_id: str | None = None, locked: dict[str, Any] | None = None
    ) -> bool:
        """Take the constraints from a message. True if the chips changed.

        Only a rule changes a standard: a value the message gives for the design (to lock,
        keep or set, or one of the `locked` parameter values, by path) is the design's and
        leaves the standard as it is (`states_a_rule`)."""
        changed = not self.active
        self.active = True
        for m in extract(text, self.standards):
            if m.value is None or not states_a_rule(m.quote, m.value, locked):
                continue
            new = Override(m.value, "user", m.quote, message_id)
            if self.overrides.get(m.standard) != new:
                changed = True
                self.overrides[m.standard] = new
        return changed

    def constraint(self, standard_id: str) -> Constraint:
        s = self.standards.get(standard_id)
        o = self.overrides.get(standard_id)
        value = o.value if o else s.value
        return Constraint(
            id=s.id,
            title=s.title,
            label=label(s, value),
            kind=s.kind,
            value=value,
            unit=s.unit,
            check=s.check,
            source=o.source if o else "standard",
            quote=o.quote if o else None,
            message_id=o.message_id if o else None,
            standard_value=s.value,
        )

    def constraints(self) -> list[Constraint]:
        if not self.active:
            return []
        board, fits = self.has_board(), self.has_object()
        return [
            self.constraint(s.id)
            for s in self.standards.enabled
            if s.applies(has_board=board, has_object=fits) or s.id in self.overrides
        ]

    def rebased(self, standards: StandardsFile) -> Spec:
        """The spec on another standards set (the user chose or edited the session's set):
        the user's own values stay for the rules the new set has on; the rest go."""
        spec = Spec(standards)
        spec.active = self.active
        on = {s.id for s in standards.enabled}
        spec.overrides = {k: o for k, o in self.overrides.items() if k in on}
        return spec

    def _standard(self, standard_id: str) -> Standard:
        if not any(s.id == standard_id for s in self.standards.enabled):
            raise KeyError(f"no constraint {standard_id}")
        return self.standards.get(standard_id)

    def edit(self, standard_id: str, value: float | str) -> Constraint:
        """Set a constraint's value by hand (a chip edit)."""
        s = self._standard(standard_id)
        if s.kind != "spec":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise SpecError(f"{s.title} needs a number in {s.unit}") from None
            if value < 0 or (value == 0 and s.kind != "min"):
                raise SpecError(f"{s.title} can't be {_number(value)} {s.unit}")
        else:
            value = str(value).strip()
        try:
            Standard.model_validate({**s.model_dump(), "value": value})
        except ValidationError as error:
            raise SpecError(error.errors()[0]["msg"]) from None
        self.active = True
        if value == s.value:
            self.overrides.pop(standard_id, None)
        else:
            self.overrides[standard_id] = Override(value, "edit")
        return self.constraint(standard_id)

    def remove(self, standard_id: str) -> Constraint:
        """Drop the user's value: the team standard applies again."""
        s = self._standard(standard_id)
        if standard_id not in self.overrides:
            raise SpecError(
                f"{s.title} is a constraint in the set (standards.yaml): "
                "it can be edited, not removed"
            )
        del self.overrides[standard_id]
        return self.constraint(standard_id)

    def effective(self) -> StandardsFile:
        """The standards with the spec's values: what the checks and Claude go by."""
        if not self.overrides:
            return self.standards
        return self.standards.model_copy(
            update={
                "standards": [
                    s.model_copy(update={"value": self.overrides[s.id].value})
                    if s.id in self.overrides
                    else s
                    for s in self.standards.standards
                ]
            }
        )


__all__ = ["Mention", "Spec", "SpecError", "extract", "label"]
