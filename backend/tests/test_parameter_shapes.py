"""Parameters say where they are measured on the model and which faces drag them.

Checked against the fixture mesh the canvas draws (`fixtures/enclosure.gltf`), so a dimension
drawn from the parameters lands on the faces it names, and a drag moves real faces.
"""

from __future__ import annotations

import json
import math
from functools import cache

import pytest
from pydantic import ValidationError

from app.contracts.common import FIXTURES_DIR
from app.contracts.mesh import FACE_ID, load_gltf, read_accessor
from app.contracts.session import Parameter
from app.fixtures.generate import boards, design_for, parameters_for
from app.fixtures.geometry import Enclosure
from app.standards import load_standards

TOL = 1e-3


def params() -> list[dict]:
    return json.loads((FIXTURES_DIR / "parameters.json").read_text())


@cache
def face_triangles() -> dict[str, list[tuple]]:
    """Face ID -> triangles (board coordinates, mm) of the fixture mesh."""
    doc, buffers = load_gltf(FIXTURES_DIR / "enclosure.gltf")
    out: dict[str, list[tuple]] = {}
    for node in doc["nodes"]:
        extras = node.get("extras", {})
        if "part" not in extras:
            continue
        faces = [f["id"] for f in extras["faces"]]
        for prim in doc["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 4:
                continue
            pos = read_accessor(doc, buffers, prim["attributes"]["POSITION"])
            ids = read_accessor(doc, buffers, prim["attributes"][FACE_ID])
            idx = read_accessor(doc, buffers, prim["indices"])
            for i in range(0, len(idx), 3):
                a, b, c = idx[i : i + 3]
                out.setdefault(faces[int(ids[a])], []).append((pos[a], pos[b], pos[c]))
    return out


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b, strict=True))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def _distance_to_triangle(p, tri) -> float:
    """Distance from p to a triangle (exact for points over it; edges by their segments)."""
    a, b, c = tri
    n = _cross(_sub(b, a), _sub(c, a))
    nn = math.sqrt(_dot(n, n))
    n = tuple(x / nn for x in n)
    d = _dot(_sub(p, a), n)
    q = tuple(pi - d * ni for pi, ni in zip(p, n, strict=True))
    inside = all(
        _dot(_cross(_sub(v1, v0), _sub(q, v0)), n) >= -1e-9 for v0, v1 in ((a, b), (b, c), (c, a))
    )
    if inside:
        return abs(d)

    def seg(p, u, v):
        uv = _sub(v, u)
        t = max(0.0, min(1.0, _dot(_sub(p, u), uv) / _dot(uv, uv)))
        w = _sub(p, tuple(ui + t * x for ui, x in zip(u, uv, strict=True)))
        return math.sqrt(_dot(w, w))

    return min(seg(p, a, b), seg(p, b, c), seg(p, c, a))


def on_face(point, face: str) -> bool:
    return min(_distance_to_triangle(point, t) for t in face_triangles()[face]) < TOL


def test_every_parameter_validates_against_the_contract() -> None:
    for p in params():
        Parameter.model_validate(p)


def test_every_length_on_the_model_has_a_dimension() -> None:
    """Lengths that have one place on the model say where it is; angles, gaps and the
    material don't have a dimension."""
    with_dims = {p["name"] for p in params() if p["dimension"]}
    assert with_dims, "no parameter is measured on the model"
    for p in params():
        if p["unit"] != "mm":
            assert p["dimension"] is None and p["drag"] is None, p["name"]
    # Every parameter that drives a feature is measured on that feature's faces.
    for p in params():
        if p["feature"]:
            # An end may be on no face: a boss's radius starts at its axis, in the insert pocket.
            ends = (p["dimension"]["start"], p["dimension"]["end"])
            faces = [e["face"] for e in ends if e["face"]]
            assert faces and all(f.split(".")[0] == p["feature"] for f in faces), p["name"]


def test_dimension_ends_lie_on_the_faces_they_name_and_measure_the_value() -> None:
    for p in params():
        dim = p["dimension"]
        if not dim:
            continue
        for end in (dim["start"], dim["end"]):
            if end["face"] is None:
                continue
            assert end["face"] in face_triangles(), f"{p['name']}: no face {end['face']}"
            assert on_face(end["point"], end["face"]), f"{p['name']}: {end} is off its face"
        span = math.dist(dim["start"]["point"], dim["end"]["point"])
        assert span == pytest.approx(p["value"], abs=2e-3), p["name"]


def test_dimension_ends_name_the_part_their_face_is_on() -> None:
    doc, _ = load_gltf(FIXTURES_DIR / "enclosure.gltf")
    part_of = {
        f["id"]: n["extras"]["part"]
        for n in doc["nodes"]
        if "part" in n.get("extras", {})
        for f in n["extras"]["faces"]
    }
    for p in params():
        if not p["dimension"]:
            continue
        for end in (p["dimension"]["start"], p["dimension"]["end"]):
            if end["face"]:
                assert part_of[end["face"]] == end["part"], p["name"]


def test_drag_faces_exist_and_move_outward_along_their_axis() -> None:
    """Dragging a face outward along the axis makes the value bigger: the axis points the way
    the faces face."""
    dragged = [p for p in params() if p["drag"]]
    assert dragged, "no parameter can be dragged"
    seen: set[str] = set()
    for p in dragged:
        axis = p["drag"]["axis"]
        assert math.isclose(math.sqrt(_dot(axis, axis)), 1.0)
        for face in p["drag"]["faces"]:
            assert face not in seen, f"{face} drags two parameters"
            seen.add(face)
            tris = face_triangles()[face]
            area_normal = [0.0, 0.0, 0.0]
            for a, b, c in tris:
                n = _cross(_sub(b, a), _sub(c, a))
                area_normal = [x + y for x, y in zip(area_normal, n, strict=True)]
            length = math.sqrt(_dot(area_normal, area_normal))
            assert _dot(area_normal, axis) / length > 0.95, f"{p['name']}: {face} faces away"


def test_draggable_parameters_are_measured_to_a_dragged_face() -> None:
    """The dimension of a draggable value ends on a face that drags it, so the drawn
    dimension and the drag are the same thing."""
    for p in params():
        if p["drag"]:
            assert p["dimension"], p["name"]
            ends = {p["dimension"]["start"]["face"], p["dimension"]["end"]["face"]}
            assert ends & set(p["drag"]["faces"]), p["name"]


def test_parameters_follow_the_design_built_from_the_board_files() -> None:
    old, _, _ = boards()
    design = design_for(old, load_standards())
    enc = Enclosure(design)
    built = parameters_for(enc, {p.name: p for p in enc.build()})
    assert built == params()


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"drag": {"faces": ["lid.outer_cap"], "axis": [0, 0, 2]}}, "unit vector"),
        ({"drag": {"faces": [], "axis": [0, 0, 1]}}, "at least 1"),
        ({"unit": "deg"}, "only lengths"),
        (
            {
                "dimension": {
                    "kind": "distance",
                    "start": {"point": [0, 0, 0], "part": "lid", "face": None},
                    "end": {"point": [0, 0, 1], "part": "lid", "face": "lid.outer_cap"},
                }
            },
            "both its face and its part",
        ),
    ],
)
def test_parameter_contract_rejects_bad_shapes(change, problem) -> None:
    base = next(p for p in params() if p["drag"])
    with pytest.raises(ValidationError, match=problem):
        Parameter.model_validate({**base, **change})
