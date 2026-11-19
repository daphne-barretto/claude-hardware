"""Analysis maps: wall thickness and draft on the generated enclosure.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`. Variants that make walls thin or thick, or drafts low, are derived from the
standards, so nothing here names a board, part or dimension.
"""

from __future__ import annotations

import copy
import math
from functools import cache

import pytest

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.maps import MAP_TYPES, validate_analysis_map
from app.contracts.mesh import MeshInfo, validate_mesh
from app.standards import load_standards
from geometry import BACKENDS, BuildResult, enclosure_parameters, generate_enclosure, occ
from geometry.analysis import THICK_FACTOR, FaceAnalysis, analysis_maps, analyze
from geometry.checks import CheckResult, run_checks
from geometry.tessellate import tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
MIN_WALL = float(STANDARDS.get("min_wall").value)
MIN_DRAFT = float(STANDARDS.get("min_draft").value)
BACKEND_NAMES = sorted(BACKENDS)
OLD, NEW = "old", "new"
BUILD_ID = "build-test"


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / (CHANGES.old_file if which == OLD else CHANGES.new_file))


def _thin(p: dict) -> None:
    p["wall"] = 0.8 * MIN_WALL


def _thick(p: dict) -> None:
    # A floor several times the minimum: a thick section that sinks.
    p["floor_top"] = p["z_bottom"] + (THICK_FACTOR + 1) * MIN_WALL


def _low_draft(p: dict) -> None:
    p["draft"] = 0.5 * MIN_DRAFT


VARIANTS = {"thin": _thin, "thick": _thick, "low_draft": _low_draft}

# (revision, params from revision, variant): the designed enclosure, the older design on the
# newer board (the demo), and variants that break one rule each.
CASES = [(OLD, OLD, None), (NEW, OLD, None)] + [(OLD, OLD, v) for v in VARIANTS]


@cache
def build(backend: str, which: str, params_from: str, variant: str | None) -> BuildResult:
    params = copy.deepcopy(enclosure_parameters(board(params_from), STANDARDS))
    if variant:
        VARIANTS[variant](params)
    return generate_enclosure(board(which), STANDARDS, params, backend)


@cache
def analysis(*case) -> dict[str, list[FaceAnalysis]]:
    return analyze(build(*case), STANDARDS)


@cache
def checks(*case) -> list[CheckResult]:
    return run_checks(board(case[1]), STANDARDS, build(*case), ["wall_thickness", "draft_angle"])


def mesh_of(result: BuildResult) -> dict:
    """The build's contract glTF, from the tessellator."""
    return tessellate(result, source={"build_id": BUILD_ID}).to_gltf()


@cache
def mesh_and_maps(*case) -> tuple[MeshInfo, dict[str, dict]]:
    mesh = validate_mesh(mesh_of(build(*case)))
    return mesh, analysis_maps(analysis(*case), STANDARDS, mesh, BUILD_ID)


def flags(amap: dict, kind: str, part: str | None = None) -> list[dict]:
    return [
        f
        for f in amap["flagged"]
        if f["kind"] == kind and (part is None or f["location"]["part"] == part)
    ]


# --- Values are ready to become vertex colors and match the analysis-map contract --------------


@pytest.mark.parametrize("case", CASES[:2], ids=["designed", "older-design-on-newer-board"])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_maps_follow_the_contract(backend: str, case: tuple) -> None:
    mesh, maps = mesh_and_maps(backend, *case)
    assert set(maps) == set(MAP_TYPES)
    for map_type, amap in maps.items():
        validated = validate_analysis_map(amap, mesh, STANDARDS)
        assert validated.type == map_type
        # Every molded part has values; the bought-in inserts are not molded and have none.
        molded = {name for name, info in mesh.parts.items() if info.kind == "enclosure"}
        assert molded and molded < set(mesh.parts)
        assert {pv.part for pv in validated.parts} == molded
        for pv in validated.parts:
            info = mesh.parts[pv.part]
            assert len(pv.values) == info.vertex_count
            numbers = [v for v in pv.values if v is not None]
            assert numbers, f"{map_type} has no values on {pv.part}"
            assert all(math.isfinite(v) and v >= 0 for v in numbers)
            # Values are per face: every vertex of a face carries the face's value.
            per_face: dict[int, set] = {}
            for v, f in zip(pv.values, info.face_index, strict=True):
                per_face.setdefault(f, set()).add(v)
            assert all(len(vs) == 1 for vs in per_face.values())


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_tessellator_faces_line_up_with_the_brep_faces(backend: str) -> None:
    # The maps' default mapping: a part's mesh faces are its B-rep faces in order.
    mesh, _ = mesh_and_maps(backend, OLD, OLD, None)
    for part, faces in analysis(backend, OLD, OLD, None).items():
        assert [f.feature for f in mesh.parts[part].faces] == [fa.feature for fa in faces]


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_wall_thickness_is_known_on_every_face(backend: str) -> None:
    for faces in analysis(backend, OLD, OLD, None).values():
        assert all(fa.wall_thickness.value is not None for fa in faces)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_draft_is_null_only_square_to_the_pull(backend: str) -> None:
    result = build(backend, OLD, OLD, None)
    for part, faces in analysis(backend, OLD, OLD, None).items():
        shape_faces = occ.faces(result.parts[part])
        for fa in faces:
            normals = [n for _, n in occ.face_samples(shape_faces[fa.index], 4)]
            square = all(abs(n[2]) > 0.999 for n in normals)
            assert (fa.draft.value is None) == square, f"{part} face {fa.index}"


def test_maps_can_follow_a_mesh_in_another_face_order() -> None:
    case = (BACKEND_NAMES[0], OLD, OLD, "thin")
    mesh, maps = mesh_and_maps(*case)
    # Reverse each part's faces in the mesh and map them back to the B-rep by ID.
    gltf = mesh_of(build(*case))
    for node in gltf["nodes"][1:]:
        node["extras"]["faces"].reverse()
    reordered = validate_mesh(_reindex(gltf))
    brep = {f.id: i for info in mesh.parts.values() for i, f in enumerate(info.faces)}
    again = analysis_maps(analysis(*case), STANDARDS, reordered, BUILD_ID, brep_faces=brep)
    assert flags(again["wall_thickness"], "thin")
    for map_type in MAP_TYPES:
        validate_analysis_map(again[map_type], reordered, STANDARDS)
        assert _by_face(again[map_type], reordered) == _by_face(maps[map_type], mesh)
        assert sorted(again[map_type]["flagged"], key=lambda f: f["id"]) == sorted(
            maps[map_type]["flagged"], key=lambda f: f["id"]
        )


def _by_face(amap: dict, mesh: MeshInfo) -> dict[str, set]:
    """Each face ID's values in a map."""
    out: dict[str, set] = {}
    for pv in amap["parts"]:
        info = mesh.parts[pv["part"]]
        for v, f in zip(pv["values"], info.face_index, strict=True):
            out.setdefault(info.faces[f].id, set()).add(v)
    return out


def _reindex(gltf: dict) -> dict:
    """Rewrite `_FACE_ID` after the faces in extras were reversed."""
    import base64
    import struct

    from app.contracts.mesh import FACE_ID

    data = bytearray(base64.b64decode(gltf["buffers"][0]["uri"].split(",", 1)[1]))
    for node in gltf["nodes"][1:]:
        count = len(node["extras"]["faces"])
        prim = gltf["meshes"][node["mesh"]]["primitives"][0]
        acc = gltf["accessors"][prim["attributes"][FACE_ID]]
        view = gltf["bufferViews"][acc["bufferView"]]
        for k in range(acc["count"]):
            offset = view["byteOffset"] + 2 * k
            (f,) = struct.unpack_from("<H", data, offset)
            struct.pack_into("<H", data, offset, count - 1 - f)
    gltf["buffers"][0]["uri"] = (
        "data:application/octet-stream;base64," + base64.b64encode(bytes(data)).decode()
    )
    return gltf


# --- Flags thin and thick spots, consistent with the wall-thickness check ---------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(str(x) for x in c))
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_thin_spots_agree_with_the_wall_thickness_check(backend: str, case: tuple) -> None:
    _, maps = mesh_and_maps(backend, *case)
    amap = maps["wall_thickness"]
    for result in checks(backend, *case):
        if result.check != "wall_thickness":
            continue
        thin = flags(amap, "thin", result.part)
        assert bool(thin) == (not result.passed), result
        if thin:
            worst = min(thin, key=lambda f: f["value"])
            assert worst["value"] == pytest.approx(result.measured, abs=1e-3)
            assert worst["message"].startswith(result.feature)
        else:
            values = [v for pv in amap["parts"] if pv["part"] == result.part for v in pv["values"]]
            assert min(v for v in values if v is not None) >= MIN_WALL


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(str(x) for x in c))
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_low_draft_agrees_with_the_draft_check(backend: str, case: tuple) -> None:
    _, maps = mesh_and_maps(backend, *case)
    amap = maps["draft"]
    for result in checks(backend, *case):
        if result.check != "draft_angle":
            continue
        low = flags(amap, "low_draft", result.part)
        assert bool(low) == (not result.passed), result
        if low:
            assert min(f["value"] for f in low) == pytest.approx(result.measured, abs=1e-3)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_the_designed_enclosure_has_no_thin_or_low_draft_spots(backend: str) -> None:
    for which in (OLD, NEW):
        _, maps = mesh_and_maps(backend, which, which, None)
        assert not flags(maps["wall_thickness"], "thin")
        assert not flags(maps["draft"], "low_draft")


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_flags_thin_walls(backend: str) -> None:
    _, maps = mesh_and_maps(backend, OLD, OLD, "thin")
    thin = flags(maps["wall_thickness"], "thin")
    assert {f["location"]["part"] for f in thin} == set(build(backend, OLD, OLD, "thin").enclosure)
    for f in thin:
        assert f["value"] < MIN_WALL
        # The standard by its title, never its ID (QA fix brief 2).
        assert STANDARDS.get("min_wall").title in f["message"] and "min_wall" not in f["message"]


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_flags_thick_sections(backend: str) -> None:
    case = (backend, OLD, OLD, "thick")
    _, maps = mesh_and_maps(*case)
    thick = flags(maps["wall_thickness"], "thick")
    assert thick, "a floor several times the minimum is not flagged"
    assert all(f["value"] > THICK_FACTOR * MIN_WALL for f in thick)
    # The designed enclosure has none, so every thick spot is on the floor's part.
    assert {f["location"]["part"] for f in thick} == {"base"}


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_designed_enclosure_has_no_thick_sections(backend: str, which: str) -> None:
    # Each revision's own design, retention posts included, has no thick flags.
    _, maps = mesh_and_maps(backend, which, which, None)
    assert not flags(maps["wall_thickness"], "thick"), flags(maps["wall_thickness"], "thick")


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_flags_low_draft(backend: str) -> None:
    _, maps = mesh_and_maps(backend, OLD, OLD, "low_draft")
    low = flags(maps["draft"], "low_draft")
    assert low
    assert all(f["value"] < MIN_DRAFT for f in low)
    assert all(STANDARDS.get("min_draft").title in f["message"] for f in low)
    assert not any("min_draft" in f["message"] for f in low)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_flagged_points_are_on_their_face(backend: str) -> None:
    case = (backend, OLD, OLD, "thin")
    result = build(*case)
    mesh, maps = mesh_and_maps(*case)
    for amap in maps.values():
        for f in amap["flagged"]:
            part = f["location"]["part"]
            ids = [mf.id for mf in mesh.parts[part].faces]
            index = ids.index(f["location"]["faces"][0])
            face = occ.faces(result.parts[part])[index]
            d, _ = occ.distance(occ.vertex(tuple(f["location"]["point"])), face)
            assert d < 1e-2, f["id"]
