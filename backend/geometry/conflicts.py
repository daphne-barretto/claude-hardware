"""Conflict detection: locked parameters, standards and the board that can't all be met.

The enclosure's vertical stack is a set of linear constraints on the parameters the generator
reads (`geometry.params`, used as in `backends/*/examples/enclosure.py`):

- the lid's roof runs from `ceiling` up to `z_top`, the base's floor from `z_bottom` up to
  `floor_top`, and the side walls are `wall` thick; each is at least `min_wall`;
- the ceiling clears every top-side part inside the footprint by `min_component_clearance`,
  and the floor clears every bottom-side part by it (the board's bottom face is z = 0);
- `z_top - z_bottom` is at most `max_height`;
- a locked parameter keeps its value.

`detect_conflicts()` solves that system as a linear program. When it has no solution, it finds
a minimal set of constraints that clash (drop any one and the rest can be met), and reports for
that set: which locks, standards and board parts clash, where on the model each one acts, by how
much the standards fall short with the locks held, and the best achievable value of each
constraint when it alone gives way and every other constraint holds. With a build and its mesh,
each location also names the face it is on, by its stable mesh ID.

`conflict_locks()` is the test fixture: it locks one parameter at a value that clashes with the
clearance standard and the height limit, derived from the board and `standards.yaml`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog

from app.boards import Board
from app.boards.schema import Component
from app.parameters import ParameterStore
from app.standards import StandardsFile
from geometry.params import opening_corners

# The parameters of the stack, in the order of the LP's variables.
WALL, TOP, BOTTOM, CEILING, FLOOR = "wall", "z_top", "z_bottom", "ceiling", "floor_top"
VARIABLES = (WALL, TOP, BOTTOM, CEILING, FLOOR)
EPS = 1e-7  # mm: LP tolerance


@dataclass
class Constraint:
    """One side of a conflict: a locked parameter, a standard or a board part."""

    kind: str  # "locked", "standard" or "board"
    id: str  # parameter path, standard ID or component reference
    value: float  # the locked value, the standard's value, or the part's height
    unit: str = "mm"
    # The best achievable value when only this constraint gives way and every other one holds;
    # None when no value of it resolves the conflict (a board part can't change).
    best: float | None = None
    locations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Conflict:
    id: str
    message: str
    over_by: float  # mm the standards fall short, with every lock held
    constraints: list[Constraint]
    location: dict[str, Any] = field(default_factory=dict)  # where it is worst

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(eq=False)  # compared by identity
class _Row:
    """`coef . x >= k * value + const` (or `==` when `eq`), for one constraint."""

    kind: str  # "locked" or "standard"
    id: str
    value: float
    coef: dict[str, float]
    k: float = 1.0
    const: float = 0.0
    eq: bool = False
    component: Component | None = None
    part: str = "lid"
    point: tuple[float, float, float] = (0.0, 0.0, 0.0)
    order: float = 0.0  # rows with a lower order are tried first when shrinking the clash

    def rhs(self, value: float | None = None) -> float:
        return self.k * (self.value if value is None else value) + self.const


def locked_values(locked: Mapping[str, Any] | ParameterStore) -> dict[str, float]:
    """The locked parameters of the stack, as {path: value}."""
    if isinstance(locked, ParameterStore):
        locked = {p.name: p.value for p in locked.locked()}
    out = {}
    for path in VARIABLES:
        value = locked.get(path)
        if isinstance(value, int | float) and not isinstance(value, bool):
            out[path] = float(value)
    return out


def _inside(c: Component, footprint: dict) -> bool:
    """Whether any of the component's body is inside the enclosure's footprint."""
    corners = opening_corners(
        {"x": c.position[0], "y": c.position[1], "rotation": c.rotation, "size": c.size}
    )
    (x0, x1), (y0, y1) = footprint["x"], footprint["y"]
    return (
        min(p[0] for p in corners) < x1
        and max(p[0] for p in corners) > x0
        and min(p[1] for p in corners) < y1
        and max(p[1] for p in corners) > y0
    )


def _rows(
    board: Board, standards: StandardsFile, params: dict[str, Any], locked: dict[str, float]
) -> list[_Row]:
    min_wall = float(standards.get("min_wall").value)
    clearance = float(standards.get("min_component_clearance").value)
    # A height limit only when the set has one (an example may bring it; it isn't a default).
    try:
        max_height: float | None = float(standards.get("max_height").value)
    except KeyError:
        max_height = None
    pcb = board.pcb.thickness
    footprint = params["footprint"]
    z = {p: float(locked.get(p, params[p])) for p in VARIABLES}
    cx = (footprint["x"][0] + footprint["x"][1]) / 2
    cy = (footprint["y"][0] + footprint["y"][1]) / 2

    rows = [
        _Row("standard", "min_wall", min_wall, {TOP: 1, CEILING: -1}, point=(cx, cy, z[TOP])),
        _Row(
            "standard",
            "min_wall",
            min_wall,
            {FLOOR: 1, BOTTOM: -1},
            part="base",
            point=(cx, cy, z[BOTTOM]),
        ),
        _Row(
            "standard",
            "min_wall",
            min_wall,
            {WALL: 1},
            point=(footprint["x"][0], cy, z[TOP] / 2),
        ),
    ]
    if max_height is not None:
        rows.append(
            _Row(
                "standard",
                "max_height",
                max_height,
                {BOTTOM: 1, TOP: -1},
                k=-1.0,
                point=(cx, cy, z[TOP]),
                order=1.0,
            )
        )
    for c in board.components:
        if not _inside(c, footprint):
            continue
        x, y = c.position
        if c.side == "top":
            # ceiling >= clearance + pcb + height
            coef, const, part, point = {CEILING: 1}, pcb + c.height, "lid", (x, y, z[CEILING])
        else:
            # -floor_top >= clearance + height
            coef, const, part, point = {FLOOR: -1}, c.height, "base", (x, y, z[FLOOR])
        rows.append(
            _Row(
                "standard",
                "min_component_clearance",
                clearance,
                coef,
                const=const,
                component=c,
                part=part,
                point=point,
                order=-1.0 / (1.0 + c.height),  # shortest parts first, so the tallest remain
            )
        )
    where = {
        WALL: ("lid", (footprint["x"][0], cy, z[TOP] / 2)),
        TOP: ("lid", (cx, cy, z[TOP])),
        BOTTOM: ("base", (cx, cy, z[BOTTOM])),
        CEILING: ("lid", (cx, cy, z[CEILING])),
        FLOOR: ("base", (cx, cy, z[FLOOR])),
    }
    for path, value in locked.items():
        part, point = where[path]
        rows.append(
            _Row("locked", path, value, {path: 1}, eq=True, part=part, point=point, order=2.0)
        )
    return rows


# --- Linear programs ---------------------------------------------------------------------------


def _solve(
    rows: list[_Row],
    *,
    objective: dict[str, float] | None = None,
    free: str | None = None,
    elastic: list[_Row] | None = None,
) -> tuple[float, dict[str, float]] | None:
    """Solve the rows; None when they have no solution.

    `objective` is minimised over the stack variables. `free` makes that standard's value a
    variable `v` (minimised as `objective["v"]`). `elastic` rows each get a slack >= 0 and the
    total slack is minimised.
    """
    names = list(VARIABLES) + (["v"] if free else [])
    elastic = elastic or []
    n = len(names) + len(elastic)
    index = {name: i for i, name in enumerate(names)}
    a_ub, b_ub, a_eq, b_eq = [], [], [], []
    for row in rows:
        a = np.zeros(n)
        for var, c in row.coef.items():
            a[index[var]] = c
        rhs = row.rhs()
        if free and row.kind == "standard" and row.id == free:
            a[index["v"]] -= row.k
            rhs = row.const
        if row in elastic:
            a[len(names) + elastic.index(row)] = 1.0
        if row.eq:
            a_eq.append(a)
            b_eq.append(rhs)
        else:
            a_ub.append(-a)
            b_ub.append(-rhs)
    c = np.zeros(n)
    for var, w in (objective or {}).items():
        c[index[var]] = w
    c[len(names) :] = 1.0
    bounds = [(None, None)] * len(names) + [(0, None)] * len(elastic)
    res = linprog(
        c,
        A_ub=np.array(a_ub) if a_ub else None,
        b_ub=np.array(b_ub) if b_ub else None,
        A_eq=np.array(a_eq) if a_eq else None,
        b_eq=np.array(b_eq) if b_eq else None,
        bounds=bounds,
        method="highs",
    )
    if res.status != 0:
        return None
    return float(res.fun), dict(zip(names, (float(v) for v in res.x[: len(names)]), strict=True))


def _feasible(rows: list[_Row]) -> bool:
    return _solve(rows) is not None


def _clash(rows: list[_Row]) -> list[_Row]:
    """A minimal infeasible subset of `rows` (deletion filter): drop any row and it's solvable."""
    keep = list(rows)
    for row in sorted(rows, key=lambda r: r.order):
        trial = [r for r in keep if r is not row]
        if not _feasible(trial):
            keep = trial
    return keep


def _best_lock(rows: list[_Row], lock: _Row) -> float | None:
    """The value nearest the locked one that the other constraints allow."""
    others = [r for r in rows if r is not lock]
    lo = _solve(others, objective={lock.id: 1.0})
    hi = _solve(others, objective={lock.id: -1.0})
    if lo is None or hi is None:
        return None
    return min(max(lock.value, lo[1][lock.id]), hi[1][lock.id])


def _best_standard(rows: list[_Row], standard: str) -> float | None:
    """The standard's most demanding value that the other constraints allow."""
    k = next(r.k for r in rows if r.kind == "standard" and r.id == standard)
    # Larger k * v is harder to meet: maximise it.
    solved = _solve(rows, objective={"v": -k}, free=standard)
    if solved is None:
        return None
    best = solved[1]["v"]
    if best < -EPS:  # a negative wall, clearance or height is no value at all
        return None
    return max(best, 0.0)


# --- Detection ---------------------------------------------------------------------------------


def detect_conflicts(
    board: Board,
    standards: StandardsFile,
    params: dict[str, Any],
    locked: Mapping[str, Any] | ParameterStore,
    result=None,
    mesh=None,
) -> list[Conflict]:
    """Conflicts between the locked parameters, the standards and the board.

    `params` is the current design (`geometry.params`); `locked` maps parameter paths to their
    locked values, or is the parameter store. Locks outside the vertical stack are not
    constrained here. Pass the design's build (`BuildResult`) and its tessellation to add the
    stable face ID of every location. Returns an empty list when everything can be met.
    """
    rows = _rows(board, standards, params, locked_values(locked))
    locator = _locator(board, result, mesh)
    conflicts: list[Conflict] = []
    active = list(rows)
    while not _feasible(active):
        clash = _clash(active)
        conflicts.append(_conflict(len(conflicts) + 1, rows, clash, locator))
        # Look for further, independent conflicts with this one's locks (or rows) released.
        released = [r for r in clash if r.kind == "locked"] or clash
        active = [r for r in active if r not in released]
    return conflicts


def _location(row: _Row, locator) -> dict[str, Any]:
    out: dict[str, Any] = {
        "point": [round(v, 4) for v in row.point],
        "part": row.part,
        "component": row.component.ref if row.component else None,
        "faces": [],
    }
    if locator is not None:
        from geometry.checks import CheckResult

        probe = CheckResult(
            "conflict",
            "fail",
            component=out["component"],
            part=row.part,
            feature=row.part,
            point=row.point,
        )
        found = locator.locate(probe, None)
        out.update(point=found["point"], part=found["part"], faces=found["faces"])
    return out


def _conflict(n: int, rows: list[_Row], clash: list[_Row], locator) -> Conflict:
    locks = [r for r in clash if r.kind == "locked"]
    standards = [r for r in clash if r.kind == "standard"]
    # How far apart: the least total the standards must give with every lock held (or the
    # locks, when they clash among themselves).
    elastic = standards if locks and standards else clash
    hard = [r for r in clash if r not in elastic]
    solved = _solve(hard + elastic, elastic=elastic)
    over_by = round(solved[0], 3) if solved else math.inf
    # Where: at the board part the clash squeezes (the tallest), else at the first lock.
    squeezed = [r for r in clash if r.component is not None]
    if squeezed:
        where = max(squeezed, key=lambda r: r.component.height)
    else:
        where = (locks or clash)[0]

    constraints: list[Constraint] = []
    for lock in locks:
        constraints.append(
            Constraint(
                "locked",
                lock.id,
                lock.value,
                best=_round(_best_lock(rows, lock)),
                locations=[_location(lock, locator)],
            )
        )
    for standard in dict.fromkeys(r.id for r in standards):
        mine = [r for r in standards if r.id == standard]
        constraints.append(
            Constraint(
                "standard",
                standard,
                mine[0].value,
                best=_round(_best_standard(rows, standard)),
                locations=[_location(r, locator) for r in mine],
            )
        )
    for r in standards:
        if r.component is not None:
            constraints.append(
                Constraint(
                    "board",
                    r.component.ref,
                    r.component.height,
                    best=None,
                    locations=[_location(r, locator)],
                )
            )

    names = ", ".join(f"{c.id} = {c.value:g} mm" for c in constraints if c.kind == "locked")
    standard_names = ", ".join(
        f"{c.id} ({c.value:g} mm)" for c in constraints if c.kind == "standard"
    )
    parts = ", ".join(c.id for c in constraints if c.kind == "board")
    head = f"The locked {names}" if names else "The board"
    message = (
        f"{head} can't meet {standard_names}"
        + (f" around {parts}" if parts else "")
        + f": {over_by:g} mm short."
    )
    return Conflict(
        id="stackup" if n == 1 else f"stackup_{n}",
        message=message,
        over_by=over_by,
        constraints=constraints,
        location=_location(where, locator),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _locator(board: Board, result, mesh):
    if result is None:
        return None
    from geometry.report import Locator
    from geometry.tessellate import tessellate

    return Locator(board, result, mesh if mesh is not None else tessellate(result))


# --- In the session contract --------------------------------------------------------------------


def relation(c: Constraint, standards: StandardsFile) -> str:
    """How a constraint's value binds: `=` for a lock or a part's height, `>=` for a `min`
    standard, `<=` for a `max` one."""
    if c.kind != "standard":
        return "="
    return {"min": ">=", "max": "<="}[standards.get(c.id).kind]


def label(c: Constraint, standards: StandardsFile, board: Board) -> str:
    """What to call a constraint: the standard's title, the part with its role, or the path."""
    if c.kind == "standard":
        return standards.get(c.id).title
    if c.kind == "board":
        part = next((p for p in board.components if p.ref == c.id), None)
        return f"{c.id} ({part.role})" if part is not None and part.role else c.id
    return c.id


def contract_location(location: dict[str, Any]) -> dict[str, Any]:
    """A detector location as a contract `Location`: its point, part and faces."""
    return {
        "point": [round(float(v), 4) for v in location["point"]],
        "part": location.get("part"),
        "faces": list(location.get("faces") or []),
        "edges": [],
    }


def conflict_contract(
    conflict: Conflict,
    board: Board,
    standards: StandardsFile,
    place: Callable[[dict[str, Any]], dict[str, Any]] = contract_location,
) -> dict[str, Any]:
    """The conflict in the session contract's `ConstraintConflict` form (a `question` event's
    `conflict`). `place` turns each detector location into a contract `Location`; by default it
    keeps the point, part and the faces the detector found on the build's mesh."""
    return {
        "id": conflict.id,
        "message": conflict.message,
        "over_by": conflict.over_by,
        "constraints": [
            {
                "kind": c.kind,
                "id": c.id,
                "label": label(c, standards, board),
                "relation": relation(c, standards),
                "value": c.value,
                "unit": c.unit,
                "best": c.best,
                "locations": [place(loc) for loc in c.locations],
            }
            for c in conflict.constraints
        ],
        "location": place(conflict.location),
    }


# --- Fixture -----------------------------------------------------------------------------------


def conflict_locks(board: Board, standards: StandardsFile, params: dict[str, Any]) -> dict:
    """Locks that can't be met: the base's floor (`floor_top`) locked so deep that, with the
    roof and floor at the minimum wall and the lid clearing the tallest top-side part, the
    enclosure is half the component clearance taller than the height limit (rounded to 0.1 mm,
    away from feasible). Returns {parameter path: locked value}.
    """
    rows = _rows(board, standards, params, {})
    lowest = _solve(rows, objective={FLOOR: 1.0})
    if lowest is None:
        raise ValueError("the standards and the board conflict without any lock")
    clearance = float(standards.get("min_component_clearance").value)
    return {FLOOR: math.floor((lowest[1][FLOOR] - clearance / 2) * 10) / 10}


__all__ = [
    "VARIABLES",
    "Conflict",
    "Constraint",
    "conflict_contract",
    "conflict_locks",
    "detect_conflicts",
    "locked_values",
]
