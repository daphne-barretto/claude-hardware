"""Multipart assemblies.

The enclosure is an assembly of parts (base, lid, heat-set inserts, board), each with a name,
material and placement. Checks run between parts and name the two parts of each result;
interference between any pair is caught; STEP keeps the parts as separate bodies, and exports
can be of one part or the whole assembly.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, component or dimension.
"""

from __future__ import annotations

import json
import tempfile
from functools import cache
from itertools import combinations
from pathlib import Path

import pytest
import trimesh
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN

from app.agent.services import SANDBOX_EPILOGUE, unpack_shapes
from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import validate_check_report
from app.contracts.mesh import validate_mesh
from app.standards import load_standards
from geometry import BACKENDS, DEFAULT_BACKEND, BuildError, BuildResult, get_backend, occ
from geometry.assembly import BOARD_PART, assembly, neighbours
from geometry.checks import run_checks
from geometry.export import export_build
from geometry.interface import BOARD, ENCLOSURE, INSERT
from geometry.params import enclosure_parameters
from geometry.report import check_report
from geometry.sandbox import run_build
from geometry.tessellate import tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
BACKEND_NAMES = sorted(BACKENDS)
MATERIAL = str(STANDARDS.get("material").value)
INSERTS = STANDARDS.get("heat_set_inserts")
VOLUME_EPS = 1e-6


@cache
def board() -> Board:
    return load_board(BOARDS_DIR / CHANGES.new_file)


@cache
def params() -> dict:
    return enclosure_parameters(board(), STANDARDS)


@cache
def build(backend: str = DEFAULT_BACKEND) -> BuildResult:
    engine = get_backend(backend)
    return engine.build(engine.example("enclosure"), params())


def inserts(result: BuildResult) -> list[str]:
    return [name for name in result.parts if result.kind(name) == INSERT]


# --- Builds return named parts with materials; the canvas shows them separately ----------------


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_build_returns_named_parts_with_materials_and_placements(backend: str) -> None:
    result = build(backend)
    assert set(result.enclosure) == {"base", "lid"}
    for name in result.enclosure:
        assert result.materials[name] == MATERIAL
        assert occ.is_closed_valid_solid(result.parts[name])
    # One insert per boss, of the standard's material, placed at its pocket's mouth.
    names = inserts(result)
    assert len(names) == len(params()["bosses"])
    mouths = {(b["x"], b["y"], params()["boss"]["bottom"]) for b in params()["bosses"]}
    for name in names:
        assert result.materials[name] == INSERTS.material
        assert occ.is_closed_valid_solid(result.parts[name])
        assert result.placement(name) in mouths
        (x0, y0, z0), (x1, y1, _) = occ.bbox(result.parts[name])
        x, y, z = result.placement(name)
        assert abs((x0 + x1) / 2 - x) < 1e-6 and abs((y0 + y1) / 2 - y) < 1e-6
        assert abs(z0 - z) < 1e-6
    assert {result.placement(n) for n in names} == mouths


def test_assembly_adds_the_board_with_its_material() -> None:
    parts = assembly(build(), board())
    assert list(parts) == [*build().parts, BOARD_PART]
    b = parts[BOARD_PART]
    assert b.kind == BOARD and b.material == board().pcb.material and b.material
    # The bare board plus a body for every component.
    assert len(occ.solids(b.shape)) == 1 + len(board().components)
    listing = [p.to_json() for p in parts.values()]
    assert all(set(p) == {"name", "kind", "material", "placement"} for p in listing)
    assert {p["kind"] for p in listing} == {ENCLOSURE, INSERT, BOARD}


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_canvas_mesh_has_one_node_per_part_with_its_material(backend: str) -> None:
    result = build(backend)
    info = validate_mesh(tessellate(result).to_gltf())
    assert set(info.parts) == set(result.parts)
    doc = tessellate(result).to_gltf()
    extras = {n["name"]: n["extras"] for n in doc["nodes"] if "mesh" in n}
    for name in result.parts:
        assert extras[name]["kind"] == result.kind(name)
        assert extras[name]["material"] == result.materials[name]
        assert extras[name]["placement"] == list(result.placement(name))
        # Faces of a part are only that part's: a part can be shown, hidden or picked alone.
        assert all(f.id.startswith(name + ".") for f in info.parts[name].faces)


def test_generator_cannot_claim_the_board_or_an_unknown_kind() -> None:
    engine = get_backend(DEFAULT_BACKEND)
    code = engine.example("enclosure")
    for patch, message in (
        ('"kinds": {name: "insert" for name in inserts}', "kind must be"),
        ('"parts": {"base": base, "lid": lid, **inserts}', "the board's part name"),
    ):
        assert patch in code
        bad = code.replace(
            patch,
            patch.replace('"insert"', '"widget"')
            if "kinds" in patch
            else patch.replace('"lid": lid', '"lid": lid, "board": base'),
        )
        with pytest.raises(BuildError, match=message):
            engine.build(bad, params())


def test_sandboxed_builds_keep_kinds_and_placements() -> None:
    engine = get_backend(DEFAULT_BACKEND)
    run = run_build(engine.example("enclosure") + SANDBOX_EPILOGUE, params(), backend=engine.name)
    assert run.ok, run.error
    result = unpack_shapes(run.shapes, engine.name)
    expected = build()
    assert result.kinds == expected.kinds
    assert result.materials == expected.materials
    for name in expected.parts:
        assert result.placement(name) == pytest.approx(expected.placement(name))


# --- Checks report which two parts a result is between -----------------------------------------


@cache
def report() -> dict:
    result = build()
    mesh = tessellate(result)
    data = check_report(board(), STANDARDS, result, "build-assembly", mesh)
    validate_check_report(data, STANDARDS, validate_mesh(mesh.to_gltf()))
    return data


def test_checks_between_parts_name_both_parts() -> None:
    results = report()["results"]
    names = set(assembly(build(), board()))
    paired = [r for r in results if r["parts"]]
    for r in paired:
        a, b = r["parts"]
        assert a != b and {a, b} <= names, r["id"]
        assert f"between {a} and {b}" in r["summary"], r["id"]
    by_check: dict[str, set[tuple[str, str]]] = {}
    for r in paired:
        by_check.setdefault(r["check"], set()).add(tuple(r["parts"]))
    # Lid to base fit and clearance.
    assert ("base", "lid") in by_check["fit_tolerance"]
    # Each insert to its boss.
    assert by_check["insert_pockets"] == {("lid", n) for n in inserts(build())}
    # Board to enclosure clearance.
    assert {p[1] for p in by_check["component_clearance"]} == {BOARD_PART}
    assert {p for p in by_check["board_fit"]} == {(n, BOARD_PART) for n in build().enclosure}
    # Interference: every pair of parts that can touch.
    pairs = set(neighbours(assembly(build(), board())))
    assert by_check["interference"] == pairs
    assert ("base", "lid") in pairs
    assert any(BOARD_PART in pair for pair in pairs)
    # A result about one part, or none, has no pair.
    single = [r for r in results if r["check"] in ("wall_thickness", "draft_angle")]
    assert single and all(r["parts"] is None for r in single)


def test_the_designed_assembly_passes_every_check_between_parts() -> None:
    failed = [r["id"] for r in report()["results"] if r["parts"] and r["status"] == "fail"]
    assert not failed, failed


def test_molding_standards_apply_to_the_molded_parts_only() -> None:
    material = [r for r in run_checks(board(), STANDARDS, build()) if r.check == "material"]
    assert {r.part for r in material} == set(build().enclosure)
    assert all(r.passed for r in material)


def _relabelled(result: BuildResult, materials: dict, kinds: dict) -> BuildResult:
    return BuildResult(
        result.backend,
        result.parts,
        result.features,
        {**result.materials, **materials},
        {**result.kinds, **kinds},
        result.placements,
    )


def _failed_report(result: BuildResult) -> dict[str, dict]:
    mesh = tessellate(result)
    data = check_report(board(), STANDARDS, result, "build-relabelled", mesh)
    validate_check_report(data, STANDARDS, validate_mesh(mesh.to_gltf()))
    return {r["id"]: r for r in data["results"] if r["status"] == "fail"}


def test_every_insert_part_is_checked_as_an_insert() -> None:
    results = [r for r in run_checks(board(), STANDARDS, build()) if r.check == "insert_pockets"]
    checked = {r.subject for r in results if r.subject in inserts(build())}
    assert checked == set(inserts(build()))
    assert all(r.passed for r in results), [r for r in results if not r.passed]


@pytest.mark.parametrize("material", [None, "insert"], ids=["own-material", "insert-material"])
def test_a_molded_part_relabelled_as_an_insert_fails(material: str | None) -> None:
    # Calling the lid an insert must not take it out of the checks unnoticed, whatever its
    # material says (even the insert material).
    other = "PLA" if MATERIAL.lower() != "pla" else "PETG"
    given = INSERTS.material if material == "insert" else other
    relabelled = _relabelled(build(), {"lid": given}, {"lid": INSERT})
    failed = _failed_report(relabelled)
    lid = [r for r in failed.values() if r["id"] == "insert_pockets:lid"]
    assert lid, sorted(failed)


def test_an_insert_of_the_wrong_material_fails() -> None:
    other = "PLA" if INSERTS.material.lower() != "pla" else "steel"
    names = inserts(build())
    wrong = _relabelled(build(), dict.fromkeys(names, other), {})
    results = run_checks(board(), STANDARDS, wrong, ["insert_pockets"])
    failed = {r.subject for r in results if not r.passed}
    assert failed == set(names)
    for r in results:
        if r.subject in names:
            assert r.measured == other and r.limit == INSERTS.material


# --- Interference between any pair of parts is caught (seeded overlaps) ------------------------


def _inside(shape, step: float = 0.05) -> tuple[float, float, float]:
    """A point inside a part's material, just under one of its faces."""
    for solid in occ.solids(shape) or [shape]:
        for face in occ.faces(solid):
            for p, n in occ.face_samples(face, 3):
                for sign in (-1, 1):
                    q = tuple(p[i] + sign * step * n[i] for i in range(3))
                    classifier = BRepClass3d_SolidClassifier(solid, gp_Pnt(*q), 1e-7)
                    if classifier.State() == TopAbs_IN:
                        return q
    raise AssertionError("no point inside the part")


def _seeded(result: BuildResult, moved: str, onto) -> BuildResult:
    """The build with one part moved so that a point inside it lands inside `onto`."""
    target = _inside(onto)
    start = _inside(result.parts[moved])
    shift = tuple(target[i] - start[i] for i in range(3))
    parts = dict(result.parts)
    parts[moved] = occ.translated(parts[moved], shift)
    return BuildResult(
        result.backend, parts, result.features, result.materials, result.kinds, result.placements
    )


def _all_pairs() -> list[tuple[str, str]]:
    return list(combinations([*build().parts, BOARD_PART], 2))


@pytest.mark.parametrize("pair", _all_pairs(), ids=lambda p: f"{p[0]}--{p[1]}")
def test_interference_between_any_pair_is_caught(pair: tuple[str, str]) -> None:
    a, b = pair
    parts = assembly(build(), board())
    # Move a part of the build onto the other part (the board stays where the board data puts it).
    moved, fixed = (b, a) if b != BOARD_PART else (a, b)
    seeded = _seeded(build(), moved, parts[fixed].shape)
    results = run_checks(board(), STANDARDS, seeded, ["interference"])
    hit = [r for r in results if r.parts == pair]
    assert len(hit) == 1, [r.parts for r in results]
    (r,) = hit
    assert r.status == "fail" and r.measured > VOLUME_EPS, r
    assert r.part in seeded.parts and r.point is not None


def test_a_seeded_overlap_fails_the_report_and_names_the_parts() -> None:
    result = build()
    insert = inserts(result)[0]
    # Push the insert sideways into its boss wall.
    gap = params()["boss"]["insert_gap"]
    parts = dict(result.parts)
    parts[insert] = occ.translated(parts[insert], (3 * gap, 0.0, 0.0))
    seeded = BuildResult(
        result.backend, parts, result.features, result.materials, result.kinds, result.placements
    )
    mesh = tessellate(seeded)
    data = check_report(board(), STANDARDS, seeded, "build-seeded", mesh)
    validate_check_report(data, STANDARDS, validate_mesh(mesh.to_gltf()))
    failed = {r["id"]: r for r in data["results"] if r["status"] == "fail"}
    assert failed[f"interference:lid--{insert}"]["parts"] == ["lid", insert]
    seat = [r for r in failed.values() if r["check"] == "insert_pockets"]
    assert [r["parts"] for r in seat] == [["lid", insert]]
    assert set(failed) == {f"interference:lid--{insert}", seat[0]["id"]}


# --- STEP keeps parts as separate bodies; exports per part or the whole assembly ---------------


@cache
def exported(parts: tuple[str, ...] | None, formats: tuple[str, ...]) -> Path:
    out = Path(tempfile.mkdtemp(prefix="assembly-"))
    code = get_backend(DEFAULT_BACKEND).example("enclosure")
    export_build(
        board(), STANDARDS, build(), code, params(), out, report(), None, list(formats), parts
    )
    return out


def _step_bodies(path: Path) -> dict[str, float]:
    from build123d import import_step

    compound = import_step(path)
    bodies = {}
    for child in compound.children:
        assert child.label not in bodies, child.label
        bodies[child.label] = sum(s.volume for s in child.solids())
    return bodies


def _volume(shape) -> float:
    return sum(occ.volume(s) for s in occ.solids(shape)) or occ.volume(shape)


def test_step_keeps_every_part_as_a_separate_body() -> None:
    bodies = _step_bodies(exported(None, ("step",)) / "enclosure.step")
    parts = assembly(build(), board())
    assert set(bodies) == set(parts)
    for name, part in parts.items():
        assert bodies[name] == pytest.approx(_volume(part.shape), rel=1e-6), name


@pytest.mark.parametrize("which", ["lid", "insert", BOARD_PART])
def test_step_of_one_part(which: str) -> None:
    name = inserts(build())[0] if which == "insert" else which
    bodies = _step_bodies(exported((name,), ("step",)) / "enclosure.step")
    assert list(bodies) == [name]
    assert bodies[name] == pytest.approx(_volume(assembly(build(), board())[name].shape), rel=1e-6)


def test_mesh_export_of_one_part() -> None:
    name = inserts(build())[0]
    out = exported((name,), ("stl", "build_note"))
    mesh = trimesh.load(out / "enclosure.stl", force="mesh")
    exact = occ.volume(build().parts[name])
    assert abs(mesh.volume - exact) <= 0.05 * exact
    assert sorted(p.name for p in out.iterdir()) == ["build-note.md", "enclosure.stl"]


def test_export_refuses_unknown_parts_and_meshes_of_the_board_alone() -> None:
    code = get_backend(DEFAULT_BACKEND).example("enclosure")
    out = Path(tempfile.mkdtemp(prefix="assembly-"))
    args = (board(), STANDARDS, build(), code, params(), out, report(), None)
    with pytest.raises(ValueError, match="unknown parts"):
        export_build(*args, ["step"], ["nope"])
    with pytest.raises(ValueError, match="only the STEP carries the board"):
        export_build(*args, ["stl"], [BOARD_PART])


def test_insert_material_and_board_material_come_from_the_data_files() -> None:
    # The materials are data, not code: the standard and the board file name them.
    assert INSERTS.material
    revisions = json.loads((BOARDS_DIR / "source" / "revisions.json").read_text())["revisions"]
    by_file = {r["output"]: r.get("pcb_material") for r in revisions}
    assert board().pcb.material == by_file[CHANGES.new_file]
