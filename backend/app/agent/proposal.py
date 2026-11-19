"""Proposed changes: a finished agent run as a change set the person reviews.

The run starts from one design (`Workspace.base`) and ends at another. `propose()` splits the
difference into changes and proves each one with the checks:

- **One change per thing.** An element of a parameter list that has an `id` or `tag` (an
  opening, a boss, a slot) is one change: added, removed or modified. Any other top-level
  parameter that differs is one change, and so is the generator code.
- **Description and reason.** The title says what moved, turned, appeared or changed. The
  reason is the board difference it answers (`demos/<board>/CHANGES.md`, by component), the
  checks it fixes, and otherwise the fix rounds that made it.
- **Proof.** The design the run started from is built and checked, then that design with
  each change alone. A check that fails before and passes with the change alone is one the
  change `fixes`; the reverse `breaks`; a check on the same component or feature is
  `related`. `after` is the check on the build of the accepted changes.
- **Review.** `Proposal.review(accept=..., reject=...)` sets the changes' status, then puts
  the design together from the starting design plus only the accepted changes, rebuilds it
  and reruns every check. In `manual` mode the changes start `proposed` and nothing is applied
  until accepted; in `auto` mode they start `accepted` (the run's design stands) and each stays
  reviewable: rejecting one rebuilds without it.

**Locks.** A locked value the run changed with the user's approval belongs to the change
that contains it: accepting or rejecting that change moves the value between the record the
run left and the record from before the run (the person's own hand edit), and the store holds
that record again as it was, so no approval is made up. Manual mode starts from the record
from before the run. Any other locked value, including a hand edit made after the run, stays
as locked: the review lays it over the combined design. If the combined design has no place
for it (rejecting the addition of an opening whose position was locked since), the review is
refused with an error that names the lock, and nothing changes.

`review()` streams the rebuild on `ws.sink` (a `build_ready`), so the caller opens a message
for it first when the run's message has ended.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.design import CODE, Params, PathError, design_key, get_path, has_path, same, set_path
from app.agent.services import BuildOutcome, CheckItem, summary
from app.agent.tools import Workspace
from app.boards.changes import parse_changes_md
from app.contracts.changes import (
    ChangeSet,
    CheckLink,
    FieldDelta,
    GeometryDelta,
    ProposedChange,
)
from app.contracts.checks import CheckSummary
from app.contracts.session import Build, Mode
from app.parameters import LockedParameterError, Parameter

KEYS = ("id", "tag")  # how list elements are matched, as in parameter paths


# --- Diff -------------------------------------------------------------------------------------


@dataclass
class Unit:
    """One change: a list element (`list_key`, `key`), a top-level parameter, or the code."""

    path: str
    kind: str  # added, removed, modified
    before: Any
    after: Any
    list_key: str | None = None  # the list the element is in
    key: str | None = None  # the element's id or tag

    @property
    def element(self) -> dict:
        return self.after if self.after is not None else self.before


def _element_key(item: Any) -> str | None:
    if isinstance(item, dict):
        for name in KEYS:
            if item.get(name) is not None:
                return str(item[name])
    return None


def _keyed(items: Any) -> dict[str, dict] | None:
    """A list's elements by id (or tag), or None if the list isn't keyed that way."""
    if not isinstance(items, list) or not items:
        return None
    out = {}
    for item in items:
        key = _element_key(item)
        if key is None or key in out:
            return None
        out[key] = item
    return out


def diff(base: dict, final: dict) -> list[Unit]:
    """The changes from one design snapshot (`code`, `params`) to another."""
    units: list[Unit] = []
    b, f = base["params"], final["params"]
    for name in [*f, *(k for k in b if k not in f)]:
        old, new = b.get(name), f.get(name)
        if name in b and name in f and same(old, new):
            continue
        old_keyed, new_keyed = _keyed(old), _keyed(new)
        if old_keyed is not None and new_keyed is not None:
            for key in [*new_keyed, *(k for k in old_keyed if k not in new_keyed)]:
                o, n = old_keyed.get(key), new_keyed.get(key)
                if o is not None and n is not None and same(o, n):
                    continue
                kind = "added" if o is None else "removed" if n is None else "modified"
                units.append(Unit(f"{name}.{key}", kind, o, n, list_key=name, key=key))
            continue
        kind = "added" if name not in b else "removed" if name not in f else "modified"
        units.append(Unit(name, kind, old, new))
    if base["code"] != final["code"]:
        units.append(Unit(CODE, "modified", base["code"], final["code"]))
    return units


def combine(base: dict, final: dict, units: list[Unit], accepted: set[str]) -> dict:
    """The starting design plus only the accepted changes."""
    if not accepted:
        return copy.deepcopy(base)
    if accepted >= {u.path for u in units}:
        return copy.deepcopy(final)
    params: Params = copy.deepcopy(base["params"])
    by_list: dict[str, list[Unit]] = {}
    for unit in units:
        if unit.path not in accepted or unit.path == CODE:
            continue
        if unit.list_key is not None:
            by_list.setdefault(unit.list_key, []).append(unit)
        elif unit.kind == "removed":
            params.pop(unit.path, None)
        else:
            params[unit.path] = copy.deepcopy(unit.after)
    for name, changed in by_list.items():
        units_by_key = {u.key: u for u in changed}
        merged = []
        for item in params[name]:
            unit = units_by_key.get(_element_key(item))
            if unit is None:
                merged.append(item)
            elif unit.kind == "modified":
                merged.append(copy.deepcopy(unit.after))
            # an accepted removal drops it
        final_list = final["params"][name]
        for unit in changed:
            if unit.kind == "added":
                index = next(
                    i for i, item in enumerate(final_list) if _element_key(item) == unit.key
                )
                merged.insert(min(index, len(merged)), copy.deepcopy(unit.after))
        params[name] = merged
    code = final["code"] if CODE in accepted else base["code"]
    return {**copy.deepcopy(base), "params": params, "code": code}


# --- Description ------------------------------------------------------------------------------


def _fields(before: Any, after: Any, prefix: str = "") -> list[FieldDelta]:
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for name in [*after, *(k for k in before if k not in after)]:
            if not same(before.get(name), after.get(name)):
                out.extend(_fields(before.get(name), after.get(name), f"{prefix}{name}."))
        return out
    return [FieldDelta(field=prefix.rstrip(".") or "value", before=before, after=after)]


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}" if abs(value) >= 1e-3 else "0"
    if isinstance(value, str) and len(value) > 40:
        return value[:37] + "..."
    if isinstance(value, dict | list):
        return f"{len(value)} values" if isinstance(value, dict) else f"{len(value)} items"
    return str(value)


def _turn(a: float, b: float) -> float:
    return (b - a + 180.0) % 360.0 - 180.0


def delta(unit: Unit) -> GeometryDelta:
    element = unit.element if isinstance(unit.element, dict) else {}
    out = GeometryDelta(
        kind=unit.kind,  # type: ignore[arg-type]
        path=unit.path,
        feature=element.get("tag") if isinstance(element.get("tag"), str) else None,
        component=element.get("component") if isinstance(element.get("component"), str) else None,
    )
    if unit.path == CODE:
        return out
    if unit.kind == "modified":
        out.fields = _fields(unit.before, unit.after)
        b, a = unit.before, unit.after
        if isinstance(b, dict) and isinstance(a, dict):
            xy = [(_num(b.get(k)), _num(a.get(k))) for k in ("x", "y")]
            if all(o is not None and n is not None for o, n in xy) and any(
                abs(n - o) > 1e-9  # type: ignore[operator]
                for o, n in xy
            ):
                out.move = (round(xy[0][1] - xy[0][0], 4), round(xy[1][1] - xy[1][0], 4))  # type: ignore[operator]
            ro, rn = _num(b.get("rotation")), _num(a.get("rotation"))
            if ro is not None and rn is not None and abs(_turn(ro, rn)) > 1e-9:
                out.rotate = round(_turn(ro, rn), 4)
    return out


def _name(unit: Unit, d: GeometryDelta) -> str:
    if unit.path == CODE:
        return "the generator code"
    name = d.feature or unit.path
    element = unit.element if isinstance(unit.element, dict) else {}
    if d.component and element.get("kind"):
        return f"{name} ({element['kind']} for {d.component})"
    return name


def title(unit: Unit, d: GeometryDelta) -> str:
    name = _name(unit, d)
    if unit.kind == "added":
        return f"Add {name}"
    if unit.kind == "removed":
        return f"Remove {name}"
    if unit.path == CODE:
        return "Rewrite the generator code"
    parts = []
    if d.move is not None:
        parts.append(f"move {name} by ({_fmt(d.move[0])}, {_fmt(d.move[1])}) mm")
    if d.rotate is not None:
        parts.append(f"turn it {_fmt(d.rotate)}°" if parts else f"turn {name} {_fmt(d.rotate)}°")
    if parts:
        text = " and ".join(parts)
        return text[0].upper() + text[1:]
    if len(d.fields) == 1:
        (only,) = d.fields
        field_name = name if only.field == "value" else f"{name}.{only.field}"
        return f"Change {field_name} from {_fmt(only.before)} to {_fmt(only.after)}"
    return f"Change {name} ({len(d.fields)} values)"


def board_changes(text: str | None) -> dict[str, dict[str, str]]:
    """The rows of a board changes file (`demos/<board>/CHANGES.md` format) by component; none
    for a session without board changes."""
    if not text:
        return {}
    rows = parse_changes_md(text).rows
    return {row.get("Component", "").strip("` "): row for row in rows}


def check_name(link: CheckLink) -> str:
    """A check as people read it: its title on the checks strip, never its ID (QA fix brief 2),
    with the subject of a check that runs once per subject: "Opening alignment (Q4 sensor
    window)". Without a title, the check's name in words."""
    name = link.title or link.check.replace("_", " ").strip().capitalize()
    subject = link.id.removeprefix(f"{link.check}:") if link.id != link.check else ""
    return f"{name} ({re.sub(r'[_-]+', ' ', subject).strip()})" if subject else name


def reason(d: GeometryDelta, links: list[CheckLink], rounds: list[int], board: dict) -> str:
    parts = []
    row = board.get(d.component or "")
    if row:
        what = row.get("What it breaks", "").strip()
        change = row.get("Change", "").strip()
        parts.append(f"Board change: {d.component} {change}. {what}".strip())
    fixes = [check_name(link) for link in links if link.effect == "fixes"]
    if fixes:
        parts.append(f"Fixes {', '.join(fixes)}.")
    breaks = [check_name(link) for link in links if link.effect == "breaks"]
    if breaks:
        parts.append(f"On its own it fails {', '.join(breaks)}.")
    if not parts:
        made = ", ".join(str(r) for r in rounds) or "the run"
        parts.append(
            f"Changed by Claude in fix round {made}; no check depends on it alone."
            if rounds
            else "Changed by Claude; no check depends on it alone."
        )
    return " ".join(parts)


# --- Proof ------------------------------------------------------------------------------------


def links(
    unit: Unit,
    d: GeometryDelta,
    before: dict[str, CheckItem],
    alone: dict[str, CheckItem] | None,
    after: dict[str, CheckItem] | None,
) -> list[CheckLink]:
    out = []
    for check_id in [*before, *(k for k in (alone or {}) if k not in before)]:
        b, a = before.get(check_id), (alone or {}).get(check_id)
        item = b or a
        assert item is not None
        if b is not None and a is not None and b.status == "fail" and a.status == "pass":
            effect = "fixes"
        elif b is not None and a is not None and b.status == "pass" and a.status == "fail":
            effect = "breaks"
        elif (d.component and item.component == d.component) or (
            d.feature and item.feature == d.feature
        ):
            effect = "related"
        else:
            continue
        now = (after or {}).get(check_id)
        out.append(
            CheckLink(
                id=check_id,
                check=item.check,
                title=item.title,
                standard=item.standard,
                effect=effect,  # type: ignore[arg-type]
                before=b.status if b else None,  # type: ignore[arg-type]
                alone=a.status if a else None,  # type: ignore[arg-type]
                after=now.status if now else None,  # type: ignore[arg-type]
            )
        )
    order = {"fixes": 0, "breaks": 1, "related": 2}
    return sorted(out, key=lambda link: order[link.effect])


def _by_id(items: list[CheckItem] | None) -> dict[str, CheckItem] | None:
    return None if items is None else {i.id: i for i in items}


# --- The proposal -----------------------------------------------------------------------------


@dataclass
class Proposal:
    ws: Workspace
    mode: Mode
    base: dict
    final: dict
    units: list[Unit]
    base_build: BuildOutcome
    base_checks: list[CheckItem]
    alone: dict[str, list[CheckItem] | None]  # checks of base + each change alone
    rounds: dict[str, list[int]]
    status: dict[str, str] = field(default_factory=dict)
    # Locked values the run changed with approval: the store record from before the run and
    # the one the run left, by path.
    records: dict[str, tuple[Parameter, Parameter]] = field(default_factory=dict)
    board: dict[str, dict[str, str]] = field(default_factory=dict)  # board changes by component
    # Accepted risk fixes (`app.agent.risks`), laid over the accepted changes: parameter edits
    # by path, and `code`.
    overlays: list[dict[str, Any]] = field(default_factory=list)
    # The comment each change answers, by change ID.
    comments: dict[str, str] = field(default_factory=dict)

    @property
    def accepted(self) -> set[str]:
        return {path for path, s in self.status.items() if s == "accepted"}

    def design_for(self, accepted: Iterable[str]) -> dict:
        from app.agent.risks import with_edits

        design = combine(self.base, self.final, self.units, set(accepted))
        for edits in self.overlays:
            design = with_edits(design, edits)
        return design

    async def review(self, accept: Iterable[str] = (), reject: Iterable[str] = ()) -> ChangeSet:
        """Accept and reject changes by ID, then rebuild and recheck with only the accepted
        ones. Returns the updated change set."""
        accept, reject = list(accept), list(reject)
        unknown = [c for c in [*accept, *reject] if c not in self.status]
        if unknown:
            raise KeyError(f"no change {', '.join(unknown)}")
        both = set(accept) & set(reject)
        if both:
            raise ValueError(f"both accepted and rejected: {', '.join(sorted(both))}")
        previous = dict(self.status)
        for path in accept:
            self.status[path] = "accepted"
        for path in reject:
            self.status[path] = "rejected"
        try:
            await self._apply({*accept, *reject})
        except LockedParameterError:
            self.status = previous
            raise
        return self.change_set()

    def _owned(self, path: str, decided: set[str] | None) -> tuple[Parameter, Parameter] | None:
        """The run's records of a locked value, if the review may move it: the run changed it
        with approval, nobody has set it since, and the review decides the change holding it
        (`decided` None: the proposal is applying its starting state)."""
        records = self.records.get(path)
        if records is None or self.ws.design.store.get(path) not in records:
            return None
        if decided is not None and not any(_covers(unit, path) for unit in decided):
            return None
        return records

    async def _apply(self, decided: set[str] | None = None) -> None:
        ws = self.ws
        target = self.design_for(self.accepted)
        params = target["params"]
        store = ws.design.store
        restores: list[tuple[Parameter, Parameter]] = []
        for path, value in ws.design.locked.items():
            new = get_path(params, path) if has_path(params, path) else None
            if same(new, value):
                continue
            records = self._owned(path, decided)
            match = [r for r in records or () if same(json.loads(str(r.value)), new)]
            if match:
                restores.append((match[0], store.get(path)))
                continue
            try:  # the lock stands: keep its value in the combined design
                params = set_path(params, path, value)
            except PathError as error:
                raise LockedParameterError(store.get(path)) from error
        target = {**target, "params": params}
        if same(target, ws.design.snapshot()) and ws.report is not None:
            return
        for record, current in restores:
            store.restore(record, replacing=current)
        ws.design.params = target["params"]
        ws.design.code = target["code"]
        ws._changed()
        await ws.check()

    def change_set(self) -> ChangeSet:
        ws = self.ws
        current = ws.latest if ws.report is not None else None
        after = _by_id(ws.report) if current is not None else None
        before = {i.id: i for i in self.base_checks}
        changes = []
        for unit in self.units:
            d = delta(unit)
            proof = links(unit, d, before, _by_id(self.alone.get(unit.path)), after)
            rounds = self.rounds.get(unit.path, [])
            changes.append(
                ProposedChange(
                    id=unit.path,
                    title=title(unit, d),
                    reason=reason(d, proof, rounds, self.board),
                    status=self.status[unit.path],  # type: ignore[arg-type]
                    rounds=rounds,
                    delta=d,
                    checks=proof,
                    comment=self.comments.get(unit.path),
                )
            )
        checks = None
        if current is not None and ws.report is not None:
            checks = CheckSummary.model_validate(summary(ws.report))
        return ChangeSet(
            mode=self.mode,
            base_build=self.base_build.build.id if self.base_build.build else "",
            build=current.build.id if current is not None and current.build else None,
            checks=checks,
            changes=changes,
        )

    # --- Saving: everything but the workspace, which the session saves itself --------

    def to_json(self) -> dict[str, Any]:
        build = self.base_build.build
        return {
            "mode": self.mode,
            "base": self.base,
            "final": self.final,
            "units": [asdict(u) for u in self.units],
            "base_build": {
                "build": build.model_dump(mode="json") if build is not None else None,
                "error": self.base_build.error,
            },
            "base_checks": [asdict(i) for i in self.base_checks],
            "alone": {
                k: [asdict(i) for i in v] if v is not None else None for k, v in self.alone.items()
            },
            "rounds": self.rounds,
            "status": self.status,
            "records": {
                k: [p.model_dump(mode="json") for p in pair] for k, pair in self.records.items()
            },
            "board": self.board,
            "overlays": self.overlays,
        }

    @classmethod
    def from_json(cls, ws: Workspace, data: dict[str, Any]) -> Proposal:
        """A saved proposal, on the session's workspace. The base build comes back without its
        shapes: the proposal only names it."""
        build = data["base_build"]["build"]

        def items(rows: list[dict] | None) -> list[CheckItem] | None:
            return None if rows is None else [CheckItem(**r) for r in rows]

        return cls(
            ws=ws,
            mode=data["mode"],
            base=data["base"],
            final=data["final"],
            units=[Unit(**u) for u in data["units"]],
            base_build=BuildOutcome(
                Build.model_validate(build) if build is not None else None,
                error=data["base_build"]["error"],
            ),
            base_checks=items(data["base_checks"]) or [],
            alone={k: items(v) for k, v in data["alone"].items()},
            rounds=data["rounds"],
            status=data["status"],
            records={
                k: (Parameter.model_validate(a), Parameter.model_validate(b))
                for k, (a, b) in data["records"].items()
            },
            board=data["board"],
            overlays=data["overlays"],
        )


def _covers(unit: str, path: str) -> bool:
    """Whether the change at `unit` holds the value at `path` (or `path` holds it)."""
    return path == unit or path.startswith(unit + ".") or unit.startswith(path + ".")


def _records(ws: Workspace) -> dict[str, tuple[Parameter, Parameter]]:
    """Locked values the run changed with approval: the record from before the run's first
    change and the record after its last one, by path."""
    out: dict[str, tuple[Parameter, Parameter]] = {}
    for edit in ws.edits:
        for path, (before, after) in edit.locks.items():
            out[path] = (out[path][0] if path in out else before, after)
    return out


def comment_links(ws: Workspace, units: list[Unit]) -> dict[str, str]:
    """The comment each change answers, by change ID, for a run sent comments.

    The paths Claude named for a comment (`reply_to_comment`) link the changes holding them.
    A change left unlinked goes to the run's only comment, or else to the one comment pinned
    on the feature it changes; otherwise it answers none of them."""
    links: dict[str, str] = {}
    if not ws.comments:
        return links
    for unit in units:
        named = [c for path, c in ws.comment_links.items() if _covers(unit.path, path)]
        if named:
            links[unit.path] = named[-1]
        elif len(ws.comments) == 1:
            links[unit.path] = ws.comments[0].id
        else:
            feature = delta(unit).feature
            on_it = [c.id for c in ws.comments if feature and c.feature == feature]
            if len(on_it) == 1:
                links[unit.path] = on_it[0]
    return links


def _rounds(ws: Workspace, units: list[Unit]) -> dict[str, list[int]]:
    """The fix rounds whose edits touched each change."""
    out: dict[str, list[int]] = {}
    paths = {u.path for u in units}
    for edit in ws.edits:
        for unit in diff(edit.before, edit.after):
            if unit.path in paths:
                out.setdefault(unit.path, []).append(edit.round)
    return out


async def propose(ws: Workspace, mode: Mode = "auto") -> Proposal:
    """The change set of the run in `ws`: from `ws.base` to the current design.

    Builds and checks the starting design and the starting design plus each change alone.
    `manual` puts the starting design back (nothing is accepted yet); `auto` keeps the run's
    design with every change accepted."""
    if ws.base is None:
        raise ValueError("the workspace has no starting design (ws.base)")
    base, final = copy.deepcopy(ws.base), ws.design.snapshot()
    units = diff(base, final)
    services = ws.services

    async def checked(design: dict) -> tuple[BuildOutcome, list[CheckItem] | None]:
        known = ws.recall(design)  # the run checked it already: the base, the final design
        if known is not None:
            return known
        outcome = await asyncio.to_thread(services.build, design)
        if not outcome.ok:
            return outcome, None
        items = await asyncio.to_thread(services.checks, outcome)
        ws.remember(design, outcome, items)
        return outcome, items

    # The base and each change alone, every distinct design once; they wait for build slots
    # (`geometry.slots`) together, so with more than one slot they build at the same time.
    designs = {"": base} | {u.path: combine(base, final, units, {u.path}) for u in units}
    distinct: dict[str, dict] = {}
    for design in designs.values():
        distinct.setdefault(design_key(design), design)
    results = dict(
        zip(distinct, await asyncio.gather(*(checked(d) for d in distinct.values())), strict=True)
    )
    base_build, base_checks = results[design_key(base)]
    alone = {u.path: results[design_key(designs[u.path])][1] for u in units}
    status = "accepted" if mode == "auto" else "proposed"
    proposal = Proposal(
        ws=ws,
        mode=mode,
        base=base,
        final=final,
        units=units,
        base_build=base_build,
        base_checks=base_checks or [],
        alone=alone,
        rounds=_rounds(ws, units),
        status={u.path: status for u in units},
        records=_records(ws),
        comments=comment_links(ws, units),
        board=board_changes(ws.changes),
    )
    await proposal._apply()
    return proposal


__all__ = ["Proposal", "Unit", "combine", "comment_links", "diff", "propose"]
