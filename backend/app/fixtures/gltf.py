"""Write tagged parts as a glTF document that follows the mesh contract."""

from __future__ import annotations

import base64
import math
import struct

from app.contracts.mesh import EDGE_ID, FACE_ID, LINES, MESH_CONTRACT, TRIANGLES
from app.fixtures.geometry import Part, Vec, normal

DIGITS = 4  # mm to 0.1 µm; keeps the output identical across platforms
# Root node rotation: -90° about X turns board Z-up into glTF Y-up.
Z_UP_TO_Y_UP = [-math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)]


def _r(v: Vec) -> Vec:
    return (round(v[0], DIGITS) + 0.0, round(v[1], DIGITS) + 0.0, round(v[2], DIGITS) + 0.0)


class PartMesh:
    """A part as indexed vertex arrays: one vertex per (face, position), smooth within a face."""

    def __init__(self, part: Part):
        self.part = part
        self.positions: list[Vec] = []
        self.normals: list[Vec] = []
        self.face_index: list[int] = []
        self.indices: list[int] = []
        for f, face in enumerate(part.faces):
            lookup: dict[Vec, int] = {}
            sums: dict[int, list[float]] = {}
            for tri in face.triangles:
                n = normal(tri)
                for p in tri:
                    key = _r(p)
                    if key not in lookup:
                        lookup[key] = len(self.positions)
                        self.positions.append(key)
                        self.face_index.append(f)
                        sums[lookup[key]] = [0.0, 0.0, 0.0]
                    i = lookup[key]
                    self.indices.append(i)
                    s = sums[i]
                    s[0], s[1], s[2] = s[0] + n[0], s[1] + n[1], s[2] + n[2]
            for i in sorted(sums):
                s = sums[i]
                length = math.sqrt(s[0] ** 2 + s[1] ** 2 + s[2] ** 2) or 1.0
                self.normals.append(_r((s[0] / length, s[1] / length, s[2] / length)))
        self.line_positions: list[Vec] = []
        self.edge_index: list[int] = []
        for e, edge in enumerate(part.edges):
            for a, b in edge.segments:
                self.line_positions += [_r(a), _r(b)]
                self.edge_index += [e, e]

    def face_points(self, face_number: int) -> list[int]:
        return [i for i, f in enumerate(self.face_index) if f == face_number]


class _Buffer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.views: list[dict] = []
        self.accessors: list[dict] = []

    def add(
        self,
        fmt: str,
        rows: list,
        gltf_type: str,
        component: int,
        target: int,
        minmax: bool = False,
    ) -> int:
        while len(self.data) % 4:
            self.data.append(0)
        offset = len(self.data)
        flat = [x for row in rows for x in row] if gltf_type != "SCALAR" else rows
        self.data += struct.pack("<" + fmt * len(flat), *flat)
        self.views.append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(self.data) - offset,
                "target": target,
            }
        )
        acc = {
            "bufferView": len(self.views) - 1,
            "componentType": component,
            "count": len(rows),
            "type": gltf_type,
        }
        if minmax:
            acc["min"] = [min(r[k] for r in rows) for k in range(3)]
            acc["max"] = [max(r[k] for r in rows) for k in range(3)]
        self.accessors.append(acc)
        return len(self.accessors) - 1


def write_gltf(meshes: list[PartMesh], source: dict) -> dict:
    buf = _Buffer()
    nodes = [{"name": "enclosure", "rotation": Z_UP_TO_Y_UP, "children": []}]
    gltf_meshes = []
    for m in meshes:
        id_fmt, id_type = ("H", 5123)
        if len(m.part.faces) > 0xFFFF or len(m.part.edges) > 0xFFFF:
            raise ValueError("too many faces or edges for 16-bit IDs")
        index_fmt, index_type = ("H", 5123) if len(m.positions) <= 0xFFFF else ("I", 5125)
        tri = {
            "mode": TRIANGLES,
            "attributes": {
                "POSITION": buf.add("f", m.positions, "VEC3", 5126, 34962, minmax=True),
                "NORMAL": buf.add("f", m.normals, "VEC3", 5126, 34962),
                FACE_ID: buf.add(id_fmt, m.face_index, "SCALAR", id_type, 34962),
            },
            "indices": buf.add(index_fmt, m.indices, "SCALAR", index_type, 34963),
        }
        primitives = [tri]
        if m.line_positions:
            primitives.append(
                {
                    "mode": LINES,
                    "attributes": {
                        "POSITION": buf.add(
                            "f", m.line_positions, "VEC3", 5126, 34962, minmax=True
                        ),
                        EDGE_ID: buf.add(id_fmt, m.edge_index, "SCALAR", id_type, 34962),
                    },
                }
            )
        gltf_meshes.append({"name": m.part.name, "primitives": primitives})
        nodes[0]["children"].append(len(nodes))
        nodes.append(
            {
                "name": m.part.name,
                "mesh": len(gltf_meshes) - 1,
                "extras": {
                    "part": m.part.name,
                    "faces": [{"id": f.id, "feature": f.feature} for f in m.part.faces],
                    "edges": [
                        {"id": e.id, "feature": e.feature, "faces": list(e.faces)}
                        for e in m.part.edges
                    ],
                },
            }
        )
    return {
        "asset": {
            "version": "2.0",
            "generator": "claude-hardware app.fixtures",
            "extras": {"contract": MESH_CONTRACT, "units": "mm", "up_axis": "Z", "source": source},
        },
        "scene": 0,
        "scenes": [{"name": "enclosure", "nodes": [0]}],
        "nodes": nodes,
        "meshes": gltf_meshes,
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
