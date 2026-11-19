"""Sketch on a face, shared by the mock API (`app.mock_api`) and the real session API
(`app.session_api`).

- `face_plane()` reads a face of a build's mesh (mesh contract) and tells whether it is flat,
  and if so its plane: centre, outward normal, sketch axes and extent.
- `new_sketch()` checks a `SketchCreate` against that plane and names the feature.
- `with_sketches()` lays the sketched features over a build's CAD source: the code gets the
  block that applies them (`geometry.sketch`), and each feature is listed, locked, at its line.

A sketch is a hand edit: the APIs keep it in the design as a locked parameter
(`sketches.<tag>`), so Claude designs around it like any locked value.
"""

from __future__ import annotations

import math
from typing import Any

from app.contracts.mesh import FACE_ID, load_gltf, read_accessor
from app.contracts.session import FacePlane, Sketch, SketchCreate
from geometry.sketch import sketch_block

Vec = tuple[float, float, float]

# Triangles whose normals differ by more than this (as 1 - cos) are not one plane.
FLAT_TOLERANCE = 1e-4
# Points off the plane by more than this (mm) are not on it.
PLANE_TOLERANCE = 1e-3
PATH = "sketches"


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a: Vec) -> Vec:
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


def _clean(v: Vec, digits: int = 6) -> Vec:
    return tuple(round(c, digits) + 0.0 for c in v)  # type: ignore[return-value]


def face_triangles(gltf: str | bytes | dict, face_id: str) -> tuple[str, str, list] | None:
    """The part, feature and triangles (three points each, board coordinates) of a face."""
    doc, buffers = load_gltf(gltf.encode() if isinstance(gltf, str) else gltf)
    for node in doc.get("nodes", []):
        extras = node.get("extras") or {}
        faces = extras.get("faces") or []
        index = next((i for i, f in enumerate(faces) if f["id"] == face_id), None)
        if index is None or "mesh" not in node:
            continue
        tris = []
        for prim in doc["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 4 or FACE_ID not in prim.get("attributes", {}):
                continue
            pos = read_accessor(doc, buffers, prim["attributes"]["POSITION"])
            ids = read_accessor(doc, buffers, prim["attributes"][FACE_ID])
            idx = (
                read_accessor(doc, buffers, prim["indices"])
                if "indices" in prim
                else list(range(len(pos)))
            )
            for t in range(0, len(idx) - 2, 3):
                a, b, c = idx[t], idx[t + 1], idx[t + 2]
                if ids[a] == index:
                    tris.append((tuple(pos[a]), tuple(pos[b]), tuple(pos[c])))
        return extras["part"], faces[index]["feature"], tris
    return None


def sketch_axes(normal: Vec) -> tuple[Vec, Vec]:
    """The sketch's x and y axes on a face with this normal: on a wall, x is level and y points
    up; on a top or bottom face, x is board x."""
    if abs(normal[2]) > 0.9:
        # board x, made to lie in the face
        x = _unit(_sub((1.0, 0.0, 0.0), tuple(normal[0] * c for c in normal)))  # type: ignore[arg-type]
    else:
        x = _unit(_cross((0.0, 0.0, 1.0), normal))
    return x, _cross(normal, x)


def face_plane(gltf: str | bytes | dict, build_id: str, face_id: str) -> FacePlane | None:
    """The face's plane, or `planar=False` if it is curved; None if the mesh has no such face."""
    found = face_triangles(gltf, face_id)
    return None if found is None else _plane(build_id, face_id, *found)


def _plane(build_id: str, face_id: str, part: str, feature: str, tris: list) -> FacePlane:
    base = FacePlane(build_id=build_id, face_id=face_id, part=part, feature=feature, planar=False)
    area = (0.0, 0.0, 0.0)
    normals = []
    for a, b, c in tris:
        n = _cross(_sub(b, a), _sub(c, a))
        if _dot(n, n) > 1e-18:
            area = (area[0] + n[0], area[1] + n[1], area[2] + n[2])
            normals.append(_unit(n))
    if not normals or _dot(area, area) < 1e-18:
        return base
    normal = _unit(area)
    if any(1 - _dot(n, normal) > FLAT_TOLERANCE for n in normals):
        return base
    points = [p for t in tris for p in t]
    d0 = _dot(points[0], normal)
    if any(abs(_dot(p, normal) - d0) > PLANE_TOLERANCE for p in points):
        return base
    x, y = sketch_axes(normal)
    us, vs = [_dot(p, x) for p in points], [_dot(p, y) for p in points]
    cu, cv = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
    origin = tuple(d0 * normal[i] + cu * x[i] + cv * y[i] for i in range(3))
    return base.model_copy(
        update={
            "planar": True,
            "origin": _clean(origin),  # type: ignore[arg-type]
            "normal": _clean(normal),
            "x_dir": _clean(x),
            "size": (round(max(us) - min(us), 6), round(max(vs) - min(vs), 6)),
        }
    )


def extent(body: SketchCreate) -> tuple[float, float]:
    """Half the shape's size along the sketch's x and y axes."""
    height = body.width if body.shape == "circle" else (body.height or 0)
    return body.width / 2, height / 2


def _on_face(plane: FacePlane, tris: list, u: float, v: float) -> bool:
    """Whether the point (u, v) of the sketch lies on one of the face's triangles."""
    assert plane.origin and plane.x_dir and plane.normal
    x, n = plane.x_dir, plane.normal
    y = _cross(n, x)
    o = plane.origin

    def uv(p: Vec) -> tuple[float, float]:
        d = _sub(p, o)
        return _dot(d, x), _dot(d, y)

    eps = 1e-6
    for tri in tris:
        (ax, ay), (bx, by), (cx, cy) = (uv(p) for p in tri)
        d1 = (bx - ax) * (v - ay) - (by - ay) * (u - ax)
        d2 = (cx - bx) * (v - by) - (cy - by) * (u - bx)
        d3 = (ax - cx) * (v - cy) - (ay - cy) * (u - cx)
        if (d1 >= -eps and d2 >= -eps and d3 >= -eps) or (d1 <= eps and d2 <= eps and d3 <= eps):
            return True
    return False


def sketch_error(body: SketchCreate, plane: FacePlane, tris: list | None = None) -> str | None:
    """Why the shape can't go on the face, or None."""
    if not plane.planar or plane.size is None:
        return f"face {plane.face_id} is not flat: sketch on a flat face"
    hw, hh = extent(body)
    su, sv = plane.size[0] / 2, plane.size[1] / 2
    eps = 1e-6
    if abs(body.x) + hw > su + eps or abs(body.y) + hh > sv + eps:
        return (
            f"the {body.shape} runs off face {plane.face_id}: it is "
            f"{plane.size[0]:g} × {plane.size[1]:g} mm, so keep |x| + {hw:g} ≤ {su:g} and "
            f"|y| + {hh:g} ≤ {sv:g}"
        )
    if tris is not None and not _on_face(plane, tris, body.x, body.y):
        return (
            f"the {body.shape}'s centre ({body.x:g}, {body.y:g}) is not on face {plane.face_id} "
            "(it is in a hole or past an edge of the face)"
        )
    return None


def next_tag(sketches: list[dict[str, Any]]) -> str:
    taken = {s["tag"] for s in sketches}
    n = 1
    while f"sketch_{n}" in taken:
        n += 1
    return f"sketch_{n}"


class SketchError(ValueError):
    """The sketch can't go where it was asked to."""


def new_sketch(
    body: SketchCreate, gltf: str | bytes | dict, sketches: list[dict[str, Any]]
) -> Sketch | None:
    """The sketched feature on the build's mesh `gltf`, named after the `sketches` there are;
    None if the mesh has no such face. Raises SketchError if the face isn't flat or the shape
    doesn't fit on it."""
    found = face_triangles(gltf, body.face_id)
    if found is None:
        return None
    plane = _plane(body.build_id, body.face_id, *found)
    error = sketch_error(body, plane, found[2])
    if error:
        raise SketchError(error)
    return Sketch(**body.model_dump(), tag=next_tag(sketches), part=plane.part, plane=plane)


def sketch_params(sketch: Sketch) -> dict[str, Any]:
    """The sketch as the generator reads it (`params["sketches"]`, `geometry.sketch`)."""
    plane = sketch.plane
    return {
        "tag": sketch.tag,
        "part": sketch.part,
        "face_id": sketch.face_id,
        "shape": sketch.shape,
        "operation": sketch.operation,
        "width": sketch.width,
        "height": sketch.height,
        "count": sketch.count,
        "x": sketch.x,
        "y": sketch.y,
        "depth": sketch.depth,
        "plane": {
            "origin": list(plane.origin or ()),
            "normal": list(plane.normal or ()),
            "x_dir": list(plane.x_dir or ()),
        },
    }


def with_sketches(source: dict[str, Any], sketches: list[dict[str, Any]]) -> dict[str, Any]:
    """A `BuildSource` with the sketched features laid over it: the code applies them, and each
    is listed after the code's own features, locked, at the line that applies it."""
    if not sketches:
        return source
    code, lines = sketch_block(source["code"], sketches, source.get("backend", "build123d"))
    tags = set(lines)
    features = [f for f in source.get("features", []) if f["tag"] not in tags]
    features += [
        {"tag": s["tag"], "part": s["part"], "lines": list(lines[s["tag"]]), "locked": True}
        for s in sketches
    ]
    return {**source, "code": code, "features": features}
