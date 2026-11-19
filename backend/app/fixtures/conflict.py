"""The conflict fixture: a recorded run that ends in the can't-satisfy state.

The conflict is the one from conflict detection: `geometry.conflicts.conflict_locks()` locks
a value the clearance standard and the height limit can't meet together on the newer board, and
`detect_conflicts()` reports the clashing constraints, where they act and the best achievable
value of each. Its points are in board coordinates of the generator's design; here each one is
moved to the nearest point on the same part of the fixture mesh and given that face's ID, so the
canvas can pin it to the sample enclosure.

Claude then asks which constraint should give, with one option per lock, the standards that need
the least relaxing, and asking the board team.
"""

from __future__ import annotations

from typing import Any

from app.boards import Board
from app.fixtures.geometry import Part
from app.standards import StandardsFile
from geometry import enclosure_parameters
from geometry.conflicts import Conflict, conflict_contract, conflict_locks, detect_conflicts

Vec = tuple[float, float, float]
MAX_OPTIONS = 4  # ask_user takes 2 to 4 options


def conflict_locks_for(board: Board, standards: StandardsFile) -> dict[str, float]:
    return conflict_locks(board, standards, enclosure_parameters(board, standards))


def detect(board: Board, standards: StandardsFile) -> Conflict:
    """The fixture's conflict: the conflict locks on the board's design."""
    params = enclosure_parameters(board, standards)
    (conflict,) = detect_conflicts(board, standards, params, conflict_locks_for(board, standards))
    return conflict


# --- Locations on the fixture mesh ---------------------------------------------------------------


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _along(a: Vec, d: Vec, t: float) -> Vec:
    return (a[0] + d[0] * t, a[1] + d[1] * t, a[2] + d[2] * t)


def closest_on_triangle(p: Vec, a: Vec, b: Vec, c: Vec) -> Vec:
    """The point of triangle abc nearest p (Ericson, Real-Time Collision Detection, 5.1.5)."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return _along(a, ab, d1 / (d1 - d3))
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return _along(a, ac, d2 / (d2 - d6))
    va = d3 * d6 - d5 * d4
    if va <= 0 and d4 - d3 >= 0 and d5 - d6 >= 0:
        return _along(b, _sub(c, b), (d4 - d3) / ((d4 - d3) + (d5 - d6)))
    denom = 1 / (va + vb + vc)
    v, w = vb * denom, vc * denom
    return (
        a[0] + ab[0] * v + ac[0] * w,
        a[1] + ab[1] * v + ac[1] * w,
        a[2] + ab[2] * v + ac[2] * w,
    )


def snap(parts: dict[str, Part], location: dict[str, Any]) -> dict[str, Any]:
    """A detector location as a contract `Location` on the fixture mesh: the nearest point on
    the same part, and the face it is on."""
    part = parts[location["part"]]
    p = tuple(location["point"])
    best: tuple[float, Vec, str] | None = None
    for face in part.faces:
        for tri in face.triangles:
            q = closest_on_triangle(p, *tri)
            d = _dot(_sub(p, q), _sub(p, q))
            if best is None or d < best[0]:
                best = (d, q, face.id)
    assert best is not None, f"part {part.name} has no faces"
    _, point, face_id = best
    return {
        "point": [round(v, 3) for v in point],
        "part": part.name,
        "faces": [face_id],
        "edges": [],
    }


# --- The question ------------------------------------------------------------------------------


def conflict_json(
    conflict: Conflict, board: Board, standards: StandardsFile, parts: dict[str, Part]
) -> dict:
    """The conflict in the session contract's `ConstraintConflict` form, on the fixture mesh."""
    return conflict_contract(conflict, board, standards, place=lambda loc: snap(parts, loc))


def _mm(value: float) -> str:
    return f"{value:g} mm"


def options_for(conflict: dict) -> list[dict]:
    """What can give: each lock at its best value, then the standards that need the least
    relaxing, then asking the board team to move the part. At most `MAX_OPTIONS`."""
    cs = conflict["constraints"]
    locks = [c for c in cs if c["kind"] == "locked" and c["best"] is not None]
    standards = sorted(
        (c for c in cs if c["kind"] == "standard" and c["best"] is not None),
        key=lambda c: abs(c["best"] - c["value"]) / abs(c["value"]),
    )
    parts = [c for c in cs if c["kind"] == "board"]
    options = [
        {
            "id": f"unlock-{c['id']}",
            "label": f"Unlock {c['label']}",
            "description": f"Use {_mm(c['best'])} instead of {_mm(c['value'])}; "
            "everything else stays.",
        }
        for c in locks
    ]
    room = MAX_OPTIONS - len(options) - (1 if parts else 0)
    options += [
        {
            "id": f"relax-{c['id']}",
            "label": f"Relax {c['label']} to {_mm(c['best'])}",
            "description": f"The constraint is {_mm(c['value'])}; the locks stay.",
        }
        for c in standards[: max(0, room)]
    ]
    if parts:
        refs = ", ".join(c["id"] for c in parts)
        options.append(
            {
                "id": "ask-board-team",
                "label": "Ask the board team",
                "description": f"Move or lower {refs} on the board.",
            }
        )
    return options[:MAX_OPTIONS]


def question_text(conflict: dict) -> str:
    cs = conflict["constraints"]
    parts = [c["id"] for c in cs if c["kind"] == "board"]
    where = f" around {', '.join(parts)}" if parts else ""
    return (
        f"I can't meet all of these at once{where}: they are {_mm(conflict['over_by'])} apart. "
        "Which should give?"
    )


def prompt_for(locks: dict[str, float]) -> str:
    """The message that locks the values: the mock plays the conflict run for it."""
    return "Lock " + " and ".join(f"{path} at {_mm(v)}" for path, v in locks.items()) + "."
