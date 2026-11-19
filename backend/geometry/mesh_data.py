"""A build's mesh as plain data: the tessellation's parts, faces, edges and their stable IDs,
and its glTF writer (`geometry.tessellate` makes them from a build).

Nothing here loads the CAD kernel, so a `Tessellation` a geometry worker sends back can be
unpickled in the server (`geometry.worker`) without loading OpenCascade there.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.contracts.mesh import EDGE_ID, FACE_ID, LINES, MESH_CONTRACT, TRIANGLES

Vec = tuple[float, float, float]
Color = Sequence[float]

DIGITS = 5  # rounding of written coordinates: 10 nm, keeps the output reproducible
# Root node rotation: -90° about X turns board Z-up into glTF Y-up.
Z_UP_TO_Y_UP = [-math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)]
MAX_IDS = 0xFFFF  # _FACE_ID and _EDGE_ID are unsigned shorts

# Matching: a face inherits an ID only if the cost is below MATCH_LIMIT. The cost adds the
# change in relative position (in units of the feature's size, at least 1 mm), the change in
# outward direction and the relative change in area.
MATCH_LIMIT = 1.0
DIRECTION_WEIGHT = 1.0
AREA_WEIGHT = 0.5


@dataclass(frozen=True)
class FaceSignature:
    """What a face looks like, relative to the feature that made it. Plain JSON."""

    part: str
    feature: str
    kind: str  # OpenCascade surface type, e.g. "Plane", "Cylinder"
    direction: Vec  # area-weighted mean outward normal (short for curved faces)
    position: Vec  # centroid minus the feature's anchor (centre of its tools)
    area: float
    scale: float  # size of the feature's tools, for normalising positions

    def cost(self, other: FaceSignature) -> float:
        if self.kind != other.kind:
            return math.inf
        moved = _norm(_sub(self.position, other.position)) / max(self.scale, other.scale, 1.0)
        turned = _norm(_sub(self.direction, other.direction))
        grown = abs(self.area - other.area) / max(self.area, other.area, 1e-9)
        return moved + DIRECTION_WEIGHT * turned + AREA_WEIGHT * grown

    def sort_key(self) -> tuple:
        r = lambda v, d: tuple(round(x, d) + 0.0 for x in v)  # noqa: E731
        return (self.kind, r(self.direction, 1), r(self.position, 1), round(self.area, 1))


@dataclass
class MeshFace:
    id: str
    feature: str
    signature: FaceSignature


@dataclass
class MeshEdge:
    id: str
    feature: str
    faces: list[str]
    segments: list[tuple[Vec, Vec]]


@dataclass
class PartMesh:
    """One part as vertex arrays. Vertices are not shared between faces."""

    name: str
    kind: str = "enclosure"
    material: str | None = None
    placement: Vec = (0.0, 0.0, 0.0)
    faces: list[MeshFace] = field(default_factory=list)
    edges: list[MeshEdge] = field(default_factory=list)
    positions: list[Vec] = field(default_factory=list)
    normals: list[Vec] = field(default_factory=list)
    face_index: list[int] = field(default_factory=list)  # per vertex, into `faces`
    indices: list[int] = field(default_factory=list)

    def face(self, face_id: str) -> MeshFace:
        return next(f for f in self.faces if f.id == face_id)


@dataclass
class Tessellation:
    parts: dict[str, PartMesh]
    counters: dict[str, int]  # "<part>.<feature>" -> next unused face number
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def faces(self) -> dict[str, MeshFace]:
        return {f.id: f for p in self.parts.values() for f in p.faces}

    @property
    def edges(self) -> dict[str, MeshEdge]:
        return {e.id: e for p in self.parts.values() for e in p.edges}

    def signatures(self) -> dict[str, Any]:
        """Everything a later rebuild needs to keep these IDs, as plain JSON."""
        return {
            "faces": {
                f.id: {
                    "part": s.part,
                    "feature": s.feature,
                    "kind": s.kind,
                    "direction": list(s.direction),
                    "position": list(s.position),
                    "area": s.area,
                    "scale": s.scale,
                }
                for f in self.faces.values()
                for s in [f.signature]
            },
            "counters": dict(self.counters),
        }

    def to_gltf(self, colors: dict[str, Sequence[Color]] | None = None) -> dict:
        """The glTF document. `colors` maps a part to one RGB or RGBA (0-1) per vertex."""
        return _write_gltf(self, colors or {})

    def write(self, path: Path | str, colors: dict[str, Sequence[Color]] | None = None) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_gltf(colors), separators=(",", ":")))
        return path


# --- glTF ---------------------------------------------------------------------------------------


class _Buffer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.views: list[dict] = []
        self.accessors: list[dict] = []

    def add(self, fmt: str, rows: list, gltf_type: str, component: int, target: int) -> int:
        while len(self.data) % 4:
            self.data.append(0)
        offset = len(self.data)
        flat = rows if gltf_type == "SCALAR" else [x for row in rows for x in row]
        self.data += struct.pack(f"<{len(flat)}{fmt}", *flat)
        self.views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(self.data) - offset}
            | {"target": target}
        )
        acc = {
            "bufferView": len(self.views) - 1,
            "componentType": component,
            "count": len(rows),
            "type": gltf_type,
        }
        if fmt == "f" and gltf_type == "VEC3" and target == 34962:
            acc["min"] = [min(r[k] for r in rows) for k in range(3)]
            acc["max"] = [max(r[k] for r in rows) for k in range(3)]
        self.accessors.append(acc)
        return len(self.accessors) - 1


def _write_gltf(mesh: Tessellation, colors: dict[str, Sequence[Color]]) -> dict:
    unknown = set(colors) - set(mesh.parts)
    if unknown:
        raise ValueError(f"colors for unknown parts: {sorted(unknown)}")
    buf = _Buffer()
    nodes: list[dict] = [{"name": "enclosure", "rotation": Z_UP_TO_Y_UP, "children": []}]
    meshes = []
    for part in mesh.parts.values():
        if len(part.faces) > MAX_IDS or len(part.edges) > MAX_IDS:
            raise ValueError(f"{part.name}: too many faces or edges for 16-bit IDs")
        index_fmt, index_type = ("H", 5123) if len(part.positions) <= 0xFFFF else ("I", 5125)
        attributes = {
            "POSITION": buf.add("f", part.positions, "VEC3", 5126, 34962),
            "NORMAL": buf.add("f", part.normals, "VEC3", 5126, 34962),
            FACE_ID: buf.add("H", part.face_index, "SCALAR", 5123, 34962),
        }
        if part.name in colors:
            rows = [tuple(float(x) for x in c) for c in colors[part.name]]
            if len(rows) != len(part.positions):
                raise ValueError(
                    f"{part.name}: {len(rows)} colors for {len(part.positions)} vertices"
                )
            width = {len(r) for r in rows}
            if width not in ({3}, {4}):
                raise ValueError(f"{part.name}: colors must all be RGB or all RGBA")
            attributes["COLOR_0"] = buf.add("f", rows, f"VEC{width.pop()}", 5126, 34962)
        primitives = [
            {
                "mode": TRIANGLES,
                "attributes": attributes,
                "indices": buf.add(index_fmt, part.indices, "SCALAR", index_type, 34963),
            }
        ]
        if part.edges:
            line_positions: list[Vec] = []
            edge_index: list[int] = []
            for e, edge in enumerate(part.edges):
                for a, b in edge.segments:
                    line_positions += [a, b]
                    edge_index += [e, e]
            primitives.append(
                {
                    "mode": LINES,
                    "attributes": {
                        "POSITION": buf.add("f", line_positions, "VEC3", 5126, 34962),
                        EDGE_ID: buf.add("H", edge_index, "SCALAR", 5123, 34962),
                    },
                }
            )
        meshes.append({"name": part.name, "primitives": primitives})
        nodes[0]["children"].append(len(nodes))
        nodes.append(
            {
                "name": part.name,
                "mesh": len(meshes) - 1,
                "extras": {
                    "part": part.name,
                    "kind": part.kind,
                    **({"material": part.material} if part.material else {}),
                    "placement": list(part.placement),
                    "faces": [{"id": f.id, "feature": f.feature} for f in part.faces],
                    "edges": [
                        {"id": e.id, "feature": e.feature, "faces": e.faces} for e in part.edges
                    ],
                },
            }
        )
    return {
        "asset": {
            "version": "2.0",
            "generator": "claude-hardware geometry.tessellate",
            "extras": {
                "contract": MESH_CONTRACT,
                "units": "mm",
                "up_axis": "Z",
                "source": mesh.source,
            },
        },
        "scene": 0,
        "scenes": [{"name": "enclosure", "nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "accessors": buf.accessors,
        "bufferViews": buf.views,
        "buffers": [
            {
                "byteLength": len(buf.data),
                "uri": "data:application/octet-stream;base64,"
                + base64.b64encode(bytes(buf.data)).decode(),
            }
        ],
    }


# --- Vectors ------------------------------------------------------------------------------------


def _sub(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v: Sequence[float]) -> float:
    return math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)


def _unit(v: Vec) -> Vec:
    n = _norm(v) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _round(v: Sequence[float]) -> Vec:
    return (
        round(v[0], DIGITS) + 0.0,
        round(v[1], DIGITS) + 0.0,
        round(v[2], DIGITS) + 0.0,
    )
