"""Risks the checks don't block, flagged in chat with a fix.

A design that passes every check can still be a poor part. After each check of a build the
workspace looks for:

- **Thick sections** (`thick`): spots the wall-thickness analysis flags because they sink when
  molded. No check fails on them.
- **Thin areas and low draft** (`thin`, `low_draft`): analysis spots that no failing check
  covers. When the check on them fails, the checks already block the design and it is not a
  risk but a failure.
- **Near a limit** (`near_limit`): a passing check of a `min` or `max` standard whose measured
  value is within `NEAR` of the limit (a fraction of the limit): a wall just over the minimum,
  a clearance just over the minimum, a feature just under the height limit.

Claude flags each risk with `flag_risk`, giving a fix: parameter edits (or new code) that are
**not applied**. The tool builds and checks the design with the fix alone, refuses a fix that
breaks a passing check, touches a locked value, or leaves the risk as bad as it was, and then
names the risk in one line in chat. The flags form their own set, separate from the run's
change set: `RiskFlags.review(accept=..., reject=...)` applies or takes back each fix on its
own, rebuilds and rechecks.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from app.agent.design import (
    CODE,
    LockedCodeError,
    PathError,
    get_path,
    has_path,
    same,
    set_path,
)
from app.agent.services import CheckItem
from app.contracts.changes import RiskFix, RiskFlag, RiskFlags, RiskLocation
from app.contracts.maps import MAP_TYPES
from app.parameters import LockedParameterError
from app.standards import StandardsFile

if TYPE_CHECKING:
    from app.agent.proposal import Proposal
    from app.agent.tools import Workspace

NEAR = 0.1  # a passing check within this fraction of its limit is near it
EPS = 1e-9


def _g(value: float) -> str:
    return f"{round(value, 3):g}"


@dataclass
class Risk:
    id: str
    kind: str  # thick, thin, low_draft, near_limit
    what: str  # what is wrong, without the location
    value: float
    limit: float
    unit: str | None
    standard: str | None
    margin: float  # how far inside the limit (near_limit), else 0
    part: str | None = None
    feature: str | None = None
    component: str | None = None
    faces: list[str] = field(default_factory=list)
    point: list[float] | None = None

    @property
    def severity(self) -> float:
        """Lower is better: how a fix is judged against the risk."""
        if self.kind == "thick":
            return self.value
        if self.kind == "near_limit":
            return -self.margin
        return -self.value  # thin, low draft: more is better

    @property
    def where(self) -> str:
        subject = self.feature or self.component
        parts = []
        if subject:
            parts.append(subject)
        if self.part and self.part != subject:
            parts.append(f"on the {self.part}")
        text = " ".join(parts) or "the enclosure"
        if self.point:
            text += " at (" + ", ".join(f"{c:.1f}" for c in self.point) + ")"
        return text

    @property
    def message(self) -> str:
        """The one line said in chat: what, and where."""
        return f"{self.what}, {self.where}."

    def location(self) -> RiskLocation:
        return RiskLocation(
            part=self.part,
            feature=self.feature,
            component=self.component,
            faces=self.faces,
            point=tuple(self.point) if self.point else None,  # type: ignore[arg-type]
        )

    def to_json(self) -> dict:
        out = {k: v for k, v in asdict(self).items() if v not in (None, [], "")}
        out["message"] = self.message
        return out


# --- Finding risks ----------------------------------------------------------------------------


def _spot_feature(spot: dict) -> str | None:
    if spot.get("feature"):
        return str(spot["feature"])
    faces = spot.get("faces") or []
    return str(faces[0]).split(".")[0] if faces else None


def _analysis_risks(
    analysis: list[dict], report: list[CheckItem], standards: StandardsFile
) -> list[Risk]:
    from geometry.params import THICK_FACTOR

    failing = {i.check for i in report if i.status == "fail"}
    worst: dict[tuple[str, str | None], Risk] = {}
    for spot in analysis:
        kind = spot["kind"]
        standard_id = MAP_TYPES[spot["map"]][0]
        standard = standards.get(standard_id)
        if kind != "thick" and standard.check in failing:
            continue  # the check fails here: the checks block it, it is no risk
        feature = _spot_feature(spot)
        limit = float(standard.value)
        unit = spot.get("unit") or standard.unit
        value = float(spot["value"])
        if kind == "thick":
            what = (
                f"Thick section: {_g(value)} {unit}, over {_g(THICK_FACTOR)}x the "
                f"{_g(limit)} {unit} minimum wall ({standard.title}), sinks when molded"
            )
        elif kind == "thin":
            what = (
                f"Thin wall: {_g(value)} {unit}, under the {_g(limit)} {unit} minimum "
                f"({standard.title})"
            )
        else:
            what = f"Low draft: {_g(value)}°, under the {_g(limit)}° minimum ({standard.title})"
        risk = Risk(
            id=f"{kind}:{feature or spot['part']}",
            kind=kind,
            what=what,
            value=value,
            limit=limit,
            unit=unit,
            standard=standard_id,
            margin=0.0,
            part=spot.get("part"),
            feature=feature,
            faces=list(spot.get("faces") or []),
            point=spot.get("point") or None,
        )
        key = (kind, feature or spot.get("part"))
        if key not in worst:
            worst[key] = risk
            continue
        held = worst[key]
        faces = held.faces + [f for f in risk.faces if f not in held.faces]
        if risk.severity > held.severity:
            risk.faces = faces
            worst[key] = risk
        else:
            held.faces = faces
    return list(worst.values())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _near_limit_risks(report: list[CheckItem], standards: StandardsFile) -> list[Risk]:
    out = []
    for item in report:
        if item.status != "pass" or not item.standard:
            continue
        try:
            standard = standards.get(item.standard)
        except KeyError:
            continue
        measured, required = _number(item.measured), _number(item.required)
        if standard.kind not in ("min", "max") or measured is None or required is None:
            continue
        margin = measured - required if standard.kind == "min" else required - measured
        if margin > NEAR * abs(required) + EPS:
            continue
        margin = max(margin, 0.0)  # it passes: a measurement past the limit is within tolerance
        unit = item.unit or standard.unit or ""
        side = "minimum" if standard.kind == "min" else "maximum"
        name = standard.title.lower()
        if margin <= EPS:
            what = (
                f"Near a limit: {name} is {_g(measured)} {unit}, right at the "
                f"{_g(required)} {unit} {side}"
            )
        else:
            over = "over" if standard.kind == "min" else "under"
            what = (
                f"Near a limit: {name} is {_g(measured)} {unit}, only {_g(margin)} {unit} "
                f"{over} the {_g(required)} {unit} {side}"
            )
        out.append(
            Risk(
                id=f"near_limit:{item.id}",
                kind="near_limit",
                what=what,
                value=measured,
                limit=required,
                unit=unit or None,
                standard=standard.id,
                margin=margin,
                part=item.part,
                feature=item.feature,
                component=item.component,
                faces=list(item.faces or []),
                point=item.point,
            )
        )
    return out


def find_risks(
    report: list[CheckItem], analysis: list[dict], standards: StandardsFile
) -> list[Risk]:
    """The risks on a checked build: analysis spots the checks don't block, and passing checks
    near their limit. None when the analysis and the checks show no risk."""
    return [*_analysis_risks(analysis, report, standards), *_near_limit_risks(report, standards)]


# --- Flags ------------------------------------------------------------------------------------


@dataclass
class Flag:
    """A flagged risk and its fix (`edits`: parameter paths, and `CODE` for new code), checked
    on the design it was flagged on (`before`) with the fix alone (`after`)."""

    risk: Risk
    title: str
    edits: dict[str, Any]
    before: dict
    after: dict
    build: str
    resolves: bool
    links: list = field(default_factory=list)  # CheckLinks
    status: str = "proposed"

    @property
    def id(self) -> str:
        return self.risk.id

    def contract(self) -> RiskFlag:
        from app.agent.proposal import delta, diff

        changes = [delta(u) for u in diff(self.before, self.after)]
        risk = self.risk
        return RiskFlag(
            id=risk.id,
            kind=risk.kind,  # type: ignore[arg-type]
            message=risk.message,
            standard=risk.standard,
            value=risk.value,
            limit=risk.limit,
            unit=risk.unit,
            location=risk.location(),
            status=self.status,  # type: ignore[arg-type]
            fix=RiskFix(
                title=self.title,
                edits=self.edits,
                changes=changes,
                resolves=self.resolves,
                checks=self.links,
            ),
        )


def with_edits(design: dict, edits: dict[str, Any]) -> dict:
    """`design` (a snapshot) with a fix's edits laid over it."""
    out = copy.deepcopy(design)
    for path, value in edits.items():
        if path == CODE:
            out["code"] = value
        else:
            out["params"] = set_path(out["params"], path, value)
    return out


class FixError(ValueError):
    """A fix the tool refuses, with the reason for Claude."""


async def make_flag(ws: Workspace, risk_id: str, edits: dict[str, Any], title: str) -> Flag:
    """Check a fix for a risk on the latest build and return the flag; raises `FixError`."""
    from app.agent.proposal import links

    if ws.report is None or ws.latest is None:
        raise FixError("Run the checks first (run_checks): risks are found on a checked build.")
    risk = next((r for r in ws.risks if r.id == risk_id), None)
    if risk is None:
        known = ", ".join(r.id for r in ws.risks) or "none"
        raise FixError(f"No risk {risk_id!r} on build {ws.latest.build.id}. Risks: {known}.")
    if any(f.id == risk_id for f in ws.flags):
        raise FixError(f"{risk_id} is already flagged.")
    if not title.strip():
        raise FixError("Give the fix a short title.")
    if not edits:
        raise FixError("Give the fix: changes (parameter paths and values), code, or both.")
    before = ws.design.snapshot()
    try:
        after = with_edits(before, edits)
    except PathError as error:
        raise FixError(str(error)) from None
    if same(before, after):
        raise FixError("The fix changes nothing.")
    for path, value in ws.design.locked.items():
        new = get_path(after["params"], path) if has_path(after["params"], path) else None
        if not same(new, value):
            raise FixError(f"The fix changes {path}, which is locked. Fix it another way.")
    if after["code"] != before["code"] and ws.design.locked:
        raise FixError("The code is locked while parameters are locked. Fix it with parameters.")

    services = ws.services
    outcome = await asyncio.to_thread(services.build, after)
    if not outcome.ok:
        raise FixError(f"The design with the fix doesn't build: {json.dumps(outcome.error)}")
    checks = await asyncio.to_thread(services.checks, outcome)
    current = {i.id: i for i in ws.report}
    alone = {i.id: i for i in checks}
    broken = [
        i for i, item in current.items() if item.status == "pass" and _status(alone, i) == "fail"
    ]
    if broken:
        raise FixError(f"The fix fails {', '.join(broken)}, which pass without it.")
    analysis = await asyncio.to_thread(services.analyze, outcome)
    left = next((r for r in find_risks(checks, analysis, ws.standards) if r.id == risk.id), None)
    if left is not None and left.severity >= risk.severity - EPS:
        raise FixError(
            f"With the fix, {risk.id} is still there ({left.what}). Try another fix, or leave it."
        )
    from app.agent.proposal import delta as geometry_delta
    from app.agent.proposal import diff

    proof: list = []
    for unit in diff(before, after):
        d = geometry_delta(unit)
        for link in links(unit, d, current, alone, None):
            if all(link.id != seen.id for seen in proof):
                proof.append(link)
    return Flag(
        risk=risk,
        title=title.strip(),
        edits=edits,
        before=before,
        after=after,
        build=ws.latest.build.id,
        resolves=left is None,
        links=proof,
    )


def _status(items: dict[str, CheckItem], check_id: str) -> str | None:
    item = items.get(check_id)
    return item.status if item else None


def chat_line(flag: Flag) -> str:
    """The one line that names the risk in chat, with its fix."""
    return f"\n\nRisk: {flag.risk.message} Fix ready to accept on its own: {flag.title}.\n\n"


# --- The flag set -----------------------------------------------------------------------------


@dataclass
class Flags:
    """The risk flags of a run, separate from its change set. Each fix is accepted or rejected
    on its own; `review` lays the accepted fixes over the design and rebuilds."""

    ws: Workspace
    flags: list[Flag]
    proposal: Proposal | None = None

    def by_id(self, flag_id: str) -> Flag:
        for flag in self.flags:
            if flag.id == flag_id:
                return flag
        raise KeyError(f"no flag {flag_id}")

    def risk_flags(self) -> RiskFlags:
        build = self.flags[0].build if self.flags else ""
        return RiskFlags(build=build, flags=[f.contract() for f in self.flags])

    def to_json(self) -> dict[str, Any]:
        """The flags as saved with the session: the contract, and the design each was flagged
        on (its fix is laid over that)."""
        return {
            "risk_flags": self.risk_flags().model_dump(mode="json", by_alias=True),
            "before": {f.id: f.before for f in self.flags},
            "with_proposal": self.proposal is not None,
        }

    @classmethod
    def from_json(cls, ws: Workspace, data: dict[str, Any], proposal: Any = None) -> Flags:
        """Saved flags (`to_json`), on the session's workspace and its change set."""
        flags = RiskFlags.model_validate(data["risk_flags"])
        return cls.from_contract(
            ws, flags, data["before"], proposal if data.get("with_proposal") else None
        )

    @classmethod
    def from_contract(
        cls, ws: Workspace, risk_flags: RiskFlags, before: dict[str, dict], proposal: Any = None
    ) -> Flags:
        """Flags as a `risks_ready` event shows them, flagged on the designs `before` (by flag ID),
        so a session that has only the event (a demo's copy) can still review them."""
        out = []
        for f in risk_flags.flags:
            loc = f.location
            risk = Risk(
                id=f.id,
                kind=f.kind,
                what="",
                value=f.value if f.value is not None else 0.0,
                limit=f.limit if f.limit is not None else 0.0,
                unit=f.unit,
                standard=f.standard,
                margin=abs((f.limit or 0.0) - (f.value or 0.0)) if f.kind == "near_limit" else 0.0,
                part=loc.part,
                feature=loc.feature,
                component=loc.component,
                faces=list(loc.faces),
                point=list(loc.point) if loc.point else None,
            )
            # The message is "<what>, <where>."; keep what it says the risk is.
            where = f", {risk.where}."
            risk.what = f.message[: -len(where)] if f.message.endswith(where) else f.message
            design = before[f.id]
            out.append(
                Flag(
                    risk=risk,
                    title=f.fix.title,
                    edits=dict(f.fix.edits),
                    before=design,
                    after=with_edits(design, f.fix.edits),
                    build=risk_flags.build,
                    resolves=f.fix.resolves,
                    links=list(f.fix.checks),
                    status=f.status,
                )
            )
        return cls(ws, out, proposal)

    async def review(self, accept: Iterable[str] = (), reject: Iterable[str] = ()) -> RiskFlags:
        """Accept and reject fixes by flag ID, then rebuild and recheck the design with the
        accepted fixes. A fix that would now change a locked value is refused (nothing
        changes)."""
        accept, reject = list(accept), list(reject)
        for flag_id in [*accept, *reject]:
            self.by_id(flag_id)
        both = set(accept) & set(reject)
        if both:
            raise ValueError(f"both accepted and rejected: {', '.join(sorted(both))}")
        previous = {f.id: f.status for f in self.flags}
        for flag in self.flags:
            if flag.id in accept:
                flag.status = "accepted"
            elif flag.id in reject:
                flag.status = "rejected"
        try:
            await self._apply(previous)
        except (LockedParameterError, LockedCodeError, PathError):
            for flag in self.flags:
                flag.status = previous[flag.id]
            raise
        return self.risk_flags()

    def accepted_edits(self) -> list[dict[str, Any]]:
        return [f.edits for f in self.flags if f.status == "accepted"]

    def _guard_locks(self, edits: Iterable[dict[str, Any]]) -> None:
        """Refuse fixes that would now change a locked value (locked since they were flagged)."""
        design = self.ws.design
        current = design.snapshot()
        for fix in edits:
            after = with_edits(current, fix)
            if after["code"] != current["code"] and design.locked:
                raise LockedCodeError(sorted(design.locked))
            for path, value in design.locked.items():
                params = after["params"]
                new = get_path(params, path) if has_path(params, path) else None
                if not same(new, value):
                    raise LockedParameterError(design.store.get(path))

    async def _apply(self, previous: dict[str, str]) -> None:
        ws = self.ws
        added = [
            f.edits for f in self.flags if f.status == "accepted" and previous[f.id] != "accepted"
        ]
        self._guard_locks(added)
        if self.proposal is not None:
            self.proposal.overlays = self.accepted_edits()
            await self.proposal._apply(set())
            return
        target = ws.design.snapshot()
        for flag in self.flags:  # take back fixes no longer accepted, then lay on new ones
            if previous[flag.id] == "accepted" and flag.status != "accepted":
                undo = {p: _value(flag.before, p) for p in flag.edits}
                target = with_edits(target, undo)
        for fix in added:
            target = with_edits(target, fix)
        if same(target, ws.design.snapshot()):
            return
        ws.design.apply(target["params"], target["code"])
        ws._changed()
        await ws.check()


def _value(design: dict, path: str) -> Any:
    return design["code"] if path == CODE else get_path(design["params"], path)


__all__ = ["NEAR", "Flag", "Flags", "FixError", "Risk", "chat_line", "find_risks", "make_flag"]
