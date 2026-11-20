"""Tessellation: a build's parts as a glTF that follows the mesh contract, with stable IDs.

`tessellate(result, previous)` meshes every face of every part and gives each face and edge an
ID that survives rebuilds, so comments, selections and check locations still point at the
same geometry after Claude or the user changes a parameter:

1. **Feature tags first.** A face's ID starts with its part and the feature that created it
   (`BuildResult.face_refs`), so a face can only ever inherit an ID from a face of the same
   feature.
2. **Then position and shape.** Within a feature, each face is matched to a face of the
   previous build by surface type, outward direction, area and position relative to the
   feature's tools. Positions are relative, so an opening that moves keeps its faces' IDs.
   Faces with no match get a new number that the feature has never used.

Without a previous build, faces are numbered in a canonical order (surface type, direction,
relative position). Edge IDs are derived from the IDs of the faces they bound.

`Tessellation.signatures()` is plain JSON: store it with a build and pass it back as
`previous` to keep IDs across processes. `Tessellation.to_gltf(colors)` writes the glTF, with
optional per-vertex colors (for example an analysis map baked in).
"""

from __future__ import annotations

from typing import Any

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from geometry import occ
from geometry.interface import BuildResult
from geometry.mesh_data import (  # noqa: F401 - the mesh's data, re-exported
    DIGITS,
    MATCH_LIMIT,
    MAX_IDS,
    Color,
    FaceSignature,
    MeshEdge,
    MeshFace,
    PartMesh,
    Tessellation,
    Vec,
    _cross,
    _norm,
    _round,
    _sub,
    _unit,
    _write_gltf,
)

LINEAR_DEFLECTION = 0.05  # mm
ANGULAR_DEFLECTION = 0.25  # radians


def tessellate(
    result: BuildResult,
    previous: Tessellation | dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    linear_deflection: float = LINEAR_DEFLECTION,
    angular_deflection: float = ANGULAR_DEFLECTION,
    face_ids: dict[str, list[str]] | None = None,
) -> Tessellation:
    """Mesh a build, keeping the face and edge IDs of `previous` where the faces match.
    `face_ids` gives some parts' face IDs outright, one per face in `occ.faces()` order (a
    build read back from the STEP file it was exported as: `geometry.step_import`)."""
    old = _previous_faces(previous)
    counters = dict(
        previous.counters
        if isinstance(previous, Tessellation)
        else (previous or {}).get("counters", {})
    )
    order = {tag: i for i, tag in enumerate(result.tags)}
    anchors = {}
    for f in result.features:
        anchors.setdefault((f.part, f.tag), []).extend(f.tools)

    parts: dict[str, PartMesh] = {}
    for name, shape in result.parts.items():
        BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)
        faces = occ.faces(shape)
        refs = result.face_refs[name]
        signatures = [
            _signature(name, ref.feature, face, anchors.get((name, ref.feature), []))
            for face, ref in zip(faces, refs, strict=True)
        ]
        given = (face_ids or {}).get(name)
        if given is not None and len(given) == len(faces) and len(set(given)) == len(given):
            ids = list(given)
            for face_id in ids:
                group, _, number = face_id.rpartition(".")
                if number.isdigit():
                    counters[group] = max(counters.get(group, 0), int(number) + 1)
        else:
            ids = _assign_ids(name, signatures, old, counters)
        part = PartMesh(name, result.kind(name), result.materials.get(name), result.placement(name))
        for face, ref, sig, face_id in zip(faces, refs, signatures, ids, strict=True):
            part.faces.append(MeshFace(face_id, ref.feature, sig))
            _add_triangles(part, face, len(part.faces) - 1)
        part.edges = _edges(shape, faces, part.faces, order, linear_deflection, angular_deflection)
        parts[name] = part

    all_ids = [f.id for p in parts.values() for f in p.faces]
    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("face IDs are not unique")
    info = {"backend": result.backend, "generator": "claude-hardware geometry.tessellate"}
    return Tessellation(parts, counters, {**info, **(source or {})})


# --- Stable IDs ---------------------------------------------------------------------------------


def _group(part: str, feature: str) -> str:
    return f"{part}.{feature}"


def _previous_faces(previous: Tessellation | dict | None) -> dict[str, list[tuple[str, Any]]]:
    """Previous faces by group: {"<part>.<feature>": [(id, signature), ...]}."""
    out: dict[str, list[tuple[str, FaceSignature]]] = {}
    if previous is None:
        return out
    if isinstance(previous, Tessellation):
        items = [(f.id, f.signature) for f in previous.faces.values()]
    else:
        items = [
            (
                face_id,
                FaceSignature(
                    part=s["part"],
                    feature=s["feature"],
                    kind=s["kind"],
                    direction=tuple(s["direction"]),
                    position=tuple(s["position"]),
                    area=s["area"],
                    scale=s["scale"],
                ),
            )
            for face_id, s in previous.get("faces", {}).items()
        ]
    for face_id, sig in items:
        out.setdefault(_group(sig.part, sig.feature), []).append((face_id, sig))
    return out


def _assign_ids(
    part: str,
    signatures: list[FaceSignature],
    old: dict[str, list[tuple[str, FaceSignature]]],
    counters: dict[str, int],
) -> list[str]:
    ids: list[str | None] = [None] * len(signatures)
    groups: dict[str, list[int]] = {}
    for i, sig in enumerate(signatures):
        groups.setdefault(_group(part, sig.feature), []).append(i)
    for group, members in groups.items():
        # Inherit: cheapest pairs first, each old and new face used once.
        candidates = sorted(
            (signatures[i].cost(sig), face_id, i)
            for i in members
            for face_id, sig in old.get(group, [])
        )
        taken: set[str] = set()
        for cost, face_id, i in candidates:
            if cost < MATCH_LIMIT and ids[i] is None and face_id not in taken:
                ids[i] = face_id
                taken.add(face_id)
        # New faces: canonical order, numbers this feature has never used.
        used = [_number(face_id) for face_id, _ in old.get(group, [])]
        next_number = max([counters.get(group, 0), *(n + 1 for n in used if n is not None)])
        for i in sorted(
            (i for i in members if ids[i] is None), key=lambda i: signatures[i].sort_key()
        ):
            ids[i] = f"{group}.{next_number}"
            next_number += 1
        counters[group] = next_number
    return [i for i in ids if i is not None]


def _number(face_id: str) -> int | None:
    tail = face_id.rsplit(".", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _signature(part: str, feature: str, face, tools: list[TopoDS_Shape]) -> FaceSignature:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    if tools:
        boxes = [occ.bbox(t) for t in tools]
        lo = tuple(min(b[0][k] for b in boxes) for k in range(3))
        hi = tuple(max(b[1][k] for b in boxes) for k in range(3))
    else:
        lo = hi = (0.0, 0.0, 0.0)
    anchor = tuple((lo[k] + hi[k]) / 2 for k in range(3))
    scale = _norm(_sub(hi, lo))
    kind = GeomAbs_SurfaceType(BRepAdaptor_Surface(face).GetType()).name.removeprefix("GeomAbs_")
    return FaceSignature(
        part=part,
        feature=feature,
        kind=kind,
        direction=_mean_normal(face),
        position=_sub((c.X(), c.Y(), c.Z()), anchor),
        area=abs(props.Mass()),
        scale=scale,
    )


def _mean_normal(face) -> Vec:
    """Area-weighted mean outward normal over the face's triangles."""
    total = [0.0, 0.0, 0.0]
    area = 0.0
    for a, b, c in _triangles(face):
        n = _cross(_sub(b, a), _sub(c, a))
        total = [total[k] + n[k] for k in range(3)]
        area += _norm(n)
    if area == 0:
        return (0.0, 0.0, 0.0)
    return (total[0] / area, total[1] / area, total[2] / area)


# --- Meshing ------------------------------------------------------------------------------------


def _face_mesh(face):
    """(nodes, triangles as node-index triples, normals) of a meshed face, outward-facing."""
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        return [], [], []
    trsf = loc.Transformation()
    nodes = []
    for i in range(1, tri.NbNodes() + 1):
        p = tri.Node(i).Transformed(trsf)
        nodes.append((p.X(), p.Y(), p.Z()))
    reversed_ = face.Orientation() == TopAbs_REVERSED
    triangles = []
    for i in range(1, tri.NbTriangles() + 1):
        a, b, c = tri.Triangle(i).Get()
        triangles.append((a - 1, c - 1, b - 1) if reversed_ else (a - 1, b - 1, c - 1))
    if not tri.HasNormals():
        BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, tri)
    sign = -1.0 if reversed_ else 1.0
    normals = []
    for i in range(1, tri.NbNodes() + 1):
        d = tri.Normal(i).Transformed(trsf)
        normals.append((sign * d.X(), sign * d.Y(), sign * d.Z()))
    return nodes, triangles, normals


def _triangles(face) -> list[tuple[Vec, Vec, Vec]]:
    nodes, triangles, _ = _face_mesh(face)
    return [(nodes[a], nodes[b], nodes[c]) for a, b, c in triangles]


def _add_triangles(part: PartMesh, face, face_number: int) -> None:
    nodes, triangles, normals = _face_mesh(face)
    if not triangles:
        raise RuntimeError(f"{part.name}: face {part.faces[face_number].id} has no triangles")
    base = len(part.positions)
    part.positions.extend(_round(p) for p in nodes)
    part.normals.extend(_round(_unit(n)) for n in normals)
    part.face_index.extend([face_number] * len(nodes))
    for a, b, c in triangles:
        part.indices.extend((base + a, base + b, base + c))


def _edges(
    shape: TopoDS_Shape,
    faces: list,
    mesh_faces: list[MeshFace],
    order: dict[str, int],
    linear_deflection: float,
    angular_deflection: float,
) -> list[MeshEdge]:
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    face_numbers = {face.TShape(): i for i, face in enumerate(faces)}
    found: list[tuple[str, MeshEdge, Vec]] = []
    for k in range(1, ancestors.Extent() + 1):
        edge = TopoDS.Edge_s(ancestors.FindKey(k))
        if BRep_Tool.Degenerated_s(edge):
            continue
        numbers = sorted({face_numbers[f.TShape()] for f in ancestors.FindFromIndex(k)})
        bounded = [mesh_faces[i] for i in numbers]
        segments = _polyline(edge, linear_deflection, angular_deflection)
        if not bounded or not segments:
            continue
        ids = sorted(f.id for f in bounded)
        key = "--".join(ids) if len(ids) > 1 else f"{ids[0]}--seam"
        feature = max((f.feature for f in bounded), key=lambda t: order.get(t, -1))
        mid = segments[len(segments) // 2][0]
        found.append((key, MeshEdge(key, feature, ids, segments), mid))
    # Several edges between the same faces: number them in a canonical order.
    by_key: dict[str, list[tuple[MeshEdge, Vec]]] = {}
    for key, e, mid in found:
        by_key.setdefault(key, []).append((e, mid))
    edges = []
    for key in sorted(by_key):
        group = sorted(by_key[key], key=lambda x: tuple(round(v, 3) for v in x[1]))
        for n, (e, _) in enumerate(group):
            if len(group) > 1:
                e.id = f"{key}.{n}"
            edges.append(e)
    return edges


def _polyline(edge, linear_deflection: float, angular_deflection: float) -> list[tuple[Vec, Vec]]:
    curve = BRepAdaptor_Curve(edge)
    points = GCPnts_TangentialDeflection(curve, angular_deflection, linear_deflection)
    pts = []
    for i in range(1, points.NbPoints() + 1):
        p = points.Value(i)
        pts.append(_round((p.X(), p.Y(), p.Z())))
    return [(a, b) for a, b in zip(pts, pts[1:], strict=False) if a != b]
