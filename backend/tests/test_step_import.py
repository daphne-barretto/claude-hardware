"""Importing STEP files.

The real test file is the existing case in `demos/b-parasite/` (`geometry.params.case_step_path`),
read here for what the import should hold; nothing about it is restated. The import itself runs as
the app runs it: the server starts the import worker, which reads the file in the build sandbox.

Acceptance criteria (the `check_*` steps run in `test_importing_the_existing_case`):

- Uploading a STEP file creates a build shown on the canvas: `check_import_creates_a_build_*`.
- Checks run on the imported design: `check_checks_run_on_the_imported_design`.
- Selection, comments and direct edits work on imported faces: `check_selection_*`,
  `check_comments_*`, `check_imported_faces_*` (an import has no parameters, so its faces
  can't be dragged to edit it).
- Bad or huge files fail with a clear message: `test_*_fails_with_a_clear_message`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from build123d import Box, Compound, Cylinder, Pos, Shape, export_step, import_step
from fastapi.testclient import TestClient
from test_session_api import Claude, events, send, start, wait_until

from app import imports
from app.contracts.checks import validate_check_report
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from app.contracts.session import Build, BuildSource, Comment, Export, validate_event
from app.main import app
from app.session_api import CONFIG, SESSIONS
from app.standards import load_standards
from geometry import occ
from geometry.params import case_step_path
from geometry.step_import import imported_result, part_names, recognize

BACKEND = Path(__file__).resolve().parents[1]


def solids_in(path: Path) -> list:
    return import_step(path).solids()


# --- Recognizing features ---------------------------------------------------------------------


def plate_with_features():
    """A plate with a round hole, a round boss, a square cut-out and a rib on top."""
    plate = Box(40, 30, 4)
    plate -= Pos(-12, 0, 0) * Cylinder(2, 10)
    plate += Pos(0, 8, 4) * Cylinder(3, 4)
    plate -= Pos(10, -6, 0) * Box(6, 4, 10)
    plate += Pos(10, 8, 3) * Box(8, 2, 2)
    return plate.wrapped


def test_simple_features_are_recognized_and_the_rest_tagged_by_face():
    shape = plate_with_features()
    tags = recognize({"plate": shape})["plate"]
    faces = occ.faces(shape)
    assert len(tags) == len(faces)
    by_tag: dict[str, list[int]] = {}
    for i, tag in enumerate(tags):
        by_tag.setdefault(tag, []).append(i)
    kinds = sorted(t.split("_")[0] for t in by_tag if t != "plate")
    assert kinds == ["boss", "boss", "hole", "opening"]
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane

    def surface(i):
        return BRepAdaptor_Surface(faces[i]).GetType()

    (hole,) = [t for t in by_tag if t.startswith("hole_")]
    assert all(surface(i) == GeomAbs_Cylinder for i in by_tag[hole])
    (opening,) = [t for t in by_tag if t.startswith("opening_")]
    assert len(by_tag[opening]) == 4 and all(surface(i) == GeomAbs_Plane for i in by_tag[opening])
    bosses = [t for t in by_tag if t.startswith("boss_")]
    assert sorted(len(by_tag[b]) for b in bosses) == [1, 4]  # the round boss and the rib
    # Everything else (the plate's own faces, the boss's cap) is known by its face ID alone.
    assert by_tag["plate"]


def test_feature_numbers_run_over_the_whole_import():
    shape = plate_with_features()
    tags = recognize({"a": shape, "b": shape})
    holes = {t for part in tags.values() for t in part if t.startswith("hole_")}
    assert holes == {"hole_1", "hole_2"}


def test_part_names_come_from_labels():
    paths = [
        ["Assembly", "Case", "SOLID"],
        ["Assembly", "Case", ""],
        ["Assembly", "PCB", "R_0402", "SOLID__1_"],
        ["Assembly", "PCB", "Body 2"],
        ["board"],
        [],
        ["Case_1"],
    ]
    assert part_names(paths) == ["Case_2", "Case_3", "R_0402", "PCB", "board_import", "part"] + [
        "Case_1"
    ]


# --- The real file through the session API ----------------------------------------------------


def test_importing_the_existing_case(tmp_path, monkeypatch):
    """The existing case in `demos/b-parasite/`, imported once and checked against every criterion.
    One test, so the import (the slowest step: every body is checked) runs once per test run however
    the tests are split across workers and shards."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        sid, _ = start(client)
        after = events(client, sid)[-1][0]
        path = case_step_path()
        r = client.post(
            f"/api/sessions/{sid}/imports",
            files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        )
        assert r.status_code == 201, r.text
        build = Build.model_validate(r.json())
        imported = (client, sid, build, events(client, sid, after))
        check_import_creates_a_build_shown_on_the_canvas(imported)
        check_import_creates_a_build_with_every_body_as_a_part_and_ids_on_every_face(imported)
        check_import_has_a_source_that_lists_its_features(imported)
        check_checks_run_on_the_imported_design(imported)
        check_the_session_is_checked_against_its_board_and_standards(imported)
        check_comments_pin_to_imported_faces(imported)
        check_find_in_model_works_on_imported_features(imported)
        check_imported_faces_have_no_parameters_to_edit(imported)
        check_selection_of_imported_faces_and_the_import_go_to_claude(imported, monkeypatch)
        check_restoring_an_import_brings_the_file_back(imported)
        check_exporting_an_import_exports_the_files_geometry(imported)
    SESSIONS.by_id.clear()


def mesh_of(client, build: Build):
    r = client.get(build.mesh_url)
    assert r.status_code == 200
    return validate_mesh(r.json())


def check_import_creates_a_build_shown_on_the_canvas(imported):
    client, sid, build, new = imported
    ready = [data for _, kind, data in new if kind == "build_ready"]
    assert [b["build"]["id"] for b in ready] == [build.id]
    assert ready[0]["message_id"] is None
    # The chat says where it came from: the imported file, in the event and the stored build.
    assert ready[0]["build"]["origin"] == {"kind": "import", "file_name": case_step_path().name}
    listed = {b["id"]: b for b in client.get(f"/api/sessions/{sid}/builds").json()}
    assert listed[build.id]["origin"] == ready[0]["build"]["origin"]
    for _, _, data in new:
        validate_event(data)
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200, r.text
    assert r.json().get("latest_build_id") == build.id, r.text
    assert build.status == "ready"


def check_import_creates_a_build_with_every_body_as_a_part_and_ids_on_every_face(imported):
    client, _, build, _ = imported
    mesh = mesh_of(client, build)
    solids = solids_in(case_step_path())
    assert len(mesh.parts) == len(solids)
    for part in mesh.parts.values():
        assert part.faces, part.name
        for face in part.faces:
            assert face.id.startswith(f"{part.name}.{face.feature}.")
    ids = [f.id for p in mesh.parts.values() for f in p.faces]
    assert len(ids) == len(set(ids))
    # The file's face count, part by part (the canvas shows every face of every body).
    assert sorted(len(p.faces) for p in mesh.parts.values()) == sorted(
        len(occ.faces(s.wrapped)) for s in solids
    )
    features = {f.feature for p in mesh.parts.values() for f in p.faces}
    assert any(f.startswith(("hole_", "boss_", "opening_")) for f in features)


def check_import_has_a_source_that_lists_its_features(imported):
    client, _, build, _ = imported
    source = BuildSource.model_validate(client.get(build.source_url).json())
    assert source.build_id == build.id
    mesh = mesh_of(client, build)
    tags = {(f.feature, p.name) for p in mesh.parts.values() for f in p.faces}
    assert {(f.tag, f.part) for f in source.features} == tags
    lines = source.code.splitlines()
    for f in source.features:
        assert f.tag in lines[f.lines[0] - 1]


def check_checks_run_on_the_imported_design(imported):
    client, _, build, _ = imported
    report = client.get(build.checks_url).json()
    # Every standard has a result, located on faces of the imported mesh.
    validate_check_report(report, load_standards(), mesh_of(client, build))
    assert report["build_id"] == build.id
    assert report["board"] == build.board.model_dump()
    assert report["summary"] == build.checks.model_dump()
    assert report["summary"]["pass"] + report["summary"]["fail"] > 0
    faces = {f.id for p in mesh_of(client, build).parts.values() for f in p.faces}
    for result in report["results"]:
        for face in (result.get("location") or {}).get("faces", []):
            assert face in faces, result["id"]
    # The analysis maps line up with the imported mesh.
    for map_type in build.maps:
        doc = client.get(f"{build.mesh_url.rsplit('/', 1)[0]}/maps/{map_type}").json()
        validate_analysis_map(doc, mesh_of(client, build), load_standards())
        assert doc["build_id"] == build.id


def check_the_session_is_checked_against_its_board_and_standards(imported):
    client, sid, build, _ = imported
    session = client.get(f"/api/sessions/{sid}").json()
    # Until the next revision is loaded, the session is on the previous one.
    assert session["next_revision"] is not None
    first = client.get(f"/api/sessions/{sid}/builds").json()[0]
    assert build.board.model_dump() == first["board"]


def check_comments_pin_to_imported_faces(imported):
    client, sid, build, _ = imported
    face = next(f.id for p in mesh_of(client, build).parts.values() for f in p.faces)
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build.id, "face_id": face, "point": [0, 0, 0], "text": "Thicker here"},
    )
    assert r.status_code == 201, r.text
    assert Comment.model_validate(r.json()).face_id == face
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build.id, "face_id": "base.base.0", "point": [0, 0, 0], "text": "x"},
    )
    assert r.status_code == 404


def check_find_in_model_works_on_imported_features(imported):
    client, sid, build, _ = imported
    mesh = mesh_of(client, build)
    tag = next(
        f.feature
        for p in mesh.parts.values()
        for f in p.faces
        if f.feature.startswith(("hole_", "boss_"))
    )
    r = client.post(f"/api/sessions/{sid}/builds/{build.id}/find", json={"query": tag})
    assert r.status_code == 200, r.text
    found = r.json()
    assert found["status"] == "found"
    assert {m["face"] for m in found["matches"]} == {
        f.id for p in mesh.parts.values() for f in p.faces if f.feature == tag
    }


def check_imported_faces_have_no_parameters_to_edit(imported):
    client, sid, _, _ = imported
    assert client.get(f"/api/sessions/{sid}/parameters").json() == []
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"value": 3.0})
    assert r.status_code == 409
    assert "imported" in r.json()["detail"]


def check_selection_of_imported_faces_and_the_import_go_to_claude(imported, monkeypatch):
    client, sid, build, _ = imported
    claude = Claude()
    monkeypatch.setattr(CONFIG, "driver", claude)
    claude.scripts.append([("finish", {"status": "done", "summary": "Looked."})])
    mesh = mesh_of(client, build)
    part = next(iter(mesh.parts.values()))
    face = part.faces[0].id
    after = events(client, sid)[-1][0]
    send(
        client,
        sid,
        text="Make this face flat",
        selection={"part": part.name, "faces": [face], "edges": []},
    )
    wait_until(client, sid, "idle", after)
    prompt = claude.prompts[-1]
    assert face in prompt
    path = case_step_path()
    assert path.name in prompt and build.id in prompt and "read_attachment" in prompt
    for name in mesh.parts:
        assert name in prompt
    # The file is in Claude's workspace: STEP is text, so Claude can read it.
    ws = SESSIONS.get(sid).ws
    assert ws.attachments[path.name].data == path.read_bytes()


def check_restoring_an_import_brings_the_file_back(imported):
    client, sid, build, _ = imported
    r = client.post(f"/api/sessions/{sid}/builds/{build.id}/restore")
    assert r.status_code == 201, r.text
    restored = Build.model_validate(r.json())
    assert restored.restored_from == build.id and restored.id != build.id
    assert client.get(restored.mesh_url).json() == client.get(build.mesh_url).json()
    url = f"/api/sessions/{sid}/builds/{restored.id}/diff"
    diff = client.get(url, params={"against": build.id})
    assert diff.status_code == 200 and diff.json()["features"] == []


def check_exporting_an_import_exports_the_files_geometry(imported):
    client, sid, build, _ = imported
    r = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build.id, "formats": ["step", "glb"]}
    )
    assert r.status_code == 201, r.text
    export = Export.model_validate(r.json())
    step = next(f for f in export.files if f.format == "step")
    data = client.get(step.url).content
    tmp = BACKEND / ".pytest-export.step"
    try:
        tmp.write_bytes(data)
        exported = import_step(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    original = sum(s.volume for s in solids_in(case_step_path()))
    # The file's own bodies, plus the board the STEP export always carries.
    assert sum(s.volume for s in exported.solids()) > original * 0.999


# --- Failures ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def post_import(client, sid: str, name: str, data: bytes):
    return client.post(f"/api/sessions/{sid}/imports", files={"file": (name, data)})


def test_a_huge_file_fails_with_a_clear_message(client, monkeypatch):
    sid, _ = start(client)
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    data = case_step_path().read_bytes()
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", len(data) - 1)
    r = post_import(client, sid, "case.step", data)
    assert r.status_code == 413
    assert "larger than" in r.json()["detail"] and "MB" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/builds").json() == builds


def test_a_file_that_is_not_step_fails_with_a_clear_message(client):
    sid, _ = start(client)
    r = post_import(client, sid, "case.stl", b"solid x\nendsolid x\n")
    assert r.status_code == 422
    assert "not a STEP file" in r.json()["detail"]


def test_a_broken_step_file_fails_with_a_clear_message(client):
    sid, _ = start(client)
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    r = post_import(client, sid, "broken.step", b"ISO-10303-21;\nthis is not a model\n")
    assert r.status_code == 422
    assert r.json()["detail"].startswith("broken.step could not be read as a STEP file")
    assert client.get(f"/api/sessions/{sid}/builds").json() == builds


def test_a_slow_read_fails_with_a_clear_message(client, monkeypatch):
    sid, _ = start(client)
    monkeypatch.setattr(imports, "PARSE_TIMEOUT_S", 0.01)
    r = post_import(client, sid, "case.step", case_step_path().read_bytes())
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Reading case.step took longer than 0.01 s")


def test_a_slow_import_fails_with_a_clear_message(client, monkeypatch):
    sid, _ = start(client)
    monkeypatch.setattr(imports, "IMPORT_TIMEOUT_S", 0.5)
    start_time = time.monotonic()
    r = post_import(client, sid, "case.step", case_step_path().read_bytes())
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Importing case.step took longer than 0.5 s")
    assert time.monotonic() - start_time < 30


def test_no_import_while_claude_is_working(client):
    sid, _ = start(client)
    SESSIONS.get(sid).session.status = "running"
    r = post_import(client, sid, "case.step", case_step_path().read_bytes())
    assert r.status_code == 409


# --- The server never parses the file ---------------------------------------------------------


def test_the_server_process_never_loads_the_cad_kernel_for_an_import(tmp_path):
    """`app.imports` runs the import in the worker, and the worker reads the file in the
    sandbox: the calling process doesn't load OpenCascade. A small file keeps it quick;
    what is in the file doesn't matter here."""
    path = tmp_path / "plate.step"
    export_step(Compound(children=[Shape.cast(plate_with_features())]), path)
    script = f"""
import asyncio, json, sys
from pathlib import Path
from app import imports
from app.boards import BOARDS_DIR
board = json.loads(sorted(BOARDS_DIR.glob("*.json"))[0].read_text())
path = Path({str(path)!r})
done = asyncio.run(imports.import_step(
    path.name, path.read_bytes(), (board["name"], board["revision"]), None, "build-1"
))
print(json.dumps({{
    "parts": len(done.parts),
    "loaded": sorted(m for m in ("OCP", "build123d", "cadquery") if m in sys.modules),
}}))
"""
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True, timeout=600
    )
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["loaded"] == []
    assert result["parts"] == 1


def test_the_worker_reads_the_file_only_in_the_sandbox(monkeypatch):
    """`geometry.step_import.parse` hands the file to the sandboxed build runner."""
    from geometry import sandbox, step_import

    seen = {}
    real = sandbox.run_build

    def spy(code, params=None, **kw):
        seen["inputs"] = kw.get("inputs")
        return real(code, params, **kw)

    monkeypatch.setattr(sandbox, "run_build", spy)
    data = case_step_path().read_bytes()
    parts = step_import.parse(data, 60)
    assert seen["inputs"] == {step_import.STEP_INPUT: data}
    result = imported_result(parts)
    assert len(result.parts) == len(solids_in(case_step_path()))


# --- The mock API -----------------------------------------------------------------------------


def test_the_mock_refuses_what_the_real_api_refuses_and_streams_a_build(monkeypatch):
    from app import mock_api

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    mock_api.SESSIONS.clear()
    with TestClient(mock_api.app) as mock:
        sid = mock.post("/api/sessions", json={}).json()["id"]
        assert post_import(mock, sid, "case.stl", b"solid").status_code == 422
        monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 10)
        assert post_import(mock, sid, "case.step", b"x" * 11).status_code == 413
        monkeypatch.undo()
        monkeypatch.setenv("MOCK_API_SPEED", "0")
        r = post_import(mock, sid, "case.step", b"ISO-10303-21;")
        assert r.status_code == 201, r.text
        build = Build.model_validate(r.json())
        found = events(mock, sid)
        assert [d["build"]["id"] for _, k, d in found if k == "build_ready"] == [build.id]
        assert build.origin is not None and build.origin.file_name == "case.step"
    mock_api.SESSIONS.clear()


def test_build_inputs_are_files_in_the_work_directory():
    """`run_build(inputs=...)` puts plain-named files where the build reads them, and refuses
    names that could leave the work directory or pass for a shape file."""
    from geometry.sandbox import run_build

    code = (
        "from build123d import Box\n"
        "size = float(open(params['file']).read())\n"
        "result = Box(size, size, size)"
    )
    run = run_build(code, {"file": "size.txt"}, inputs={"size.txt": b"3"})
    assert run.ok, run.error
    for name in ("../size.txt", "a/b.txt", "shape_0.brep", ".hidden"):
        with pytest.raises(ValueError):
            run_build(code, {"file": name}, inputs={name: b"3"})
