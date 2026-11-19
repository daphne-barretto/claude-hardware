"""Change set contract: the changes a finished agent run proposes, for review.

One `ProposedChange` per thing the run changed in the design (an opening, a list element, a
top-level parameter, the generator code). Each carries what changed (`delta`), why (`reason`),
the checks that prove it (`checks`) and its review status. The frontend's tracked-changes
overlay draws `delta` and the accept and reject buttons set `status`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.contracts.checks import CheckSummary, Status
from app.contracts.common import Contract, StableId

ChangeStatus = Literal["proposed", "accepted", "rejected"]
ChangeKind = Literal["added", "removed", "modified"]
Effect = Literal["fixes", "breaks", "related"]


class FieldDelta(Contract):
    """One value that differs: its dot path inside the changed element, before and after
    (null when absent)."""

    field: str
    before: Any = None
    after: Any = None


class GeometryDelta(Contract):
    kind: ChangeKind
    path: str = Field(description="Parameter path of the changed element (`openings.<id>`)")
    feature: StableId | None = Field(
        default=None, description="Feature tag on the mesh, e.g. `opening_Q4`, if it has one"
    )
    component: str | None = Field(default=None, description="Board component it serves")
    fields: list[FieldDelta] = Field(default_factory=list)
    move: tuple[float, float] | None = Field(
        default=None, description="How far it moved in x and y, mm (modified elements only)"
    )
    rotate: float | None = Field(default=None, description="How far it turned, degrees")


class CheckLink(Contract):
    """A check that proves (or argues against) a change.

    `before` is the check on the design the run started from, `alone` on that design plus this
    change only, and `after` on the current build (the accepted changes); null when that build
    has no such check or doesn't exist yet."""

    id: StableId
    check: str
    title: str | None = Field(
        default=None, description="The result's title on the checks strip, as people read it"
    )
    standard: str | None = None
    effect: Effect = Field(
        description="`fixes`: fails before, passes with the change alone; `breaks`: the "
        "reverse; `related`: about the same component or feature"
    )
    before: Status | None = None
    alone: Status | None = None
    after: Status | None = None


class ProposedChange(Contract):
    id: str = Field(min_length=1, description="Stable within the set: the parameter path")
    title: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: ChangeStatus
    rounds: list[int] = Field(default_factory=list, description="Fix rounds whose edits touched it")
    delta: GeometryDelta
    checks: list[CheckLink] = Field(default_factory=list)
    comment: StableId | None = Field(
        default=None,
        description="The comment this change answers (a comment sent to Claude): its thread "
        "shows the change with accept, reject and Undo",
    )


class ChangeSet(Contract):
    contract: Literal["change-set/1"] = "change-set/1"
    mode: Literal["manual", "auto"] = Field(
        description="manual: only accepted changes are applied; auto: all start accepted"
    )
    base_build: str = Field(description="Build of the design the run started from")
    build: str | None = Field(description="Build of the accepted changes, once checked")
    checks: CheckSummary | None = Field(default=None, description="Summary of `build`")
    changes: list[ProposedChange]


# --- Risk flags ------------------------------------------------------------------------

RiskKind = Literal["thick", "thin", "low_draft", "near_limit"]


class RiskLocation(Contract):
    """Where a risk is: the part, the feature or component, the faces when the analysis names
    them, and a point (mm, board coordinates) when known."""

    part: str | None = None
    feature: str | None = None
    component: str | None = None
    faces: list[StableId] = Field(default_factory=list)
    point: tuple[float, float, float] | None = None


class RiskFix(Contract):
    """The proposed change that answers a risk: what it sets, and the checks of the design with
    it alone. A fix can't break a check that passes without it."""

    title: str = Field(min_length=1)
    edits: dict[str, Any] = Field(description="Parameter path to new value; `code` for new code")
    changes: list[GeometryDelta] = Field(min_length=1)
    resolves: bool = Field(description="The risk is gone on the build with the fix alone")
    checks: list[CheckLink] = Field(default_factory=list)


class RiskFlag(Contract):
    """A risk the checks don't block, named in one line in chat, with a fix."""

    id: StableId = Field(description="`<kind>:<feature or check>`, unique in the set")
    kind: RiskKind = Field(
        description="`thick`: a section that sinks when molded; `thin`, `low_draft`: an "
        "analysis spot no failing check covers; `near_limit`: a passing check close to its limit"
    )
    message: str = Field(min_length=1, description="The one line said in chat")
    standard: str | None = None
    value: float | None = None
    limit: float | None = None
    unit: str | None = None
    location: RiskLocation
    status: ChangeStatus = Field(description="The fix's review status; starts `proposed`")
    fix: RiskFix


class RiskFlags(Contract):
    contract: Literal["risk-flags/1"] = "risk-flags/1"
    build: str = Field(description="The build the risks were found on")
    flags: list[RiskFlag]


class ChangeReview(Contract):
    """Accept and reject changes of the session's change set, by change `id`. Changes not
    named keep their status."""

    accept: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)


class RiskReview(Contract):
    """Accept and reject the fixes of the session's risk flags, by flag `id`. Flags not named
    keep their status; rejecting an accepted fix takes it back."""

    accept: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
