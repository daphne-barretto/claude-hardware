"""Mesh contract: the glTF the tessellator writes and the 3D canvas loads.

The rules are in `contracts/README.md` ("Mesh"). In short: glTF 2.0, millimetres in board
coordinates (Z up) under a root node that turns Z up into glTF's Y up, one node per part, one
triangle primitive per part with a `_FACE_ID` vertex attribute indexing `extras.faces`, and an
optional line primitive with `_EDGE_ID` indexing `extras.edges`.

`validate_mesh()` checks a file against all of it and returns a `MeshInfo` that the check-result
and analysis-map validators use to resolve face IDs and vertex counts.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from app.contracts.common import Contract, ContractError, StableId

MESH_CONTRACT = "mesh/1"

TRIANGLES = 4
LINES = 1
FACE_ID = "_FACE_ID"
EDGE_ID = "_EDGE_ID"

# glTF accessor component types: code -> (struct format, size).
COMPONENTS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
ID_COMPONENTS = (5121, 5123)  # unsigned byte or short: glTF allows no 32-bit vertex attributes


class AssetExtras(Contract):
    """`asset.extras`: identifies the contract and the units."""

    contract: Literal["mesh/1"]
    units: Literal["mm"]
    up_axis: Literal["Z"] = Field(description="Part nodes are in board coordinates, Z up")
    source: dict[str, Any] = Field(
        default_factory=dict, description="Free-form provenance: board, build ID, generator"
    )


class MeshFace(Contract):
    id: StableId = Field(description="Stable face ID, unique in the whole mesh")
    feature: StableId = Field(description="Feature that created the face, e.g. `opening_Q4`")


class MeshEdge(Contract):
    id: StableId = Field(description="Stable edge ID, unique in the whole mesh")
    feature: StableId
    faces: list[StableId] = Field(
        max_length=2, description="The faces the edge bounds (two, or one on a seam)"
    )


class PartExtras(Contract):
    """`node.extras` of a part node."""

    part: StableId = Field(description="Part name, e.g. `base` or `lid`; same as `node.name`")
    kind: Literal["enclosure", "insert"] = Field(
        default="enclosure",
        description="`enclosure` for a molded part (base, lid), `insert` for bought-in hardware "
        "pressed into it (a heat-set insert)",
    )
    material: str | None = Field(default=None, description="e.g. `molded ABS`, `brass`")
    placement: tuple[float, float, float] = Field(
        default=(0.0, 0.0, 0.0),
        description="Where the part's own origin sits in the assembly (mm, board coordinates). "
        "Informational: vertex positions are already in board coordinates",
    )
    faces: list[MeshFace] = Field(min_length=1, description="Indexed by `_FACE_ID`")
    edges: list[MeshEdge] = Field(default_factory=list, description="Indexed by `_EDGE_ID`")


@dataclass
class PartInfo:
    name: str
    node: int
    vertex_count: int  # of the triangle primitive: analysis maps have one value per vertex
    triangle_count: int
    faces: list[MeshFace]
    edges: list[MeshEdge]
    positions: list[tuple[float, float, float]] = field(repr=False)
    face_index: list[int] = field(repr=False)  # per vertex
    has_colors: bool = False
    kind: str = "enclosure"
    indices: list[int] = field(default_factory=list, repr=False)  # of the triangles


@dataclass
class MeshInfo:
    parts: dict[str, PartInfo]
    face_parts: dict[str, str]  # face ID -> part
    edge_parts: dict[str, str]
    features: set[str]
    asset: AssetExtras

    def face(self, face_id: str) -> MeshFace:
        part = self.parts[self.face_parts[face_id]]
        return next(f for f in part.faces if f.id == face_id)


def load_gltf(source: Path | str | bytes | dict) -> tuple[dict, list[bytes]]:
    """Read a `.gltf` (JSON, external or data-URI buffers) or `.glb` into (json, buffers)."""
    base: Path | None = None
    if isinstance(source, dict):
        doc, blob = source, None
    else:
        if isinstance(source, str | Path):
            base = Path(source).parent
            data = Path(source).read_bytes()
        else:
            data = source
        doc, blob = _parse(data)
    buffers = []
    for i, buf in enumerate(doc.get("buffers", [])):
        uri = buf.get("uri")
        if uri is None:
            if blob is None:
                raise ContractError(f"buffer {i} has no uri and there is no GLB binary chunk")
            buffers.append(blob)
        elif uri.startswith("data:"):
            buffers.append(base64.b64decode(uri.split(",", 1)[1]))
        elif base is not None:
            buffers.append((base / uri).read_bytes())
        else:
            raise ContractError(f"buffer {i}: cannot resolve {uri!r} without a file path")
        if len(buffers[-1]) < buf.get("byteLength", 0):
            raise ContractError(f"buffer {i} is shorter than its byteLength")
    return doc, buffers


def _parse(data: bytes) -> tuple[dict, bytes | None]:
    if data[:4] != b"glTF":
        try:
            return json.loads(data), None
        except ValueError as error:
            raise ContractError(f"not glTF JSON or GLB: {error}") from error
    _, version, length = struct.unpack_from("<III", data, 0)
    if version != 2:
        raise ContractError(f"GLB version {version}, expected 2")
    offset, doc, blob = 12, None, None
    while offset < length:
        size, kind = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8 : offset + 8 + size]
        if kind == 0x4E4F534A:
            doc = json.loads(chunk)
        elif kind == 0x004E4942:
            blob = chunk
        offset += 8 + size
    if doc is None:
        raise ContractError("GLB has no JSON chunk")
    return doc, blob


def read_accessor(doc: dict, buffers: list[bytes], index: int) -> list:
    """Decode an accessor into a list of scalars or tuples."""
    acc = doc["accessors"][index]
    fmt, size = COMPONENTS[acc["componentType"]]
    width = WIDTHS[acc["type"]]
    count = acc["count"]
    view = doc["bufferViews"][acc["bufferView"]]
    data = buffers[view["buffer"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or size * width
    end = start + stride * (count - 1) + size * width if count else start
    if end > view.get("byteOffset", 0) + view["byteLength"] or end > len(data):
        raise ContractError(f"accessor {index} runs past its buffer view")
    item = struct.Struct("<" + fmt * width)
    out = [item.unpack_from(data, start + i * stride) for i in range(count)]
    return [v[0] for v in out] if width == 1 else out


def _attribute(doc, buffers, prim, name, types, components, where) -> list:
    if name not in prim.get("attributes", {}):
        raise ContractError(f"{where}: missing attribute {name}")
    index = prim["attributes"][name]
    acc = doc["accessors"][index]
    if acc["type"] not in types or acc["componentType"] not in components:
        raise ContractError(
            f"{where}: {name} must be {'/'.join(types)} of component type {components}"
        )
    return read_accessor(doc, buffers, index)


def _indices(doc, buffers, prim, count) -> list[int]:
    if "indices" not in prim:
        return list(range(count))
    return read_accessor(doc, buffers, prim["indices"])


def validate_mesh(source: Path | str | bytes | dict) -> MeshInfo:
    """Check a glTF against the mesh contract. Raises `ContractError` on the first problem."""
    doc, buffers = load_gltf(source)
    if doc.get("asset", {}).get("version") != "2.0":
        raise ContractError("asset.version must be 2.0")
    try:
        asset = AssetExtras.model_validate(doc["asset"].get("extras", {}))
    except ValidationError as error:
        raise ContractError(f"asset.extras: {error}") from error

    nodes = doc.get("nodes", [])
    scene = doc.get("scenes", [])[doc.get("scene", 0)] if doc.get("scenes") else None
    if scene is None or not scene.get("nodes"):
        raise ContractError("the glTF needs a default scene with nodes")
    reachable = _reachable(nodes, scene["nodes"])

    parts: dict[str, PartInfo] = {}
    face_parts: dict[str, str] = {}
    edge_parts: dict[str, str] = {}
    features: set[str] = set()
    for n in reachable:
        node = nodes[n]
        if "mesh" not in node:
            continue
        where = f"node {n} ({node.get('name', '?')})"
        try:
            extras = PartExtras.model_validate(node.get("extras", {}))
        except ValidationError as error:
            raise ContractError(f"{where}: extras: {error}") from error
        if node.get("name") != extras.part:
            raise ContractError(f"{where}: node name must equal extras.part {extras.part!r}")
        if extras.part in parts:
            raise ContractError(f"{where}: duplicate part {extras.part!r}")
        if any(key in node for key in ("matrix", "rotation", "scale", "translation")):
            raise ContractError(f"{where}: part nodes must not be transformed (board coordinates)")
        info = _validate_part(doc, buffers, n, extras, where)
        info.kind = extras.kind
        for face in extras.faces:
            if face.id in face_parts:
                raise ContractError(f"{where}: duplicate face id {face.id!r}")
            face_parts[face.id] = extras.part
            features.add(face.feature)
        for edge in extras.edges:
            if edge.id in edge_parts:
                raise ContractError(f"{where}: duplicate edge id {edge.id!r}")
            edge_parts[edge.id] = extras.part
        parts[extras.part] = info
    if not parts:
        raise ContractError("no part nodes (nodes with a mesh and extras.part)")
    for part in parts.values():
        for edge in part.edges:
            for face in edge.faces:
                if face not in face_parts:
                    raise ContractError(f"edge {edge.id!r}: unknown face {face!r}")
    return MeshInfo(parts, face_parts, edge_parts, features, asset)


def _reachable(nodes: list[dict], roots: list[int]) -> list[int]:
    seen: list[int] = []
    stack = list(reversed(roots))
    while stack:
        n = stack.pop()
        if n in seen:
            raise ContractError(f"node {n} appears twice in the scene graph")
        seen.append(n)
        stack.extend(reversed(nodes[n].get("children", [])))
    return seen


def _validate_part(doc, buffers, n, extras: PartExtras, where) -> PartInfo:
    mesh = doc["meshes"][doc["nodes"][n]["mesh"]]
    tris = [p for p in mesh["primitives"] if p.get("mode", TRIANGLES) == TRIANGLES]
    lines = [p for p in mesh["primitives"] if p.get("mode", TRIANGLES) == LINES]
    if len(tris) != 1 or len(lines) > 1 or len(tris) + len(lines) != len(mesh["primitives"]):
        raise ContractError(
            f"{where}: needs exactly one triangle primitive and at most one line primitive"
        )
    tri = tris[0]
    positions = _attribute(doc, buffers, tri, "POSITION", ("VEC3",), (5126,), where)
    normals = _attribute(doc, buffers, tri, "NORMAL", ("VEC3",), (5126,), where)
    face_index = _attribute(doc, buffers, tri, FACE_ID, ("SCALAR",), ID_COMPONENTS, where)
    count = len(positions)
    if len(normals) != count or len(face_index) != count:
        raise ContractError(f"{where}: attribute counts differ")
    has_colors = "COLOR_0" in tri.get("attributes", {})
    if has_colors:
        colors = _attribute(
            doc, buffers, tri, "COLOR_0", ("VEC3", "VEC4"), (5126, 5121, 5123), where
        )
        if len(colors) != count:
            raise ContractError(f"{where}: COLOR_0 count differs from POSITION")
    indices = _indices(doc, buffers, tri, count)
    if len(indices) % 3:
        raise ContractError(f"{where}: triangle index count is not a multiple of 3")
    used = set()
    for t in range(0, len(indices), 3):
        a, b, c = indices[t : t + 3]
        if max(a, b, c) >= count:
            raise ContractError(f"{where}: index out of range")
        fa = face_index[a]
        if face_index[b] != fa or face_index[c] != fa:
            raise ContractError(f"{where}: triangle {t // 3} spans several faces")
        used.add(fa)
    if max(face_index, default=0) >= len(extras.faces):
        raise ContractError(f"{where}: {FACE_ID} value past the end of extras.faces")
    unused = [extras.faces[i].id for i in range(len(extras.faces)) if i not in used]
    if unused:
        raise ContractError(f"{where}: faces without triangles: {unused[:5]}")

    if lines:
        line = lines[0]
        lpos = _attribute(doc, buffers, line, "POSITION", ("VEC3",), (5126,), where)
        edge_index = _attribute(doc, buffers, line, EDGE_ID, ("SCALAR",), ID_COMPONENTS, where)
        if len(edge_index) != len(lpos):
            raise ContractError(f"{where}: line attribute counts differ")
        lind = _indices(doc, buffers, line, len(lpos))
        if len(lind) % 2:
            raise ContractError(f"{where}: line index count is odd")
        used_edges = set()
        for s in range(0, len(lind), 2):
            a, b = lind[s], lind[s + 1]
            if edge_index[a] != edge_index[b]:
                raise ContractError(f"{where}: line segment {s // 2} spans several edges")
            used_edges.add(edge_index[a])
        if max(edge_index, default=0) >= max(len(extras.edges), 1):
            raise ContractError(f"{where}: {EDGE_ID} value past the end of extras.edges")
        unused = [e.id for i, e in enumerate(extras.edges) if i not in used_edges]
        if unused:
            raise ContractError(f"{where}: edges without segments: {unused[:5]}")
    elif extras.edges:
        raise ContractError(f"{where}: extras.edges given but no line primitive")

    return PartInfo(
        name=extras.part,
        node=n,
        vertex_count=count,
        triangle_count=len(indices) // 3,
        faces=extras.faces,
        edges=extras.edges,
        positions=positions,
        face_index=face_index,
        has_colors=has_colors,
        indices=indices,
    )
