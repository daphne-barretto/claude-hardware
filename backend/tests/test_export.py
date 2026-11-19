"""Exporters and build note.

Every format is written and read back with a different reader than the one that wrote it:
STEP through OpenCascade, the meshes through trimesh, DXF through ezdxf, the PDF through pypdf
and the source by running it again. The build note is compared with the check report.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import json
import re
import zipfile
from functools import cache
from pathlib import Path

import ezdxf
import pypdf
import pytest
import trimesh
from ezdxf import bbox as dxf_bbox
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.mesh import validate_mesh
from app.standards import load_standards
from geometry import BACKENDS, DEFAULT_BACKEND, BuildResult, enclosure_parameters, get_backend, occ
from geometry.assembly import BOARD_PART, board_shape
from geometry.export import (
    FILE_NAMES,
    FORMATS,
    ExportedFile,
    export_build,
    key_dimensions,
    lengths_in,
    profile,
    profile_planes,
)
from geometry.report import check_report
from geometry.tessellate import LINEAR_DEFLECTION, Tessellation, tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
OLD, NEW = "old", "new"
BACKEND_NAMES = sorted(BACKENDS)
MESH_VOLUME_REL = 0.005  # a tessellation's volume is within 0.5% of the exact solid's
MESH_BOUNDS = 0.05  # mm: a tessellation's bounds are within this of the exact solid's
PROFILE_BOUNDS = 0.01  # mm


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / (CHANGES.old_file if which == OLD else CHANGES.new_file))


@cache
def exported(
    backend: str, which: str, params_from: str
) -> tuple[BuildResult, Tessellation, dict, dict, list[ExportedFile], Path]:
    """The enclosure designed for `params_from` on board `which`, exported in every format."""
    import tempfile

    engine = get_backend(backend)
    params = enclosure_parameters(board(params_from), STANDARDS)
    code = engine.example("enclosure")
    result = engine.build(code, params)
    build_id = f"build-{backend}-{which}-{params_from}"
    mesh = tessellate(result, source={"build_id": build_id})
    report = check_report(board(which), STANDARDS, result, build_id, mesh)
    out = Path(tempfile.mkdtemp(prefix="export-"))
    files = export_build(board(which), STANDARDS, result, code, params, out, report, mesh)
    return result, mesh, report, params, files, out


def path(backend: str, fmt: str, which: str = NEW, params_from: str = NEW) -> Path:
    *_, out = exported(backend, which, params_from)
    return out / FILE_NAMES[fmt][0]


def exact_volumes(result: BuildResult) -> dict[str, float]:
    return {name: occ.volume(shape) for name, shape in result.parts.items()}


def exact_bounds(result: BuildResult) -> tuple[list[float], list[float]]:
    boxes = [occ.bbox(s) for s in result.parts.values()]
    return (
        [min(b[0][i] for b in boxes) for i in range(3)],
        [max(b[1][i] for b in boxes) for i in range(3)],
    )


def close(a: float, b: float, rel: float = MESH_VOLUME_REL) -> bool:
    return abs(a - b) <= rel * abs(b)


def mesh_close(mesh_volume: float, result: BuildResult, name: str) -> bool:
    """A part's mesh volume against its exact volume: within 0.5%, or for a small part (an
    insert), within the tessellation's chord deflection over the part's surface."""
    shape = result.parts[name]
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    allowed = max(MESH_VOLUME_REL * occ.volume(shape), LINEAR_DEFLECTION * props.Mass())
    return abs(mesh_volume - occ.volume(shape)) <= allowed


# --- Every format is written --------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_every_format_is_written(backend: str) -> None:
    *_, files, out = exported(backend, NEW, NEW)
    assert {f.format for f in files} == set(FORMATS)
    for fmt, names in FILE_NAMES.items():
        written = [f for f in files if f.format == fmt]
        assert [f.name for f in written] == list(names)
        for f in written:
            assert f.path == out / f.name and f.path.stat().st_size > 0, f.name
            assert f.media_type


def test_formats_can_be_chosen() -> None:
    result, mesh, report, params, _, _ = exported(DEFAULT_BACKEND, NEW, NEW)
    import tempfile

    out = Path(tempfile.mkdtemp(prefix="export-"))
    code = get_backend(DEFAULT_BACKEND).example("enclosure")
    files = export_build(
        board(NEW), STANDARDS, result, code, params, out, report, mesh, ["stl", "build_note"]
    )
    assert [f.format for f in files] == ["stl", "build_note"]
    assert sorted(p.name for p in out.iterdir()) == sorted(f.name for f in files)
    with pytest.raises(ValueError, match="unknown export formats"):
        export_build(board(NEW), STANDARDS, result, code, params, out, report, mesh, ["iges"])


# --- STEP re-imports through OpenCascade --------------------------------------------------------


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_step_reimports_through_opencascade(backend: str) -> None:
    result, *_ = exported(backend, NEW, NEW)
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path(backend, "step"))) == IFSelect_RetDone
    assert reader.TransferRoots() > 0
    shape = reader.OneShape()
    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()
    # Every part of the build, then the board: the bare board and each component body.
    board_solids = occ.solids(board_shape(board(NEW)))
    assert len(solids) == len(result.parts) + len(board_solids)
    for solid in solids:
        assert BRepCheck_Analyzer(solid).IsValid()
    imported = sorted(occ.volume(s) for s in solids)
    expected = sorted([*exact_volumes(result).values(), *map(occ.volume, board_solids)])
    for a, b in zip(imported, expected, strict=True):
        assert close(a, b, 1e-6), (a, b)


def test_step_names_every_part() -> None:
    from build123d import import_step

    result, *_ = exported(DEFAULT_BACKEND, NEW, NEW)
    compound = import_step(path(DEFAULT_BACKEND, "step"))
    labels = [child.label for child in compound.children]
    assert sorted(labels) == sorted([*result.parts, BOARD_PART])  # one body per part


# --- Meshes re-import through trimesh -----------------------------------------------------------


def _meshes(scene) -> list[trimesh.Trimesh]:
    geometry = scene.geometry.values() if isinstance(scene, trimesh.Scene) else [scene]
    return [g for g in geometry if isinstance(g, trimesh.Trimesh)]


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_stl_reimports_through_trimesh(backend: str) -> None:
    result, *_ = exported(backend, NEW, NEW)
    (mesh,) = _meshes(trimesh.load(path(backend, "stl"), force="mesh"))
    assert len(mesh.faces) > 0
    assert close(mesh.volume, sum(exact_volumes(result).values()))
    lo, hi = exact_bounds(result)
    for i in range(3):
        assert abs(mesh.bounds[0][i] - lo[i]) <= MESH_BOUNDS
        assert abs(mesh.bounds[1][i] - hi[i]) <= MESH_BOUNDS


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_3mf_reimports_through_trimesh_one_closed_object_per_part(backend: str) -> None:
    result, *_ = exported(backend, NEW, NEW)
    scene = trimesh.load(path(backend, "3mf"))
    by_name = {g.metadata.get("name", key): g for key, g in scene.geometry.items()}
    assert set(by_name) == set(result.parts)
    for name, volume in exact_volumes(result).items():
        assert by_name[name].is_watertight, name
        assert mesh_close(by_name[name].volume, result, name), (name, volume)
    with zipfile.ZipFile(path(backend, "3mf")) as z:
        assert 'unit="millimeter"' in z.read("3D/3dmodel.model").decode()


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_obj_reimports_through_trimesh_one_object_per_part(backend: str) -> None:
    result, *_ = exported(backend, NEW, NEW)
    scene = trimesh.load(path(backend, "obj"), split_objects=True, group_material=False)
    meshes = {key: g for key, g in scene.geometry.items() if isinstance(g, trimesh.Trimesh)}
    assert set(meshes) == set(result.parts)
    for name, volume in exact_volumes(result).items():
        assert meshes[name].is_watertight, name
        assert mesh_close(meshes[name].volume, result, name), (name, volume)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_glb_reimports_through_trimesh_and_keeps_the_mesh_contract(backend: str) -> None:
    result, mesh, *_ = exported(backend, NEW, NEW)
    file = path(backend, "glb")
    info = validate_mesh(file)  # the mesh contract, read from the binary file
    assert set(info.face_parts) == set(mesh.faces)
    scene = trimesh.load(file)
    # trimesh names a mesh's primitives `<mesh>`, `<mesh>_1`, ... and so renames parts whose
    # own names end in a number (`heat_set_insert_1`); the triangle primitives keep the order of
    # the glTF meshes, which is the order of the parts.
    triangles = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
    assert len(triangles) == len(result.parts) == len(mesh.parts)
    for name, part in zip(mesh.parts, triangles, strict=True):
        volume = exact_volumes(result)[name]
        part = part.copy()
        part.merge_vertices()
        assert part.is_watertight, name
        assert mesh_close(part.volume, result, name), (name, volume)


# --- DXF flat profiles re-import through ezdxf --------------------------------------------------


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_dxf_reimports_with_a_flat_profile_per_part(backend: str) -> None:
    result, *_ = exported(backend, NEW, NEW)
    doc = ezdxf.readfile(path(backend, "dxf"))
    auditor = doc.audit()
    assert not auditor.has_errors, [str(e) for e in auditor.errors]
    assert doc.units == ezdxf.units.MM
    planes = profile_planes(result, STANDARDS)
    assert {f"{p}_{side}" for p in result.parts for side in ("top", "bottom")} == set(planes)
    msp = doc.modelspace()
    for layer, z in planes.items():
        entities = msp.query(f'*[layer=="{layer}"]')
        assert len(entities) > 0, layer
        extents = dxf_bbox.extents(entities)
        (x0, y0, _), (x1, y1, _) = occ.bbox(profile(result.parts[layer.rsplit("_", 1)[0]], z))
        assert abs(extents.extmin.x - x0) <= PROFILE_BOUNDS, layer
        assert abs(extents.extmin.y - y0) <= PROFILE_BOUNDS, layer
        assert abs(extents.extmax.x - x1) <= PROFILE_BOUNDS, layer
        assert abs(extents.extmax.y - y1) <= PROFILE_BOUNDS, layer


# --- The drawing re-imports and states the key dimensions ---------------------------------------


def test_pdf_drawing_reimports_and_states_the_key_dimensions() -> None:
    """A sheet per part (`test_drawings.py` checks the sheets in full)."""
    result, _, report, *_ = exported(DEFAULT_BACKEND, NEW, NEW)
    reader = pypdf.PdfReader(path(DEFAULT_BACKEND, "pdf"))
    assert len(reader.pages) == len(result.parts)
    for page, part in zip(reader.pages, result.parts, strict=True):
        text = page.extract_text()
        lines = {line.strip() for line in text.splitlines()}
        (x0, y0, z0), (x1, y1, z1) = occ.bbox(result.parts[part])
        dims = key_dimensions(result, part)
        assert [round(v, 2) for _, v in dims[:3]] == [
            round(x1 - x0, 2),
            round(y1 - y0, 2),
            round(z1 - z0, 2),
        ]
        for label, value in dims:
            assert f"{label} {value:.2f}" in lines, (part, label)
        assert f"Part {part}" in lines
        assert board(NEW).name in text and report["build_id"] in text


# --- The source rebuilds the same model ---------------------------------------------------------


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_source_rebuilds_the_same_model(backend: str) -> None:
    result, _, _, params, _, out = exported(backend, NEW, NEW)
    code_name, params_name = FILE_NAMES["source"]
    saved = json.loads((out / params_name).read_text())
    assert saved == json.loads(json.dumps(params))
    rebuilt = get_backend(backend).build((out / code_name).read_text(), saved)
    assert rebuilt.tags == result.tags
    for name, volume in exact_volumes(result).items():
        assert close(occ.volume(rebuilt.parts[name]), volume, 1e-9), name


# --- The build note matches the check results ---------------------------------------------------

NOTE_CASES = [(NEW, NEW), (NEW, OLD)]  # a passing build, and the old design on the new board


def _note_sections(text: str) -> dict[str, list[list[str]]]:
    """Each `## heading` -> its table rows (cells), header and divider skipped."""
    sections: dict[str, list[list[str]]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            sections[current].append(cells)
    return {k: rows[1:] for k, rows in sections.items()}  # drop each table's header


def _value(v: dict | None) -> str:
    if not v:
        return ""
    value = v["value"]
    text = f"{value:g}" if isinstance(value, int | float) else str(value)
    unit = f" {v['unit']}" if v.get("unit") else ""
    if v.get("kind") == "tolerance":
        return f"±{text}{unit}"
    return f"{v['kind']} {text}{unit}" if v.get("kind") in ("min", "max") else f"{text}{unit}"


@pytest.mark.parametrize(("which", "params_from"), NOTE_CASES)
def test_build_note_matches_the_check_results(which: str, params_from: str) -> None:
    _, _, report, _, _, out = exported(DEFAULT_BACKEND, which, params_from)
    text = (out / FILE_NAMES["build_note"][0]).read_text()
    sections = _note_sections(text)
    headings = {"pass": "Checks passed", "fail": "Checks failed", "untested": "Not tested"}
    for status, heading in headings.items():
        expected = [r for r in report["results"] if r["status"] == status]
        rows = sections[heading]
        assert [row[0] for row in rows] == [r["id"] for r in expected], heading
        for row, r in zip(rows, expected, strict=True):
            assert row[1] == r["title"]
            if status == "untested":
                assert row[2] == r["covers"]
            else:
                assert row[2:] == [
                    r["standard"] or "",
                    _value(r["measured"]),
                    _value(r["required"]),
                    r["summary"],
                ]
    s = report["summary"]
    na = f", {s['not_applicable']} not applicable" if s.get("not_applicable") else ""
    assert f"{s['pass']} checks passed, {s['fail']} failed, {s['untested']} not tested{na}." in text
    assert report["build_id"] in text
    assert f"{board(which).name} {board(which).revision}" in text


def test_build_note_lists_failures_when_the_board_changed() -> None:
    """The old design on the new board fails checks, and the note says so."""
    _, _, report, _, _, out = exported(DEFAULT_BACKEND, NEW, OLD)
    failed = [r["id"] for r in report["results"] if r["status"] == "fail"]
    assert failed
    rows = _note_sections((out / FILE_NAMES["build_note"][0]).read_text())["Checks failed"]
    assert [row[0] for row in rows] == failed


def test_build_note_lists_strength_drop_and_heat_as_not_tested() -> None:
    _, _, report, _, _, out = exported(DEFAULT_BACKEND, NEW, NEW)
    rows = _note_sections((out / FILE_NAMES["build_note"][0]).read_text())["Not tested"]
    assert {row[0] for row in rows} >= {"strength", "drop", "heat"}


def test_build_note_lists_every_exported_file() -> None:
    *_, files, out = exported(DEFAULT_BACKEND, NEW, NEW)
    rows = _note_sections((out / FILE_NAMES["build_note"][0]).read_text())["Files"]
    assert [row[0] for row in rows] == [f.name for f in files]
    assert [row[1] for row in rows] == [f.format for f in files]


def test_lengths_in_text_convert_to_inches() -> None:
    """The build note's text follows the display unit."""
    assert lengths_in("within ±0.2 mm, 25.4mm tall", "mm") == "within ±0.2 mm, 25.4mm tall"
    assert lengths_in("within ±0.2 mm, 25.4mm tall", "in") == "within ±0.007874 in, 1 in tall"
    assert lengths_in("0.80 mm from the base, area 4 mm2", "in") == (
        "0.0315 in from the base, area 4 mm2"
    )
    assert lengths_in("turned 90°, 3 mmHg", "in") == "turned 90°, 3 mmHg"
