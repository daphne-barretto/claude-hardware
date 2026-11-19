"""What changed when a session's standards set changed: for the chat notice and for Claude.

`rule_changes()` compares two sets rule by rule (added, removed, turned on or off, new value);
`change_notice()` adds what the recheck found (newly failing, newly passing, no longer checked,
newly checked). Both are plain text and general: they name rules by their titles and values,
whatever the set holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.standards import Standard, StandardsFile
from app.standards_catalog import value_text

CHECKED = ("pass", "fail")


def _rule(s: Standard) -> str:
    return f"{s.title} ({value_text(s)})"


def rule_changes(old: StandardsFile, new: StandardsFile) -> list[str]:
    """Each difference between the rules of `old` and `new`, in words."""
    before = {s.check: s for s in old.standards}
    after = {s.check: s for s in new.standards}
    out = []
    for check, s in after.items():
        was = before.get(check)
        if was is None:
            out.append(f"added {_rule(s)}" + ("" if s.enabled else ", turned off"))
            continue
        if was.enabled != s.enabled:
            out.append(f"turned {'on' if s.enabled else 'off'} {s.title}")
        if was.value != s.value or was.unit != s.unit:
            out.append(f"{s.title} {value_text(was)} → {value_text(s)}")
        elif (was.dimensions or {}) != (s.dimensions or {}):
            out.append(f"{s.title}: new sizes")
        if was.applies_to != s.applies_to:
            out.append(f"{s.title} now applies to {_designs(s.applies_to)}")
    for check, was in before.items():
        if check not in after:
            out.append(f"removed {_rule(was)}")
    return out


def _designs(applies_to: str) -> str:
    return {
        "any": "every design",
        "board": "designs built around a board",
        "object": "designs made to fit an object",
    }.get(applies_to, applies_to)


def _title(item: Any) -> str:
    title = getattr(item, "title", None)
    if title:
        return title
    summary = getattr(item, "summary", "") or ""
    return summary.split(":", 1)[0] if ":" in summary else item.id


def _status(item: Any | None) -> str | None:
    return None if item is None else item.status


def check_changes(before: Sequence[Any] | None, after: Sequence[Any]) -> dict[str, list[Any]]:
    """The results whose verdict changed: `failing` (now fail, wasn't), `passing` (was fail,
    now pass), `checked` (now pass, wasn't checked) and `unchecked` (was checked, isn't)."""
    old = {i.id: i for i in before or []}
    new = {i.id: i for i in after}
    out: dict[str, list[Any]] = {"failing": [], "passing": [], "checked": [], "unchecked": []}
    for rid, item in new.items():
        was = _status(old.get(rid))
        if item.status == "fail" and was != "fail":
            out["failing"].append(item)
        elif item.status == "pass" and was == "fail":
            out["passing"].append(item)
        elif item.status == "pass" and was not in CHECKED and before is not None:
            out["checked"].append(item)
        elif item.status not in CHECKED and was in CHECKED:
            out["unchecked"].append(item)
    for rid, was_item in old.items():
        if rid not in new and was_item.status in CHECKED:
            out["unchecked"].append(was_item)
    return out


def _failing(item: Any) -> str:
    summary = getattr(item, "summary", "") or ""
    return summary.rstrip(".") if summary else _title(item)


def change_notice(
    old: StandardsFile,
    new: StandardsFile,
    before: Sequence[Any] | None,
    after: Sequence[Any] | None,
) -> str:
    """The chat notice after the user chose or edited the session's standards set."""
    same_set = old.id == new.id
    head = (
        f"You updated the constraint set “{new.name}”."
        if same_set
        else f"You switched the constraint set to “{new.name}”."
    )
    lines = [head]
    changes = rule_changes(old, new)
    if changes:
        lines.append("Rule changes: " + "; ".join(changes) + ".")
    elif same_set:
        lines.append("No rule changed.")
    if after is None:
        lines.append(
            "Nothing is built yet: the checks run against these constraints on the first build."
        )
    else:
        found = check_changes(before, after)
        if found["failing"]:
            lines.append("Now failing:\n" + "\n".join(f"- {_failing(i)}" for i in found["failing"]))
        if found["passing"]:
            lines.append("Now passing:\n" + "\n".join(f"- {_title(i)}" for i in found["passing"]))
        if found["checked"]:
            lines.append(
                "Newly checked (pass):\n" + "\n".join(f"- {_title(i)}" for i in found["checked"])
            )
        if found["unchecked"]:
            lines.append(
                "No longer checked:\n" + "\n".join(f"- {_title(i)}" for i in found["unchecked"])
            )
        if not any(found.values()):
            lines.append("Rechecked the current design: no check changed its verdict.")
        counts = {s: sum(i.status == s for i in after) for s in ("pass", "fail", "untested")}
        lines.append(
            f"Now {counts['pass']} pass, {counts['fail']} fail, {counts['untested']} untested."
        )
    lines.append("Claude designs to these constraints from your next message.")
    return "\n\n".join(lines)


def claude_note(old: StandardsFile, new: StandardsFile) -> str:
    """What Claude is told with the next message after the user changed the standards."""
    changes = rule_changes(old, new)
    what = f" ({'; '.join(changes)})" if changes else ""
    return (
        f"The user changed the constraint set to {new.name} ({new.id}){what}. Design and check "
        "against these from now on (read_spec lists them). Only the user changes constraints: "
        "if one gets in the way, ask the user (ask_user) rather than working around it."
    )


__all__ = ["change_notice", "check_changes", "claude_note", "rule_changes"]
