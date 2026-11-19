"""Analysis maps: per-face wall thickness and draft on a built enclosure.

Both are measured on the exact B-rep, at the same sample points the checks use
(`occ.face_samples(face, SAMPLES)`), so a map and its check always agree:

- **Wall thickness.** From each sample, a ray goes square into the material; its length to the
  next surface is the thickness there (`ray`). That is all the `wall_thickness` check looks at,
  so the thin side of the map is exactly the check: a face is thin when its shortest ray is below
  `min_wall`, with that value.

  A ray that starts on the narrow end of a wall (a rim, the side of a cut-out) runs along the
  wall rather than across it and comes back long, although the wall is not thick. So the map
  also measures the ball that fits at the ray's midpoint (twice its exact distance to the
  surface) and takes a sample's thickness as `min(ray, max(ball, min_wall))`: the ball can shorten
  a long ray but never make a sample thin, which stays the ray's (and the check's) call. A face
  shows its thinnest sample if it has a thin one, else its thickest, and is flagged `thick` when
  that is more than `THICK_FACTOR` times `min_wall` (thick sections sink when molded).
- **Draft.** The angle between each sample's normal and the mold pull (Z); a face shows its
  smallest, like the `draft_angle` check, and is flagged `low_draft` below `min_draft`. Faces
  square to the pull have no draft to judge and show null.

`analysis_maps()` turns the per-face values into the analysis-map contract
(`contracts/README.md`, "Analysis maps"): one value per vertex of a contract mesh, ready to
become vertex colors.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from OCP.TopoDS import TopoDS_Face, TopoDS_Shape

from app.contracts.maps import MAP_TYPES, MAPS_CONTRACT
from app.contracts.mesh import MeshInfo
from app.standards import StandardsFile
from geometry import occ
from geometry.interface import BuildResult
from geometry.params import THICK_FACTOR  # walls thicker than this times `min_wall` are flagged

SAMPLES = 4  # n x n sample grid per face, shared with the checks
LEGEND_FACTOR = 3.0  # the color scale runs from 0 to this times the standard's limit
SQUARE_TO_PULL = 0.999  # |normal z| above this: the face is square to the pull, no draft
DIGITS = 3

Point = occ.Point


@dataclass(frozen=True)
class Sample:
    point: Point
    normal: Point
    ray: float | None  # wall thickness along the inward normal; None if the ray left the part
    ball: float | None = None  # diameter of the ball at the ray's midpoint


def wall_samples(
    shape: TopoDS_Shape, face: TopoDS_Face, rays: occ.RayCaster, balls: bool = True
) -> list[Sample]:
    """Thickness samples of one face of `shape` (the `wall_thickness` check uses the rays)."""
    out = []
    for p, n in occ.face_samples(face, SAMPLES):
        t = rays.first_hit(p, (-n[0], -n[1], -n[2]))
        ball = None
        if balls and t is not None:
            mid = (p[0] - n[0] * t / 2, p[1] - n[1] * t / 2, p[2] - n[2] * t / 2)
            ball = 2 * occ.distance(occ.vertex(mid), shape)[0]
        out.append(Sample(p, n, t, ball))
    return out


def draft_degrees(normal: Point) -> float:
    """Draft of a surface against the Z pull (90 for a face square to the pull)."""
    return occ.draft_degrees(normal)


@dataclass(frozen=True)
class FaceValue:
    """A face's value in one map, and the sample where it occurs."""

    value: float | None
    point: Point | None
    kind: str | None = None  # "thin", "thick" or "low_draft" when the face is flagged


@dataclass(frozen=True)
class FaceAnalysis:
    part: str
    index: int  # in `occ.faces(part)`
    feature: str
    wall_thickness: FaceValue
    draft: FaceValue

    def value(self, map_type: str) -> FaceValue:
        return getattr(self, map_type)


def _thickness(samples: list[Sample], limit: float) -> FaceValue:
    values = []
    for s in samples:
        if s.ray is None:
            continue
        ball = s.ball if s.ball is not None else s.ray
        values.append((min(s.ray, max(ball, limit)), s.point))
    if not values:
        return FaceValue(None, None)
    thinnest = min(values, key=lambda v: v[0])
    if thinnest[0] < limit:
        return FaceValue(thinnest[0], thinnest[1], "thin")
    thickest = max(values, key=lambda v: v[0])
    kind = "thick" if thickest[0] > THICK_FACTOR * limit else None
    return FaceValue(thickest[0], thickest[1], kind)


def _draft(samples: list[Sample], limit: float) -> FaceValue:
    drafted = [(draft_degrees(s.normal), s.point) for s in samples]
    drafted = [d for d in drafted if math.sin(math.radians(d[0])) <= SQUARE_TO_PULL]
    if not drafted:
        return FaceValue(None, None)
    a, p = min(drafted, key=lambda d: d[0])
    return FaceValue(a, p, "low_draft" if a < limit else None)


def analyze(result: BuildResult, standards: StandardsFile) -> dict[str, list[FaceAnalysis]]:
    """Wall thickness and draft of every face of every molded part, in `occ.faces()` order.

    Bought-in parts (the inserts) are not molded, so the molding maps leave them out."""
    min_wall = float(standards.get(MAP_TYPES["wall_thickness"][0]).value)
    min_draft = float(standards.get(MAP_TYPES["draft"][0]).value)
    out: dict[str, list[FaceAnalysis]] = {}
    for part, shape in (result.enclosure or result.parts).items():
        rays = occ.RayCaster(shape)
        refs = result.face_refs[part]
        faces = []
        for index, face in enumerate(occ.faces(shape)):
            samples = wall_samples(shape, face, rays)
            faces.append(
                FaceAnalysis(
                    part,
                    index,
                    refs[index].feature,
                    _thickness(samples, min_wall),
                    _draft(samples, min_draft),
                )
            )
        out[part] = faces
    return out


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, DIGITS) + 0.0


def _point(p: Point) -> list[float]:
    return [_round(c) for c in p]


MESSAGES = {
    "thin": "{feature} is {value:g} {unit} thick, thinner than the {limit:g} {unit} minimum "
    "({standard})",
    "thick": "{feature} is {value:g} {unit} thick, more than {factor:g} times the {limit:g} "
    "{unit} minimum ({standard}); thick sections sink when molded",
    "low_draft": "{feature} has {value:g} {unit} of draft, less than the {limit:g} {unit} "
    "minimum ({standard})",
}


def analysis_maps(
    analysis: Mapping[str, list[FaceAnalysis]],
    standards: StandardsFile,
    mesh: MeshInfo,
    build_id: str,
    brep_faces: Mapping[str, int] | None = None,
) -> dict[str, dict]:
    """Every map type as an analysis-map document, aligned with `mesh`.

    `brep_faces` maps a mesh face ID to the index of the B-rep face it was tessellated from
    (`occ.faces(part)` order). By default a part's mesh faces are its B-rep faces in order.
    """
    out = {}
    for map_type, (standard_id, unit) in MAP_TYPES.items():
        standard = standards.get(standard_id)
        limit = float(standard.value)
        parts, flagged = [], []
        for name, info in mesh.parts.items():
            if info.kind != "enclosure":
                continue  # not molded: the inserts carry no molding map values
            faces = analysis.get(name)
            if faces is None:
                raise ValueError(f"no analysis for mesh part {name!r}")
            if brep_faces is None and len(info.faces) != len(faces):
                raise ValueError(
                    f"{name}: {len(info.faces)} mesh faces for {len(faces)} B-rep faces; "
                    "pass brep_faces"
                )
            per_face = []
            for i, mesh_face in enumerate(info.faces):
                index = brep_faces[mesh_face.id] if brep_faces is not None else i
                fa = faces[index]
                fv = fa.value(map_type)
                per_face.append(_round(fv.value))
                if fv.kind is None:
                    continue
                value = _round(fv.value)
                flagged.append(
                    {
                        "id": f"{fv.kind}:{mesh_face.id}",
                        "kind": fv.kind,
                        "value": value,
                        "message": MESSAGES[fv.kind].format(
                            feature=fa.feature,
                            value=value,
                            unit=unit,
                            limit=limit,
                            # The standard's title, never its ID (QA fix brief 2).
                            standard=standard.title,
                            factor=THICK_FACTOR,
                        ),
                        "location": {
                            "point": _point(fv.point),
                            "part": name,
                            "faces": [mesh_face.id],
                            "edges": [],
                        },
                    }
                )
            parts.append({"part": name, "values": [per_face[f] for f in info.face_index]})
        out[map_type] = {
            "contract": MAPS_CONTRACT,
            "build_id": build_id,
            "type": map_type,
            "standard": standard_id,
            "standard_title": standard.title,
            "unit": unit,
            "legend": {"min": 0.0, "max": LEGEND_FACTOR * limit, "limit": limit, "unit": unit},
            "parts": parts,
            "flagged": flagged,
        }
    return out


__all__ = [
    "LEGEND_FACTOR",
    "SAMPLES",
    "THICK_FACTOR",
    "FaceAnalysis",
    "FaceValue",
    "Sample",
    "analysis_maps",
    "analyze",
    "wall_samples",
]
