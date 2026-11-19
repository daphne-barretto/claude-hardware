"""The export menu's files: every format the exporters write is offered by
the session API contract, and the mock API hands out each one as a download that re-imports
cleanly. STEP is read back through OpenCascade, the meshes through trimesh, DXF through ezdxf,
the PDF through pypdf and the source by running it again.

Nothing here names a board, part or dimension: they come from `fixtures/` and `demos/b-parasite/`.
"""

from __future__ import annotations

import json
import math
import re
import typing
import zipfile
from functools import cache
from io import BytesIO
from pathlib import Path

import ezdxf
import pypdf
import pytest
import trimesh
from fastapi.testclient import TestClient
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from app.contracts.common import FIXTURES_DIR
from app.contracts.mesh import validate_mesh
from app.contracts.session import ExportFormat
from app.examples import examples
from app.exports import export_build_geometry
from app.mock_api import SESSIONS, app
from geometry import DEFAULT_BACKEND, get_backend, occ
from geometry.assembly import board_shape
from geometry.export import FILE_NAMES, FORMATS
from geometry.tessellate import ANGULAR_DEFLECTION

# The board revision example: these tests walk its board and existing design.
EXAMPLE = examples()[0].id

BUILD_ID = json.loads((FIXTURES_DIR / "build.json").read_text())["id"]
CHECKS = json.loads((FIXTURES_DIR / "checks.json").read_text())
EVERY_FORMAT = list(FORMATS)


@pytest.fixture(scope="module")
def client():
    SESSIONS.clear()
    with TestClient(app) as test_client:
        yield test_client


def session(client: TestClient) -> str:
    return client.post("/api/sessions", json={"example": EXAMPLE}).json()["id"]


@pytest.fixture(scope="module")
def downloads(client, tmp_path_factory) -> dict[str, dict[str, Path]]:
    """Every format exported through the mock API and downloaded: format -> name -> path."""
    sid = session(client)
    r = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": BUILD_ID, "formats": EVERY_FORMAT}
    )
    assert r.status_code == 201, r.text
    out = tmp_path_factory.mktemp("downloads")
    files: dict[str, dict[str, Path]] = {}
    for f in r.json()["files"]:
        got = client.get(f["url"])
        assert got.status_code == 200, f["name"]
        assert got.headers["content-type"].split(";")[0] == f["media_type"]
        assert f'filename="{f["name"]}"' in got.headers["content-disposition"]
        assert got.content, f["name"]
        (out / f["name"]).write_bytes(got.content)
        files.setdefault(f["format"], {})[f["name"]] = out / f["name"]
    return files


@cache
def parts() -> dict[str, float]:
    """The exported build's parts and their exact volumes."""
    return {name: occ.volume(s) for name, s in export_build_geometry().result.parts.items()}


def board_solids() -> list[float]:
    """The exact volumes of the board's bodies, which the STEP carries alongside the parts."""
    return [occ.volume(s) for s in occ.solids(board_shape(export_build_geometry().board))]


# A mesh facets round faces into chords at most ANGULAR_DEFLECTION apart, and a chord polygon
# keeps sin(a)/a of the circle's area: small round parts (the inserts) lose about that much.
MESH_REL = 1 - math.sin(ANGULAR_DEFLECTION) / ANGULAR_DEFLECTION + 0.001


def close(a: float, b: float, rel: float = MESH_REL) -> bool:
    return abs(a - b) <= rel * abs(b)


def one(downloads, fmt: str) -> Path:
    (file,) = downloads[fmt].values()
    return file


def test_the_contract_offers_every_exporter_format() -> None:
    assert list(typing.get_args(ExportFormat)) == EVERY_FORMAT


def test_every_format_downloads(downloads) -> None:
    assert {fmt: sorted(files) for fmt, files in downloads.items()} == {
        fmt: sorted(names) for fmt, names in FILE_NAMES.items()
    }


def test_only_the_chosen_formats_are_exported(client) -> None:
    sid = session(client)
    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": BUILD_ID, "formats": ["stl", "build_note"]},
    )
    assert [f["format"] for f in r.json()["files"]] == ["stl", "build_note"]
    bad = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": BUILD_ID, "formats": ["iges"]}
    )
    assert bad.status_code == 422
    empty = client.post(f"/api/sessions/{sid}/exports", json={"build_id": BUILD_ID, "formats": []})
    assert empty.status_code == 422
    missing = f"/api/sessions/{sid}/exports/{r.json()['id']}/files/nope.step"
    assert client.get(missing).status_code == 404


def step_volumes(path: Path) -> list[float]:
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    assert reader.TransferRoots() > 0
    return [occ.volume(s) for s in occ.solids(reader.OneShape())]


def mesh_parts() -> list[str]:
    """The fixture build's mesh parts, which an export can choose from (with the board)."""
    return list(validate_mesh(FIXTURES_DIR / "enclosure.gltf").parts)


def export(client: TestClient, body: dict):
    sid = session(client)
    return client.post(f"/api/sessions/{sid}/exports", json={"build_id": BUILD_ID, **body})


def test_an_export_of_one_part_has_only_that_part(client, tmp_path) -> None:
    """`parts` picks which parts of the assembly are exported."""
    for part in mesh_parts():
        r = export(client, {"formats": ["step", "glb"], "parts": [part]})
        assert r.status_code == 201, r.text
        files = {f["format"]: client.get(f["url"]).content for f in r.json()["files"]}
        (tmp_path / "part.step").write_bytes(files["step"])
        volumes = step_volumes(tmp_path / "part.step")
        assert len(volumes) == 1 and close(volumes[0], parts()[part], 1e-6)
        assert list(validate_mesh(files["glb"]).parts) == [part]


def test_an_export_of_parts_and_the_board_carries_the_board_in_the_step(client, tmp_path) -> None:
    chosen = mesh_parts()
    r = export(client, {"formats": ["step"], "parts": [*chosen, "board"]})
    assert r.status_code == 201, r.text
    (tmp_path / "parts.step").write_bytes(client.get(r.json()["files"][0]["url"]).content)
    expected = sorted([*(parts()[p] for p in chosen), *board_solids()])
    volumes = sorted(step_volumes(tmp_path / "parts.step"))
    assert len(volumes) == len(expected)
    for a, b in zip(volumes, expected, strict=True):
        assert close(a, b, 1e-6)


def test_the_board_alone_exports_as_step(client, tmp_path) -> None:
    r = export(client, {"formats": ["step"], "parts": ["board"]})
    assert r.status_code == 201, r.text
    (tmp_path / "board.step").write_bytes(client.get(r.json()["files"][0]["url"]).content)
    assert len(step_volumes(tmp_path / "board.step")) == len(board_solids())


def test_unknown_or_unexportable_parts_are_422(client) -> None:
    unknown = "-".join(mesh_parts()) + "-nope"
    r = export(client, {"formats": ["step"], "parts": [unknown]})
    assert r.status_code == 422 and unknown in r.json()["detail"]
    assert export(client, {"formats": ["step"], "parts": []}).status_code == 422
    # Only the STEP carries the board.
    r = export(client, {"formats": ["stl"], "parts": ["board"]})
    assert r.status_code == 422 and r.json()["detail"]


def test_step_reimports_through_opencascade(downloads) -> None:
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(one(downloads, "step"))) == IFSelect_RetDone
    assert reader.TransferRoots() > 0
    explorer = TopExp_Explorer(reader.OneShape(), TopAbs_SOLID)
    volumes = []
    while explorer.More():
        assert BRepCheck_Analyzer(explorer.Current()).IsValid()
        volumes.append(occ.volume(explorer.Current()))
        explorer.Next()
    expected = sorted([*parts().values(), *board_solids()])
    assert len(volumes) == len(expected)
    for a, b in zip(sorted(volumes), expected, strict=True):
        assert close(a, b, 1e-6)


def test_stl_reimports_through_trimesh(downloads) -> None:
    mesh = trimesh.load(one(downloads, "stl"), force="mesh")
    assert len(mesh.faces) > 0
    assert close(mesh.volume, sum(parts().values()))


@pytest.mark.parametrize("fmt", ["3mf", "obj"])
def test_part_meshes_reimport_through_trimesh(downloads, fmt: str) -> None:
    kwargs = {"split_objects": True, "group_material": False} if fmt == "obj" else {}
    scene = trimesh.load(one(downloads, fmt), **kwargs)
    meshes = {
        g.metadata.get("name", key): g
        for key, g in scene.geometry.items()
        if isinstance(g, trimesh.Trimesh)
    }
    assert set(meshes) == set(parts())
    for name, volume in parts().items():
        assert meshes[name].is_watertight, name
        assert close(meshes[name].volume, volume), name
    if fmt == "3mf":
        with zipfile.ZipFile(one(downloads, fmt)) as z:
            assert 'unit="millimeter"' in z.read("3D/3dmodel.model").decode()


def test_glb_reimports_through_trimesh_and_keeps_the_mesh_contract(downloads) -> None:
    file = one(downloads, "glb")
    info = validate_mesh(file)
    assert info.face_parts  # face IDs, as the canvas mesh has them
    assert set(info.parts) == set(parts())
    # trimesh renames meshes whose names clash with its own `_<n>` suffixes (part names ending
    # in a number do), so the re-imported meshes are matched by volume, not name.
    scene = trimesh.load(file)
    meshes = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
    assert len(meshes) == len(parts())
    for a, b in zip(sorted(m.volume for m in meshes), sorted(parts().values()), strict=True):
        assert close(a, b)


def test_dxf_reimports_through_ezdxf(downloads) -> None:
    doc = ezdxf.readfile(one(downloads, "dxf"))
    auditor = doc.audit()
    assert not auditor.has_errors, [str(e) for e in auditor.errors]
    assert doc.units == ezdxf.units.MM
    layers = {e.dxf.layer for e in doc.modelspace()}
    assert {layer.rsplit("_", 1)[0] for layer in layers} == set(parts())


def test_pdf_drawing_reimports_through_pypdf(downloads) -> None:
    reader = pypdf.PdfReader(BytesIO(one(downloads, "pdf").read_bytes()))
    assert len(reader.pages) == len(parts())  # a sheet per part
    for page in reader.pages:
        text = page.extract_text()
        assert CHECKS["board"]["name"] in text and BUILD_ID in text


def test_source_rebuilds_the_exported_model(downloads) -> None:
    code_name, params_name = FILE_NAMES["source"]
    files = downloads["source"]
    params = json.loads(files[params_name].read_text())
    rebuilt = get_backend(DEFAULT_BACKEND).build(files[code_name].read_text(), params)
    for name, volume in parts().items():
        assert close(occ.volume(rebuilt.parts[name]), volume, 1e-9), name


def test_build_note_lists_the_fixture_checks_and_the_files(downloads) -> None:
    text = one(downloads, "build_note").read_text()
    s = CHECKS["summary"]
    na = f", {s['not_applicable']} not applicable" if s.get("not_applicable") else ""
    assert f"{s['pass']} checks passed, {s['fail']} failed, {s['untested']} not tested{na}." in text
    assert BUILD_ID in text
    for r in CHECKS["results"]:
        assert f"| {r['id']} |" in text, r["id"]
    for files in downloads.values():
        for name in files:
            assert f"| {name} |" in text, name


def test_exports_are_written_by_the_shared_helper(client, monkeypatch) -> None:
    """The mock writes its export files through `app.exports.write_exports`, the
    helper the real session API uses."""
    from app import exports, mock_api

    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return exports.write_exports(*args, **kwargs)

    assert mock_api.write_exports is exports.write_exports
    monkeypatch.setattr(mock_api, "write_exports", spy)
    sid = session(client)
    r = client.post(f"/api/sessions/{sid}/exports", json={"build_id": BUILD_ID, "formats": ["stl"]})
    assert r.status_code == 201, r.text
    assert len(calls) == 1
    assert [f["format"] for f in r.json()["files"]] == ["stl"]


def test_the_build_note_follows_the_display_unit(client) -> None:
    """With `units: in` the build note's lengths (the measured and required values
    and the lengths in the text) are inches; the geometry files stay in millimetres."""
    sid = session(client)

    def note(units: str | None) -> str:
        body = {"build_id": BUILD_ID, "formats": ["build_note", "stl"]}
        if units:
            body["units"] = units
        r = client.post(f"/api/sessions/{sid}/exports", json=body)
        assert r.status_code == 201, r.text
        [f] = [f for f in r.json()["files"] if f["format"] == "build_note"]
        stl = next(f for f in r.json()["files"] if f["format"] == "stl")
        stls.append(client.get(stl["url"]).content)
        return client.get(f["url"]).text

    stls: list[bytes] = []
    mm, default = note("mm"), note(None)
    inches = note("in")
    assert mm == default and "Units: mm." in mm
    assert "Units: in." in inches
    assert stls[0] == stls[2], "the geometry files don't change with the display unit"
    lengths = [
        r[k]
        for r in CHECKS["results"]
        for k in ("measured", "required")
        if r.get(k) and r[k].get("unit") == "mm" and isinstance(r[k]["value"], int | float)
    ]
    assert lengths, "the fixture report has lengths to convert"
    rows = [line for line in inches.splitlines() if line.startswith("| ")]
    assert not [r for r in rows if re.search(r"\d\s?mm\b", r)], "a length is still in mm"
    for v in lengths:
        assert (
            f"{v['value'] / 25.4:.4g}" in inches
            or f"{v['value'] / 25.4:.3f}".rstrip("0").rstrip(".") in inches
        )


def test_an_unknown_unit_is_refused(client) -> None:
    sid = session(client)
    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": BUILD_ID, "formats": ["build_note"], "units": "cm"},
    )
    assert r.status_code == 422
