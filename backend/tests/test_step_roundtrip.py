"""A design exported as STEP and imported again is the same build.

Regression: a design with no failing checks, exported as STEP and imported into a session, came
back with hundreds of failing checks. The STEP file held only named bodies, so the import
guessed: the board's bodies (which the export includes) became enclosure parts that overlapped
the board and each other, every part became a molded enclosure part with no material (heat-set
inserts too), and features were recognized afresh under other tags (`opening_7` for
`opening_Q4`), so the board-derived checks found no button or sensor openings and no insert
pockets. The export now records the design in the file (`geometry.step_manifest`), and the
import reads it back: the same parts, materials, kinds, placements, features, face IDs and check
results.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import replace
from functools import cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import CheckReport
from app.contracts.session import Build
from app.exports import mesh_face_ids
from app.main import app
from app.session_api import SESSIONS
from app.standards import load_standards
from geometry import DEFAULT_BACKEND, BuildResult, enclosure_parameters, get_backend, step_manifest
from geometry.export import export_build
from geometry.params import case_step_path
from geometry.report import check_report
from geometry.step_import import import_step, read_design
from geometry.tessellate import tessellate

STANDARDS = load_standards()


def without_build_id(report: dict) -> dict:
    """A check report as the API serves it, without its build ID."""
    served = CheckReport.model_validate(report).model_dump(mode="json", by_alias=True)
    return {k: v for k, v in served.items() if k != "build_id"}


def mesh_parts(gltf: dict) -> dict[str, dict]:
    """Each part's mesh extras: kind, material, placement and every face's ID and feature."""
    return {
        n["extras"]["part"]: n["extras"]
        for n in gltf["nodes"]
        if "mesh" in n and (n.get("extras") or {}).get("part")
    }


def as_from_the_sandbox(result: BuildResult) -> BuildResult:
    """The build as the app has it: every shape written to BREP and read back, as the build
    sandbox hands a build's shapes to the server (`geometry.sandbox`). Built in process here
    because the sandbox's CPU limit is too tight for this enclosure on a slow test host."""
    from OCP.BRepTools import BRepTools

    from geometry.sandbox import _read_brep

    with tempfile.TemporaryDirectory(prefix="breps-") as tmp:

        def again(shape):
            path = str(Path(tmp) / "shape.brep")
            assert BRepTools.Write_s(shape, path)
            return _read_brep(path)

        return replace(
            result,
            parts={n: again(s) for n, s in result.parts.items()},
            features=[replace(f, tools=tuple(again(t) for t in f.tools)) for f in result.features],
        )


def step_file(out: Path) -> bytes:
    """The one STEP file an export wrote to `out`, whatever it is named."""
    (path,) = out.glob("*.step")
    return path.read_bytes()


@cache
def board_design() -> tuple:
    """The board example's enclosure on the board's new revision, its check report, mesh and
    its STEP export (the board's bodies included, as the Export panel writes it)."""
    changes = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    board = load_board(BOARDS_DIR / changes.new_file)
    engine = get_backend(DEFAULT_BACKEND)
    code = engine.example("enclosure")
    params = enclosure_parameters(board, STANDARDS)
    result = as_from_the_sandbox(engine.build(code, params))
    mesh = tessellate(result, source={"build_id": "build-1"})
    report = check_report(board, STANDARDS, result, "build-1", mesh)
    out = Path(tempfile.mkdtemp(prefix="roundtrip-"))
    export_build(board, STANDARDS, result, code, params, out, report, mesh, formats=["step"])
    return board, result, mesh, report, step_file(out), code, params


def test_a_board_design_checks_the_same_after_a_step_round_trip() -> None:
    board, result, mesh, report, data, *_ = board_design()
    assert report["summary"]["fail"] == 0  # the design passes, as the user's did
    back = import_step(data, "design.step", board, STANDARDS, "build-1", 60)
    # The board's bodies are the board, not parts of the design.
    assert [p["name"] for p in back["parts"]] == list(result.parts)
    assert without_build_id(back["checks"]) == without_build_id(report)
    # Every part's kind, material and placement, and every face's ID and feature tag.
    assert mesh_parts(back["mesh"]) == mesh_parts(mesh.to_gltf())
    assert {f["tag"] for f in back["source"]["features"]} == set(result.tags)


def test_a_board_design_imported_into_a_session_without_a_board_is_checked_against_it(
    tmp_path, monkeypatch
) -> None:
    board, _, _, report, data, *_ = board_design()
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        r = client.post(f"/api/sessions/{sid}/imports", files={"file": ("case.step", data)})
        assert r.status_code == 201, r.text
        build = Build.model_validate(r.json())
        assert build.board is not None
        assert (build.board.name, build.board.revision) == (board.name, board.revision)
        checks = client.get(build.checks_url).json()
        assert without_build_id(checks) == without_build_id(report)
    SESSIONS.by_id.clear()


def test_the_step_file_keeps_the_face_ids_the_session_gave_the_build() -> None:
    """A session's build inherits face IDs from the builds before it, which comments and check
    locations use: the export records those, not a fresh numbering."""
    board, result, mesh, report, _, code, params = board_design()
    ids = {
        part: [f"{fid.rsplit('.', 1)[0]}.{int(fid.rsplit('.', 1)[1]) + 100}" for fid in faces]
        for part, faces in mesh_face_ids(mesh.to_gltf()).items()
    }
    out = Path(tempfile.mkdtemp(prefix="roundtrip-"))
    export_build(
        board, STANDARDS, result, code, params, out, report, formats=["step"], face_ids=ids
    )
    data = step_file(out)
    back = import_step(data, "design.step", board, STANDARDS, "build-2", 60)
    assert mesh_face_ids(back["mesh"]) == ids


def test_a_standalone_demo_round_trips_through_the_export_and_import_api(
    tmp_path, monkeypatch
) -> None:
    """The Export panel's STEP and the canvas's Import STEP, on a build of a copy of a demo
    without a board."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        copy = client.post("/api/demos/sample-phone-stand/session").json()
        base = f"/api/sessions/{copy['id']}"
        # A hand edit rebuilds the design in the session (the demo's recorded checks were made
        # by an older version of the checks); the new build inherits its face IDs.
        count = len(client.get(base + "/builds").json())
        edit = client.patch(base + "/parameters/width", json={"value": 82})
        assert edit.status_code == 200, edit.text
        deadline = time.monotonic() + 120
        while True:
            builds = client.get(base + "/builds").json()
            if len(builds) > count and builds[-1]["status"] == "ready":
                break
            assert time.monotonic() < deadline, builds[-1]
            time.sleep(0.05)
        original = builds[-1]
        before = client.get(original["checks_url"]).json()
        export = client.post(
            base + "/exports", json={"build_id": original["id"], "formats": ["step"]}
        )
        assert export.status_code == 201, export.text
        (file,) = export.json()["files"]
        data = client.get(file["url"]).content
        r = client.post(base + "/imports", files={"file": (file["name"], data)})
        assert r.status_code == 201, r.text
        build = Build.model_validate(r.json())
        after = client.get(build.checks_url).json()
        assert without_build_id(after) == without_build_id(before)
        assert build.board == (Build.model_validate(original).board)
        mesh_before = json.loads(client.get(original["mesh_url"]).content)
        mesh_after = json.loads(client.get(build.mesh_url).content)
        assert mesh_parts(mesh_after) == mesh_parts(mesh_before)
    SESSIONS.by_id.clear()


def test_a_record_that_does_not_match_the_files_bodies_is_ignored() -> None:
    """A file changed after export (here: another file's bodies under the record) is imported
    for what it holds, its features recognized, rather than as the recorded design."""
    *_, data, _, _ = board_design()
    found = step_manifest._COMMENT.search(data)
    assert found is not None
    other = case_step_path().read_bytes()
    first, _, rest = other.partition(b"\n")
    spliced = first + b"\n" + found.group(0) + b"\n" + rest
    assert step_manifest.read(spliced) is not None
    result, record = read_design(spliced, 60)
    assert record is None and result.parts


@pytest.mark.parametrize(
    "comment",
    [
        b"/* claude-hardware design 1\nnot base64!\n*/",
        b"/* claude-hardware design 1\n" + __import__("base64").b64encode(b"junk") + b"\n*/",
        b"/* claude-hardware design 99\nAAAA\n*/",
    ],
)
def test_a_malformed_record_is_no_record(comment: bytes) -> None:
    assert step_manifest.read(b"ISO-10303-21;\n" + comment + b"\nHEADER;\n") is None


def test_a_record_with_a_field_out_of_bounds_is_no_record() -> None:
    import base64
    import zlib

    part = {"name": "lid", "material": "ABS", "kind": "board", "placement": None}
    part |= {"faces": ["lid"], "ids": ["lid.lid.0"], "bbox": [[0, 0, 0], [1, 1, 1]]}
    payload = json.dumps({"parts": [part], "features": []}).encode() + b"\0"
    blob = base64.b64encode(zlib.compress(payload))
    data = b"ISO-10303-21;\n/* claude-hardware design 1\n" + blob + b"\n*/\nHEADER;\n"
    assert step_manifest.read(data) is None
    part["kind"] = "enclosure"
    payload = json.dumps({"parts": [part], "features": []}).encode() + b"\0"
    blob = base64.b64encode(zlib.compress(payload))
    data = b"ISO-10303-21;\n/* claude-hardware design 1\n" + blob + b"\n*/\nHEADER;\n"
    assert step_manifest.read(data) is not None
