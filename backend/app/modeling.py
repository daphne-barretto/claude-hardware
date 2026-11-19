"""Modeling tools on the canvas, shared by the mock API (`app.mock_api`) and the real
session API (`app.session_api`).

A modeling edit (move a part, push or pull a face, add a hole, boss, rib, fillet, chamfer or
shell, mirror or pattern a feature) is a hand edit: it goes into the design's parameters
(`params["edits"]`) and code (`geometry.modeling`) as a named feature, locked
(`edits.<tag>`), so Claude designs around it like any locked value. Editing its values,
suppressing it and deleting it are hand edits too.

- `new_edit()` checks an `EditCreate` against the build it was made on (its mesh and features)
  and names it.
- `updated_edit()` applies an `EditUpdate`.
- `with_edits()` lays the edits over a build's CAD source, each listed, locked, at its line.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.contracts.mesh import EDGE_ID, load_gltf, read_accessor
from app.contracts.session import (
    EDIT_VALUES,
    Edit,
    EditCreate,
    EditUpdate,
)
from app.sketches import _on_face, _plane, face_triangles
from app.standards import StandardsFile
from geometry.modeling import dependents, edits_block

PATH = "edits"
Vec = tuple[float, float, float]


class EditError(ValueError):
    """The edit can't be made where it was asked to."""


def edge_point(gltf: str | bytes | dict, edge_id: str) -> tuple[str, Vec] | None:
    """The part an edge of the mesh is on, and a point on the edge (halfway along it)."""
    doc, buffers = load_gltf(gltf.encode() if isinstance(gltf, str) else gltf)
    for node in doc.get("nodes", []):
        extras = node.get("extras") or {}
        edges = extras.get("edges") or []
        index = next((i for i, e in enumerate(edges) if e["id"] == edge_id), None)
        if index is None or "mesh" not in node:
            continue
        segments = []
        for prim in doc["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 1 or EDGE_ID not in prim.get("attributes", {}):
                continue
            pos = read_accessor(doc, buffers, prim["attributes"]["POSITION"])
            ids = read_accessor(doc, buffers, prim["attributes"][EDGE_ID])
            idx = (
                read_accessor(doc, buffers, prim["indices"])
                if "indices" in prim
                else list(range(len(pos)))
            )
            for s in range(0, len(idx) - 1, 2):
                a, b = idx[s], idx[s + 1]
                if ids[a] == index:
                    segments.append((tuple(pos[a]), tuple(pos[b])))
        if not segments:
            return None
        lengths = [sum((p - q) ** 2 for p, q in zip(a, b, strict=True)) ** 0.5 for a, b in segments]
        half, run = sum(lengths) / 2, 0.0
        for (a, b), length in zip(segments, lengths, strict=True):
            if run + length >= half and length > 0:
                t = (half - run) / length
                point = tuple(round(a[i] + t * (b[i] - a[i]), 6) for i in range(3))
                return extras["part"], point  # type: ignore[return-value]
            run += length
        return extras["part"], segments[0][0]  # type: ignore[return-value]
    return None


def mesh_parts(gltf: str | bytes | dict) -> list[str]:
    doc, _ = load_gltf(gltf.encode() if isinstance(gltf, str) else gltf)
    extras = [n.get("extras") or {} for n in doc.get("nodes", [])]
    return [e["part"] for e in extras if e.get("part")]


def next_tag(kind: str, taken: set[str]) -> str:
    n = 1
    while f"{kind}_hand_{n}" in taken:
        n += 1
    return f"{kind}_hand_{n}"


def insert_sizes(standards: StandardsFile) -> tuple[float, float]:
    """The heat-set insert's pocket diameter and depth, from the standards."""
    dims = standards.get("heat_set_inserts").dimensions or {}
    return float(dims["hole_diameter"]), float(dims["insert_length"])


def _fits(edit: Edit, tris: list) -> str | None:
    """Why a hole, boss or rib doesn't sit on its face, or None."""
    plane = edit.plane
    assert plane is not None and plane.size is not None
    if edit.kind == "rib":
        hw, hh = (edit.length or 0) / 2, (edit.thickness or 0) / 2
    else:
        hw = hh = (edit.diameter or 0) / 2
    su, sv = plane.size[0] / 2, plane.size[1] / 2
    x, y = edit.x or 0, edit.y or 0
    eps = 1e-6
    if abs(x) + hw > su + eps or abs(y) + hh > sv + eps:
        return (
            f"the {edit.kind} runs off face {plane.face_id}: it is "
            f"{plane.size[0]:g} × {plane.size[1]:g} mm, so keep |x| + {hw:g} ≤ {su:g} and "
            f"|y| + {hh:g} ≤ {sv:g}"
        )
    if not _on_face(plane, tris, x, y):
        return f"the {edit.kind}'s centre ({x:g}, {y:g}) is not on face {plane.face_id}"
    return None


def new_edit(
    body: EditCreate,
    gltf: str | bytes | dict,
    features: dict[str, str],
    taken: set[str],
    standards: StandardsFile,
) -> Edit | None:
    """The edit on the build's mesh `gltf`, whose features are `features` (tag -> part), named
    apart from the tags in `taken`. None if the mesh has no such face or edge. Raises EditError
    if the edit can't be made there."""
    data: dict[str, Any] = body.model_dump()
    kind = body.kind
    if body.face_id is not None:
        found = face_triangles(gltf, body.face_id)
        if found is None:
            return None
        plane = _plane(body.build_id, body.face_id, *found)
        if not plane.planar:
            raise EditError(f"face {body.face_id} is not flat: pick a flat face")
        if body.part is not None and body.part != plane.part:
            raise EditError(f"face {body.face_id} is on {plane.part}, not {body.part}")
        data.update(plane=plane, part=plane.part)
    if body.edge_id is not None:
        at = edge_point(gltf, body.edge_id)
        if at is None:
            return None
        data.update(part=at[0], point=at[1])
    if kind in ("move", "shell") and body.part not in mesh_parts(gltf):
        raise EditError(f"the build has no part {body.part}")
    if body.source is not None:
        if body.source not in features:
            raise EditError(f"the build has no feature {body.source}")
        data["part"] = features[body.source]
    for name in EDIT_VALUES[kind]:  # the values it reads, with their defaults
        if data.get(name) is None and name in ("x", "y", "dx", "dy", "dz", "rotation"):
            data[name] = 0.0
    if kind in ("hole", "boss"):
        data["insert"] = bool(body.insert)
        if body.insert:
            diameter, depth = insert_sizes(standards)
            if kind == "hole":
                data["diameter"] = diameter
                data["depth"] = max(depth, body.depth or 0)
            else:
                data["pocket_diameter"], data["pocket_depth"] = diameter, depth
                if body.diameter is None:
                    data["diameter"] = diameter + 3
                if body.height is None:
                    data["height"] = depth + 1
    tag = next_tag(kind, taken)
    data["tag"] = tag
    if kind == "boss" and data.get("insert"):
        data["insert_tag"] = "insert_" + tag.removeprefix("boss_")
    try:
        edit = Edit.model_validate(data)
    except ValidationError as error:
        raise EditError(error.errors()[0]["msg"]) from None
    if kind == "boss" and edit.insert and edit.pocket_depth and edit.height:
        if edit.pocket_depth >= edit.height:
            raise EditError(
                f"the insert pocket ({edit.pocket_depth:g} mm) is deeper than the boss "
                f"({edit.height:g} mm)"
            )
    if kind in ("hole", "boss", "rib"):
        assert body.face_id is not None
        error = _fits(edit, face_triangles(gltf, body.face_id)[2])  # type: ignore[index]
        if error:
            raise EditError(error)
    return edit


def source_parts(source: dict[str, Any]) -> dict[str, str]:
    """Each feature of a `BuildSource` and its part."""
    return {f["tag"]: f["part"] for f in source.get("features", [])}


def updated_edit(edit: Edit, body: EditUpdate) -> Edit:
    """The edit with the update's values. Raises EditError if a value doesn't fit its kind."""
    changes = body.model_dump(exclude_unset=True)
    suppressed = changes.pop("suppressed", None)
    wrong = [k for k in changes if k not in EDIT_VALUES[edit.kind]]
    if wrong:
        raise EditError(f"a {edit.kind} has no {', '.join(wrong)}")
    if edit.kind in ("hole", "boss") and edit.insert and changes.get("insert") is not False:
        fixed = [k for k in ("diameter", "depth") if k in changes and edit.kind == "hole"]
        if fixed:
            raise EditError("the hole is sized for the set's insert: turn the insert off first")
    data = edit.model_dump()
    data.update(changes)
    if suppressed is not None:
        data["suppressed"] = suppressed
    if edit.kind == "boss" and "insert" in changes:
        pocket = "insert_" + edit.tag.removeprefix("boss_")
        data["insert_tag"] = pocket if changes["insert"] else None
        if not changes["insert"]:
            data["pocket_diameter"] = data["pocket_depth"] = None
    try:
        return Edit.model_validate(data)
    except ValidationError as error:
        raise EditError(error.errors()[0]["msg"]) from None


def edit_params(edit: Edit) -> dict[str, Any]:
    """The edit as the generator reads it (`params["edits"]`, `geometry.modeling`)."""
    out: dict[str, Any] = {"tag": edit.tag, "kind": edit.kind, "part": edit.part}
    for name in EDIT_VALUES[edit.kind]:
        value = getattr(edit, name)
        if value is not None:
            out[name] = value
    if edit.plane is not None:
        out["plane"] = {
            "origin": list(edit.plane.origin or ()),
            "normal": list(edit.plane.normal or ()),
            "x_dir": list(edit.plane.x_dir or ()),
        }
    if edit.point is not None:
        out["point"] = list(edit.point)
    if edit.source is not None:
        out["source"] = edit.source
    if edit.pocket_diameter is not None:
        out["pocket_diameter"] = edit.pocket_diameter
        out["pocket_depth"] = edit.pocket_depth
    out["suppressed"] = edit.suppressed
    return out


def with_edits(source: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
    """A `BuildSource` with the modeled features laid over it: the code applies them, and each
    is listed after the code's own features, locked, at its line (a suppressed one at its
    comment)."""
    if not edits:
        return source
    code, lines = edits_block(source["code"], edits, source.get("backend", "build123d"))
    pockets = {t: "insert_" + t.removeprefix("boss_") for t in lines if t.startswith("boss_")}
    tags = set(lines) | set(pockets.values())
    features = [f for f in source.get("features", []) if f["tag"] not in tags]
    for e in edits:
        at = list(lines[e["tag"]])
        features.append({"tag": e["tag"], "part": e["part"], "lines": at, "locked": True})
        if e["kind"] == "boss" and e.get("pocket_diameter") and not e.get("suppressed"):
            pocket = pockets[e["tag"]]
            features.append({"tag": pocket, "part": e["part"], "lines": at, "locked": True})
    return {**source, "code": code, "features": features}


def removal(edits: list[dict[str, Any]], tag: str) -> list[str]:
    """The tags a delete of `tag` takes out: it and the edits that copy it."""
    return [tag, *dependents(edits, tag)]


__all__ = [
    "PATH",
    "EditError",
    "edge_point",
    "edit_params",
    "new_edit",
    "removal",
    "source_parts",
    "updated_edit",
    "with_edits",
]
