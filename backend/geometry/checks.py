"""Checks: measure a built enclosure against the standards and the board.

There is one check for every `check:` named in `standards.yaml`, plus the checks the board
itself implies (`BOARD_CHECKS`) and the checks between the parts of the assembly
(`ASSEMBLY_CHECKS`; see `geometry.assembly`). Each check returns one result per thing it
looked at, located at a component and/or a feature, so a failure points at what to fix; a
result between two parts names them (`parts`).

Every measurement is on the exact B-rep: solid booleans for overlaps and clear paths, exact
distances for clearance, ray-surface intersections for wall thickness, surface normals for
draft. Nothing is meshed.

The check runner (`geometry.report`) turns these results into the check-results
contract: face IDs, titles, what each check covers, and the always-untested strength, drop and
heat items.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import cached_property
from typing import Literal

from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
from OCP.TopAbs import TopAbs_VERTEX
from OCP.TopoDS import TopoDS_Shape

from app.boards import Board
from app.boards.schema import Component, Opening
from app.standards import StandardsFile
from geometry import occ
from geometry.analysis import SAMPLES, wall_samples
from geometry.assembly import (
    BOARD_PART,
    AssemblyPart,
    assembly,
    boxes_touch,
    component_box,
    neighbours,
    pcb_shape,
)
from geometry.fit_object import FitObject
from geometry.interface import INSERT, BuildResult
from geometry.params import CUT_KINDS, distance_to_polygon, opening_corners, opening_tags

Status = Literal["pass", "fail", "not_applicable"]

VOLUME_EPS = 1e-6  # mm^3: anything smaller is numerical noise from the booleans
LENGTH_EPS = 1e-6  # mm: two shapes this close touch
# A measured value within this of a min or max limit meets it. Measurements on the B-rep
# (ray lengths, distances, bounding boxes) carry noise of the kernel's tolerances, so a wall
# modelled at exactly 3.0 mm can measure 2.999; 0.005 mm is far below anything a printer or
# mold resolves, so it hides no real violation.
LENGTH_TOLERANCE = 0.005  # mm
ANGLE_TOLERANCE = 0.01  # degrees, for the same reason (face normals sampled on the B-rep)


def _at_least(value: float, limit: float, tolerance: float = LENGTH_TOLERANCE) -> bool:
    return value >= limit - tolerance


def _at_most(value: float, limit: float, tolerance: float = LENGTH_TOLERANCE) -> bool:
    return value <= limit + tolerance


@dataclass
class CheckResult:
    check: str
    status: Status
    measured: float | str | None = None
    limit: float | str | None = None
    unit: str | None = None
    component: str | None = None
    feature: str | None = None
    part: str | None = None
    point: tuple[float, float, float] | None = None
    detail: str = ""
    opening: str | None = None  # board opening ID, for results about one opening
    subject: str | None = None  # what the result is about, when a check runs more than once
    parts: tuple[str, str] | None = None  # the two assembly parts a result is between

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_json(self) -> dict:
        out = asdict(self)
        out["parts"] = list(self.parts) if self.parts else None
        return out


@dataclass
class CheckContext:
    board: Board | None
    standards: StandardsFile
    result: BuildResult
    cache: dict = field(default_factory=dict)
    fit_object: FitObject | None = None  # the object the design fits (`params.object`)

    def value(self, standard_id: str) -> float:
        return float(self.standards.get(standard_id).value)

    def tag_of(self, part: str, index: int) -> str:
        return self.result.face_refs[part][index].feature

    @cached_property
    def opening_tags(self) -> dict[str, str]:
        return opening_tags(self.board)

    @cached_property
    def pcb(self) -> TopoDS_Shape:
        """The bare board: its outline through its thickness, less the mounting holes."""
        return pcb_shape(self.board)

    @cached_property
    def assembly(self) -> dict[str, AssemblyPart]:
        """Every part of the build plus the board."""
        return assembly(self.result, self.board)

    @cached_property
    def enclosure(self) -> dict[str, TopoDS_Shape]:
        """The molded parts: what the molding standards and the shell rules apply to."""
        return self.result.enclosure or self.result.parts

    @cached_property
    def z_range(self) -> tuple[float, float]:
        boxes = [occ.bbox(s) for s in self.enclosure.values()]
        return min(b[0][2] for b in boxes), max(b[1][2] for b in boxes)


CheckFn = Callable[[CheckContext], list[CheckResult]]
CHECKS: dict[str, CheckFn] = {}
BOARD_CHECKS = ("openings", "antenna_keepout", "board_fit", "board_retention")
ASSEMBLY_CHECKS = ("interference",)


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return register


def _status(ok: bool) -> Status:
    return "pass" if ok else "fail"


def _component_box(board: Board, c: Component) -> TopoDS_Shape:
    return component_box(board, c)


def _opening_dict(o: Opening) -> dict:
    return {"x": o.position[0], "y": o.position[1], "rotation": o.rotation, "size": o.size}


def _opening_point(ctx: CheckContext, o: Opening) -> tuple[float, float, float]:
    """The opening's centre on the outside of the enclosure, on the board side it faces."""
    z0, z1 = ctx.z_range
    return (o.position[0], o.position[1], z1 if o.side == "top" else z0)


def _board_point(ctx: CheckContext) -> tuple[float, float, float]:
    """The middle of the board."""
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(ctx.pcb)
    return ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)


# --- Standards ---------------------------------------------------------------------------------


@check("overall_height")
def overall_height(ctx: CheckContext) -> list[CheckResult]:
    limit = ctx.value("max_height")
    z0, z1 = ctx.z_range
    # Located at the top of the tallest part, where any extra height would have to come off.
    part, ((x0, y0, _), (x1, y1, _)) = max(
        ((name, occ.bbox(s)) for name, s in ctx.enclosure.items()), key=lambda i: i[1][1][2]
    )
    return [
        CheckResult(
            "overall_height",
            _status(_at_most(z1 - z0, limit)),
            round(z1 - z0, 4),
            limit,
            "mm",
            part=part,
            point=((x0 + x1) / 2, (y0 + y1) / 2, z1),
            detail=f"from z {z0:.3f} to {z1:.3f}",
        )
    ]


@check("material")
def material(ctx: CheckContext) -> list[CheckResult]:
    required = str(ctx.standards.get("material").value)
    out = []
    for part in ctx.enclosure:
        given = ctx.result.materials.get(part, "")
        ok = given.strip().lower() == required.strip().lower()
        out.append(CheckResult("material", _status(ok), given or None, required, part=part))
    return out


@check("wall_thickness")
def wall_thickness(ctx: CheckContext) -> list[CheckResult]:
    """Thinnest wall per part: rays cast from points on every face, square into the material.

    The same samples as the wall-thickness analysis map (`geometry.analysis`), so the map's thin
    spots are exactly this check's failures."""
    limit = ctx.value("min_wall")
    out = []
    for part, shape in ctx.enclosure.items():
        rays = occ.RayCaster(shape)
        worst = (math.inf, None, None)
        for index, face in enumerate(occ.faces(shape)):
            for s in wall_samples(shape, face, rays, balls=False):
                if s.ray is not None and s.ray < worst[0]:
                    worst = (s.ray, index, s.point)
        t, index, p = worst
        out.append(
            CheckResult(
                "wall_thickness",
                _status(_at_least(t, limit)),
                round(t, 4),
                limit,
                "mm",
                feature=ctx.tag_of(part, index) if index is not None else None,
                part=part,
                point=p,
            )
        )
    return out


@check("draft_angle")
def draft_angle(ctx: CheckContext) -> list[CheckResult]:
    """Smallest draft per part, over every face that is not horizontal (pull direction z)."""
    limit = ctx.value("min_draft")
    out = []
    for part, shape in ctx.enclosure.items():
        worst = (90.0, None, None)
        for index, face in enumerate(occ.faces(shape)):
            for p, n in occ.face_samples(face, SAMPLES):
                a = occ.draft_degrees(n)
                if a < worst[0]:
                    worst = (a, index, p)
        a, index, p = worst
        out.append(
            CheckResult(
                "draft_angle",
                _status(_at_least(a, limit, ANGLE_TOLERANCE)),
                round(a, 4),
                limit,
                "deg",
                feature=ctx.tag_of(part, index) if index is not None else None,
                part=part,
                point=p,
            )
        )
    return out


@check("component_clearance")
def component_clearance(ctx: CheckContext) -> list[CheckResult]:
    limit = ctx.value("min_component_clearance")
    out = []
    for c in ctx.board.components:
        box = _component_box(ctx.board, c)
        best = (math.inf, None, None)
        for part, shape in ctx.result.parts.items():
            d, p = occ.distance(shape, box)
            if occ.overlap(shape, box) > VOLUME_EPS:
                d = 0.0
            if d < best[0]:
                best = (d, part, p)
        d, part, p = best
        out.append(
            CheckResult(
                "component_clearance",
                _status(_at_least(d, limit)),
                round(d, 4),
                limit,
                "mm",
                component=c.ref,
                part=part,
                point=p,
                parts=(part, BOARD_PART) if part else None,
            )
        )
    return out


def _parting_pairs(ctx: CheckContext) -> list[tuple[str, str, float]]:
    """(lower part, upper part, z) for parts that meet at a horizontal parting plane."""
    boxes = {name: occ.bbox(s) for name, s in ctx.enclosure.items()}
    pairs = []
    for lower, (_, lmax) in boxes.items():
        for upper, (umin, _) in boxes.items():
            if lower != upper and abs(lmax[2] - umin[2]) < 1e-3:
                pairs.append((lower, upper, (lmax[2] + umin[2]) / 2))
    return pairs


def _section_extent(shape: TopoDS_Shape, z: float) -> tuple[float, float, float, float] | None:
    section = BRepAlgoAPI_Section(shape, gp_Pln(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)))
    section.Build()
    if not section.IsDone() or not occ.subshapes(section.Shape(), TopAbs_VERTEX):
        return None
    (x0, y0, _), (x1, y1, _) = occ.bbox(section.Shape())
    return x0, y0, x1, y1


@check("fit_tolerance")
def fit_tolerance(ctx: CheckContext) -> list[CheckResult]:
    """Mating parts line up at the parting plane, and openings match their components."""
    tol = ctx.value("fit_tolerance")
    out = []
    eps = 1e-3
    for lower, upper, z in _parting_pairs(ctx):
        a = _section_extent(ctx.result.parts[lower], z - eps)
        b = _section_extent(ctx.result.parts[upper], z + eps)
        interference = occ.overlap(ctx.result.parts[lower], ctx.result.parts[upper])
        if a is None or b is None:
            dev = math.inf
        else:
            dev = max(abs(p - q) for p, q in zip(a, b, strict=True))
        out.append(
            CheckResult(
                "fit_tolerance",
                _status(_at_most(dev, tol) and interference <= VOLUME_EPS),
                round(dev, 4),
                tol,
                "mm",
                feature=upper,
                part=upper,
                detail=f"{upper} on {lower}: outline deviation, interference "
                f"{interference:.3g} mm^3",
                parts=(lower, upper),
            )
        )
    for o in ctx.board.openings:
        if o.kind not in CUT_KINDS:
            continue
        tag = ctx.opening_tags[o.id]
        polygon = opening_corners(_opening_dict(o))
        faces = ctx.result.faces_of(tag)
        if not faces:
            out.append(
                CheckResult(
                    "fit_tolerance",
                    "fail",
                    "no opening",
                    tol,
                    "mm",
                    o.component,
                    None,
                    point=_opening_point(ctx, o),
                    detail=f"no {o.kind} cut for {o.component}",
                    opening=o.id,
                )
            )
            continue
        dev, worst, where = 0.0, None, None
        for part, face in faces:
            points = [p for p, _ in occ.face_samples(face, 4)]
            points += [occ.vertex_point(v) for v in occ.subshapes(face, TopAbs_VERTEX)]
            for p in points:
                d = distance_to_polygon(p[0], p[1], polygon)
                if d > dev:
                    dev, worst, where = d, p, part
        out.append(
            CheckResult(
                "fit_tolerance",
                _status(_at_most(dev, tol)),
                round(dev, 4),
                tol,
                "mm",
                o.component,
                tag,
                part=where,
                point=worst,
                detail=f"{o.kind} walls against the required {o.size[0]} x {o.size[1]} mm",
                opening=o.id,
            )
        )
    return out


@check("insert_pockets")
def insert_pockets(ctx: CheckContext) -> list[CheckResult]:
    """Every boss has a pocket that takes the standard heat-set insert, and holds it; and every
    part the build calls an insert is one: it sits in an insert pocket and is made of the
    standard's insert material. (Without that, calling a molded part an insert would take it
    out of the molding and lid-to-base checks unnoticed.)"""
    standard = ctx.standards.get("heat_set_inserts")
    dims = standard.dimensions or {}
    tol = ctx.value("fit_tolerance")
    hole, length = dims["hole_diameter"], dims["insert_length"]
    bosses = [t for t in ctx.result.tags if t.startswith("boss_")]
    out = []
    held_by: dict[str, str] = {}  # insert part -> the part whose pocket it is in
    if not bosses:
        out.append(CheckResult("insert_pockets", "fail", 0, ">= 1 boss", detail="no bosses"))
        return out + _insert_parts(ctx, standard.material, held_by)
    for boss in bosses:
        tag = "insert_" + boss.removeprefix("boss_")
        faces = ctx.result.faces_of(tag)
        if not faces:
            out.append(
                CheckResult(
                    "insert_pockets",
                    "fail",
                    "no insert pocket",
                    hole,
                    "mm",
                    None,
                    boss,
                    detail=f"boss {boss} has no pocket",
                    subject=tag,
                )
            )
            continue
        part = faces[0][0]
        shape = ctx.result.parts[part]
        boxes = [occ.bbox(f) for _, f in faces]
        x = (min(b[0][0] for b in boxes) + max(b[1][0] for b in boxes)) / 2
        y = (min(b[0][1] for b in boxes) + max(b[1][1] for b in boxes)) / 2
        z0, z1 = min(b[0][2] for b in boxes), max(b[1][2] for b in boxes)
        # The insert goes in from the open end, which is the end at the part's outside.
        mouth_low = occ.overlap(shape, occ.cylinder(x, y, 0.1, z0 - 0.2, z0)) < VOLUME_EPS
        a, b = (z0, z0 + length) if mouth_low else (z1 - length, z1)
        clear = occ.overlap(shape, occ.cylinder(x, y, hole / 2 - tol, a, b))
        ring_outer = occ.cylinder(x, y, hole / 2 + tol + 0.5, a, b)
        ring_inner = occ.cylinder(x, y, hole / 2 + tol, a, b)
        ring = occ.volume(ring_outer) - occ.volume(ring_inner)
        held = occ.overlap(shape, ring_outer) - occ.overlap(shape, ring_inner)
        depth = z1 - z0
        ok = _at_least(depth, length) and clear <= VOLUME_EPS and held >= ring - 1e-3
        detail = (
            f"boss {boss}: obstruction {clear:.3g} mm^3, "
            f"material around the pocket {held / ring:.0%}"
        )
        insert = _insert_in(ctx, x, y, hole / 2)
        if insert is not None:
            held_by[insert] = part
            seated, seat = _insert_seat(ctx, insert, shape, (z0, z1), tol)
            ok = ok and seated
            detail += f"; {insert}: {seat}"
        out.append(
            CheckResult(
                "insert_pockets",
                _status(ok),
                round(depth, 4),
                f"{standard.value}: {hole} mm hole ±{tol}, {length} mm deep",
                "mm",
                feature=tag,
                part=part,
                point=(x, y, a),
                detail=detail,
                parts=(part, insert) if insert else None,
            )
        )
    return out + _insert_parts(ctx, standard.material, held_by)


def _insert_parts(
    ctx: CheckContext, required: str | None, held_by: dict[str, str]
) -> list[CheckResult]:
    """One result per insert part: it is in an insert pocket and of the insert material."""
    out = []
    for name, shape in ctx.result.parts.items():
        if ctx.result.kind(name) != INSERT:
            continue
        given = ctx.result.materials.get(name, "")
        material_ok = required is None or given.strip().lower() == required.strip().lower()
        holder = held_by.get(name)
        problems = []
        if holder is None:
            problems.append("not in any insert pocket")
        if not material_ok:
            problems.append(f"material {given or 'none'}, not {required}")
        (x0, y0, z0), (x1, y1, z1) = occ.bbox(shape)
        out.append(
            CheckResult(
                "insert_pockets",
                _status(not problems),
                given or None,
                required,
                part=name,
                point=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                detail=f"insert part {name}: "
                + ("; ".join(problems) if problems else f"in a pocket of {holder}"),
                subject=name,
                parts=(holder, name) if holder else None,
            )
        )
    return out


def _insert_in(ctx: CheckContext, x: float, y: float, radius: float) -> str | None:
    """The insert part whose axis is inside the pocket at (x, y), if the build has one."""
    for name, shape in ctx.result.parts.items():
        if ctx.result.kind(name) != INSERT:
            continue
        (x0, y0, _), (x1, y1, _) = occ.bbox(shape)
        if math.hypot((x0 + x1) / 2 - x, (y0 + y1) / 2 - y) < radius:
            return name
    return None


def _insert_seat(
    ctx: CheckContext, insert: str, holder: TopoDS_Shape, pocket: tuple[float, float], tol: float
) -> tuple[bool, str]:
    """Is an insert seated in its pocket: no interference with the part that holds it, its
    sides within the fit tolerance of the pocket wall, and all of it inside the pocket."""
    shape = ctx.result.parts[insert]
    interference = occ.overlap(shape, holder)
    gap = 0.0
    for face in occ.faces(shape):
        for p, n in occ.face_samples(face, 3):
            if abs(n[2]) > 0.99:  # the ends: the side walls are what hold it
                continue
            gap = max(gap, occ.distance(occ.vertex(p), holder)[0])
    (_, _, b0), (_, _, b1) = occ.bbox(shape)
    inside = _at_least(b0, pocket[0]) and _at_most(b1, pocket[1])
    ok = interference <= VOLUME_EPS and _at_most(gap, tol) and inside
    text = f"interference {interference:.3g} mm^3, gap to the pocket wall {gap:.3f} mm" + (
        "" if inside else ", sticks out of the pocket"
    )
    return ok, text


# --- Board -------------------------------------------------------------------------------------


@check("openings")
def openings(ctx: CheckContext) -> list[CheckResult]:
    """Every opening the board needs is clear from the component to the outside.

    Sensor windows see out, vents breathe, and buttons can be reached through their opening.
    """
    z0, z1 = ctx.z_range
    out = []
    for o in ctx.board.openings:
        if o.kind not in CUT_KINDS:
            continue
        c = ctx.board.component(o.component)
        polygon = opening_corners(_opening_dict(o))
        if o.side == "top":
            column = occ.prism(polygon, ctx.board.pcb.thickness + c.height + 1e-3, z1 + 1)
        else:
            column = occ.prism(polygon, z0 - 1, -c.height - 1e-3)
        blocked, part, hit = 0.0, None, None
        for name, shape in ctx.result.parts.items():
            common = occ.common(shape, column)
            v = occ.volume(common)
            if v > blocked:
                blocked, part, hit = v, name, common
        tag = ctx.opening_tags[o.id]
        ok = blocked <= VOLUME_EPS
        if ok:
            feature, point = tag if ctx.result.faces_of(tag) else None, _opening_point(ctx, o)
        else:
            # Located on the material in the way, on the outside, so it points at what to cut.
            (x0, y0, b0), (x1, y1, b1) = occ.bbox(hit)
            features = _features_touching(ctx, part, hit)
            feature = features[0] if features else None
            point = ((x0 + x1) / 2, (y0 + y1) / 2, b1 if o.side == "top" else b0)
        out.append(
            CheckResult(
                "openings",
                _status(ok),
                round(blocked, 4),
                0.0,
                "mm^3",
                o.component,
                feature,
                part,
                point,
                f"{o.kind}: material in the way",
                opening=o.id,
            )
        )
    return out


@check("antenna_keepout")
def antenna_keepout(ctx: CheckContext) -> list[CheckResult]:
    """No hardware or internal features (bosses, insert pockets, screws) over an antenna."""
    clearance = ctx.value("min_component_clearance")
    z0, z1 = ctx.z_range
    shells = set(ctx.enclosure)
    out = []
    for o in ctx.board.openings:
        if o.kind != "antenna_keepout":
            continue
        zone = occ.prism(opening_corners(_opening_dict(o), clearance), z0 - 1, z1 + 1)
        intruders = []
        for f in ctx.result.features:
            if f.tag in shells or f.tag.startswith("opening_"):
                continue
            for _, face in ctx.result.faces_of(f.tag):
                d, _ = occ.distance(face, zone)
                if d <= LENGTH_EPS:
                    intruders.append(f.tag)
                    break
        out.append(
            CheckResult(
                "antenna_keepout",
                _status(not intruders),
                ", ".join(sorted(set(intruders))) or "no features",
                None,
                component=o.component,
                feature=intruders[0] if intruders else None,
                point=(o.position[0], o.position[1], ctx.board.pcb.thickness),
                detail="features over the antenna keep-out",
                opening=o.id,
            )
        )
    return out


@check("board_fit")
def board_fit(ctx: CheckContext) -> list[CheckResult]:
    """The board itself fits: no part of the enclosure runs into the PCB."""
    out = []
    for part, shape in ctx.enclosure.items():
        v = occ.overlap(shape, ctx.pcb)
        out.append(
            CheckResult(
                "board_fit",
                _status(v <= VOLUME_EPS),
                round(v, 4),
                0.0,
                "mm^3",
                component="board",
                part=part,
                point=_board_point(ctx),
                detail="enclosure material inside the board",
                parts=(part, BOARD_PART),
            )
        )
    return out


def _features_touching(ctx: CheckContext, part: str, solid: TopoDS_Shape) -> list[str]:
    """Features of a part whose faces bound a solid (the volume a moved board ran into)."""
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(solid)
    tags = []
    shape = ctx.result.parts[part]
    for index, face in enumerate(occ.faces(shape)):
        (a0, b0, c0), (a1, b1, c1) = occ.bbox(face)
        if a0 > x1 or a1 < x0 or b0 > y1 or b1 < y0 or c0 > z1 or c1 < z0:
            continue
        if occ.distance(face, solid)[0] <= 1e-6:
            tags.append(ctx.tag_of(part, index))
    # Retention features first, then the shells they belong to.
    shells = set(ctx.enclosure)
    return sorted(set(tags), key=lambda t: (t in shells, t))


@check("board_retention")
def board_retention(ctx: CheckContext) -> list[CheckResult]:
    """The board cannot move in the closed enclosure.

    Each edge of the board inside the enclosure is pushed just past the fit tolerance: outward
    in the board's plane (the whole board slides), and up and down (the board tilts about the
    opposite edge). Something must stop it. For a tilt only the half of the board nearer the
    pushed edge counts, so a feature at the other end cannot hold the edge it is not near.
    Every move is an exact rigid transform and every collision an exact solid boolean.
    """
    # How far each edge moves; the results' limit (mm), while `measured` is the volume it hits.
    travel = ctx.value("fit_tolerance") + 1e-3
    t = ctx.board.pcb.thickness
    pcb = ctx.pcb
    boxes = [occ.bbox(s) for s in ctx.enclosure.values()]
    lo = (min(b[0][0] for b in boxes), min(b[0][1] for b in boxes))
    hi = (max(b[1][0] for b in boxes), max(b[1][1] for b in boxes))
    inside = occ.common(pcb, occ.prism([lo, (hi[0], lo[1]), hi, (lo[0], hi[1])], -1.0, t + 1))
    if occ.volume(inside) <= VOLUME_EPS:
        return [
            CheckResult(
                "board_retention",
                "fail",
                "board not inside",
                round(travel, 3),
                "mm^3",
                component="board",
                point=_board_point(ctx),
                detail="the board is not inside the enclosure",
            )
        ]
    (ex0, ey0, _), (ex1, ey1, _) = occ.bbox(inside)
    zmin, zmax = ctx.z_range
    mid_z = t / 2
    out = []

    def stopped(moved: TopoDS_Shape) -> tuple[float, list[str]]:
        total, tags = 0.0, []
        for part, shape in ctx.result.parts.items():
            hit = occ.common(shape, moved)
            v = occ.volume(hit)
            if v > VOLUME_EPS:
                total += v
                tags += _features_touching(ctx, part, hit)
        return total, tags

    def result(edge: str, motion: str, moved: TopoDS_Shape, point) -> None:
        v, tags = stopped(moved)
        shells = set(ctx.enclosure)
        stops = [x for x in dict.fromkeys(tags) if x not in shells] or list(dict.fromkeys(tags))
        out.append(
            CheckResult(
                "board_retention",
                _status(v > VOLUME_EPS),
                round(v, 4),
                round(travel, 3),
                "mm^3",
                component="board",
                feature=stops[0] if stops else None,
                point=point,
                detail=f"{edge} edge {motion}: "
                + (f"stopped by {', '.join(stops)}" if stops else "nothing stops it"),
                subject=f"{edge}.{motion.split()[0]}",
            )
        )

    # (name, outward unit vector, coordinate of the edge, coordinate of the opposite edge)
    edges = [
        ("-x", (-1, 0), ex0, ex1),
        ("+x", (1, 0), ex1, ex0),
        ("-y", (0, -1), ey0, ey1),
        ("+y", (0, 1), ey1, ey0),
    ]
    cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
    for name, (ux, uy), at, opposite in edges:
        point = (at if ux else cx, at if uy else cy, mid_z)
        moved = occ.translated(pcb, (ux * travel, uy * travel, 0.0))
        result(name, f"pushed out {travel:.3f} mm", moved, point)

        # Half of the board nearer this edge, through the enclosure's full height.
        if ux:
            a, b = sorted((at, (at + opposite) / 2))
            half = occ.prism([(a, ey0), (b, ey0), (b, ey1), (a, ey1)], zmin - 1, zmax + 1)
            axis = (0.0, 1.0, 0.0)
            pivot = (opposite, cy, mid_z)
        else:
            a, b = sorted((at, (at + opposite) / 2))
            half = occ.prism([(ex0, a), (ex1, a), (ex1, b), (ex0, b)], zmin - 1, zmax + 1)
            axis = (1.0, 0.0, 0.0)
            pivot = (cx, opposite, mid_z)
        span = abs(at - opposite)
        angle = math.degrees(math.asin(min(1.0, travel / span)))
        for direction, label in ((1, "lifted"), (-1, "pressed down")):
            # Right-hand rotation: about +y a positive angle lifts the -x side; about +x it
            # lifts the +y side.
            sign = -direction * ux if ux else direction * uy
            moved = occ.common(occ.rotated(pcb, pivot, axis, sign * angle), half)
            result(name, f"{label} {travel:.3f} mm about the opposite edge", moved, point)
    return out


# --- Assembly ----------------------------------------------------------------------------------


def _overlap(a: TopoDS_Shape, b: TopoDS_Shape) -> tuple[float, list[TopoDS_Shape]]:
    """Shared volume of two parts, solid by solid (the board's component bodies may touch each
    other, so each is intersected on its own), and the shared pieces."""
    total, pieces = 0.0, []
    for sa in occ.solids(a) or [a]:
        for sb in occ.solids(b) or [b]:
            if not boxes_touch(sa, sb):
                continue
            common = occ.common(sa, sb)
            v = occ.volume(common)
            if v > VOLUME_EPS:
                total += v
                pieces.append(common)
    return total, pieces


@check("interference")
def interference(ctx: CheckContext) -> list[CheckResult]:
    """No two parts of the assembly share material: base, lid, inserts and the board (with its
    component bodies), every pair whose bounding boxes meet, by exact solid booleans. Pairs
    whose boxes are apart cannot share material."""
    parts = ctx.assembly
    out = []
    for a, b in neighbours(parts):
        v, pieces = _overlap(parts[a].shape, parts[b].shape)
        # Located on a built part (the board is drawn from the board data, not the mesh).
        on = a if a in ctx.result.parts else b
        feature, point = None, None
        if pieces:
            worst = max(pieces, key=occ.volume)
            (x0, y0, z0), (x1, y1, z1) = occ.bbox(worst)
            point = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
            if on in ctx.result.parts:
                touching = _features_touching(ctx, on, worst)
                feature = touching[0] if touching else None
        else:
            (x0, y0, z0), (x1, y1, z1) = occ.bbox(parts[on].shape)
            point = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        out.append(
            CheckResult(
                "interference",
                _status(v <= VOLUME_EPS),
                round(v, 4),
                0.0,
                "mm^3",
                feature=feature,
                part=on,
                point=point,
                detail=f"material shared by {a} and {b}",
                subject=f"{a}--{b}",
                parts=(a, b),
            )
        )
    return out


@check("object_fit")
def object_fit(ctx: CheckContext) -> list[CheckResult]:
    """The part fits the object it is made around (`params.object`), by exact solid booleans
    and distances: no part takes up any of the object's volume, and the closest part is
    within the standard's gap of it."""
    fit = ctx.fit_object
    assert fit is not None, "object_fit runs only on a design with an object"
    limit = ctx.value("object_fit")
    obj = fit.solid()
    parts = ctx.result.parts
    overlaps = {name: occ.common(shape, obj) for name, shape in parts.items()}
    volumes = {name: occ.volume(shape) for name, shape in overlaps.items()}
    worst = max(volumes, key=lambda n: volumes[n]) if volumes else None
    if worst is not None and volumes[worst] > VOLUME_EPS:
        (x0, y0, z0), (x1, y1, z1) = occ.bbox(overlaps[worst])
        return [
            CheckResult(
                "object_fit",
                "fail",
                "cuts into the object",
                limit,
                "mm",
                part=worst,
                point=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                detail=f"{worst} takes up {volumes[worst]:.3g} mm^3 of {fit.describe()}",
            )
        ]
    gaps = {name: occ.distance(shape, obj) for name, shape in parts.items()}
    closest = min(gaps, key=lambda n: gaps[n][0])
    gap, point = gaps[closest]
    gap = round(gap, 4)
    return [
        CheckResult(
            "object_fit",
            _status(gap <= limit + LENGTH_EPS),
            gap,
            limit,
            "mm",
            part=closest,
            point=point,
            detail=f"{closest} is {gap:g} mm from {fit.describe()} at its closest",
        )
    ]


def run_checks(
    board: Board | None,
    standards: StandardsFile,
    result: BuildResult,
    names: list[str] | None = None,
    fit_object: FitObject | None = None,
) -> list[CheckResult]:
    """Run the named checks (default: the check of every rule that is on, and every board check).

    A design without a board gets no board checks, and each standard that applies only to
    designs with a board gets one "not_applicable" result with the reason instead of running.
    So does a standard for designs that fit an object (`object_fit`) when `fit_object` is None.
    """
    if names is None:
        names = list(
            dict.fromkeys(
                [s.check for s in standards.enabled] + list(BOARD_CHECKS) + list(ASSEMBLY_CHECKS)
            )
        )
    missing = [n for n in names if n not in CHECKS]
    if missing:
        raise KeyError(f"no check implements {missing}")
    ctx = CheckContext(board, standards, result, fit_object=fit_object)
    skipped = {
        s.check: s
        for s in standards.standards
        if not s.applies(has_board=board is not None, has_object=fit_object is not None)
    }
    out: list[CheckResult] = []
    for name in names:
        if board is None and name in BOARD_CHECKS:
            continue
        if name in skipped:
            reason = skipped[name].not_applicable_reason()
            out.append(CheckResult(name, "not_applicable", detail=reason))
            continue
        out.extend(CHECKS[name](ctx))
    return out


__all__ = [
    "ASSEMBLY_CHECKS",
    "BOARD_CHECKS",
    "CHECKS",
    "CheckContext",
    "CheckResult",
    "run_checks",
]
