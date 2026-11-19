"""A simplified sample enclosure as a tagged triangle mesh, for the contract fixtures.

This is not the enclosure generator and not the tessellator. It builds a
deterministic, pure-Python stand-in with the same kinds of features: a base and a lid (drafted
cups split at the board's top face), an opening cut through the lid or floor for every opening
the board needs (`opening_<component ref>`), and a drafted boss over every mounting hole
(`boss_<n>`) with a pocket for a heat-set insert in its free end (`insert_<n>`).
Every face and edge has a stable ID and the feature that created it.

All dimensions come from a board file, `standards.yaml` and the existing case's extents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.boards import Board

Vec = tuple[float, float, float]
P2 = tuple[float, float]

# Openings that are cut through the enclosure. An antenna keep-out only forbids material and
# features over the antenna, so it is checked but not cut.
CUT_KINDS = ("sensor_window", "vent", "button")
ARC_STEP_DEG = 5.0  # same arc resolution as the board outlines
BOSS_SEGMENTS = 24
EPS = 1e-9


@dataclass
class Face:
    id: str
    feature: str
    triangles: list[tuple[Vec, Vec, Vec]] = field(default_factory=list)

    def add_quad(self, a: Vec, b: Vec, c: Vec, d: Vec) -> None:
        """Quad a-b-c-d, counter-clockwise seen from the side the normal points to."""
        for tri in ((a, b, c), (a, c, d)):
            if area(*tri) > EPS:
                self.triangles.append(tri)


@dataclass
class Edge:
    id: str
    feature: str
    faces: tuple[str, ...]
    segments: list[tuple[Vec, Vec]] = field(default_factory=list)


@dataclass
class Part:
    name: str
    faces: list[Face] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def face(self, feature: str, local: str) -> Face:
        face = Face(f"{feature}.{local}", feature)
        self.faces.append(face)
        return face

    def edge(self, a: Face, b: Face | None, segments: list[tuple[Vec, Vec]]) -> None:
        faces = (a.id,) if b is None else (a.id, b.id)
        feature = next((f.feature for f in (b, a) if f and f.feature != self.name), self.name)
        self.edges.append(Edge("--".join(faces), feature, faces, segments))


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def area(a: Vec, b: Vec, c: Vec) -> float:
    n = _cross(_sub(b, a), _sub(c, a))
    return 0.5 * math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)


def normal(tri: tuple[Vec, Vec, Vec]) -> Vec:
    n = _cross(_sub(tri[1], tri[0]), _sub(tri[2], tri[0]))
    length = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    return (n[0] / length, n[1] / length, n[2] / length)


# --- Plan shapes ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Footprint:
    """A rounded rectangle in board x-y. `ring(inset)` is the same outline moved inward."""

    x0: float
    x1: float
    y0: float
    y1: float
    radius: float

    def ring(self, inset: float) -> tuple[list[P2], list[int]]:
        """Counter-clockwise outline, and for each point the side (0-7) of the segment that
        starts there: 0 bottom, 1 bottom-right arc, 2 right, ... 7 bottom-left arc."""
        r = self.radius - inset
        if r <= 0:
            raise ValueError("inset larger than the corner radius")
        x0, x1, y0, y1 = self.x0 + inset, self.x1 - inset, self.y0 + inset, self.y1 - inset
        centres = [(x1 - r, y0 + r), (x1 - r, y1 - r), (x0 + r, y1 - r), (x0 + r, y0 + r)]
        steps = round(90 / ARC_STEP_DEG)
        points: list[P2] = []
        sides: list[int] = []
        for k, (cx, cy) in enumerate(centres):
            start = -90 + 90 * k
            for s in range(steps + 1):
                a = math.radians(start + 90 * s / steps)
                points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
                sides.append(2 * k + 1 if s < steps else (2 * k + 2) % 8)
        return points, sides


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def grow(self, d: float) -> Rect:
        return Rect(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def corners(self) -> list[P2]:
        return [(self.x0, self.y0), (self.x1, self.y0), (self.x1, self.y1), (self.x0, self.y1)]

    @property
    def centre(self) -> P2:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


def rect_of(position: P2, rotation: float, size: P2, grow: float = 0.0) -> Rect:
    """Axis-aligned rectangle of a board body or opening (rotations are multiples of 90°)."""
    quarter = round(rotation / 90)
    if abs(rotation - 90 * quarter) > 1e-6:
        raise ValueError(f"rotation {rotation} is not a multiple of 90 degrees")
    w, h = size if quarter % 2 == 0 else (size[1], size[0])
    x, y = position
    return Rect(x - w / 2, y - h / 2, x + w / 2, y + h / 2).grow(grow)


def inside_convex(poly: list[P2], p: P2, margin: float = 0.0) -> bool:
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        length = math.hypot(bx - ax, by - ay)
        if length < EPS:
            continue
        if ((bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)) / length < margin:
            return False
    return True


def _x_range(poly: list[P2], y: float) -> tuple[float, float]:
    xs = []
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        lo, hi = min(ay, by), max(ay, by)
        if lo - EPS <= y <= hi + EPS:
            if abs(by - ay) < EPS:
                xs += [ax, bx]
            else:
                xs.append(ax + (bx - ax) * (y - ay) / (by - ay))
    return min(xs), max(xs)


def cap_quads(poly: list[P2], holes: list[Rect]) -> list[tuple[P2, P2, P2, P2]]:
    """Tile a convex polygon minus axis-aligned rectangular holes with counter-clockwise
    trapezoids, slab by slab between every vertex and hole y."""
    ys = sorted({round(p[1], 9) for p in poly} | {h.y0 for h in holes} | {h.y1 for h in holes})
    out = []
    for ya, yb in zip(ys, ys[1:], strict=False):
        if yb - ya < 1e-7:
            continue
        la, ra = _x_range(poly, ya)
        lb, rb = _x_range(poly, yb)
        cuts = sorted((h.x0, h.x1) for h in holes if h.y0 <= ya + EPS and h.y1 >= yb - EPS)
        merged: list[list[float]] = []
        for a, b in cuts:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        # Pieces between the left edge, the holes and the right edge.
        lefts = [(la, lb)] + [(b, b) for _, b in merged]
        rights = [(a, a) for a, _ in merged] + [(ra, rb)]
        for (l0, l1), (r0, r1) in zip(lefts, rights, strict=True):
            out.append(((l0, ya), (r0, ya), (r1, yb), (l1, yb)))
    return out


# --- The enclosure ------------------------------------------------------------------------------


@dataclass
class Design:
    """Every value the sample enclosure is built from (fixture parameters)."""

    footprint: Footprint
    z_bottom: float
    z_top: float
    parting_z: float
    wall: float
    draft_deg: float
    clearance: float
    fit: float
    material: str
    # {tag, hole, x, y, radius (at the free end, widening by the draft towards the ceiling),
    #  insert (the pocket's feature tag), pocket_radius (at the mouth, narrowing by the draft
    #  as it goes in), pocket_depth}
    bosses: list[dict]
    openings: list[dict]  # {id, tag, component, kind, side, rect (inner cap), cut}


def opening_tags(board: Board) -> dict[str, str]:
    """`opening_<component ref>` per cut opening; `_<kind>` added if a part has several."""
    count: dict[str, int] = {}
    for o in board.openings:
        if o.kind in CUT_KINDS:
            count[o.component] = count.get(o.component, 0) + 1
    return {
        o.id: f"opening_{o.component}" + (f"_{o.kind}" if count[o.component] > 1 else "")
        for o in board.openings
        if o.kind in CUT_KINDS
    }


class Enclosure:
    """Builds the base and lid meshes for a design."""

    def __init__(self, design: Design):
        self.d = design
        self.tan = math.tan(math.radians(design.draft_deg))
        # Horizontal wall width that gives the nominal thickness square to the drafted face.
        self.wall_h = design.wall / math.cos(math.radians(design.draft_deg))

    def outer_inset(self, z: float) -> float:
        return abs(z - self.d.parting_z) * self.tan

    def inner_inset(self, z: float) -> float:
        return self.wall_h + abs(z - self.d.parting_z) * self.tan

    def inner_cap_z(self, part: str) -> float:
        return self.d.z_top - self.d.wall if part == "lid" else self.d.z_bottom + self.d.wall

    def build(self) -> list[Part]:
        d = self.d
        return [self._cup("base", d.z_bottom, "bottom"), self._cup("lid", d.z_top, "top")]

    def _cup(self, name: str, z_closed: float, side: str) -> Part:
        d = self.d
        part = Part(name)
        up = z_closed > d.parting_z  # the closed end is above the parting plane (lid)
        z_open = d.parting_z
        z_in = self.inner_cap_z(name)
        fp = d.footprint

        outer_open, sides = fp.ring(self.outer_inset(z_open))
        outer_closed, _ = fp.ring(self.outer_inset(z_closed))
        inner_open, _ = fp.ring(self.inner_inset(z_open))
        inner_closed, _ = fp.ring(self.inner_inset(z_in))

        holes = [o for o in d.openings if o["cut"] and o["side"] == side]
        grow = d.wall * self.tan  # holes widen toward the outside so they release from the mold
        inner_holes = [o["rect"] for o in holes]
        outer_holes = [o["rect"].grow(grow) for o in holes]
        for o, rect in zip(holes, outer_holes, strict=True):
            for corner in rect.corners():
                if not inside_convex(inner_closed, corner, margin=0.2):
                    raise ValueError(f"{o['id']} does not fit inside the {name}'s cap")

        outer_cap = part.face(name, "outer_cap")
        inner_cap = part.face(name, "inner_cap")
        rim = part.face(name, "rim")
        self._cap(outer_cap, outer_closed, outer_holes, z_closed, facing_up=up)
        self._cap(inner_cap, inner_closed, inner_holes, z_in, facing_up=not up)

        outer_walls = self._wall(
            part, "outer_wall", outer_closed, outer_open, sides, z_closed, z_open, outward=True
        )
        inner_walls = self._wall(
            part, "inner_wall", inner_closed, inner_open, sides, z_in, z_open, outward=False
        )

        # Rim: the ring at the parting plane between the outer and inner walls.
        n = len(outer_open)
        for i in range(n):
            j = (i + 1) % n
            a, b = (*outer_open[i], z_open), (*outer_open[j], z_open)
            c, e = (*inner_open[j], z_open), (*inner_open[i], z_open)
            if up:  # lid rim faces down
                rim.add_quad(a, e, c, b)
            else:
                rim.add_quad(a, b, c, e)

        self._ring_edges(part, outer_walls, outer_closed, sides, z_closed, outer_cap)
        self._ring_edges(part, outer_walls, outer_open, sides, z_open, rim)
        self._ring_edges(part, inner_walls, inner_closed, sides, z_in, inner_cap)
        self._ring_edges(part, inner_walls, inner_open, sides, z_open, rim)
        self._seam_edges(part, outer_walls, outer_closed, outer_open, sides, z_closed, z_open)
        self._seam_edges(part, inner_walls, inner_closed, inner_open, sides, z_in, z_open)

        for o, inner_rect, outer_rect in zip(holes, inner_holes, outer_holes, strict=True):
            self._hole(part, o["tag"], inner_rect, outer_rect, z_in, z_closed, inner_cap, outer_cap)
        if up:
            for boss in d.bosses:
                self._boss(part, boss, z_in, z_open, inner_cap)
        return part

    def _cap(self, face: Face, poly, holes, z, facing_up: bool) -> None:
        for a, b, c, e in cap_quads(poly, holes):
            q = [(*a, z), (*b, z), (*c, z), (*e, z)]
            face.add_quad(*(q if facing_up else q[::-1]))

    def _wall(self, part, local, ring_a, ring_b, sides, za, zb, outward) -> dict[int, Face]:
        """Wall between two rings, one face per side of the rounded rectangle."""
        faces = {s: part.face(part.name, f"{local}_{s + 1}") for s in range(8)}
        lo, hi = (ring_a, ring_b) if za < zb else (ring_b, ring_a)
        zlo, zhi = min(za, zb), max(za, zb)
        n = len(lo)
        for i in range(n):
            j = (i + 1) % n
            q = [(*lo[i], zlo), (*lo[j], zlo), (*hi[j], zhi), (*hi[i], zhi)]
            faces[sides[i]].add_quad(*(q if outward else q[::-1]))
        return faces

    def _ring_edges(self, part, walls, ring, sides, z, other: Face) -> None:
        n = len(ring)
        for s, wall in walls.items():
            segs = [((*ring[i], z), (*ring[(i + 1) % n], z)) for i in range(n) if sides[i] == s]
            part.edge(wall, other, segs)

    def _seam_edges(self, part, walls, ring_a, ring_b, sides, za, zb) -> None:
        n = len(ring_a)
        for i in range(n):
            prev = sides[i - 1]
            if prev != sides[i]:
                seg = ((*ring_a[i], za), (*ring_b[i], zb))
                part.edge(walls[prev], walls[sides[i]], [seg])

    def _hole(self, part, tag, inner: Rect, outer: Rect, z_in, z_out, inner_cap, outer_cap):
        """Four drafted side faces from the inner cap to the outer cap, normals into the hole."""
        ci, co = inner.corners(), outer.corners()
        up = z_out > z_in
        walls = []
        for k in range(4):
            face = part.face(tag, f"wall_{k + 1}")
            j = (k + 1) % 4
            a, b = (*ci[k], z_in), (*ci[j], z_in)
            c, e = (*co[j], z_out), (*co[k], z_out)
            # Seen from inside the hole the corners run clockwise when looking down.
            face.add_quad(a, e, c, b) if up else face.add_quad(a, b, c, e)
            walls.append(face)
            part.edge(face, inner_cap, [(a, b)])
            part.edge(face, outer_cap, [(e, c)])
        for k in range(4):
            j = (k + 1) % 4
            part.edge(walls[k], walls[j], [((*ci[j], z_in), (*co[j], z_out))])

    def boss_radius(self, boss: dict, z: float) -> float:
        """A boss's radius at height z: it narrows by the draft towards its free end."""
        return boss["radius"] + abs(z - self.d.parting_z) * self.tan

    def pocket_radius(self, boss: dict, depth: float) -> float:
        """A boss's insert pocket radius `depth` in from its mouth: it narrows by the draft."""
        return boss["pocket_radius"] - depth * self.tan

    def _boss(self, part, boss, z_ceiling, z_end, inner_cap) -> None:
        """A drafted boss hanging from the lid's ceiling down to the board's top face, with a
        drafted pocket for a heat-set insert going in from its free end."""
        tag, cx, cy = boss["tag"], boss["x"], boss["y"]
        depth = boss["pocket_depth"]
        z_pocket = z_end + depth if z_ceiling > z_end else z_end - depth

        def circle(r: float) -> list[P2]:
            return [
                (
                    cx + r * math.cos(2 * math.pi * k / BOSS_SEGMENTS),
                    cy + r * math.sin(2 * math.pi * k / BOSS_SEGMENTS),
                )
                for k in range(BOSS_SEGMENTS)
            ]

        foot = circle(self.boss_radius(boss, z_end))
        root = circle(self.boss_radius(boss, z_ceiling))
        mouth = circle(self.pocket_radius(boss, 0.0))
        bottom = circle(self.pocket_radius(boss, depth))
        side = part.face(tag, "side")
        end = part.face(tag, "end")
        wall = part.face(boss["insert"], "wall")
        floor = part.face(boss["insert"], "end")
        n = BOSS_SEGMENTS
        ring = range(n)
        for i in ring:
            j = (i + 1) % n
            side.add_quad(
                (*foot[i], z_end), (*foot[j], z_end), (*root[j], z_ceiling), (*root[i], z_ceiling)
            )
            # The free end: the ring between the boss's side and the pocket's mouth.
            end.add_quad(
                (*foot[i], z_end), (*mouth[i], z_end), (*mouth[j], z_end), (*foot[j], z_end)
            )
            # The pocket's wall faces into the pocket.
            wall.add_quad(
                (*mouth[i], z_end),
                (*bottom[i], z_pocket),
                (*bottom[j], z_pocket),
                (*mouth[j], z_end),
            )
        centre = (cx, cy, z_pocket)
        for i in ring:
            j = (i + 1) % n
            floor.triangles.append((centre, (*bottom[j], z_pocket), (*bottom[i], z_pocket)))

        def loop(points: list[P2], z: float) -> list[tuple[Vec, Vec]]:
            return [((*points[i], z), (*points[(i + 1) % n], z)) for i in ring]

        part.edge(side, end, loop(foot, z_end))
        part.edge(side, inner_cap, loop(root, z_ceiling))
        part.edge(end, wall, loop(mouth, z_end))
        part.edge(wall, floor, loop(bottom, z_pocket))
