"""Where each design parameter is measured on the model: `Parameter.dimension`, inferred from a
build's mesh.

Nothing declares where a value is measured: a design is code and named values. So this reads
the built model and puts a dimension on a parameter only when the model shows the value plainly:

1. **Overall extents.** A length whose value is the extent of the assembled enclosure parts
   along X, Y or Z (within `tolerance()`) is the distance between the outermost faces on that
   axis: one end on the lowest face, one on the highest, the line parallel to the axis. When
   several parameters match one axis, the one whose name or description says it is a size
   (length, width, height, outside, overall...) wins, then the closest value, then the name.
2. **Radii.** A length tied to a feature (`Parameter.feature`, or named `<feature>_radius`)
   whose value is the radius of a round face of that feature (a cylinder, or one end of a
   drafted cone) is a radius: from the centre on the face's axis to a point on the face.

Everything else keeps `dimension: null` (an angle, a gap applied everywhere, a material, a z
level that isn't an extent): no guessing. `drag` is never inferred: whether dragging a face
changes a value one for one depends on how the code builds it (a centred box grows both ways).

The mesh is the build's contract glTF (`contracts/README.md`), so this works the same for every
build: a session's, a stored one after a restart, a demo's and its copies. Its vertices are
points of the exact B-rep faces (the tessellator puts every node on its surface), stored as
32-bit floats: a few millionths of a millimetre off, far inside the tolerance.

A parameter without a unit (a design written in a session lists its values bare) counts as a
length when it matches, since board coordinates are millimetres, unless its name says it is an
angle or a ratio. It comes back with `unit: "mm"`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

from app.contracts.mesh import MeshInfo, PartInfo, validate_mesh
from app.contracts.session import Dimension, DimensionEnd, Parameter

AXES = "xyz"
# Two vertices this close (mm) are the same place: far above float32 noise, far below a feature.
EPS = 1e-3
# Names that say a value is a size of the whole part, and those that say it is not a length.
SIZE_WORDS = ("length", "width", "height", "depth", "outside", "overall", "size", "extent")
NOT_LENGTH = re.compile(r"(^|_)(draft|angle|lean|tilt|taper|deg|degrees|ratio|count|n)(_|$)")
LEVEL = re.compile(r"^[xyz]_|_[xyz]$")  # a coordinate (z_top), not a size


def tolerance(value: float) -> float:
    """How close a measurement must be to a value to be it: 0.01 mm or 0.1 %, the larger."""
    return max(0.01, 1e-3 * abs(value))


@dataclass(frozen=True)
class Extent:
    """The assembled enclosure's size along one axis, between its outermost faces."""

    axis: int
    length: float
    start: DimensionEnd
    end: DimensionEnd


@dataclass(frozen=True)
class Round:
    """A round face: a circle of it, about an axis-aligned axis."""

    feature: str
    part: str
    face: str
    radius: float
    centre: tuple[float, float, float]
    point: tuple[float, float, float]  # on the face, `radius` from `centre`
    axis: int
    cylinder: bool  # the same radius all along (else one end of a cone)
    full: bool  # all the way round (else an arc, as a rounded corner)


@dataclass(frozen=True)
class Sites:
    """What a build's model can measure."""

    extents: tuple[Extent, ...]
    rounds: tuple[Round, ...]
    faces: frozenset[str]  # every face ID in the mesh
    features: frozenset[str]


# --- Reading the mesh ---------------------------------------------------------------------------


@dataclass
class _Face:
    id: str
    feature: str
    part: str
    points: np.ndarray  # (n, 3), unique vertices
    normal: np.ndarray  # area-weighted unit normal (zero if none)


def _faces(part: PartInfo) -> list[_Face]:
    pos = np.asarray(part.positions, dtype=float)
    owner = np.asarray(part.face_index)
    tris = np.asarray(part.indices or range(len(pos)), dtype=int).reshape(-1, 3)
    normals = np.zeros((len(part.faces), 3))
    if len(tris):
        a, b, c = pos[tris[:, 0]], pos[tris[:, 1]], pos[tris[:, 2]]
        np.add.at(normals, owner[tris[:, 0]], np.cross(b - a, c - a))
    out = []
    for i, face in enumerate(part.faces):
        pts = pos[owner == i]
        if not len(pts):
            continue
        pts = np.unique(np.round(pts, 6), axis=0)
        n = normals[i]
        norm = float(np.linalg.norm(n))
        out.append(_Face(face.id, face.feature, part.name, pts, n / norm if norm else n))
    return out


def _point(p: np.ndarray) -> tuple[float, float, float]:
    return (round(float(p[0]), 4), round(float(p[1]), 4), round(float(p[2]), 4))


def _extents(faces: list[_Face]) -> list[Extent]:
    if not faces:
        return []
    allpts = np.concatenate([f.points for f in faces])
    lo_all, hi_all = allpts.min(axis=0), allpts.max(axis=0)
    out = []
    for axis in range(3):
        lo, hi = float(lo_all[axis]), float(hi_all[axis])
        if hi - lo < EPS:
            continue
        ends = []
        for level, sign in ((lo, -1.0), (hi, 1.0)):
            best = None
            for f in faces:
                at = f.points[np.abs(f.points[:, axis] - level) < EPS]
                if not len(at):
                    continue
                # The face that looks most along the axis, then the one most on the extreme.
                key = (-round(sign * float(f.normal[axis]), 6), -len(at), f.id)
                if best is None or key < best[0]:
                    best = (key, f, at)
            assert best is not None
            ends.append(best)
        (_, f_lo, at_lo), (_, f_hi, at_hi) = ends
        perp = [i for i in range(3) if i != axis]
        spot = _common_spot(at_lo[:, perp], at_hi[:, perp])
        start, end = np.empty(3), np.empty(3)
        start[perp] = end[perp] = spot
        start[axis], end[axis] = lo, hi
        out.append(
            Extent(
                axis=axis,
                length=hi - lo,
                start=DimensionEnd(point=_point(start), part=f_lo.part, face=f_lo.id),
                end=DimensionEnd(point=_point(end), part=f_hi.part, face=f_hi.id),
            )
        )
    return out


def _common_spot(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """A place (in the two coordinates across the axis) where both outermost faces reach the
    extreme: a vertex of either that lies within both, nearest the middle of their overlap.
    Without an overlap, the middle between them."""
    lo, hi = np.maximum(a.min(axis=0), b.min(axis=0)), np.minimum(a.max(axis=0), b.max(axis=0))
    if np.any(hi < lo - EPS):
        return (a.mean(axis=0) + b.mean(axis=0)) / 2
    middle = (lo + hi) / 2
    pts = np.concatenate([a, b])
    inside = pts[np.all((pts >= lo - EPS) & (pts <= hi + EPS), axis=1)]
    if not len(inside):
        return middle
    return inside[np.argmin(np.linalg.norm(inside - middle, axis=1))]


def _on_line(pts: np.ndarray) -> bool:
    centred = pts - pts.mean(axis=0)
    scale = max(1.0, float(np.abs(centred).max()))
    return bool(np.linalg.svd(centred, compute_uv=False)[-1] < 1e-6 * scale)


def _revolution(pts: np.ndarray, axis: int) -> tuple[np.ndarray, float, float] | None:
    """If the points lie on a cylinder or cone about an axis parallel to `axis`: its centre
    (across the axis), and the radius r0 + k·t at t along the axis as (r0, k).

    The centre is a linear least-squares fit of |p - centre|² = a + b·t + c·t² (exact for a
    cone), the radius a straight line through the points' distances from it, and every point
    is then checked against both. A flat face (no extent along the axis, or across it on a
    line) is not a round face; nor is one with too few points to tell."""
    perp = [i for i in range(3) if i != axis]
    uv, t = pts[:, perp], pts[:, axis]
    if len(pts) < 8 or np.ptp(t) < EPS or _on_line(uv):
        return None
    a = np.column_stack([2 * uv, np.ones(len(pts)), t, t * t])
    rhs = (uv**2).sum(axis=1)
    (cx, cy, *_), *_ = np.linalg.lstsq(a, rhs, rcond=None)
    centre = np.array([cx, cy])
    dist = np.linalg.norm(uv - centre, axis=1)
    k, r0 = np.polyfit(t, dist, 1)
    if dist.max() > 1e4 or np.abs(r0 + k * t - dist).max() > EPS:
        return None
    return centre, float(r0), float(k)


def _rounds(face: _Face) -> list[Round]:
    """The face's circles if it is a cylinder or cone about an X, Y or Z axis: one site along
    a cylinder (at mid-length), one at each end of a cone."""
    for axis in range(3):
        fit = _revolution(face.points, axis)
        if fit is None:
            continue
        centre, r0, k = fit
        perp = [i for i in range(3) if i != axis]
        t = face.points[:, axis]
        lo, hi = float(t.min()), float(t.max())
        cylinder = abs(k * (hi - lo)) < EPS
        levels = [(lo + hi) / 2] if cylinder else [lo, hi]
        spread = face.points[:, perp] - centre
        angles = np.sort(np.arctan2(spread[:, 1], spread[:, 0]))
        gaps = np.diff(np.concatenate([angles, angles[:1] + 2 * math.pi]))
        full = bool(gaps.max() < math.pi / 2)  # all the way round (the tessellation's steps)
        # Towards the middle of the arc the face covers (any way round a full circle).
        d = spread.mean(axis=0)
        d = d / np.linalg.norm(d) if not full and np.linalg.norm(d) > 1e-9 else np.array([1.0, 0.0])
        out = []
        for level in levels:
            r = r0 + k * level
            c3, p3 = np.empty(3), np.empty(3)
            c3[axis] = p3[axis] = level
            c3[perp] = centre
            p3[perp] = centre + r * d
            out.append(
                Round(
                    feature=face.feature,
                    part=face.part,
                    face=face.id,
                    radius=r,
                    centre=_point(c3),
                    point=_point(p3),
                    axis=axis,
                    cylinder=cylinder,
                    full=full,
                )
            )
        return out
    return []


def sites_of(mesh: MeshInfo) -> Sites:
    faces = {p.name: _faces(p) for p in mesh.parts.values()}
    enclosure = [f for p in mesh.parts.values() if p.kind == "enclosure" for f in faces[p.name]]
    rounds = [r for fs in faces.values() for f in fs for r in _rounds(f)]
    return Sites(
        extents=tuple(_extents(enclosure)),
        rounds=tuple(rounds),
        faces=frozenset(mesh.face_parts),
        features=frozenset(mesh.features),
    )


@lru_cache(maxsize=32)
def mesh_sites(mesh: str) -> Sites:
    """The sites of a build's glTF (cached: the parameters are listed often)."""
    return sites_of(validate_mesh(mesh.encode()))


# --- Matching parameters ------------------------------------------------------------------------


def _length(p: Parameter) -> float | None:
    """The parameter's value if it can be a length."""
    if isinstance(p.value, str | bool):
        return None
    if p.unit == "deg" or (p.unit is None and NOT_LENGTH.search(p.name.lower())):
        return None
    value = float(p.value)
    return value if value > 0 else None


def _size_score(p: Parameter, axis: int) -> int:
    text = f"{p.name} {p.description}".lower()
    score = sum(word in text for word in SIZE_WORDS)
    if axis == 2 and ("height" in text or "tall" in text):
        score += 1
    if LEVEL.search(p.name.lower()):
        score -= 2
    if "." in p.name:  # a value inside a group: the design's own top-level sizes come first
        score -= 1
    return score


def _extent_matches(params: Sequence[Parameter], sites: Sites) -> dict[str, Dimension]:
    pairs = []
    for p in params:
        value = _length(p)
        if value is None:
            continue
        for e in sites.extents:
            diff = abs(value - e.length)
            if diff <= tolerance(value):
                pairs.append(((-_size_score(p, e.axis), diff, e.axis, p.name), p, e))
    out: dict[str, Dimension] = {}
    taken: set[int] = set()
    for _, p, e in sorted(pairs, key=lambda t: t[0]):
        if p.name in out or e.axis in taken:
            continue
        taken.add(e.axis)
        out[p.name] = Dimension(kind="distance", start=e.start, end=e.end)
    return out


def _natural(name: str) -> tuple:
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name))


ROUND_NAME = re.compile(r"(?P<prefix>.+?)[._](?P<what>radius|diameter)")


def _round_features(p: Parameter, features: frozenset[str]) -> tuple[list[str], str] | None:
    """The features a radius or diameter parameter is of, and which it is: its own feature
    (`Parameter.feature`), or the one its name starts with (`boss_1_radius`), or every feature
    of the kind it names (`boss.radius`, `hole_diameter`: `boss_1`, `boss_2`...)."""
    m = ROUND_NAME.fullmatch(p.name)
    what = m.group("what") if m else "radius"
    if p.feature:
        return [p.feature], what
    if m is None:
        return None
    prefix = m.group("prefix")
    if prefix in features:
        return [prefix], what
    kind = re.compile(re.escape(prefix) + r"_\d+")
    named = sorted((f for f in features if kind.fullmatch(f)), key=_natural)
    return (named, what) if named else None


def _round_match(p: Parameter, sites: Sites) -> Dimension | None:
    value = _length(p)
    of = _round_features(p, sites.features)
    if value is None or of is None:
        return None
    features, what = of
    order = {f: i for i, f in enumerate(features)}
    radius = value / 2 if what == "diameter" else value
    found = sorted(
        (
            r
            for r in sites.rounds
            if r.feature in order
            and (r.full or what == "radius")
            and abs(r.radius - radius) <= tolerance(radius)
        ),
        key=lambda r: (not r.cylinder, order[r.feature], abs(r.radius - radius), r.face, r.centre),
    )
    if not found:
        return None
    r = found[0]
    end = DimensionEnd(point=r.point, part=r.part, face=r.face)
    if what == "radius":
        return Dimension(kind="radius", start=DimensionEnd(point=r.centre), end=end)
    # Across the circle, through its centre: both ends on the face.
    opposite = tuple(round(2 * c - q, 4) for c, q in zip(r.centre, r.point, strict=True))
    start = DimensionEnd(point=opposite, part=r.part, face=r.face)
    return Dimension(kind="distance", start=start, end=end)


def _faces_exist(dim: Dimension, sites: Sites) -> bool:
    return all(e.face is None or e.face in sites.faces for e in (dim.start, dim.end))


def with_dimensions(params: Sequence[Parameter], sites: Sites | None) -> list[Parameter]:
    """`params` with each one's dimension as the model shows it (copies; `params` unchanged).

    A dimension or drag a parameter already has is kept when every face it names is on this
    model, and dropped otherwise (it was measured on another build). Without a model (`sites`
    None: a failed build, an import), nothing is measured."""
    if sites is None:
        return [p.model_copy() for p in params]
    kept = {p.name for p in params if p.dimension is not None and _faces_exist(p.dimension, sites)}
    extents = _extent_matches([p for p in params if p.name not in kept], sites)
    out = []
    for p in params:
        update: dict = {}
        if p.drag is not None and not set(p.drag.faces) <= sites.faces:
            update["drag"] = None
        if p.name not in kept:
            dim = extents.get(p.name) or _round_match(p, sites)
            update["dimension"] = dim
            if dim is not None and p.unit is None:
                update["unit"] = "mm"
        out.append(p.model_copy(update=update))
    return out


def grouped_values(design: dict[str, Any], prefix: str = "") -> list[Parameter]:
    """The numbers inside the design's groups (`boss.radius`), by path: values a person can
    edit (the session API sets a path), though a design lists only its top-level ones."""
    out = []
    for key, value in design.items():
        if not isinstance(key, str):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out += grouped_values(value, f"{path}.")
        elif prefix and isinstance(value, int | float) and not isinstance(value, bool):
            try:
                out.append(Parameter(name=path, value=float(value), locked=False))
            except ValueError:  # a key that can't be in a parameter name
                continue
    return out


def measured(
    params: Sequence[Parameter], mesh: str | None, design: dict[str, Any] | None = None
) -> list[Parameter]:
    """The parameters with their dimensions on a build's glTF text (None: no model). With the
    design's values (`design`), the numbers inside its groups that measure on the model
    (`boss.radius` 4.5 on the bosses) come after them, as parameters a person can edit there;
    the rest stay out of the list."""
    sites = mesh_sites(mesh) if mesh else None
    named = {p.name for p in params}
    extra = [g for g in grouped_values(design or {}) if g.name not in named] if sites else []
    out = with_dimensions([*params, *extra], sites)
    return out[: len(params)] + [p for p in out[len(params) :] if p.dimension is not None]
