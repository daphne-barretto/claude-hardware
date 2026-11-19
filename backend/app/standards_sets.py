"""Standards sets per browser: the built-in team set plus the sets each owner makes.

The built-in set (`standards.yaml`) is read-only. A browser (its owner cookie, `app.owners`)
can duplicate any set it can see or start a new one, then rename it, change values, turn rules
on and off, add rules from the catalogue (`app.standards_catalog`) and remove them. Its sets
are saved in the session store (`app.session_store`, the `standards_sets` table) and only that
browser sees them: another owner gets 404, as for sessions.

Every rule is of a catalogue kind, so every rule has a check. Everything is validated before it
is saved (`validate`): the kind, the unit, the value against the range the check accepts, a
spec's text and sizes, `applies_to`, aliases, and duplicates (one rule per kind, an alias in one
rule only, set names unique for the owner). Problems come back together, each naming the rule
and field, so the editor can show them in place.

A set the owner made is used as a `StandardsFile` (`resolve`). Rules it doesn't have keep a
fallback value (the built-in set's, `StandardsFile.defaults`) for the design code that needs a
number; they are never checked or shown as rules.

Only the user changes standards: these functions are called from the API routes the editor
uses, and no agent tool reaches them.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from typing import Any, Protocol

from app.contracts.session import StandardInput, StandardsProblem, StandardsSet
from app.owners import current_owner
from app.standards import Standard, StandardsFile, load_standards
from app.standards_catalog import BY_CHECK, summary

NAME_MAX = 80
TITLE_MAX = 80
DESCRIPTION_MAX = 400
TEXT_MAX = 120
ALIAS_MAX = 40
APPLIES_TO = ("any", "board", "object")


class SetsStore(Protocol):
    def standards_sets(self, owner: str) -> list[dict[str, Any]]: ...

    def save_standards_set(self, owner: str, set_id: str, data: dict[str, Any]) -> None: ...

    def delete_standards_set(self, owner: str, set_id: str) -> bool: ...


class ReadOnlySet(PermissionError):
    """The built-in set can't be changed or deleted, only duplicated."""


class SetProblems(ValueError):
    """The set as sent is not valid; `problems` says each thing wrong."""

    def __init__(self, problems: list[StandardsProblem]):
        self.problems = problems
        super().__init__("; ".join(p.message for p in problems))


def owner_key() -> str:
    """The current browser's owner token; "" outside a request (scripts and tests)."""
    return current_owner.get() or ""


def built_in_sets() -> list[StandardsFile]:
    """The read-only team sets: today the one file (`standards.yaml`)."""
    return [load_standards()]


def built_in() -> StandardsFile:
    return built_in_sets()[0]


def _built_in_ids() -> set[str]:
    return {s.id for s in built_in_sets()}


def is_built_in(set_id: str) -> bool:
    return set_id in _built_in_ids()


def resolve(doc: dict[str, Any]) -> StandardsFile:
    """A saved set as the checks and Claude use it, with the built-in values as fallbacks."""
    base = built_in()
    standards = [Standard.model_validate(s) for s in doc["standards"]]
    have = {s.id for s in standards}
    defaults = [s for s in (*base.standards, *base.defaults) if s.id not in have]
    return StandardsFile.model_validate(
        {"version": 1, "id": doc["id"], "name": doc["name"], "standards": standards}
    ).model_copy(update={"defaults": defaults})


def owned(store: SetsStore, owner: str) -> list[dict[str, Any]]:
    return store.standards_sets(owner)


def list_sets(store: SetsStore, owner: str) -> list[StandardsFile]:
    """The built-in sets, then the owner's, oldest first."""
    return [*built_in_sets(), *(resolve(d) for d in owned(store, owner))]


def _doc(store: SetsStore, owner: str, set_id: str) -> dict[str, Any]:
    for doc in owned(store, owner):
        if doc["id"] == set_id:
            return doc
    raise KeyError(set_id)


def find_set(store: SetsStore, owner: str, set_id: str) -> StandardsFile:
    """The set with this ID the owner can see; KeyError if there is none."""
    for s in built_in_sets():
        if s.id == set_id:
            return s
    return resolve(_doc(store, owner, set_id))


def describe(store: SetsStore, owner: str, standards: StandardsFile) -> StandardsSet:
    """A set as `GET /api/standards` lists it."""
    built = is_built_in(standards.id)
    source = None
    if not built:
        try:
            source = _doc(store, owner, standards.id).get("duplicated_from")
        except KeyError:
            source = None
    return StandardsSet(
        id=standards.id,
        name=standards.name,
        built_in=built,
        duplicated_from=source,
        standards=[summary(s) for s in standards.standards],
    )


# --- Validation --------------------------------------------------------------------------------


def _name(name: str | None, taken: set[str]) -> tuple[str, list[StandardsProblem]]:
    text = " ".join((name or "").split())
    if not text:
        return text, [StandardsProblem(field="name", message="Give the set a name.")]
    if len(text) > NAME_MAX:
        return text, [
            StandardsProblem(field="name", message=f"Keep the name under {NAME_MAX} characters.")
        ]
    if text.casefold() in taken:
        return text, [
            StandardsProblem(field="name", message=f"You already have a set called “{text}”.")
        ]
    return text, []


def _fmt(value: float, unit: str | None) -> str:
    text = f"{value:g}"
    return f"{text}°" if unit == "deg" else f"{text} {unit}" if unit else text


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _in_range(
    label: str, raw: Any, unit: str, low: float, high: float, problem: Callable[[str], None]
) -> float | None:
    value = _number(raw)
    if value is None:
        problem(f"{label} needs a number in {unit}.")
        return None
    if value < low:
        problem(f"{label} can't be {_fmt(value, unit)}: the check accepts {_fmt(low, unit)} up.")
        return None
    if value > high:
        problem(
            f"{label} can't be {_fmt(value, unit)}: the check accepts up to {_fmt(high, unit)}."
        )
        return None
    return value


def validate_rules(rules: list[StandardInput]) -> list[Standard]:
    """The rules as standards, or SetProblems naming everything wrong."""
    problems: list[StandardsProblem] = []
    out: list[Standard] = []
    seen_checks: dict[str, int] = {}
    alias_owner: dict[str, int] = {}
    out_titles: dict[int, str] = {}  # rule index -> its title, for messages about it
    for index, rule in enumerate(rules):

        def problem(message: str, field: str | None = None, index: int = index) -> None:
            problems.append(StandardsProblem(rule=index, field=field, message=message))

        kind = BY_CHECK.get(rule.check)
        if kind is None:
            problem(
                f"“{rule.check}” is not a kind of check the app can enforce. Rules are made from "
                "the catalogue; a new kind of check needs code.",
                "check",
            )
            continue
        title = " ".join((rule.title or kind.title).split())
        name = title or kind.title
        if rule.check in seen_checks:
            problem(
                f"{name}: this set already has a {kind.title.lower()} rule. A set has one rule "
                "of each kind; change that one instead.",
                "check",
            )
            continue
        seen_checks[rule.check] = index
        before = len(problems)
        if not title:
            problem("Give the rule a title.", "title")
        elif len(title) > TITLE_MAX:
            problem(f"{name}: keep the title under {TITLE_MAX} characters.", "title")
        if rule.unit not in (None, "", kind.unit):
            expected = kind.unit or "no unit (it is text)"
            problem(f"{name} is measured in {expected}, not {rule.unit}.", "unit")
        value: float | str | None = None
        if kind.kind == "spec":
            text = " ".join(str(rule.value or "").split())
            if not text:
                problem(f"{name} needs a value: {kind.value_label.lower()}.", "value")
            elif len(text) > TEXT_MAX:
                problem(f"{name}: keep the value under {TEXT_MAX} characters.", "value")
            value = text
        else:
            assert kind.unit is not None and kind.min is not None and kind.max is not None
            value = _in_range(
                name,
                rule.value,
                kind.unit,
                kind.min,
                kind.max,
                lambda m: problem(m, "value"),
            )
        dimensions: dict[str, float] | None = None
        if kind.dimensions:
            dimensions = {}
            given = dict(rule.dimensions or {})
            for dim in kind.dimensions:
                if given.get(dim.name) in (None, ""):
                    problem(f"{name} needs the {dim.label.lower()} in {dim.unit}.", dim.name)
                    continue
                size = _in_range(
                    f"{name}: the {dim.label.lower()}",
                    given.pop(dim.name),
                    dim.unit,
                    dim.min,
                    dim.max,
                    lambda m, f=dim.name: problem(m, f),
                )
                if size is not None:
                    dimensions[dim.name] = size
            for extra in given:
                problem(f"{name} has no size called “{extra}”.", "dimensions")
        elif rule.dimensions:
            problem(f"{name} takes no sizes.", "dimensions")
        material = " ".join((rule.material or "").split()) or None
        if material is not None and not kind.material:
            problem(f"{name} names no material.", "material")
        elif material is not None and len(material) > TEXT_MAX:
            problem(f"{name}: keep the material under {TEXT_MAX} characters.", "material")
        applies_to = rule.applies_to or kind.applies_to
        if applies_to not in APPLIES_TO:
            problem(
                f"{name}: “{applies_to}” is not a kind of design. Use any, board or object.",
                "applies_to",
            )
        aliases: list[str] = []
        for raw in rule.aliases:
            alias = " ".join(raw.split())
            if not alias:
                continue
            key = alias.casefold()
            if len(alias) > ALIAS_MAX:
                problem(f"{name}: keep each word under {ALIAS_MAX} characters.", "aliases")
            elif key in (a.casefold() for a in aliases):
                problem(f"{name}: “{alias}” is listed twice.", "aliases")
            elif key in alias_owner and alias_owner[key] != index:
                other = out_titles.get(alias_owner[key], "another rule")
                problem(f"{name}: “{alias}” already names {other}.", "aliases")
            else:
                aliases.append(alias)
                alias_owner[key] = index
        description = " ".join((rule.description or "").split())
        if len(description) > DESCRIPTION_MAX:
            problem(f"{name}: keep the description under {DESCRIPTION_MAX} characters.")
        if len(problems) > before:
            out_titles[index] = name
            continue
        draft = {
            "id": kind.standard_id,
            "title": title,
            "description": description or "-",
            "kind": kind.kind,
            "value": value,
            "unit": kind.unit,
            "check": kind.check,
            "applies_to": applies_to,
            "aliases": aliases,
            "enabled": rule.enabled,
            "dimensions": dimensions,
            "material": material,
        }
        standard = Standard.model_validate(draft)
        if not description:  # say the rule in plain words
            from app.standards_catalog import plain

            standard = standard.model_copy(update={"description": plain(standard) + "."})
        out.append(standard)
        out_titles[index] = name
    if problems:
        raise SetProblems(problems)
    return out


# --- Changes -----------------------------------------------------------------------------------


def _taken_names(store: SetsStore, owner: str, but: str | None = None) -> set[str]:
    names = {s.name.casefold() for s in built_in_sets()}
    names |= {d["name"].casefold() for d in owned(store, owner) if d["id"] != but}
    return names


def _new_id(store: SetsStore, owner: str) -> str:
    taken = _built_in_ids() | {d["id"] for d in owned(store, owner)}
    while True:
        set_id = f"set_{secrets.token_hex(8)}"
        if set_id not in taken:
            return set_id


def _dump(standards: list[Standard]) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json", exclude_none=True) for s in standards]


def create(
    store: SetsStore, owner: str, name: str, duplicate_of: str | None = None
) -> StandardsFile:
    """A new set: empty, or a copy of a set the owner can see (KeyError if there's none)."""
    rules: list[Standard] = []
    if duplicate_of is not None:
        rules = list(find_set(store, owner, duplicate_of).standards)
    text, problems = _name(name, _taken_names(store, owner))
    if problems:
        raise SetProblems(problems)
    doc = {
        "id": _new_id(store, owner),
        "name": text,
        "duplicated_from": duplicate_of,
        "standards": _dump(rules),
    }
    store.save_standards_set(owner, doc["id"], doc)
    return resolve(doc)


def update(
    store: SetsStore,
    owner: str,
    set_id: str,
    name: str | None = None,
    rules: list[StandardInput] | None = None,
) -> StandardsFile:
    """Rename a set and/or replace its rules. ReadOnlySet for the built-in set, KeyError for a
    set the owner can't see, SetProblems for anything invalid (nothing is saved then)."""
    if is_built_in(set_id):
        raise ReadOnlySet(set_id)
    doc = dict(_doc(store, owner, set_id))
    problems: list[StandardsProblem] = []
    if name is not None:
        text, problems = _name(name, _taken_names(store, owner, but=set_id))
        doc["name"] = text
    if rules is not None:
        try:
            doc["standards"] = _dump(validate_rules(rules))
        except SetProblems as error:
            problems += error.problems
    if problems:
        raise SetProblems(problems)
    store.save_standards_set(owner, set_id, doc)
    return resolve(doc)


def delete(store: SetsStore, owner: str, set_id: str) -> None:
    if is_built_in(set_id):
        raise ReadOnlySet(set_id)
    if not store.delete_standards_set(owner, set_id):
        raise KeyError(set_id)


def choose(
    store: SetsStore, owner: str, current: str | None, requested: str | None, first: bool
) -> StandardsFile | None:
    """The set a message chooses for its session, or None if it chooses nothing new.

    A message chooses a set with the session's first message (`first`); later messages may
    repeat the session's set but not change it (a session changes sets with
    `PUT .../standards`, which rechecks). Raises KeyError for an unknown set and ValueError
    for a change after the first message.
    """
    if requested is None or requested == current:
        return None
    if not first:
        raise ValueError(
            "the constraint set is chosen with the session's first message; "
            "change it with PUT /api/sessions/{id}/standards"
        )
    return find_set(store, owner, requested)


__all__ = [
    "ReadOnlySet",
    "SetProblems",
    "SetsStore",
    "built_in",
    "built_in_sets",
    "choose",
    "create",
    "delete",
    "describe",
    "find_set",
    "is_built_in",
    "list_sets",
    "owner_key",
    "resolve",
    "update",
    "validate_rules",
]
