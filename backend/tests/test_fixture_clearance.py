"""The fixture's component clearance uses the real check's definition.

`component_clearance` in `geometry/checks.py` measures the exact distance from each component's
body to every part of the enclosure, in any direction. The fixture result must report the same
number on the fixture mesh, so the canvas section cut and the checks panel agree in the mock.
"""

from __future__ import annotations

import json

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS_Compound

from app.contracts.common import FIXTURES_DIR
from app.contracts.mesh import load_gltf, read_accessor
from app.fixtures.generate import boards
from geometry import occ
from geometry.checks import _component_box

TOLERANCE = 0.05  # mm


def _fixture_parts() -> dict[str, TopoDS_Compound]:
    """Every part of `fixtures/enclosure.gltf` as an exact shape made of its triangles."""
    doc, buffers = load_gltf(FIXTURES_DIR / "enclosure.gltf")
    parts = {}
    for node in doc["nodes"]:
        if "mesh" not in node:
            continue
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for prim in doc["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 4:
                continue
            positions = read_accessor(doc, buffers, prim["attributes"]["POSITION"])
            indices = read_accessor(doc, buffers, prim["indices"])
            for k in range(0, len(indices), 3):
                poly = BRepBuilderAPI_MakePolygon()
                for i in indices[k : k + 3]:
                    poly.Add(gp_Pnt(*positions[i]))
                poly.Close()
                builder.Add(compound, BRepBuilderAPI_MakeFace(poly.Wire(), True).Face())
        parts[node["name"]] = compound
    return parts


def _clearance_result() -> dict:
    report = json.loads((FIXTURES_DIR / "checks.json").read_text())
    return next(r for r in report["results"] if r.get("standard") == "min_component_clearance")


def test_fixture_component_clearance_is_the_exact_distance_to_the_enclosure() -> None:
    _, board, _ = boards()
    parts = _fixture_parts()
    closest = min(
        (occ.distance(shape, _component_box(board, c))[0], c.ref, part)
        for c in board.components
        for part, shape in parts.items()
    )
    result = _clearance_result()
    measured = result["measured"]["value"]
    assert abs(measured - closest[0]) <= TOLERANCE, (
        f"fixture says {measured} mm, the exact distance is {closest[0]:.3f} mm "
        f"({closest[1]} to the {closest[2]})"
    )
    limit = result["required"]["value"]
    assert result["status"] == ("pass" if closest[0] >= limit else "fail")


def test_fixture_component_clearance_says_what_it_measures() -> None:
    covers = _clearance_result()["covers"].lower()
    assert "exact distance" in covers
    assert "every part of the enclosure" in covers
    assert "vertical" not in covers
