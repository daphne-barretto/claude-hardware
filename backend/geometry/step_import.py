"""Import a STEP file as a build: parts, feature tags, mesh, checks and maps.

A STEP file comes from the user, so the server never parses it. The import runs as a short-lived
worker process (`python -m geometry.step_import`, started by `app.imports`), and the worker reads
the file only through the sandboxed build runner (`geometry.sandbox.run_build`), which hands back
the file's solids as BREP. The worker then does what a build does: tags the faces, tessellates
(`geometry.tessellate`), runs the check report (`geometry.report`) and the analysis maps
(`geometry.analysis`), and prints them as one JSON document.

- **Parts.** Every solid body in the file is a part, named after the nearest meaningful label
  in the file's assembly tree (`case`, `R_0402_1005Metric`); bodies that share a name are
  numbered (`case_1`, `case_2`). Every part is an `enclosure` part, at the origin, in the file's
  own coordinates (millimetres).
- **Feature tags.** Simple features are recognized on the exact B-rep: a full cylinder (one
  face or several on the same axis and radius) is a `hole_<n>` when it is concave and a
  `boss_<n>` when it is convex; the walls around an inner loop of a flat face are an
  `opening_<n>` when they face into the loop (a cut-out) and a `boss_<n>` when they face out of
  it (something standing on the face). Every other face has its part's name as its feature, so
  it is known by its face ID alone. Numbers run over the whole import, so tags are unique.

The worker reads one JSON request on stdin, `{"file", "name", "board": [name, revision],
"standards", "build_id", "parse_timeout_s"}`, and writes `{"result": {...}}` or `{"error":
{"kind", "message"}}` on stdout.
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

from geometry import occ
from geometry.interface import BuildResult, FaceRef, Feature

STEP_INPUT = "import.step"
SOURCE_BACKEND = "step"

# Runs in the sandbox: read the file, and return every solid named by its path of labels.
PARSE_CODE = """\
from build123d import import_step


def _leaves(node, path):
    label = (node.label or "").strip().replace("|", "_")
    path = path + [label] if label else path
    children = list(getattr(node, "children", None) or ())
    if children:
        for child in children:
            yield from _leaves(child, path)
    else:
        for solid in node.solids():
            yield path, solid


_found = list(_leaves(import_step(params["file"]), []))
if not _found:
    raise ValueError("the file has no solid bodies")
result = {"%d|%s" % (_i, "|".join(_path)): _solid for _i, (_path, _solid) in enumerate(_found)}
"""

# Labels that say nothing about the body (`SOLID`, `SOLID__3_`, `Body1`, `Part 2`, ...).
_GENERIC_LABEL = re.compile(
    r"(?i)(solid|body|shape|compound|part|component|product|open ?cascade[^|]*|unnamed)?[\W_\d]*"
)
FULL_TURN = 2 * math.pi * 0.98  # a cylinder group spanning this much is a whole hole or boss
_TOL = 1e-4


class ImportFailed(Exception):
    """The file couldn't be imported. `kind` is a sandbox error kind or `unreadable`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# --- Parts -------------------------------------------------------------------------------------


def part_names(paths: list[list[str]]) -> list[str]:
    """A part name for each body from its path of labels: the last label that names something,
    as an ID, numbered where bodies share a name."""
    bases = []
    for path in paths:
        label = next((p for p in reversed(path) if not _GENERIC_LABEL.fullmatch(p)), "")
        name = re.sub(r"[^A-Za-z0-9_\-]+", "_", label).strip("_-") or "part"
        if name == "board":  # the board's part name (`geometry.assembly`)
            name = "board_import"
        bases.append(name)
    counts: dict[str, int] = {}
    for b in bases:
        counts[b] = counts.get(b, 0) + 1
    taken = {b for b in bases if counts[b] == 1}
    numbers: dict[str, int] = {}
    names = []
    for b in bases:
        if counts[b] == 1:
            names.append(b)
            continue
        n = numbers.get(b, 0)
        while True:
            n += 1
            candidate = f"{b}_{n}"
            if candidate not in taken:
                break
        numbers[b] = n
        taken.add(candidate)
        names.append(candidate)
    return names


def parts_from_sandbox(shapes: dict[str, TopoDS_Shape]) -> dict[str, TopoDS_Shape]:
    """The sandbox's `<index>|<label>|<label>...` shapes as named parts, in file order."""
    entries = []
    for key, shape in shapes.items():
        index, _, path = key.partition("|")
        if not index.isdigit():
            raise ImportFailed("crash", f"the reader returned an unexpected shape name {key!r}")
        entries.append((int(index), [p for p in path.split("|") if p], shape))
    entries.sort(key=lambda e: e[0])
    names = part_names([e[1] for e in entries])
    return {name: shape for name, (_, _, shape) in zip(names, entries, strict=True)}


# --- Features ------------------------------------------------------------------------------


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = math.sqrt(sum(c * c for c in v))
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _sub(a, b) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


@dataclass
class _Cylinder:
    index: int
    axis: tuple[tuple[float, float, float], tuple[float, float, float]]  # point, direction
    radius: float
    span: float  # radians
    concave: bool

    def same_axis(self, other: _Cylinder) -> bool:
        (p, d), (q, e) = self.axis, other.axis
        if abs(abs(_dot(d, e)) - 1) > _TOL or abs(self.radius - other.radius) > _TOL:
            return False
        if self.concave != other.concave:
            return False
        offset = _sub(q, p)
        along = _dot(offset, d)
        off_axis = _sub(offset, tuple(along * c for c in d))
        return math.sqrt(_dot(off_axis, off_axis)) < max(_TOL, 1e-3 * self.radius)


def _cylinder(index: int, face) -> _Cylinder | None:
    surface = BRepAdaptor_Surface(face)
    if surface.GetType() != GeomAbs_Cylinder:
        return None
    cyl = surface.Cylinder()
    loc, direction = cyl.Axis().Location(), cyl.Axis().Direction()
    p = (loc.X(), loc.Y(), loc.Z())
    d = _unit((direction.X(), direction.Y(), direction.Z()))
    samples = occ.face_samples(face, 3)
    if not samples:
        return None
    point, normal = samples[0]
    offset = _sub(point, p)
    radial = _sub(offset, tuple(_dot(offset, d) * c for c in d))
    span = abs(surface.LastUParameter() - surface.FirstUParameter())
    return _Cylinder(index, (p, d), cyl.Radius(), span, _dot(normal, radial) < 0)


def _inner_wires(face) -> list:
    outer = BRepTools.OuterWire_s(face)
    return [w for w in occ.subshapes(face, TopAbs_WIRE) if not w.IsSame(outer)]


def recognize(parts: dict[str, TopoDS_Shape]) -> dict[str, list[str]]:
    """The feature tag of every face of every part, in `occ.faces()` order."""
    counters = {"hole": 0, "boss": 0, "opening": 0}

    def new_tag(kind: str) -> str:
        counters[kind] += 1
        return f"{kind}_{counters[kind]}"

    out: dict[str, list[str]] = {}
    for part, shape in parts.items():
        faces = occ.faces(shape)
        tags: list[str | None] = [None] * len(faces)

        # Holes and bosses: whole cylinders, one face or several on one axis.
        groups: list[list[_Cylinder]] = []
        for i, face in enumerate(faces):
            cyl = _cylinder(i, face)
            if cyl is None:
                continue
            group = next((g for g in groups if g[0].same_axis(cyl)), None)
            if group is None:
                groups.append([cyl])
            else:
                group.append(cyl)
        for group in groups:
            if sum(c.span for c in group) >= FULL_TURN:
                tag = new_tag("hole" if group[0].concave else "boss")
                for c in group:
                    tags[c.index] = tag

        # Openings (and raised features): the walls around an inner loop of a flat face.
        face_map = TopTools_IndexedMapOfShape()  # the same order as occ.faces()
        TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
        ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
        for i, face in enumerate(faces):
            if BRepAdaptor_Surface(face).GetType() != GeomAbs_Plane:
                continue
            for wire in _inner_wires(face):
                walls: set[int] = set()
                points = []
                for edge in occ.subshapes(wire, TopAbs_EDGE):
                    points.extend(occ.vertex_point(v) for v in occ.subshapes(edge, TopAbs_VERTEX))
                    for neighbour in ancestors.FindFromKey(edge):
                        j = face_map.FindIndex(neighbour) - 1
                        if j >= 0 and j != i:
                            walls.add(j)
                walls = {j for j in walls if tags[j] is None or tags[j].startswith("opening_")}
                if not walls or not points:
                    continue
                centre = tuple(sum(p[k] for p in points) / len(points) for k in range(3))
                inward = 0
                for j in walls:
                    samples = occ.face_samples(faces[j], 1) or occ.face_samples(faces[j], 3)
                    if samples:
                        p, n = samples[0]
                        inward += 1 if _dot(n, _sub(centre, p)) > 0 else -1
                known = sorted({tags[j] for j in walls if tags[j] is not None})
                tag = known[0] if known else new_tag("opening" if inward > 0 else "boss")
                for j in walls:
                    tags[j] = tag
        out[part] = [t or part for t in tags]
    return out


@dataclass
class ImportedResult(BuildResult):
    """A build read from a STEP file: its parts, with the feature tags `recognize` found."""

    face_tags: dict[str, list[str]] = field(default_factory=dict)

    @cached_property
    def face_refs(self) -> dict[str, list[FaceRef]]:
        return {
            part: [FaceRef(part, i, tag) for i, tag in enumerate(self.face_tags[part])]
            for part in self.parts
        }


def _extent(faces: list) -> TopoDS_Shape:
    """Where a feature is, as its tool: the two corners of its faces' bounding box. Tessellation
    measures face positions from the middle of a feature's tools (stable face IDs)."""
    boxes = [occ.bbox(f) for f in faces]
    lo = tuple(min(b[0][k] for b in boxes) for k in range(3))
    hi = tuple(max(b[1][k] for b in boxes) for k in range(3))
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for corner in (lo, hi):
        builder.Add(compound, occ.vertex(corner))
    return compound


def imported_result(parts: dict[str, TopoDS_Shape]) -> ImportedResult:
    """The parts as a build: each face's tag from `recognize`, and each feature's extent as its
    tool."""
    tags = recognize(parts)
    features: dict[tuple[str, str], list] = {}
    for part, shape in parts.items():
        for face, tag in zip(occ.faces(shape), tags[part], strict=True):
            features.setdefault((part, tag), []).append(face)
    return ImportedResult(
        SOURCE_BACKEND,
        dict(parts),
        [Feature(tag, part, (_extent(faces),)) for (part, tag), faces in features.items()],
        face_tags=tags,
    )


# --- The import --------------------------------------------------------------------------------


def parse(data: bytes, timeout_s: float) -> dict[str, TopoDS_Shape]:
    """Read a STEP file's solids in the sandbox. Raises ImportFailed."""
    from geometry.sandbox import MAX_SHAPES, run_build

    run = run_build(
        PARSE_CODE, {"file": STEP_INPUT}, timeout_s=timeout_s, inputs={STEP_INPUT: data}
    )
    if run.error is not None:
        kind, message = run.error.kind, run.error.message
        if kind == "output" and "shapes" in message:
            message = f"it has more than {MAX_SHAPES} solid bodies, the most an import takes"
        elif kind == "runtime":
            message = message.removeprefix("ValueError: ")
        raise ImportFailed(kind, message)
    return parts_from_sandbox(run.shapes)


def source_code(name: str, result: ImportedResult) -> tuple[str, list[dict[str, Any]]]:
    """What stands for the code of an imported build: a description of the file's parts and
    the features recognized on them, one line per feature (`BuildSource.features`)."""
    lines = [
        f"# Imported from {name} (STEP). The geometry is the file's own, not generated:",
        "# there is no generator code to rerun. Parts and the features recognized on them:",
    ]
    features = []
    for part in result.parts:
        lines.append(f"# part {part}")
        for f in result.features:
            if f.part != part:
                continue
            count = sum(1 for tag in result.face_tags[part] if tag == f.tag)
            what = "faces" if f.tag == part else f"{f.tag.split('_')[0]} faces"
            lines.append(f"#   {f.tag}: {count} {what}")
            features.append({"tag": f.tag, "part": part, "lines": [len(lines), len(lines)]})
    return "\n".join(lines) + "\n", features


def import_step(
    data: bytes,
    name: str,
    board,
    standards,
    build_id: str,
    parse_timeout_s: float,
) -> dict[str, Any]:
    """Parse (in the sandbox), tag, tessellate, check and analyze a STEP file."""
    from app.contracts.mesh import validate_mesh
    from geometry.analysis import analysis_maps, analyze
    from geometry.report import check_report
    from geometry.tessellate import tessellate

    result = imported_result(parse(data, parse_timeout_s))
    mesh = tessellate(result, source={"imported_from": name, "build": build_id})
    gltf = mesh.to_gltf()
    report = check_report(board, standards, result, build_id, mesh)
    maps = analysis_maps(analyze(result, standards), standards, validate_mesh(gltf), build_id)
    code, features = source_code(name, result)
    return {
        "mesh": gltf,
        "checks": report,
        "maps": maps,
        "source": {
            "build_id": build_id,
            "backend": SOURCE_BACKEND,
            "language": "python",
            "filename": f"{Path(name).stem or 'import'}.py",
            "code": code,
            "features": features,
        },
        "parts": [
            {
                "name": part,
                "bbox": [list(c) for c in occ.bbox(shape)],
                "features": [f.tag for f in result.features if f.part == part and f.tag != part],
            }
            for part, shape in result.parts.items()
        ],
    }


def export_step(
    data: bytes,
    name: str,
    board,
    standards,
    build_id: str,
    parse_timeout_s: float,
    report: dict[str, Any],
    formats: list[str],
    out: Path,
) -> list[dict[str, str]]:
    """Export an imported build in `formats` (`geometry.export`) to `out`, with its check
    `report` as the build note."""
    from geometry.export import export_build
    from geometry.tessellate import tessellate

    result = imported_result(parse(data, parse_timeout_s))
    mesh = tessellate(result, source={"imported_from": name, "build": build_id})
    code, _ = source_code(name, result)
    files = export_build(board, standards, result, code, {}, out, report, mesh, formats=formats)
    return [{"format": f.format, "name": f.name, "media_type": f.media_type} for f in files]


def main() -> None:
    request = json.loads(sys.stdin.read())
    out = sys.stdout
    sys.stdout = sys.stderr  # library prints never reach the result
    try:
        from app.boards import find_board
        from app.standards import from_payload, load_standards

        board = find_board(*request["board"]) if request.get("board") else None
        # The session's set as the server has it (a browser's own set too: `to_payload`).
        payload = request.get("standards")
        standards = from_payload(payload) if payload else load_standards()
        data = Path(request["file"]).read_bytes()
        args = (data, request["name"], board, standards, request["build_id"])
        timeout_s = float(request["parse_timeout_s"])
        if request.get("mode") == "export":
            files = export_step(
                *args, timeout_s, request["report"], request["formats"], Path(request["out"])
            )
            result = {"files": files}
        else:
            result = import_step(*args, timeout_s)
        out.write(json.dumps({"result": result}, separators=(",", ":")))
    except ImportFailed as error:
        out.write(json.dumps({"error": {"kind": error.kind, "message": error.message}}))
    except Exception as error:  # noqa: BLE001 - reported to the user, never lost
        message = f"{type(error).__name__}: {error}"
        out.write(json.dumps({"error": {"kind": "crash", "message": message}}))
    out.flush()


if __name__ == "__main__":
    main()
