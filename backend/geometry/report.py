"""The check runner: every check on a build, as a check-results report (`contracts/README.md`).

`check_report()` runs one check per standard in `standards.yaml` plus the board-derived checks
(`geometry.checks.BOARD_CHECKS`), then turns each result into the contract shape: a stable ID,
a title, the standard it enforces, pass or fail, the measured value and the limit, a location
(a point on the model's surface and the mesh face IDs there) and what the check covers.
Strength, drop and heat are not simulated, so they are always listed as untested.

Every measurement comes from `geometry.checks`, which uses exact solid booleans and exact
distances on the B-rep. The face IDs come from the build's tessellation (`geometry.tessellate`),
so a location resolves in the mesh the 3D canvas shows.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from OCP.TopoDS import TopoDS_Face

from app.boards import Board
from app.boards.schema import Opening
from app.contracts.checks import CHECKS_CONTRACT
from app.standards import Standard, StandardsFile
from geometry import occ
from geometry.checks import ASSEMBLY_CHECKS, BOARD_CHECKS, CHECKS, CheckResult, run_checks
from geometry.fit_object import FitObject
from geometry.interface import BuildResult
from geometry.tessellate import Tessellation, tessellate
from geometry.untested import UNTESTED

Point = tuple[float, float, float]

# Not simulated: listed in every report as untested, so nobody mistakes silence for a pass.

# Board-derived checks: what each one covers, and the limit it judges against.
BOARD_COVERS = {
    "openings": "The {kind} for {component} is open from the component to the outside: an exact "
    "solid boolean of the enclosure with a column over the required opening, so a window that "
    "moved or turned away from its part, or a missing cut, fails.",
    "antenna_keepout": "No boss, insert, screw, peg or other internal feature within the minimum "
    "component clearance of the antenna keep-out of {component}. The shell walls are not counted.",
    "board_fit": "No material of {part} inside the board (outline, thickness and mounting holes).",
    "board_retention": "The board cannot move once the enclosure is closed: an edge pushed out, "
    "lifted or pressed down just past the fit tolerance runs into enclosure material.",
    "interference": "No material shared by {pair}: an exact solid boolean of the two parts "
    "(the board with every component body). Every pair of parts whose bounding boxes meet is "
    "checked; parts whose boxes are apart cannot interfere.",
}
OPENING_TITLES = {
    "sensor_window": "Sensor window clear",
    "vent": "Vent clear",
    "button": "Button reachable",
}
BOARD_TITLES = {
    "antenna_keepout": "Antenna keep-out",
    "board_fit": "Board fits",
    "board_retention": "Board held",
    "interference": "No interference",
}
# The limit each board and assembly check judges against, as (kind, value, unit) for the
# contract: a number with its unit, or for a spec, words that read after "requires" and name
# the value with its unit ("a stop within 0.2 mm"). `{limit:g}` in a spec is the check's own
# limit (`CheckResult.limit`, in mm). Every check without a standard needs an entry here: its
# result is shown under its title with this value, never the check's raw limit.
BOARD_REQUIRED = {
    "openings": ("max", 0.0, "mm3"),
    "antenna_keepout": ("spec", "no internal features", None),
    "board_fit": ("max", 0.0, "mm3"),
    "board_retention": ("spec", "a stop within {limit:g} mm", None),
    "interference": ("max", 0.0, "mm3"),
}
STANDARD_COVERS = {
    "overall_height": "The Z extent of the closed enclosure, all parts together.",
    "material": "The material each part is set up for, which the molding rules assume.",
    "wall_thickness": "Every face of every part, by rays cast into the material from sample "
    "points (the same samples as the wall-thickness map).",
    "draft_angle": "Every face that is not horizontal, against the mold pull along Z.",
    "component_clearance": "Exact distance from {component}'s body to every part of the enclosure.",
    "fit_tolerance": "Lid to base at the parting plane (outline and interference), and the walls "
    "of every opening against the size the board requires.",
    "object_fit": "Every part against the object it fits (the design's object parameters, "
    "as a solid): no part takes up any of its volume, and the closest is within the gap.",
    "insert_pockets": "Every boss has a pocket that takes the insert, clear and deep enough, "
    "with material all round it, and the insert part in it is seated: no interference with "
    "the boss, its sides within the fit tolerance of the pocket wall.",
}
UNITS = {"mm": "mm", "deg": "deg", "mm^2": "mm2", "mm^3": "mm3", "mm2": "mm2", "mm3": "mm3"}
_ID_CHARS = re.compile(r"[^A-Za-z0-9_.:\-]+")


def standard_for_check(standards: StandardsFile) -> dict[str, Standard]:
    """Each standard's check name -> the standard."""
    out: dict[str, Standard] = {}
    for s in standards.standards:
        if s.check in out:
            raise ValueError(f"standards {out[s.check].id} and {s.id} share the check {s.check}")
        out[s.check] = s
    return out


def check_names(standards: StandardsFile) -> list[str]:
    """Every check the report runs: each check of a rule that is on, then the board checks.

    Raises `KeyError` naming any standard whose `check:` is not implemented.
    """
    missing = sorted(f"{s.id} -> {s.check}" for s in standards.standards if s.check not in CHECKS)
    if missing:
        raise KeyError(f"standards name unimplemented checks: {', '.join(missing)}")
    return list(
        dict.fromkeys(
            [s.check for s in standards.enabled] + list(BOARD_CHECKS) + list(ASSEMBLY_CHECKS)
        )
    )


def check_report(
    board: Board | None,
    standards: StandardsFile,
    result: BuildResult,
    build_id: str,
    mesh: Tessellation | None = None,
    fit_object: FitObject | None = None,
) -> dict:
    """Run every check on a build and return the check-results report (a JSON-ready dict).

    `mesh` is the build's tessellation, whose face IDs the locations use; by default the build
    is tessellated here.
    """
    return checked_report(board, standards, result, build_id, mesh, fit_object)[1]


def checked_report(
    board: Board | None,
    standards: StandardsFile,
    result: BuildResult,
    build_id: str,
    mesh: Tessellation | None = None,
    fit_object: FitObject | None = None,
) -> tuple[list[CheckResult], dict]:
    """`check_report()`, with the check results it was made from: the report's first results
    are these, in the same order (the untested ones follow)."""
    names = check_names(standards)
    by_check = standard_for_check(standards)
    mesh = mesh if mesh is not None else tessellate(result)
    locator = Locator(board, result, mesh)
    raw = run_checks(board, standards, result, names, fit_object)

    counts = Counter(r.check for r in raw)
    results = []
    for r in raw:
        standard = by_check.get(r.check)
        if r.status == "not_applicable" and standard is not None:
            results.append(_not_applicable(r, standard))
            continue
        results.append(_result(r, board, standard, counts[r.check] > 1, locator))
    has_object = fit_object is not None
    for standard in standards.standards:
        if standard.enabled:
            continue
        if not standard.applies(has_board=board is not None, has_object=has_object):
            reason = standard.not_applicable_reason()
            r = CheckResult(standard.check, "not_applicable", detail=reason)
            results.append(_not_applicable(r, standard))
        else:
            results.append(_turned_off(standard, standards.name))
    for check, (title, covers) in UNTESTED.items():
        results.append(
            {
                "id": check,
                "check": check,
                "title": title,
                "standard": None,
                "component": None,
                "opening": None,
                "parts": None,
                "status": "untested",
                "measured": None,
                "required": None,
                "summary": f"{title} is not tested; review it by hand.",
                "covers": covers,
                "location": None,
            }
        )
    _unique_ids(results)
    summary = {
        s: sum(r["status"] == s for r in results)
        for s in ("pass", "fail", "untested", "not_applicable")
    }
    return raw, {
        "contract": CHECKS_CONTRACT,
        "build_id": build_id,
        "board": {"name": board.name, "revision": board.revision} if board else None,
        "results": results,
        "summary": summary,
    }


def _not_applicable(r: CheckResult, standard: Standard) -> dict:
    """A standard that does not apply to this design: no measurement, and the reason."""
    return {
        "id": r.check,
        "check": r.check,
        "title": standard.title,
        "standard": standard.id,
        "component": None,
        "opening": None,
        "parts": None,
        "status": "not_applicable",
        "measured": None,
        "required": None,
        "summary": f"{standard.title}: not applicable. {r.detail}",
        "reason": r.detail,
        "covers": STANDARD_COVERS.get(r.check, standard.description).format(
            component="each component", part="the enclosure", kind="opening"
        ),
        "location": None,
    }


def _turned_off(standard: Standard, set_name: str) -> dict:
    """A rule turned off in its set: not run, so not checked, with the reason."""
    reason = f"Turned off in {set_name}, so it was not checked."
    return {
        "id": standard.check,
        "check": standard.check,
        "title": standard.title,
        "standard": standard.id,
        "component": None,
        "opening": None,
        "parts": None,
        "status": "untested",
        "measured": None,
        "required": None,
        "summary": f"{standard.title}: not run. {reason}",
        "reason": reason,
        "covers": STANDARD_COVERS.get(standard.check, standard.description).format(
            component="each component", part="the enclosure", kind="opening"
        ),
        "location": None,
    }


# --- One result --------------------------------------------------------------------------------


def _opening(board: Board | None, opening_id: str | None) -> Opening | None:
    if board is None:
        return None
    return next((o for o in board.openings if o.id == opening_id), None)


def _component(board: Board | None, ref: str | None) -> str | None:
    """The reference designator, if `ref` names a component (results may say `board`)."""
    if board is None:
        return None
    return ref if ref and any(c.ref == ref for c in board.components) else None


def _subject(r: CheckResult) -> str:
    return r.subject or r.opening or r.component or r.feature or r.part or "result"


def _number(v):
    return round(v, 4) if isinstance(v, float) else v


def _result(
    r: CheckResult,
    board: Board | None,
    standard: Standard | None,
    many: bool,
    locator: Locator,
) -> dict:
    opening = _opening(board, r.opening)
    component = _component(board, r.component)
    unit = UNITS.get(r.unit) if r.unit else None
    measured_value = r.measured if r.measured is not None else "not measured"
    if isinstance(measured_value, float) and not math.isfinite(measured_value):
        measured_value = "not measured"
    measured = {"value": _number(measured_value)}
    if unit and not isinstance(measured_value, str):
        measured["unit"] = unit

    fmt = {
        "component": r.component or "the board",
        "part": r.part or "the enclosure",
        "pair": " and ".join(r.parts) if r.parts else "the parts",
        "kind": (opening.kind if opening else "opening").replace("_", " "),
    }
    if standard is not None:
        title = standard.title
        required = {"kind": standard.kind, "value": standard.value}
        if standard.unit:
            required["unit"] = standard.unit
        covers = STANDARD_COVERS.get(r.check, standard.description).format(**fmt)
    else:
        if r.check == "openings" and opening is not None:
            title = OPENING_TITLES.get(opening.kind, "Opening clear")
        else:
            title = BOARD_TITLES.get(r.check, r.check.replace("_", " ").capitalize())
        required = board_required(r)
        covers = BOARD_COVERS.get(r.check, r.detail or r.check).format(**fmt)

    location = locator.locate(r, opening)
    return {
        "id": f"{r.check}:{_subject(r)}" if many else r.check,
        "check": r.check,
        "title": title,
        "standard": standard.id if standard else None,
        "component": component,
        "opening": opening.id if opening else None,
        "parts": list(r.parts) if r.parts else None,
        "status": r.status,
        "measured": measured,
        "required": required,
        "summary": _summary(r, title, measured, required),
        "covers": covers,
        "location": location,
    }


def board_required(r: CheckResult) -> dict:
    """The contract's `required` for a result of a check without a standard (`BOARD_REQUIRED`).

    Raises `KeyError` for a check with no entry there, so a new check can't show its raw limit
    (or "None") as its requirement."""
    kind, value, unit = BOARD_REQUIRED[r.check]
    if isinstance(value, str) and "{limit" in value:
        if not isinstance(r.limit, int | float):
            raise ValueError(f"{r.check}: the requirement needs a numeric limit, not {r.limit!r}")
        value = value.format(limit=r.limit)
    required = {"kind": kind, "value": value}
    if unit:
        required["unit"] = unit
    return required


def _value_text(v: dict) -> str:
    value = v["value"]
    text = f"{value:g}" if isinstance(value, float | int) else str(value)
    return f"{text} {v['unit']}" if v.get("unit") else text


def _summary(r: CheckResult, title: str, measured: dict, required: dict) -> str:
    verdict = "Passes" if r.passed else "Fails"
    where = [x for x in (r.component, r.feature, r.part) if x]
    at = f" at {', '.join(dict.fromkeys(where))}" if where else ""
    if r.parts:
        at += f" between {r.parts[0]} and {r.parts[1]}"
    limit = _value_text(required)
    if required["kind"] == "tolerance":
        limit = f"±{limit}"
    elif required["kind"] in ("min", "max"):
        limit = f"{required['kind']} {limit}"
    detail = f" ({r.detail})" if r.detail else ""
    return f"{title}: {verdict}{at}, measured {_value_text(measured)} against {limit}{detail}."


def _unique_ids(results: list[dict]) -> None:
    seen: Counter[str] = Counter()
    for r in results:
        base = _ID_CHARS.sub("_", r["id"])
        seen[base] += 1
        r["id"] = base if seen[base] == 1 else f"{base}.{seen[base]}"


# --- Locations ---------------------------------------------------------------------------------


class Locator:
    """Turns a check's part, feature and point into a contract location.

    The location's face is the B-rep face nearest the check's point (exact distances), among
    the faces of the feature the check names, else among all faces of its part (or of every
    part). The location's point is the nearest point on that face, so it lies on the surface.
    """

    def __init__(self, board: Board | None, result: BuildResult, mesh: Tessellation) -> None:
        self.board = board
        self.result = result
        self.faces = {name: occ.faces(shape) for name, shape in result.parts.items()}
        self.ids: dict[tuple[str, int], str] = {}
        for name in result.parts:
            part = mesh.parts.get(name)
            if part is None or len(part.faces) != len(self.faces[name]):
                raise ValueError(f"the mesh does not match the build's part {name!r}")
            for index, face in enumerate(part.faces):
                self.ids[(name, index)] = face.id
        boxes = [occ.bbox(s) for s in result.parts.values()]
        lo = [min(b[0][i] for b in boxes) for i in range(3)]
        hi = [max(b[1][i] for b in boxes) for i in range(3)]
        self.centre: Point = tuple((a + b) / 2 for a, b in zip(lo, hi, strict=True))

    def _candidates(self, part: str | None, feature: str | None) -> list[tuple[str, int]]:
        parts = [part] if part in self.result.parts else list(self.result.parts)
        if feature:
            tagged = [
                (r.part, r.index)
                for name in parts
                for r in self.result.face_refs[name]
                if r.feature == feature
            ]
            if tagged:
                return tagged
        return [(name, i) for name in parts for i in range(len(self.faces[name]))]

    def _reference(self, r: CheckResult, opening: Opening | None) -> Point:
        if r.point is not None:
            return tuple(float(v) for v in r.point)
        if opening is not None:
            return (opening.position[0], opening.position[1], self.centre[2])
        ref = _component(self.board, r.component)
        if ref is not None and self.board is not None:
            c = self.board.component(ref)
            return (c.position[0], c.position[1], self.board.pcb.thickness)
        return self.centre

    def locate(self, r: CheckResult, opening: Opening | None) -> dict:
        point = self._reference(r, opening)
        (part, index), on_face = _nearest_face(
            point, self._candidates(r.part, r.feature), self.faces
        )
        return {
            "point": [round(v, 4) for v in on_face],
            "part": part,
            "faces": [self.ids[(part, index)]],
            "edges": [],
        }


def _box_distance(p: Point, face: TopoDS_Face) -> float:
    lo, hi = occ.bbox(face)
    return math.sqrt(sum(max(lo[i] - p[i], 0.0, p[i] - hi[i]) ** 2 for i in range(3)))


def _nearest_face(
    point: Point,
    candidates: Iterable[tuple[str, int]],
    faces: dict[str, list[TopoDS_Face]],
) -> tuple[tuple[str, int], Point]:
    """The candidate face nearest `point` and the nearest point on it (exact distances)."""
    vertex = occ.vertex(point)
    ordered = sorted((_box_distance(point, faces[p][i]), (p, i)) for p, i in candidates)
    best: tuple[float, tuple[str, int] | None, Point] = (math.inf, None, point)
    for bound, key in ordered:
        if bound > best[0]:
            break
        d, on_face = occ.distance(faces[key[0]][key[1]], vertex)
        if d < best[0]:
            best = (d, key, on_face)
    if best[1] is None:
        raise ValueError("no faces to locate a result on")
    return best[1], best[2]


__all__ = [
    "UNTESTED",
    "Locator",
    "check_names",
    "check_report",
    "checked_report",
    "standard_for_check",
]
