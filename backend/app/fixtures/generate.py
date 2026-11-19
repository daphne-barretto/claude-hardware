"""Generate the contract fixtures in `fixtures/` from `demos/<board>/` and `standards.yaml`.

The scenario: the sample enclosure is designed for the older board revision named in
`demos/<board>/CHANGES.md`, then checked against the newer one, so the check results fail at
every difference listed there. The recorded session is the short run that reports this, ends with a
build and asks how to proceed.

    cd backend && .venv/bin/python -m app.fixtures          # write fixtures/
    cd backend && .venv/bin/python -m app.fixtures --check  # fail if fixtures/ is out of date
"""

from __future__ import annotations

import json
import math
from functools import cache
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS_Compound

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import Change, enclosure_changes, parse_changes_md
from app.contracts.checks import STATUSES
from app.contracts.common import FIXTURES_DIR
from app.fixtures import conflict as conflict_fixture
from app.fixtures.geometry import (
    CUT_KINDS,
    Design,
    Enclosure,
    Face,
    Footprint,
    Part,
    Rect,
    area,
    inside_convex,
    opening_tags,
    rect_of,
)
from app.fixtures.gltf import PartMesh, write_gltf
from app.session_store import MemoryStandardsStore
from app.standards import StandardsFile, load_standards
from app.standards_catalog import kinds
from app.standards_sets import describe
from geometry import DEFAULT_BACKEND, enclosure_parameters, get_backend, occ
from geometry.analysis import LEGEND_FACTOR, THICK_FACTOR
from geometry.checks import CheckResult, _component_box
from geometry.report import board_required

BUILD_ID = "build-1"
# The existing enclosure's build on the board it was designed for: the revision the session
# starts from, which the compare view puts next to the new one.
PREVIOUS_BUILD_ID = "build-0"
SESSION_TITLE = "Sample session"
CREATED_AT = "2026-01-01T00:00:00Z"  # fixed so the fixtures are reproducible
TOKEN_DELAY_MS = 30
STEP_DELAY_MS = 400
SOURCE_EXAMPLE = "enclosure"


# --- Inputs ---------------------------------------------------------------------------------


def boards() -> tuple[Board, Board, list[Change]]:
    """The older and newer revisions named in CHANGES.md, and their differences."""
    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    old, new = load_board(BOARDS_DIR / doc.old_file), load_board(BOARDS_DIR / doc.new_file)
    return old, new, enclosure_changes(old, new)


@cache
def case_extents() -> tuple[tuple[float, float], ...]:
    """Bounding box of the existing case (`demos/<board>/*-case.step`) in its own axes, to
    0.1 mm."""
    from build123d import import_step

    (path,) = BOARDS_DIR.glob("*-case.step")
    shape = import_step(path)
    case = next((c for c in shape.children if c.label == "Case"), None)
    if case is None:
        case = max(shape.children, key=lambda c: sum(s.volume for s in c.solids()))
    box = case.bounding_box()
    return tuple(
        (round(lo, 1), round(hi, 1))
        for lo, hi in ((box.min.X, box.max.X), (box.min.Y, box.max.Y), (box.min.Z, box.max.Z))
    )


def board_box(board: Board) -> tuple[float, float, float, float]:
    xs = [p[0] for p in board.pcb.outline]
    ys = [p[1] for p in board.pcb.outline]
    return min(xs), max(xs), min(ys), max(ys)


def design_for(board: Board, standards: StandardsFile) -> Design:
    """The sample enclosure for a board: the existing case's box, the team's standards."""
    std = {s.id: s.value for s in standards.standards}
    wall = float(std["min_wall"]) * 1.25  # headroom over the minimum, square to drafted faces
    draft = float(std["min_draft"]) + 0.5
    clearance = float(std["min_component_clearance"])

    # demos/<board>/README.md: case x = top edge y - board y, case y = board x - centre line.
    (cx0, cx1), (cy0, cy1), (z0, z1) = case_extents()
    bx0, bx1, _, by1 = board_box(board)
    centre = (bx0 + bx1) / 2
    z1 = min(z1, z0 + float(std["max_height"]))
    footprint = Footprint(centre + cy0, centre + cy1, by1 - cx1, by1 - cx0, radius=2 * wall)

    design = Design(
        footprint=footprint,
        z_bottom=z0,
        z_top=z1,
        parting_z=board.pcb.thickness,
        wall=wall,
        draft_deg=draft,
        clearance=clearance,
        fit=float(std["fit_tolerance"]),
        material=str(std["material"]),
        bosses=[],
        openings=[],
    )
    enc = Enclosure(design)
    lid_inner, _ = footprint.ring(enc.inner_inset(enc.inner_cap_z("lid")))
    # Each boss takes the standard heat-set insert: a pocket the insert's hole size, as deep
    # as the insert is long, with a wall around it.
    inserts = standards.get("heat_set_inserts").dimensions or {}
    pocket_radius, pocket_depth = inserts["hole_diameter"] / 2, inserts["insert_length"]
    if z1 - board.pcb.thickness - pocket_depth <= 0:
        raise ValueError("the insert pocket goes through the lid's top")
    for n, hole in enumerate(board.mounting_holes, 1):
        radius = max(hole.diameter / 2, pocket_radius) + enc.wall_h
        if not inside_convex(lid_inner, hole.position):
            raise ValueError(f"mounting hole {hole.id} is outside the case")
        design.bosses.append(
            {
                "tag": f"boss_{n}",
                "insert": f"insert_{n}",
                "hole": hole.id,
                "x": hole.position[0],
                "y": hole.position[1],
                "radius": radius,
                "pocket_radius": pocket_radius,
                "pocket_depth": pocket_depth,
            }
        )
    tags = opening_tags(board)
    for o in board.openings:
        rect = rect_of(o.position, o.rotation, o.size, grow=clearance)
        design.openings.append(
            {
                "id": o.id,
                "tag": tags.get(o.id),
                "component": o.component,
                "kind": o.kind,
                "side": o.side,
                "rotation": o.rotation,
                "rect": rect,
                "cut": o.kind in CUT_KINDS,
            }
        )
    return design


# --- Analysis --------------------------------------------------------------------------------


def surface_point(part: Part, face_id: str) -> tuple[float, float, float]:
    """A point on a face: the centroid of its largest triangle."""
    face = next(f for f in part.faces if f.id == face_id)
    tri = max(face.triangles, key=lambda t: area(*t))
    return tuple(round(sum(p[k] for p in tri) / 3, 3) for k in range(3))


def face_thickness(enc: Enclosure, part: Part, face_id: str) -> float:
    """Wall thickness behind a face, from the design (the sample's walls are uniform)."""
    d = enc.d
    feature, local = face_id.split(".", 1)
    boss = next((b for b in d.bosses if feature in (b["tag"], b["insert"])), None)
    if boss is not None:
        # Around the pocket the boss is thinnest at its free end, where it is narrowest and the
        # pocket widest.
        around = boss["radius"] - boss["pocket_radius"]
        if feature == boss["insert"]:
            return around if local == "wall" else d.z_top - d.parting_z - boss["pocket_depth"]
        length = abs(enc.inner_cap_z("lid") - d.parting_z)
        return around if local == "side" else length + d.wall
    return d.wall


def draft_of(normal: tuple[float, float, float]) -> float | None:
    """Draft angle against a Z pull; None for faces square to the pull (draft does not apply)."""
    nz = min(1.0, abs(normal[2]))
    if nz > 0.999:
        return None
    return math.degrees(math.asin(nz))


def maps_for(enc: Enclosure, meshes: list[PartMesh], standards: StandardsFile) -> dict:
    out = {}
    for map_type, standard_id, unit in (
        ("wall_thickness", "min_wall", "mm"),
        ("draft", "min_draft", "deg"),
    ):
        standard = standards.get(standard_id)
        limit = float(standard.value)
        parts, flagged = [], []
        for m in meshes:
            face_values: dict[int, float | None] = {}
            if map_type == "wall_thickness":
                for f, face in enumerate(m.part.faces):
                    face_values[f] = round(face_thickness(enc, m.part, face.id), 3)
                values = [face_values[f] for f in m.face_index]
            else:
                values = [None if (v := draft_of(n)) is None else round(v, 3) for n in m.normals]
                for f in range(len(m.part.faces)):
                    vs = [values[i] for i in m.face_points(f) if values[i] is not None]
                    face_values[f] = min(vs) if vs else None
            parts.append({"part": m.part.name, "values": values})
            for f, face in enumerate(m.part.faces):
                v = face_values[f]
                if v is None:
                    continue
                kind = None
                if v < limit - 1e-9:
                    kind = "thin" if map_type == "wall_thickness" else "low_draft"
                elif map_type == "wall_thickness" and v > THICK_FACTOR * limit:
                    kind = "thick"
                if kind is None:
                    continue
                what = {
                    "thin": "thinner than",
                    "thick": "more than twice",
                    "low_draft": "less draft than",
                }[kind]
                flagged.append(
                    {
                        "id": f"{kind}:{face.id}",
                        "kind": kind,
                        "value": v,
                        "message": f"{face.feature} is {v:g} {unit}, {what} the "
                        f"{limit:g} {unit} minimum ({standard.title})",
                        "location": {
                            "point": surface_point(m.part, face.id),
                            "part": m.part.name,
                            "faces": [face.id],
                        },
                    }
                )
        hi = LEGEND_FACTOR * limit
        out[map_type] = {
            "contract": "analysis-map/1",
            "build_id": BUILD_ID,
            "type": map_type,
            "standard": standard_id,
            "standard_title": standard.title,
            "unit": unit,
            "legend": {"min": 0.0, "max": hi, "limit": limit, "unit": unit},
            "parts": parts,
            "flagged": flagged,
        }
    return out


# --- Checks ------------------------------------------------------------------------------------


def checks_for(
    enc: Enclosure, parts: dict[str, Part], board: Board, standards: StandardsFile, maps: dict
) -> dict:
    """Check results for the sample enclosure on `board` (the newer revision)."""
    d = enc.d
    lid, base = parts["lid"], parts["base"]
    results = []

    def standard_result(standard_id, status, measured, summary, covers, location):
        s = standards.get(standard_id)
        required = {"kind": s.kind, "value": s.value}
        if s.unit:
            required["unit"] = s.unit
        results.append(
            {
                "id": s.check,
                "check": s.check,
                "title": s.title,
                "standard": s.id,
                "status": status,
                "measured": measured,
                "required": required,
                "summary": summary,
                "covers": covers,
                "location": location,
            }
        )

    def loc(part: Part, faces: list[str], point=None):
        return {"point": point or surface_point(part, faces[0]), "part": part.name, "faces": faces}

    # fit_tolerance: lid and base share one footprint at the parting plane.
    fit = standards.get("fit_tolerance")
    standard_result(
        "fit_tolerance",
        "pass",
        {"value": 0.0, "unit": "mm"},
        f"Lid and base rims match within ±{fit.value:g} mm.",
        "Lid-to-base fit at the parting plane. Openings are covered by opening_alignment.",
        loc(lid, ["lid.rim"]),
    )

    standard_result(*insert_result(enc, standards), loc(lid, [f"{d.bosses[0]['insert']}.wall"]))

    standard_result(
        "material",
        "pass",
        {"value": d.material},
        f"The design is set up for {d.material}.",
        "The material the design and its molding rules assume.",
        loc(lid, ["lid.outer_cap"]),
    )

    for map_type, standard_id in (("wall_thickness", "min_wall"), ("draft", "min_draft")):
        amap = maps[map_type]
        worst, where = None, None
        for pv in amap["parts"]:
            m = parts[pv["part"]]
            for i, v in enumerate(pv["values"]):
                if v is not None and (worst is None or v < worst):
                    worst, where = v, (m, i)
        limit = float(standards.get(standard_id).value)
        part, vertex = where
        mesh = PartMesh(part)
        face = part.faces[mesh.face_index[vertex]].id
        ok = worst >= limit - 1e-9
        unit = amap["unit"]
        standard_result(
            standard_id,
            "pass" if ok else "fail",
            {"value": worst, "unit": unit},
            f"The {'thinnest wall' if map_type == 'wall_thickness' else 'lowest draft'} is "
            f"{worst:g} {unit} on {face.split('.')[0]}.",
            f"Every face of every part, from the {map_type} analysis map.",
            loc(part, [face]),
        )

    # Component clearance, as `component_clearance` in geometry/checks.py measures it: the exact
    # distance from each component's body to every part of the enclosure, in any direction.
    gap, comp, part, face, point = closest_component(parts, board)
    limit = float(standards.get("min_component_clearance").value)
    standard_result(
        "min_component_clearance",
        "pass" if gap >= limit - 1e-6 else "fail",
        {"value": round(gap, 3), "unit": "mm"},
        f"The closest part is {comp.ref}, {gap:.2f} mm from the {part.name}.",
        "Exact distance from every component's body to every part of the enclosure.",
        loc(part, [face], point=tuple(round(v, 3) for v in point)),
    )

    height = round(d.z_top - d.z_bottom, 3)
    max_height = float(standards.get("max_height").value)
    standard_result(
        "max_height",
        "pass" if height <= max_height + 1e-9 else "fail",
        {"value": height, "unit": "mm"},
        f"The enclosure is {height:g} mm tall.",
        "Overall Z extent of the closed enclosure.",
        loc(lid, ["lid.outer_cap", "base.outer_cap"]),
    )
    results[-1]["location"]["part"] = None

    # Board checks: every opening the newer board needs is provided where it now is.
    cut = {o["id"]: o for o in d.openings if o["cut"]}
    fit = float(standards.get("fit_tolerance").value)
    for o in board.openings:
        part = lid if o.side == "top" else base
        cap = f"{part.name}.outer_cap"
        z = d.z_top if o.side == "top" else d.z_bottom
        need = rect_of(o.position, o.rotation, o.size, grow=d.clearance)
        point = (round(o.position[0], 3), round(o.position[1], 3), z)
        if o.kind not in CUT_KINDS:
            # Keep-out: no boss may intrude on it.
            widest = enc.inner_cap_z("lid")
            hits = [b["tag"] for b in d.bosses if _overlaps(need, b, enc.boss_radius(b, widest))]
            results.append(
                {
                    "id": f"antenna_keepout:{o.id}",
                    "check": "antenna_keepout",
                    "title": "Antenna keep-out",
                    "component": o.component,
                    "opening": o.id,
                    "status": "fail" if hits else "pass",
                    "measured": {"value": ", ".join(hits) or "no features"},
                    "required": board_required(CheckResult("antenna_keepout", "pass")),
                    "summary": f"{len(hits)} features over the {o.component} keep-out.",
                    "covers": "Bosses and other internal features over the antenna.",
                    "location": loc(part, [cap], point=point),
                }
            )
            continue
        have = cut.get(o.id)
        faces = [cap]
        if have is None:
            status, offset = "fail", None
            summary = f"There is no opening for {o.component} ({o.kind})."
        else:
            hc = have["rect"].centre
            offset = round(math.hypot(o.position[0] - hc[0], o.position[1] - hc[1]), 3)
            turned = abs(((o.rotation - have["rotation"]) + 90) % 180 - 90) > 1.0
            status = "pass" if offset <= fit and not turned else "fail"
            faces += [f"{have['tag']}.wall_{k}" for k in range(1, 5)]
            summary = f"The {o.component} opening is {offset:g} mm off" + (
                " and turned 90°." if turned else "."
            )
        results.append(
            {
                "id": f"opening_alignment:{o.id}",
                "check": "opening_alignment",
                "title": "Opening alignment",
                "component": o.component,
                "opening": o.id,
                "status": status,
                "measured": {
                    "value": offset if offset is not None else "no opening",
                    **({"unit": "mm"} if offset is not None else {}),
                },
                "required": {"kind": "tolerance", "value": fit, "unit": "mm"},
                "summary": summary,
                "covers": f"The {o.kind.replace('_', ' ')} for {o.component} is cut where the "
                "board needs it.",
                "location": loc(part, faces, point=point),
            }
        )

    # Standards for other designs (an enclosure fits no object): not applicable, with the reason.
    for s in standards.standards:
        if not s.applies(has_board=True):
            reason = s.not_applicable_reason()
            results.append(
                {
                    "id": s.check,
                    "check": s.check,
                    "title": s.title,
                    "standard": s.id,
                    "status": "not_applicable",
                    "measured": None,
                    "required": None,
                    "summary": f"{s.title}: not applicable. {reason}",
                    "reason": reason,
                    "covers": s.description,
                    "location": None,
                }
            )

    for check, title in (("strength", "Strength"), ("drop", "Drop"), ("heat", "Heat")):
        results.append(
            {
                "id": check,
                "check": check,
                "title": title,
                "status": "untested",
                "summary": f"{title} is not simulated.",
                "covers": "Not covered by any check yet; review it by hand.",
            }
        )

    counts = {s: sum(r["status"] == s for r in results) for s in STATUSES}
    return {
        "contract": "check-results/1",
        "build_id": BUILD_ID,
        "board": {"name": board.name, "revision": board.revision},
        "results": results,
        "summary": counts,
    }


def _triangles_shape(faces: list[Face]) -> TopoDS_Compound:
    """The triangles of some mesh faces as one exact shape, for distances to it."""
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for face in faces:
        for tri in face.triangles:
            poly = BRepBuilderAPI_MakePolygon()
            for p in tri:
                poly.Add(gp_Pnt(*p))
            poly.Close()
            builder.Add(compound, BRepBuilderAPI_MakeFace(poly.Wire(), True).Face())
    return compound


def closest_component(parts: dict[str, Part], board: Board):
    """The component body closest to the enclosure: (distance, component, part, face id, point
    on the part). The distance is exact on the mesh's triangles; a body that cuts through a
    wall is 0 away."""
    shapes = {name: _triangles_shape(part.faces) for name, part in parts.items()}
    best = None
    for c in board.components:
        box = _component_box(board, c)
        for name in sorted(parts):
            d, _ = occ.distance(shapes[name], box)
            if best is None or d < best[0] - 1e-9:
                best = (d, c, name, box)
    d, comp, name, box = best
    part = parts[name]
    nearest = None  # (distance, face id, point) of the part's face nearest to the body
    for f in part.faces:
        if f.triangles:
            fd, p = occ.distance(_triangles_shape([f]), box)
            if nearest is None or fd < nearest[0] - 1e-9:
                nearest = (fd, f.id, p)
    _, face_id, point = nearest
    return d, comp, part, face_id, point


def insert_result(enc: Enclosure, standards: StandardsFile) -> tuple:
    """The heat-set insert standard on the sample's bosses, as `insert_pockets` in
    geometry/checks.py measures it: every boss has a pocket the insert's hole size (within the
    fit tolerance of its wall all the way in), at least as deep as the insert is long, with
    material around it. Returns (standard id, status, measured, summary, covers)."""
    d = enc.d
    standard = standards.get("heat_set_inserts")
    dims = standard.dimensions or {}
    hole, length = dims["hole_diameter"], dims["insert_length"]
    tol = float(standards.get("fit_tolerance").value)
    problems = []
    for b in d.bosses:
        depth = b["pocket_depth"]
        # Its wall stays within the fit tolerance of the hole's radius over the insert's length.
        radii = [enc.pocket_radius(b, z) for z in (0.0, min(depth, length))]
        if depth < length - 1e-9:
            problems.append(f"{b['insert']} is {depth:g} mm deep")
        elif any(abs(r - hole / 2) > tol + 1e-9 for r in radii):
            problems.append(f"{b['insert']} is not a {hole:g} mm hole")
        elif b["radius"] < hole / 2 + tol + 0.5 - 1e-9:  # the ring the check needs around it
            problems.append(f"{b['tag']} has too little material around its pocket")
    depth = min(b["pocket_depth"] for b in d.bosses)
    summary = (
        f"Every boss has a pocket for an {standard.value}, {depth:g} mm deep."
        if not problems
        else f"Not every boss takes an {standard.value}: {'; '.join(problems)}."
    )
    return (
        "heat_set_inserts",
        "fail" if problems else "pass",
        {"value": depth, "unit": "mm"},
        summary,
        f"Every boss has a pocket that takes the insert: a {hole:g} mm hole, its wall within "
        f"±{tol:g} mm, at least {length:g} mm deep.",
    )


def _overlaps(rect: Rect, boss: dict, r: float) -> bool:
    """Does a boss of (widest) radius `r` reach over `rect`?"""
    return not (
        boss["x"] + r <= rect.x0
        or boss["x"] - r >= rect.x1
        or boss["y"] + r <= rect.y0
        or boss["y"] - r >= rect.y1
    )


# --- Session ------------------------------------------------------------------------------------


def build_for(board: Board, checks: dict, maps: dict, build_id: str = BUILD_ID) -> dict:
    """The build resource, with URLs relative to `/api/sessions/{session_id}`."""
    return {
        "id": build_id,
        "created_at": CREATED_AT,
        "board": {"name": board.name, "revision": board.revision},
        "status": "ready",
        "checks": checks["summary"],
        "maps": sorted(maps),
        "mesh_url": f"builds/{build_id}/mesh",
        "checks_url": f"builds/{build_id}/checks",
        "source_url": f"builds/{build_id}/source",
        "board_url": f"builds/{build_id}/board",
        "step_url": None,
        "restored_from": None,
        "failure": None,
    }


def source_for(
    design_board: Board, standards: StandardsFile, backend: str = DEFAULT_BACKEND
) -> dict:
    """The build's CAD source: the backend's enclosure generator (`geometry/backends/`), and
    where in it each feature is created, recorded by running it with the parameters of the
    board the enclosure was designed for."""
    engine = get_backend(backend)
    code = engine.example(SOURCE_EXAMPLE)
    result = engine.build(code, enclosure_parameters(design_board, standards))
    return {
        "build_id": BUILD_ID,
        "backend": backend,
        "language": "python",
        "filename": f"{SOURCE_EXAMPLE}.py",
        "code": code,
        "features": result.feature_sources(),
    }


def diff_for(
    old: Board, new: Board, standards: StandardsFile, backend: str = DEFAULT_BACKEND
) -> dict:
    """The diff (`geometry.diff`) from the enclosure generator's design for the older
    revision to its design for the newer one: the parameters changed and the features added,
    removed, moved, rotated or resized, with distances."""
    from geometry import generate_enclosure
    from geometry.diff import diff_features, diff_parameters, feature_summary

    a, b = (generate_enclosure(x, standards, backend=backend) for x in (old, new))
    return {
        "old": PREVIOUS_BUILD_ID,
        "new": BUILD_ID,
        "parameters": diff_parameters(
            enclosure_parameters(old, standards), enclosure_parameters(new, standards)
        ),
        "features": diff_features(feature_summary(a), feature_summary(b)),
    }


def _in_triangle_xy(p: tuple[float, float], tri) -> bool:
    (ax, ay, _), (bx, by, _), (cx, cy, _) = tri

    def side(x0, y0, x1, y1):
        return (x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0)

    s = (side(ax, ay, bx, by), side(bx, by, cx, cy), side(cx, cy, ax, ay))
    return all(v >= -1e-9 for v in s) or all(v <= 1e-9 for v in s)


def shared_xy(a: Part, face_a: str, b: Part, face_b: str) -> tuple[float, float]:
    """A plan point on two horizontal faces (caps): the centroid of the largest triangle of
    `face_a` that lies over `face_b`, so a vertical dimension between them misses any hole."""
    fa = next(f for f in a.faces if f.id == face_a)
    fb = next(f for f in b.faces if f.id == face_b)
    for tri in sorted(fa.triangles, key=lambda t: -area(*t)):
        p = (sum(v[0] for v in tri) / 3, sum(v[1] for v in tri) / 3)
        if any(_in_triangle_xy(p, t) for t in fb.triangles):
            return round(p[0], 3), round(p[1], 3)
    raise ValueError(f"{face_a} and {face_b} don't overlap in plan")


def _end(point, part: str | None = None, face: str | None = None) -> dict:
    return {"point": [round(c, 3) for c in point], "part": part, "face": face}


def _outer_wall(side: int, part: str = "lid") -> str:
    """A part's outer wall face on a side of the footprint (Footprint.ring numbering)."""
    return f"{part}.outer_wall_{side + 1}"


def parameters_for(enc: Enclosure, parts: dict[str, Part]) -> list[dict]:
    """The design parameters, with where each length is measured on the model and which faces
    drag it. Lengths are measured where the design defines them: the footprint at the parting
    plane, the caps square to the pull."""
    design = enc.d
    fp = design.footprint
    pz, zb, zt = design.parting_z, design.z_bottom, design.z_top
    cx, cy = (fp.x0 + fp.x1) / 2, (fp.y0 + fp.y1) / 2
    lid, base = parts["lid"], parts["base"]

    def distance(start, end):
        return {"kind": "distance", "start": start, "end": end}

    wx, wy = shared_xy(lid, "lid.inner_cap", lid, "lid.outer_cap")
    hx, hy = shared_xy(lid, "lid.outer_cap", base, "base.outer_cap")
    r = fp.radius
    # Side 1 of the footprint is the arc at (x1, y0); its end at 45° is on that wall.
    arc = (fp.x1 - r, fp.y0 + r)
    params = [
        (
            "wall",
            design.wall,
            "mm",
            "Wall thickness, square to the face",
            None,
            distance(
                _end((wx, wy, zt), "lid", "lid.outer_cap"),
                _end((wx, wy, enc.inner_cap_z("lid")), "lid", "lid.inner_cap"),
            ),
            None,
        ),
        ("draft", design.draft_deg, "deg", "Draft on faces along the mold pull", None, None, None),
        ("clearance", design.clearance, "mm", "Gap kept around every component", None, None, None),
        (
            "corner_radius",
            fp.radius,
            "mm",
            "Outside corner radius",
            None,
            {
                "kind": "radius",
                "start": _end((*arc, pz)),
                "end": _end(
                    (arc[0] + r * math.sqrt(0.5), arc[1] - r * math.sqrt(0.5), pz),
                    "lid",
                    _outer_wall(1),
                ),
            },
            None,
        ),
        (
            "length",
            round(fp.y1 - fp.y0, 3),
            "mm",
            "Outside length along the board",
            None,
            distance(
                _end((cx, fp.y0, pz), "lid", _outer_wall(0)),
                _end((cx, fp.y1, pz), "lid", _outer_wall(4)),
            ),
            {"faces": [_outer_wall(0, "base"), _outer_wall(0)], "axis": [0.0, -1.0, 0.0]},
        ),
        (
            "width",
            round(fp.x1 - fp.x0, 3),
            "mm",
            "Outside width",
            None,
            distance(
                _end((fp.x0, cy, pz), "lid", _outer_wall(6)),
                _end((fp.x1, cy, pz), "lid", _outer_wall(2)),
            ),
            {"faces": [_outer_wall(2, "base"), _outer_wall(2)], "axis": [1.0, 0.0, 0.0]},
        ),
        (
            "height",
            round(zt - zb, 3),
            "mm",
            "Overall height",
            None,
            distance(
                _end((hx, hy, zb), "base", "base.outer_cap"),
                _end((hx, hy, zt), "lid", "lid.outer_cap"),
            ),
            {"faces": ["lid.outer_cap"], "axis": [0.0, 0.0, 1.0]},
        ),
        ("material", design.material, None, "Material", None, None, None),
    ]
    for b in design.bosses:
        tag = b["tag"]
        params.append(
            (
                f"{tag}_radius",
                round(b["radius"], 3),
                "mm",
                f"Boss over {b['hole']}",
                tag,
                {
                    "kind": "radius",
                    # The boss's axis: in its insert pocket, on no face of the boss.
                    "start": _end((b["x"], b["y"], pz)),
                    "end": _end((b["x"] + b["radius"], b["y"], pz), "lid", f"{tag}.side"),
                },
                None,
            )
        )
    return [
        {
            "name": name,
            "value": value,
            "unit": unit,
            "locked": False,
            "min": None,
            "max": None,
            "description": text,
            "feature": feature,
            "dimension": dimension,
            "drag": drag,
        }
        for name, value, unit, text, feature, dimension, drag in params
    ]


def events_for(
    old: Board, new: Board, changes: list[Change], checks: dict, build: dict, previous: dict
) -> list:
    """The recorded run: the build plan, the existing enclosure's build on its own revision
    (`previous`, with no message, as the session API's first build), what changed, a build with
    its checks on the new revision, then a question."""
    events = []
    message_id = "msg-2"  # msg-1 is the user's message that starts the run

    def emit(event: dict, delay: int = TOKEN_DELAY_MS) -> None:
        events.append({"id": len(events) + 1, "delay_ms": delay, "event": event})

    def say(text: str) -> None:
        words = text.split(" ")
        for i, w in enumerate(words):
            delta = w + (" " if i < len(words) - 1 else "")
            emit({"type": "text", "message_id": message_id, "delta": delta})

    rounds = 2
    emit({"type": "message_start", "message_id": message_id, "role": "assistant"}, 0)
    steps = [f"Compare revision {old.revision} with {new.revision}"]
    steps += [f"Rework the enclosure for {c.component} ({c.role})" for c in changes]
    steps += [f"Recheck every constraint on {new.revision}"]
    emit({"type": "plan", "message_id": message_id, "steps": steps}, STEP_DELAY_MS)
    emit(
        {
            "type": "progress",
            "message_id": message_id,
            "round": 1,
            "rounds": rounds,
            "text": f"Comparing revision {old.revision} with {new.revision} · round 1 of {rounds}",
        },
        STEP_DELAY_MS,
    )
    lines = [f"Revision {new.revision} changes {len(changes)} things that affect the enclosure:"]
    lines += [f"\n- {c.component} ({c.role}): {c.change}" for c in changes]
    say("".join(lines) + "\n\n")
    emit(
        {
            "type": "progress",
            "message_id": message_id,
            "round": 2,
            "rounds": rounds,
            "text": f"Checking the current enclosure on {new.revision} · round 2 of {rounds}",
        },
        STEP_DELAY_MS,
    )
    # Both builds at once (no delay between them), so a client never sees one without the other.
    emit({"type": "build_ready", "message_id": None, "build": previous}, STEP_DELAY_MS)
    emit({"type": "build_ready", "message_id": message_id, "build": build}, 0)
    s = checks["summary"]
    failed = [r for r in checks["results"] if r["status"] == "fail"]
    text = f"{s['pass']} checks pass, {s['fail']} fail and {s['untested']} are untested:"
    text += "".join(f"\n- {r['title']}: {r['summary']}" for r in failed)
    say(text)
    emit(
        {
            "type": "question",
            "message_id": message_id,
            "question_id": "q-1",
            "text": f"How should I fix the {len(failed)} failing checks?",
            "options": [
                {
                    "id": "fix-all",
                    "label": "Fix them all",
                    "description": "Propose every change at once as tracked changes.",
                },
                {
                    "id": "one-by-one",
                    "label": "One at a time",
                    "description": "Propose one change, wait for review, then the next.",
                },
                {
                    "id": "explain",
                    "label": "Explain first",
                    "description": "Walk through each failure before changing anything.",
                },
            ],
        },
        STEP_DELAY_MS,
    )
    emit({"type": "message_end", "message_id": message_id}, 0)
    return events


# --- Change set ---------------------------------------------------------------------------------


def change_set_for(
    old: Board, new: Board, changes: list[Change], standards: StandardsFile, checks: dict
) -> dict:
    """The change set a run on the recorded session would propose (`change-set/1`), in manual
    mode with nothing reviewed yet: one change per `CHANGES.md` row, the opening for that
    component moved, turned or added as the new board needs it. It is built by
    `app.agent.proposal` itself; the stand-in checks are `checks.json`, where a board check of
    an opening passes once the design's opening is the one the new board needs."""
    from types import SimpleNamespace

    from app.agent.proposal import Proposal, board_changes, combine, diff
    from app.agent.services import BuildOutcome, items_from_report
    from app.contracts.session import Build

    base = {"code": "", "params": enclosure_parameters(old, standards)}
    full = {"code": "", "params": enclosure_parameters(new, standards)}
    components = {c.component for c in changes}
    wanted = {
        u.path
        for u in diff(base, full)
        if isinstance(u.element, dict) and u.element.get("component") in components
    }
    final = combine(base, full, diff(base, full), wanted)
    units = diff(base, final)
    target = {o["id"]: o for o in final["params"].get("openings", [])}

    def checked(design: dict) -> list:
        have = {o["id"]: o for o in design["params"].get("openings", [])}
        results = []
        for r in checks["results"]:
            opening = r.get("opening")
            if opening in target and r["status"] == "fail" and have.get(opening) == target[opening]:
                r = {**r, "status": "pass"}
            results.append(r)
        return items_from_report({"results": results})

    base_items = checked(base)
    build = BuildOutcome(Build.model_validate(build_for(new, checks, {})))
    proposal = Proposal(
        ws=SimpleNamespace(latest=build, report=base_items),  # type: ignore[arg-type]
        mode="manual",
        base=base,
        final=final,
        units=units,
        base_build=build,
        base_checks=base_items,
        alone={u.path: checked(combine(base, final, units, {u.path})) for u in units},
        rounds={},
        status={u.path: "proposed" for u in units},
        board=board_changes((BOARDS_DIR / "CHANGES.md").read_text()),
    )
    return proposal.change_set().model_dump(mode="json", by_alias=True)


# --- Risk flags ---------------------------------------------------------------------------------


def _spots(maps: dict) -> list[dict]:
    """The analysis maps' flagged spots, as `FixtureServices.analyze` gives them to the agent."""
    return [
        {
            "map": m["type"],
            "kind": spot["kind"],
            "value": spot["value"],
            "unit": m["unit"],
            "message": spot["message"],
            "part": spot["location"]["part"],
            "faces": spot["location"]["faces"],
            "point": spot["location"]["point"],
        }
        for m in maps.values()
        for spot in m["flagged"]
    ]


# mm the near-limit fix for the height takes off. It comes off the bottom of the base: the lid
# top can't come down, the wall over the insert pockets is at its minimum.
SHORTER_BY = 0.1


def risk_flags_for(
    old: Board,
    new: Board,
    design: Design,
    standards: StandardsFile,
    checks: dict,
    maps: dict,
) -> dict:
    """The risk flags a run on the recorded session would raise (`risk-flags/1`), nothing
    reviewed yet. The risks are what `app.agent.risks` finds on the fixture checks and maps,
    and each fix is the one Claude would give, checked on the fixture geometry as `flag_risk`
    checks it: rebuilt with the fix alone, the risk must be smaller (`resolves` when it is
    gone) and no check may change.

    - A thick boss: slim the bosses (`boss.radius`) as far as the insert allows, a minimum
      wall (`min_wall`) around its hole. None when they are that slim already.
    - The overall height near its maximum (`max_height`): raise the bottom of the base
      (`z_bottom`) by `SHORTER_BY`."""
    import copy

    from app.agent.risks import EPS, Flag, Flags, find_risks, with_edits
    from app.agent.services import items_from_report

    report = items_from_report(checks)
    risks = find_risks(report, _spots(maps), standards)
    params = enclosure_parameters(old, standards)
    before = {"code": "", "params": params}

    def with_fix(fixed: Design) -> tuple[dict[str, str], dict]:
        """The check statuses and the risks of the fixture enclosure built from `fixed`."""
        enc = Enclosure(fixed)
        parts = enc.build()
        fixed_maps = maps_for(enc, [PartMesh(p) for p in parts], standards)
        fixed_checks = checks_for(enc, {p.name: p for p in parts}, new, standards, fixed_maps)
        items = items_from_report(fixed_checks)
        found = find_risks(items, _spots(fixed_maps), standards)
        return {i.id: i.status for i in items}, {r.id: r for r in found}

    statuses = {i.id: i.status for i in report}
    flags = []

    def flag(risk, title: str, edits: dict, left: dict) -> None:
        after = left.get(risk.id)
        if after is not None and after.severity >= risk.severity - EPS:
            return  # the fix doesn't help: flag_risk would refuse it
        flags.append(
            Flag(
                risk=risk,
                title=title,
                edits=edits,
                before=before,
                after=with_edits(before, edits),
                build=checks["build_id"],
                resolves=after is None,
            )
        )

    boss = params["boss"]
    radius = round(boss["insert_diameter"] / 2 + float(standards.get("min_wall").value), 3)
    cut = boss["radius"] - radius
    thick = [
        r for r in risks if r.kind == "thick" and r.feature in {b["tag"] for b in design.bosses}
    ]
    if cut > 0 and thick:
        slim = copy.deepcopy(design)
        for b in slim.bosses:
            b["radius"] -= cut
        changed, left = with_fix(slim)
        if changed == statuses:
            for risk in thick:
                flag(
                    risk, f"Slim the bosses to {radius:g} mm radius", {"boss.radius": radius}, left
                )

    tall = [r for r in risks if r.kind == "near_limit" and r.standard == "max_height"]
    if tall:
        shorter = copy.deepcopy(design)
        shorter.z_bottom += SHORTER_BY
        changed, left = with_fix(shorter)
        if changed == statuses:
            edits = {"z_bottom": round(params["z_bottom"] + SHORTER_BY, 3)}
            for risk in tall:
                flag(risk, f"Raise the bottom of the base {SHORTER_BY:g} mm", edits, left)

    data = Flags(ws=None, flags=flags).risk_flags()  # type: ignore[arg-type]
    return data.model_dump(mode="json", by_alias=True)


def conflict_events_for(conflict: dict, locks: dict[str, float], build: dict) -> list:
    """The recorded can't-satisfy run: the locks are applied, the build's stack can't meet
    the standards, and Claude asks which constraint should give, pinned where it clashes."""
    events = []
    message_id = "msg-2"

    def emit(event: dict, delay: int = TOKEN_DELAY_MS) -> None:
        events.append({"id": len(events) + 1, "delay_ms": delay, "event": event})

    def say(text: str) -> None:
        words = text.split(" ")
        for i, w in enumerate(words):
            delta = w + (" " if i < len(words) - 1 else "")
            emit({"type": "text", "message_id": message_id, "delta": delta})

    locked = ", ".join(f"{path} = {v:g} mm" for path, v in locks.items())
    emit({"type": "message_start", "message_id": message_id, "role": "assistant"}, 0)
    emit(
        {
            "type": "progress",
            "message_id": message_id,
            "round": 1,
            "rounds": 1,
            "text": f"Rebuilding with {locked} locked · round 1 of 1",
        },
        STEP_DELAY_MS,
    )
    emit({"type": "build_ready", "message_id": message_id, "build": build}, STEP_DELAY_MS)
    lines = [f"With {locked} locked, these constraints clash:"]
    for c in conflict["constraints"]:
        lines.append(f"\n- {c['label']}: {c['relation']} {c['value']:g} {c['unit']}")
    say("".join(lines) + "\n\n")
    emit(
        {
            "type": "question",
            "message_id": message_id,
            "question_id": "q-conflict",
            "text": conflict_fixture.question_text(conflict),
            "options": conflict_fixture.options_for(conflict),
            "location": conflict["location"],
            "conflict": conflict,
        },
        STEP_DELAY_MS,
    )
    emit({"type": "message_end", "message_id": message_id}, 0)
    return events


# --- Output -------------------------------------------------------------------------------------


def _jsonl(events: list) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


def _json(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def generate(standards: StandardsFile | None = None) -> dict[str, str]:
    """Every fixture file: relative path -> text."""
    from app.examples import example_standards

    # The board revision example's scenario: its constraint set, with what the example brings.
    standards = standards or example_standards()
    old, new, changes = boards()
    design = design_for(old, standards)
    enc = Enclosure(design)
    parts = enc.build()
    meshes = [PartMesh(p) for p in parts]
    source = {
        "designed_for": {"name": old.name, "revision": old.revision},
        "build_id": BUILD_ID,
        "generator": "app.fixtures",
    }
    gltf = write_gltf(meshes, source)
    maps = maps_for(enc, meshes, standards)
    checks = checks_for(enc, {p.name: p for p in parts}, new, standards, maps)
    build = build_for(new, checks, maps)
    previous_checks = checks_for(enc, {p.name: p for p in parts}, old, standards, maps)
    previous_checks["build_id"] = PREVIOUS_BUILD_ID
    previous = build_for(old, previous_checks, maps, PREVIOUS_BUILD_ID)
    events = events_for(old, new, changes, checks, build, previous)
    locks = conflict_fixture.conflict_locks_for(new, standards)
    conflict = conflict_fixture.conflict_json(
        conflict_fixture.detect(new, standards), new, standards, {p.name: p for p in parts}
    )
    session = {
        "title": SESSION_TITLE,
        "board": {"name": new.name, "revision": new.revision},
        "prompt": f"Here is revision {new.revision}. Check the current enclosure against it.",
        "conflict_prompt": conflict_fixture.prompt_for(locks),
    }
    files = {
        "enclosure.gltf": json.dumps(gltf, separators=(",", ":")) + "\n",
        "checks.json": _json(checks),
        "build.json": _json(build),
        "previous-build.json": _json(previous),
        "previous-checks.json": _json(previous_checks),
        "diff.json": _json(diff_for(old, new, standards)),
        "build-source.json": _json(source_for(old, standards)),
        "parameters.json": _json(parameters_for(enc, {p.name: p for p in parts})),
        "session.json": _json(session),
        "change-set.json": _json(change_set_for(old, new, changes, standards, checks)),
        # The built-in set as GET /api/standards lists it: without what an example brings.
        "standards.json": _json(
            [describe(MemoryStandardsStore(), "", load_standards()).model_dump(mode="json")]
        ),
        "standards-kinds.json": _json([k.model_dump(mode="json") for k in kinds(load_standards())]),
        "risk-flags.json": _json(risk_flags_for(old, new, design, standards, checks, maps)),
        "session-events.jsonl": _jsonl(events),
        "conflict-events.jsonl": _jsonl(conflict_events_for(conflict, locks, build)),
    }
    for map_type, amap in maps.items():
        files[f"maps/{map_type}.json"] = _json(amap)
    return files


def write(files: dict[str, str], root: Path = FIXTURES_DIR) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
