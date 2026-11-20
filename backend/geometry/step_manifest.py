"""The design record a STEP export carries, so importing the file gives back the same build.

A STEP file holds named bodies, nothing more. A build is more than that: each part's material,
kind (molded enclosure part or bought-in insert) and placement, the features that made it with
their tools, the feature every face belongs to, the face IDs comments and check locations point
at, the board the design was made for (whose bodies the export includes as the part `board`)
and the object it fits. Without them an import has to
guess: every body becomes a molded enclosure part with no material, the board's bodies become
enclosure parts that overlap the board, and features are recognized afresh under other tags, so
the same design checks very differently once it comes back.

`embed` writes that record into the exported file as a STEP comment (`/* ... */`, which every
STEP reader skips), and `read` finds it in an imported file. The comment holds one zlib stream,
base64 encoded: the record as JSON, a NUL byte, then one BREP compound of every part's exact
solid (in the record's order) followed by every feature's tools. The parts come back from that
BREP, not from the STEP bodies, so their geometry is exactly the build's (a STEP writer may
reparametrize a surface, which moves the points checks sample on it); the STEP bodies are
compared with them, and a file whose bodies were changed after export is imported as if it had
no record.

`read` runs in the import worker on a user's file, so it only decompresses (with a size cap) and
parses JSON, checking every field; the BREP bytes are handed, unparsed, to the sandbox that reads
the file (`geometry.step_import`). Nothing here loads the CAD kernel except `describe` and
`shapes_brep`, which run on the exporting side.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geometry.parts import ENCLOSURE, INSERT

MARK = "claude-hardware design"
VERSION = 1
MAX_RECORD_BYTES = 64 * 1024 * 1024  # decompressed: the record and its shapes
_LINE = 76
_COMMENT = re.compile(
    rb"/\* " + re.escape(MARK.encode()) + rb" (\d+)\n([A-Za-z0-9+/=\n]*?)\*/", re.DOTALL
)
NAME = re.compile(r"[A-Za-z0-9_\-]{1,128}")  # part names and feature tags, as builds make them
FACE_ID = re.compile(r"[A-Za-z0-9_\-]{1,128}\.[A-Za-z0-9_\-]{1,128}\.\d{1,9}")  # part.feature.n
_KINDS = (ENCLOSURE, INSERT)


class BadRecord(ValueError):
    """The record in a file is malformed: the file is imported as if it had none."""


@dataclass(frozen=True)
class PartRecord:
    name: str
    material: str | None
    kind: str
    placement: tuple[float, float, float] | None
    faces: list[str]  # the feature of each face, in `occ.faces()` order
    ids: list[str]  # the ID of each face (`geometry.tessellate`), in the same order
    bbox: tuple[tuple[float, float, float], tuple[float, float, float]]


@dataclass(frozen=True)
class FeatureRecord:
    tag: str
    part: str
    tools: int  # how many of the compound's tools, in order, are this feature's


@dataclass(frozen=True)
class DesignRecord:
    parts: list[PartRecord]
    features: list[FeatureRecord]
    board: tuple[str, str] | None  # name, revision: the board the design was made for
    board_part: str | None  # the label of the board's bodies in the file, if it has them
    fit_object: dict[str, Any] | None  # the design's `object` parameter (`geometry.fit_object`)
    counters: dict[str, int]  # the tessellation's next unused face numbers
    shapes: bytes  # BREP of a compound of every part's solid, then every feature's tools

    def part(self, name: str) -> PartRecord | None:
        return next((p for p in self.parts if p.name == name), None)


# --- Writing (the exporting side) ---------------------------------------------------------------


def shapes_brep(shapes: list) -> bytes:
    """A compound of `shapes`, in order, as BREP bytes (exact, without triangulations)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_FormatVersion

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    stream = io.BytesIO()
    version = TopTools_FormatVersion.TopTools_FormatVersion_CURRENT
    BRepTools.Write_s(compound, stream, False, False, version)
    return stream.getvalue()


def describe(
    result,
    parts: list[str],
    board: tuple[str, str] | None,
    board_part: str | None,
    fit_object: dict[str, Any] | None,
    mesh,
) -> DesignRecord:
    """The record of `parts` of a build (`geometry.interface.BuildResult`), whose face IDs are
    those of its tessellation `mesh` (`geometry.tessellate.Tessellation`)."""
    from geometry import occ

    records = []
    for name in parts:
        placement = result.placements.get(name)
        records.append(
            PartRecord(
                name,
                result.materials.get(name),
                result.kind(name),
                tuple(placement) if placement is not None else None,
                [ref.feature for ref in result.face_refs[name]],
                [face.id for face in mesh.parts[name].faces],
                occ.bbox(result.parts[name]),
            )
        )
    features = [f for f in result.features if f.part in parts]
    return DesignRecord(
        records,
        [FeatureRecord(f.tag, f.part, len(f.tools)) for f in features],
        board,
        board_part,
        fit_object,
        {g: n for g, n in mesh.counters.items() if g.split(".", 1)[0] in parts},
        shapes_brep([result.parts[n] for n in parts] + [t for f in features for t in f.tools]),
    )


def _to_json(record: DesignRecord) -> dict[str, Any]:
    return {
        "parts": [
            {
                "name": p.name,
                "material": p.material,
                "kind": p.kind,
                "placement": list(p.placement) if p.placement is not None else None,
                "faces": p.faces,
                "ids": p.ids,
                "bbox": [list(c) for c in p.bbox],
            }
            for p in record.parts
        ],
        "features": [{"tag": f.tag, "part": f.part, "tools": f.tools} for f in record.features],
        "board": list(record.board) if record.board else None,
        "board_part": record.board_part,
        "object": record.fit_object,
        "counters": record.counters,
    }


def embed(path: Path | str, record: DesignRecord) -> None:
    """Write `record` into the STEP file at `path`, as a comment after its first line."""
    path = Path(path)
    text = path.read_bytes()
    first, sep, rest = text.partition(b"\n")
    if not sep or not first.strip().startswith(b"ISO-10303-21"):
        raise ValueError(f"{path} is not a STEP file")
    payload = json.dumps(_to_json(record), separators=(",", ":")).encode()
    blob = base64.b64encode(zlib.compress(payload + b"\0" + record.shapes, 9))
    lines = b"\n".join(blob[i : i + _LINE] for i in range(0, len(blob), _LINE))
    comment = b"/* %s %d\n%s\n*/\n" % (MARK.encode(), VERSION, lines)
    path.write_bytes(first + b"\n" + comment + rest)


# --- Reading (the import worker, on a user's file) ----------------------------------------------


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise BadRecord(f"bad name {value!r}")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise BadRecord(f"bad number {value!r}")
    return float(value)


def _point(value: Any) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise BadRecord(f"bad point {value!r}")
    x, y, z = (_number(v) for v in value)
    return (x, y, z)


def _text(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise BadRecord(f"bad text {value!r}")
    return value


def _face_id(value: Any) -> str:
    if not isinstance(value, str) or not FACE_ID.fullmatch(value):
        raise BadRecord(f"bad face ID {value!r}")
    return value


def _parse(data: dict[str, Any], shapes: bytes) -> DesignRecord:
    if not isinstance(data, dict):
        raise BadRecord("the record is not an object")
    parts = []
    for p in data.get("parts") or []:
        if not isinstance(p, dict):
            raise BadRecord("bad part")
        kind = p.get("kind")
        if kind not in _KINDS:
            raise BadRecord(f"bad part kind {kind!r}")
        faces = p.get("faces")
        if not isinstance(faces, list) or not faces:
            raise BadRecord("bad faces")
        bbox = p.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 2:
            raise BadRecord("bad bounding box")
        ids = p.get("ids")
        if not isinstance(ids, list) or len(ids) != len(faces):
            raise BadRecord("bad face IDs")
        placement = p.get("placement")
        parts.append(
            PartRecord(
                _name(p.get("name")),
                _text(p.get("material")),
                kind,
                _point(placement) if placement is not None else None,
                [_name(f) for f in faces],
                [_face_id(i) for i in ids],
                (_point(bbox[0]), _point(bbox[1])),
            )
        )
    names = [p.name for p in parts]
    if not parts or len(set(names)) != len(names):
        raise BadRecord("no parts, or parts that share a name")
    features = []
    for f in data.get("features") or []:
        if not isinstance(f, dict):
            raise BadRecord("bad feature")
        count = f.get("tools")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BadRecord("bad tool count")
        part = _name(f.get("part"))
        if part not in names:
            raise BadRecord(f"a feature of an unknown part {part!r}")
        features.append(FeatureRecord(_name(f.get("tag")), part, count))
    board = data.get("board")
    if board is not None:
        if not isinstance(board, list) or len(board) != 2:
            raise BadRecord("bad board")
        board = (_text(board[0]) or "", _text(board[1]) or "")
    board_part = data.get("board_part")
    if board_part is not None:
        board_part = _name(board_part)
        if board_part in names:
            raise BadRecord("the board shares a name with a part")
    fit_object = data.get("object")
    if fit_object is not None and not isinstance(fit_object, dict):
        raise BadRecord("bad object")
    counters = data.get("counters") or {}
    if not isinstance(counters, dict):
        raise BadRecord("bad counters")
    for group, number in counters.items():
        _face_id(f"{group}.0")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise BadRecord("bad counter")
    return DesignRecord(parts, features, board, board_part, fit_object, counters, shapes)


def read(data: bytes) -> DesignRecord | None:
    """The design record in a STEP file's bytes, or None if it has none (or a malformed one)."""
    found = _COMMENT.search(data)
    if found is None or int(found.group(1)) != VERSION:
        return None
    try:
        packed = base64.b64decode(found.group(2).replace(b"\n", b""), validate=True)
        inflate = zlib.decompressobj()
        raw = inflate.decompress(packed, MAX_RECORD_BYTES)
        if inflate.unconsumed_tail or not inflate.eof:
            return None
        payload, sep, shapes = raw.partition(b"\0")
        if not sep:
            return None
        return _parse(json.loads(payload), shapes)
    except (binascii.Error, zlib.error, ValueError, RecursionError):
        return None


__all__ = [
    "MARK",
    "BadRecord",
    "DesignRecord",
    "FeatureRecord",
    "PartRecord",
    "describe",
    "embed",
    "read",
    "shapes_brep",
]
