"""Enclosure parameters: the named values the generator code builds from.

`enclosure_parameters(board, standards)` derives a full set from a board revision, the team
standards and the existing case (`demos/<board>/*-case.step`). The result is plain JSON so it can
go to either backend, into the parameter store, or to Claude.

Parameters are a design: they fix where every feature goes. Building the parameters derived
from one revision against another revision's board is how the demo shows what a board change
breaks (the openings stay where the old board needed them).
"""

from __future__ import annotations

import math
from functools import cache
from pathlib import Path
from typing import Any

from app.boards import BOARDS_DIR, Board
from app.standards import StandardsFile

Params = dict[str, Any]

# Openings the generator cuts through the enclosure. An antenna keep-out is not cut: it only
# forbids hardware and internal features over the antenna (see the `antenna_keepout` check).
CUT_KINDS = ("sensor_window", "vent", "button")

# Sections thicker than this times `min_wall` sink when molded: the wall-thickness analysis map
# flags them, and the generator keeps solid features (the retention posts) under it.
THICK_FACTOR = 2.0


def case_step_path() -> Path:
    """The team's existing enclosure: the one `*-case.step` file in `demos/<board>/`."""
    (path,) = BOARDS_DIR.glob("*-case.step")
    return path


@cache
def case_extents(path: Path | None = None) -> tuple[tuple[float, float], ...]:
    """The existing case's bounding box in its own axes: ((x0, x1), (y0, y1), (z0, z1)).

    The STEP file holds the case and a reference board; the case is the top-level shape with
    the most volume (see `demos/<board>/README.md`).
    """
    from build123d import import_step

    shape = import_step(path or case_step_path())
    case = max(shape.children, key=lambda c: sum(s.volume for s in c.solids()))
    box = case.bounding_box()
    return ((box.min.X, box.max.X), (box.min.Y, box.max.Y), (box.min.Z, box.max.Z))


def board_extents(board: Board) -> tuple[float, float, float, float]:
    xs = [p[0] for p in board.pcb.outline]
    ys = [p[1] for p in board.pcb.outline]
    return min(xs), max(xs), min(ys), max(ys)


def case_in_board_axes(board: Board, path: Path | None = None) -> dict[str, list[float]]:
    """The existing case's extents in board coordinates.

    `demos/<board>/README.md` gives the case axes: case x runs down the board from its top edge
    (case x = top edge y - board y), case y is board x measured from the board's centre line,
    and z is shared.
    """
    (cx0, cx1), (cy0, cy1), (cz0, cz1) = case_extents(path)
    x0, x1, _, y1 = board_extents(board)
    centre = (x0 + x1) / 2
    return {"x": [centre + cy0, centre + cy1], "y": [y1 - cx1, y1 - cx0], "z": [cz0, cz1]}


def _probe_slot(board: Board, y_wall: tuple[float, float], fit: float) -> dict | None:
    """Where the board leaves the enclosure through the wall between y_wall[0] and y_wall[1].

    The x range of the outline inside that band, widened by the fit tolerance.
    """
    pts = board.pcb.outline
    xs: list[float] = []
    lo, hi = y_wall
    n = len(pts)
    for i in range(n):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % n]
        if lo <= ay <= hi:
            xs.append(ax)
        for y in (lo, hi):
            if (ay - y) * (by - y) < 0:
                xs.append(ax + (bx - ax) * (y - ay) / (by - ay))
    if not xs:
        return None
    return {"x": [min(xs) - fit, max(xs) + fit], "y": [lo - 1.0, hi + 1.0]}


def opening_tags(board: Board) -> dict[str, str]:
    """Feature tag for each opening the enclosure cuts: `opening_<component ref>`.

    A component with more than one cut opening gets `opening_<ref>_<kind>` for each.
    """
    count: dict[str, int] = {}
    for o in board.openings:
        if o.kind in CUT_KINDS:
            count[o.component] = count.get(o.component, 0) + 1
    tags = {}
    for o in board.openings:
        if o.kind in CUT_KINDS:
            tag = f"opening_{o.component}"
            tags[o.id] = f"{tag}_{o.kind}" if count[o.component] > 1 else tag
    return tags


def enclosure_parameters(
    board: Board, standards: StandardsFile, case_step: Path | None = None
) -> Params:
    """Derive every value the generator needs from a board, the standards and the existing case."""
    fit = float(standards.get("fit_tolerance").value)
    clearance = float(standards.get("min_component_clearance").value)
    min_wall = float(standards.get("min_wall").value)
    min_draft = float(standards.get("min_draft").value)
    # A height limit is not a default rule: an example (or the user's own set) may bring one.
    try:
        max_height: float | None = float(standards.get("max_height").value)
    except KeyError:
        max_height = None
    insert_standard = standards.get("heat_set_inserts")
    inserts = insert_standard.dimensions or {}
    material = str(standards.get("material").value)

    # Drafted walls are measured square to the face, so give the wall and the draft headroom
    # over the minimums rather than designing exactly on the limit.
    wall = min_wall * 1.25
    draft = min_draft + 0.5

    case = case_in_board_axes(board, case_step)
    z_bottom, z_top = case["z"]
    if max_height is not None and z_top - z_bottom > max_height:
        z_top = z_bottom + max_height

    bx0, bx1, _, _ = board_extents(board)
    pcb = board.pcb.thickness

    insert_d = inserts["hole_diameter"]
    insert_len = inserts["insert_length"]
    boss_r = insert_d / 2 + wall
    corner_r = boss_r + clearance  # bosses sit concentric with the rounded corners
    boss_gap = 2 * clearance  # between the board edge and a boss

    # Start from the existing case and widen it where the corner bosses need room.
    x0 = min(case["x"][0], bx0 - boss_gap - boss_r - corner_r)
    x1 = max(case["x"][1], bx1 + boss_gap + boss_r + corner_r)
    y0, y1 = case["y"]
    ceiling = z_top - wall
    pocket_top = ceiling
    boss_bottom = pocket_top - (insert_len + clearance)

    bosses = []
    for n, (x, y) in enumerate(
        [
            (x0 + corner_r, y0 + corner_r),
            (x1 - corner_r, y0 + corner_r),
            (x0 + corner_r, y1 - corner_r),
            (x1 - corner_r, y1 - corner_r),
        ],
        start=1,
    ):
        bosses.append({"id": str(n), "tag": f"boss_{n}", "x": x, "y": y})

    tags = opening_tags(board)
    openings = []
    for o in board.openings:
        openings.append(
            {
                "id": o.id,
                "tag": tags.get(o.id),
                "component": o.component,
                "kind": o.kind,
                "side": o.side,
                "x": o.position[0],
                "y": o.position[1],
                "rotation": o.rotation,
                "size": list(o.size),
                "cut": o.kind in CUT_KINDS,
            }
        )

    # The board leaves through the end wall it crosses; both ends are checked.
    slots = []
    for name, band in (("pcb", (y0, y0 + wall)), ("pcb_top", (y1 - wall, y1))):
        slot = _probe_slot(board, band, fit)
        if slot is not None:
            slots.append({"tag": "opening_" + name, **slot})

    retention = board_retention(
        board,
        fit=fit,
        clearance=clearance,
        wall=wall,
        draft=draft,
        footprint=(x0, x1, y0, y1),
        z_span=z_top - z_bottom,
        bosses=[(b["x"], b["y"], boss_r) for b in bosses],
        slots=slots,
        post_height=ceiling + wall / 2 - (pcb + fit / 2),
        max_section=THICK_FACTOR * min_wall,
    )

    return {
        "source": {"board": board.name, "revision": board.revision},
        "material": material,
        "fit": fit,
        "clearance": clearance,
        "draft": draft,
        "wall": wall,
        "footprint": {"x": [x0, x1], "y": [y0, y1], "corner_radius": corner_r},
        "z_bottom": z_bottom,
        "z_top": z_top,
        "parting_z": 0.0,
        "pcb_thickness": pcb,
        "floor_top": z_bottom + wall,
        "ceiling": ceiling,
        "boss": {
            "radius": boss_r,
            "bottom": boss_bottom,
            "insert_diameter": insert_d,
            "insert_top": pocket_top,
            "screw_clearance": inserts["screw_clearance"],
            # The insert, a part of its own: pressed in flush with the pocket's mouth, standing
            # off the pocket wall by half the fit tolerance.
            "insert_length": insert_len,
            "insert_gap": fit / 2,
            "insert_material": insert_standard.material or str(insert_standard.value),
        },
        "bosses": bosses,
        "openings": openings,
        "slots": slots,
        "retention": retention,
    }


# --- Board retention ---------------------------------------------------------------------------

RETENTION_GRID = 0.5  # mm: step of the search for clear spots on the board


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        (ax, ay), (bx, by) = polygon[i], polygon[(i + 1) % n]
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            inside = not inside
    return inside


def distance_to_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> float:
    """Distance from a point to a polygon's boundary."""
    best = math.inf
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
    return best


def _distance_to_area(x: float, y: float, polygon: list[tuple[float, float]]) -> float:
    """Distance from a point to a filled polygon (0 inside it)."""
    return 0.0 if point_in_polygon(x, y, polygon) else distance_to_polygon(x, y, polygon)


def _rect(position, rotation: float, size, grow: float = 0.0) -> list[tuple[float, float]]:
    return opening_corners(
        {"x": position[0], "y": position[1], "rotation": rotation, "size": list(size)}, grow
    )


def board_retention(
    board: Board,
    *,
    fit: float,
    clearance: float,
    wall: float,
    draft: float,
    footprint: tuple[float, float, float, float],
    z_span: float,
    bosses: list[tuple[float, float, float]],
    slots: list[dict],
    post_height: float = 0.0,
    max_section: float = math.inf,
) -> Params:
    """Features that hold the board still in the closed enclosure, placed from the board data.

    - `pegs`: one per mounting hole whose top side is clear, hanging from the lid. A pin goes
      into the hole (tag `peg_<hole id>`) and a wider post above it presses on the board's top
      face. The pin stops short of the board's bottom face when a bottom-side part covers the
      hole, so it keeps the component clearance. The post is solid and too small to core with
      walls of the minimum thickness, so it stays no wider than `max_section` over its whole
      `post_height` (it widens with the draft as it rises): a thicker section sinks when molded.
    - `clamps` (lid, pressing on the top face) and `supports` (base, under the bottom face):
      a round pad each in the half of the board away from where it leaves the enclosure, at the
      clear spot with the most room. With the slot and the pegs at the other end, the far end
      can then neither lift nor drop.

    Every feature stands off the board by half the fit tolerance.
    """
    gap = fit / 2
    t = math.tan(math.radians(draft))
    taper = z_span * t  # the most any drafted feature widens over the enclosure's height
    pcb = board.pcb.thickness
    outline = [tuple(p) for p in board.pcb.outline]
    fx0, fx1, fy0, fy1 = footprint

    # Areas a feature on each side keeps the component clearance from.
    side_areas = {
        side: [_rect(c.position, c.rotation, c.size) for c in board.components if c.side == side]
        + [
            _rect(o.position, o.rotation, o.size)
            for o in board.openings
            if o.side == side and o.kind in CUT_KINDS
        ]
        for side in ("top", "bottom")
    }

    keepouts = [
        _rect(o.position, o.rotation, o.size) for o in board.openings if o.kind == "antenna_keepout"
    ]

    def clear_radius(x: float, y: float, side: str) -> float:
        """Largest radius around (x, y) that stays the component clearance from that side."""
        d = min((_distance_to_area(x, y, a) for a in side_areas[side]), default=math.inf)
        k = min((_distance_to_area(x, y, a) for a in keepouts), default=math.inf)
        return min(d, k) - clearance - taper

    pegs = []
    for hole in board.mounting_holes:
        x, y = hole.position
        hole_r = hole.diameter / 2
        pin_r = hole_r - gap
        free = clear_radius(x, y, "top")
        if pin_r <= 0 or free < hole_r:
            continue  # parts crowd the hole from above: no peg here
        post_r = min(hole_r + wall / 2, free, max_section / 2 - post_height * t)
        covered = clear_radius(x, y, "bottom") < hole_r
        tip = clearance + gap if covered else 0.0
        if tip >= pcb:
            continue
        pegs.append(
            {
                "tag": f"peg_{hole.id}",
                "hole": hole.id,
                "x": x,
                "y": y,
                "pin_radius": pin_r,
                "post_radius": post_r if post_r >= hole_r + fit else None,
                "tip": tip,
            }
        )

    # The pads go in the half of the board farther from where it leaves the enclosure, at the
    # spot with the most room around it, so small moves of nearby parts leave them clear.
    bx0, bx1, by0, by1 = board_extents(board)
    lo, hi = max(fy0, by0), min(fy1, by1)
    mid = (lo + hi) / 2
    slot_ys = [(s["y"][0] + s["y"][1]) / 2 for s in slots]
    far = -1.0 if slot_ys and min(slot_ys) > mid else 1.0
    radius = wall / 2

    def room(x: float, y: float, side: str) -> float:
        """Largest pad radius that fits at (x, y) on this side."""
        if not point_in_polygon(x, y, outline):
            return -math.inf
        limits = [
            clear_radius(x, y, side),
            distance_to_polygon(x, y, outline),
            x - (fx0 + wall),
            (fx1 - wall) - x,
            y - (fy0 + wall),
            (fy1 - wall) - y,
        ]
        limits += [math.hypot(x - bx, y - by) - br - taper - clearance for bx, by, br in bosses]
        limits += [
            math.hypot(x - p["x"], y - p["y"]) - (p["post_radius"] or 0) - clearance for p in pegs
        ]
        return min(limits)

    step = RETENTION_GRID
    spots = [
        (bx0 + i * step, by0 + j * step)
        for i in range(int((bx1 - bx0) / step) + 1)
        for j in range(int((by1 - by0) / step) + 1)
    ]
    spots = [(x, y) for x, y in spots if far * (y - mid) > 0 and lo <= y <= hi]

    def place(side: str, tag: str) -> list[dict]:
        scored = [(round(room(x, y, side), 6), far * y, -x, x, y) for x, y in spots]
        best = max(scored, default=None)
        if best is None or best[0] < radius:
            return []
        return [{"tag": f"{tag}_1", "x": best[3], "y": best[4], "radius": radius}]

    return {
        "gap": gap,
        "pegs": pegs,
        "clamps": place("top", "clamp"),
        "supports": place("bottom", "support"),
    }


def opening_corners(o: dict, grow: float = 0.0) -> list[tuple[float, float]]:
    """Corners of an opening's rectangle (counter-clockwise), optionally grown on every side."""
    w, length = o["size"][0] + 2 * grow, o["size"][1] + 2 * grow
    r = math.radians(o["rotation"])
    c, s = math.cos(r), math.sin(r)
    pts = [(-w / 2, -length / 2), (w / 2, -length / 2), (w / 2, length / 2), (-w / 2, length / 2)]
    return [(o["x"] + x * c - y * s, o["y"] + x * s + y * c) for x, y in pts]


__all__ = [
    "CUT_KINDS",
    "THICK_FACTOR",
    "Params",
    "board_extents",
    "board_retention",
    "case_extents",
    "case_in_board_axes",
    "case_step_path",
    "distance_to_polygon",
    "enclosure_parameters",
    "opening_corners",
    "opening_tags",
    "point_in_polygon",
]
