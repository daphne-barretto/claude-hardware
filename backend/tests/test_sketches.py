"""Sketch on a face: a circle, slot, rectangle or vent on a flat face with typed size and
position, cut or extruded. It becomes a named, locked feature in the code Claude keeps, and the
design rebuilds and rechecks.

The faces come from the fixture mesh and from a real build of the enclosure generator; nothing
about the scenario is written here.
"""

from __future__ import annotations

import json
import math
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.design import CODE
from app.agent.scripted import ScriptedDriver
from app.contracts.checks import validate_check_report
from app.contracts.common import FIXTURES_DIR
from app.contracts.session import (
    Build,
    BuildSource,
    FacePlane,
    Session,
    Sketch,
    SketchCreate,
)
from app.examples import examples
from app.exports import export_build_geometry
from app.parameters import Approval
from app.sketches import (
    SketchError,
    _on_face,
    face_plane,
    face_triangles,
    new_sketch,
    sketch_params,
    with_sketches,
)
from app.standards import load_standards
from geometry import get_backend, occ
from geometry.sketch import MARKER, sketch_block, strip_block
from geometry.tessellate import tessellate

SHAPES = {
    "circle": {},
    "slot": {"height": 0.4},
    "rectangle": {"height": 0.6},
    "vent": {"height": 0.8, "count": 3},
}

# A session on the example case: a new session starts empty, with no build to sketch on.
ON_THE_CASE = {"example": examples()[0].id}


def fixture_mesh() -> str:
    return (FIXTURES_DIR / "enclosure.gltf").read_text()


def face_ids(gltf: str | dict) -> list[str]:
    doc = json.loads(gltf) if isinstance(gltf, str) else gltf
    return [f["id"] for n in doc["nodes"] for f in (n.get("extras") or {}).get("faces", [])]


def sketchable(gltf: str | dict, build_id: str = "b") -> list[FacePlane]:
    """The flat faces whose centre is on the face, largest first."""
    out = []
    for fid in face_ids(gltf):
        plane = face_plane(gltf, build_id, fid)
        assert plane is not None
        if plane.planar and _on_face(plane, face_triangles(gltf, fid)[2], 0, 0):
            out.append(plane)
    return sorted(out, key=lambda p: -p.size[0] * p.size[1])


def body_for(plane: FacePlane, shape: str = "circle", operation: str = "cut", **over) -> dict:
    """A shape a quarter of the face's smaller side, at the face's centre. Heights in `SHAPES`
    are fractions of the width."""
    width = min(plane.size) / 4
    extra = {k: (v * width if k == "height" else v) for k, v in SHAPES[shape].items()}
    return {
        "build_id": plane.build_id,
        "face_id": plane.face_id,
        "shape": shape,
        "operation": operation,
        "width": width,
        "depth": 0.5,
        **extra,
        **over,
    }


# --- Face planes ------------------------------------------------------------------------------


def test_flat_faces_have_a_plane_and_curved_ones_do_not() -> None:
    gltf = fixture_mesh()
    planes = [face_plane(gltf, "b", fid) for fid in face_ids(gltf)]
    assert any(p.planar for p in planes) and any(not p.planar for p in planes)
    for plane in planes:
        _, _, tris = face_triangles(gltf, plane.face_id)
        points = [p for t in tris for p in t]
        if not plane.planar:
            assert plane.origin is plane.normal is plane.x_dir is plane.size is None
            continue
        n, x = plane.normal, plane.x_dir
        assert math.isclose(math.hypot(*n), 1, abs_tol=1e-5)
        assert math.isclose(math.hypot(*x), 1, abs_tol=1e-5)
        assert abs(sum(a * b for a, b in zip(n, x, strict=True))) < 1e-5
        # every point of the face is on the plane through the origin
        d = sum(a * b for a, b in zip(n, plane.origin, strict=True))
        for p in points:
            assert abs(sum(a * b for a, b in zip(n, p, strict=True)) - d) < 1e-3
        # the normal points the way the triangles wind: out of the part
        a, b, c = tris[0]
        ab = [b[i] - a[i] for i in range(3)]
        ac = [c[i] - a[i] for i in range(3)]
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        assert sum(a * b for a, b in zip(cross, n, strict=True)) > 0
        # on a wall the sketch's y axis points up
        if abs(n[2]) < 0.9:
            y = (n[1] * x[2] - n[2] * x[1], n[2] * x[0] - n[0] * x[2], n[0] * x[1] - n[1] * x[0])
            assert y[2] > 0.9


def test_a_shape_must_fit_on_a_flat_face() -> None:
    gltf = fixture_mesh()
    plane = sketchable(gltf)[0]
    ok = SketchCreate.model_validate(body_for(plane))
    sketch = new_sketch(ok, gltf, [])
    assert sketch is not None and sketch.tag == "sketch_1" and sketch.locked
    assert sketch.part == plane.part and sketch.plane == plane
    assert new_sketch(ok, gltf, [sketch_params(sketch)]).tag == "sketch_2"

    off = SketchCreate.model_validate(body_for(plane, x=plane.size[0] / 2))
    with pytest.raises(SketchError, match="runs off"):
        new_sketch(off, gltf, [])

    curved = next(fid for fid in face_ids(gltf) if not face_plane(gltf, "b", fid).planar)
    with pytest.raises(SketchError, match="not flat"):
        new_sketch(SketchCreate.model_validate({**body_for(plane), "face_id": curved}), gltf, [])

    missing = SketchCreate.model_validate({**body_for(plane), "face_id": "no-such-face"})
    assert new_sketch(missing, gltf, []) is None


def test_a_shape_centred_in_a_hole_of_the_face_is_refused() -> None:
    gltf = fixture_mesh()
    ring = next(
        (
            p
            for p in (face_plane(gltf, "b", fid) for fid in face_ids(gltf))
            if p.planar and not _on_face(p, face_triangles(gltf, p.face_id)[2], 0, 0)
        ),
        None,
    )
    if ring is None:
        pytest.skip("the fixture mesh has no flat face with a hole in the middle")
    small = SketchCreate.model_validate({**body_for(ring), "width": 0.1})
    with pytest.raises(SketchError, match="not on face"):
        new_sketch(small, gltf, [])


@pytest.mark.parametrize(
    "body, error",
    [
        ({"shape": "circle", "height": 2}, "diameter"),
        ({"shape": "rectangle"}, "needs a height"),
        ({"shape": "slot", "height": 5}, "less than its length"),
        ({"shape": "vent", "height": 2}, "count"),
        ({"shape": "rectangle", "height": 2, "count": 3}, "no count"),
        ({"shape": "circle", "width": 0}, "greater than 0"),
        ({"shape": "circle", "depth": -1}, "greater than 0"),
        ({"shape": "star"}, "shape"),
        ({"shape": "circle", "operation": "drill"}, "operation"),
    ],
)
def test_sizes_are_checked_for_each_shape(body: dict, error: str) -> None:
    base = {"build_id": "b", "face_id": "f", "operation": "cut", "width": 4, "depth": 1}
    with pytest.raises(ValidationError, match=error):
        SketchCreate.model_validate({**base, **body})


# --- The code -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_build():
    """The enclosure generator's real build, its mesh, and its largest sketchable face."""
    build = export_build_geometry()
    gltf = tessellate(build.result).to_gltf({})
    return build, gltf, sketchable(gltf)[0]


@pytest.mark.parametrize("operation", ["cut", "extrude"])
@pytest.mark.parametrize("shape", list(SHAPES))
def test_each_shape_is_cut_or_extruded_on_real_geometry(real_build, shape, operation) -> None:
    build, gltf, plane = real_build
    sketch = new_sketch(SketchCreate.model_validate(body_for(plane, shape, operation)), gltf, [])
    params = {**build.params, "sketches": [sketch_params(sketch)]}
    code, lines = sketch_block(build.code, params["sketches"], "build123d")
    result = get_backend("build123d").build(code, params)

    before = occ.volume(build.result.parts[plane.part])
    after = occ.volume(result.parts[plane.part])
    assert (after < before - 1e-3) if operation == "cut" else (after > before + 1e-3)
    # the feature is tagged on the faces it made, at the line that applies it
    assert any(ref.feature == sketch.tag for ref in result.face_refs[plane.part])
    feature = result.feature(sketch.tag)
    assert feature is not None and feature.lines == lines[sketch.tag]
    assert sketch.tag in code.splitlines()[lines[sketch.tag][0] - 1]
    # the other parts are untouched
    for part, shape_ in build.result.parts.items():
        if part != plane.part:
            assert math.isclose(occ.volume(result.parts[part]), occ.volume(shape_), rel_tol=1e-9)


@pytest.mark.parametrize("operation", ["cut", "extrude"])
def test_cadquery_applies_the_same_sketch(real_build, operation) -> None:
    build, gltf, plane = real_build
    body = SketchCreate.model_validate(body_for(plane, "vent", operation))
    sketches = [sketch_params(new_sketch(body, gltf, []))]
    params = {**build.params, "sketches": sketches}
    volumes = {}
    for backend in ("build123d", "cadquery"):
        engine = get_backend(backend)
        code, _ = sketch_block(engine.example("enclosure"), sketches, backend)
        result = engine.build(code, params)
        volumes[backend] = occ.volume(result.parts[plane.part])
        assert any(ref.feature == "sketch_1" for ref in result.face_refs[plane.part])
    assert math.isclose(volumes["build123d"], volumes["cadquery"], rel_tol=1e-6)


def test_the_block_is_rewritten_not_added_twice() -> None:
    code = get_backend("build123d").example("enclosure")
    one = [{"tag": "sketch_1", "shape": "circle", "operation": "cut"}]
    two = [*one, {"tag": "sketch_2", "shape": "vent", "operation": "extrude"}]
    first, _ = sketch_block(code, one)
    second, lines = sketch_block(first, two)
    assert second.count(MARKER) == 1
    assert strip_block(second) == strip_block(code) == code.rstrip("\n") + "\n"
    rows = second.splitlines()
    assert '"sketch_1"' in rows[lines["sketch_1"][0] - 1]
    assert '"sketch_2"' in rows[lines["sketch_2"][0] - 1]
    assert sketch_block(second, [])[0] == strip_block(code)


def test_the_source_lists_sketched_features_locked_at_their_lines() -> None:
    fixture = json.loads((FIXTURES_DIR / "build-source.json").read_text())
    gltf = fixture_mesh()
    plane = sketchable(gltf)[0]
    sketch = new_sketch(SketchCreate.model_validate(body_for(plane)), gltf, [])
    source = BuildSource.model_validate(with_sketches(fixture, [sketch_params(sketch)]))
    assert source.code.startswith(fixture["code"].rstrip("\n"))
    *own, added = source.features
    assert [f.model_dump(mode="json", exclude_defaults=True) for f in own] == fixture["features"]
    assert (added.tag, added.part, added.locked) == (sketch.tag, sketch.part, True)
    assert sketch.tag in source.code.splitlines()[added.lines[0] - 1]
    assert with_sketches(fixture, []) == fixture


# --- The mock API ---------------------------------------------------------------------------


def mock_events(client, sid: str) -> list[tuple[str, dict]]:
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"})
    out = []
    for frame in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    from app.mock_api import app

    with TestClient(app) as client:
        yield client


def test_the_mock_sketches_locks_rebuilds_and_rechecks(mock) -> None:
    sid = Session.model_validate(mock.post("/api/sessions", json={}).json()).id
    mock.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    for _ in range(500):
        if mock.get(f"/api/sessions/{sid}").json()["status"] != "running":
            break
        time.sleep(0.02)
    before = [Build.model_validate(b) for b in mock.get(f"/api/sessions/{sid}/builds").json()]
    first = before[-1]  # the latest build: the one the canvas shows

    gltf = fixture_mesh()
    plane = sketchable(gltf, first.id)[0]
    r = mock.get(f"/api/sessions/{sid}/builds/{first.id}/faces/{plane.face_id}")
    assert r.status_code == 200 and FacePlane.model_validate(r.json()) == plane
    assert mock.get(f"/api/sessions/{sid}/builds/{first.id}/faces/nope").status_code == 404

    r = mock.post(f"/api/sessions/{sid}/sketches", json=body_for(plane, "slot"))
    assert r.status_code == 201, r.text
    sketch = Sketch.model_validate(r.json())
    assert sketch.tag == "sketch_1" and sketch.locked and sketch.part == plane.part
    assert mock.get(f"/api/sessions/{sid}/sketches").json() == [r.json()]

    # a rebuild without a message follows, and its checks are served
    builds = [Build.model_validate(b) for b in mock.get(f"/api/sessions/{sid}/builds").json()]
    assert [b.id for b in builds][:-1] == [b.id for b in before]
    rebuilt = builds[-1]
    ready = [d for t, d in mock_events(mock, sid) if t == "build_ready"]
    assert ready[-1]["build"]["id"] == rebuilt.id and ready[-1].get("message_id") is None
    report = mock.get(rebuilt.checks_url).json()
    validate_check_report(report, load_standards())
    assert report["build_id"] == rebuilt.id

    # the rebuild's code applies the sketch and lists it, locked; the earlier build's doesn't
    source = BuildSource.model_validate(mock.get(rebuilt.source_url).json())
    (feature,) = [f for f in source.features if f.tag == sketch.tag]
    assert feature.locked and feature.part == sketch.part
    assert sketch.tag in source.code.splitlines()[feature.lines[0] - 1]
    old = BuildSource.model_validate(mock.get(first.source_url).json())
    assert all(f.tag != sketch.tag for f in old.features) and MARKER not in old.code

    # refused: off the face, a curved face, a face that isn't there
    off = body_for(plane, y=plane.size[1])
    assert mock.post(f"/api/sessions/{sid}/sketches", json=off).status_code == 409
    curved = next(fid for fid in face_ids(gltf) if not face_plane(gltf, "b", fid).planar)
    r = mock.post(f"/api/sessions/{sid}/sketches", json={**body_for(plane), "face_id": curved})
    assert r.status_code == 409 and "not flat" in r.json()["detail"]
    r = mock.post(f"/api/sessions/{sid}/sketches", json={**body_for(plane), "face_id": "nope"})
    assert r.status_code == 404
    r = mock.post(f"/api/sessions/{sid}/sketches", json={**body_for(plane), "build_id": "nope"})
    assert r.status_code == 404
    assert len(mock.get(f"/api/sessions/{sid}/builds").json()) == len(builds)


# --- The session API: Claude keeps the feature ------------------------------------------------


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.main import app
    from app.session_api import SESSIONS

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        yield client


def wait_for_builds(client, sid: str, count: int) -> list[Build]:
    # Up to a minute: the example's first build is a real build, slower on a busy machine.
    for _ in range(3000):
        builds = client.get(f"/api/sessions/{sid}/builds").json()
        if len(builds) >= count:
            return [Build.model_validate(b) for b in builds]
        time.sleep(0.02)
    raise AssertionError(f"session {sid} never had {count} builds")


def test_a_sketch_is_a_locked_feature_claude_keeps(api, monkeypatch) -> None:
    from app.session_api import CONFIG, SESSIONS

    sid = Session.model_validate(api.post("/api/sessions", json=ON_THE_CASE).json()).id
    (first,) = wait_for_builds(api, sid, 1)
    plane = sketchable(api.get(first.mesh_url).text, first.id)[0]

    r = api.get(f"/api/sessions/{sid}/builds/{first.id}/faces/{plane.face_id}")
    assert r.status_code == 200 and FacePlane.model_validate(r.json()) == plane

    r = api.post(f"/api/sessions/{sid}/sketches", json=body_for(plane, "rectangle", "extrude"))
    assert r.status_code == 201, r.text
    sketch = Sketch.model_validate(r.json())
    path = f"sketches.{sketch.tag}"

    # in the design's parameters, locked, and in its code
    design = SESSIONS.get(sid).ws.design
    assert design.locked[path] == sketch_params(sketch)
    assert design.params["sketches"] == [sketch_params(sketch)]
    assert MARKER in design.code and f'"{sketch.tag}"' in design.code

    # rebuilt and rechecked: the new build's code lists the feature, locked
    _, rebuilt = wait_for_builds(api, sid, 2)
    assert rebuilt.status == "ready"
    validate_check_report(api.get(rebuilt.checks_url).json(), load_standards())
    source = BuildSource.model_validate(api.get(rebuilt.source_url).json())
    (feature,) = [f for f in source.features if f.tag == sketch.tag]
    assert feature.locked and sketch.tag in source.code.splitlines()[feature.lines[0] - 1]

    # Claude's next turn: it sees the lock, and can't move, drop or code around the feature
    moved = {**sketch_params(sketch), "x": sketch.x + 0.1}
    steps = [
        ("read_spec", {}),
        ("set_parameter", {"name": f"{path}.x", "value": moved["x"]}),
        ("write_design", {"params": {**design.params, "sketches": []}}),
        ("write_design", {"code": "def build(params):\n    return {}\n"}),
        ("finish", {"outcome": "blocked", "summary": "Kept the sketch."}),
    ]
    drivers = []

    def driver(state, body, prompt):
        drivers.append(ScriptedDriver(state.ws, steps))
        return drivers[-1]

    monkeypatch.setattr(CONFIG, "driver", driver)
    r = api.post(f"/api/sessions/{sid}/messages", json={"text": "Tidy the design."})
    assert r.status_code == 202, r.text
    for _ in range(500):
        if drivers and api.get(f"/api/sessions/{sid}").json()["status"] == "idle":
            break
        time.sleep(0.02)
    spec, move, drop, recode = drivers[0].calls[:4]
    assert json.loads(spec.text)["design"]["locked"][path] == sketch_params(sketch)
    for call in (move, drop, recode):
        assert call.is_error and "locked" in call.text, call.text
    assert design.params["sketches"] == [sketch_params(sketch)]
    assert design.locked[path] == sketch_params(sketch)


def test_new_code_still_applies_the_sketch(api) -> None:
    """Even with the user's approval of new code, the build applies the sketched features."""
    from app.session_api import SESSIONS

    sid = Session.model_validate(api.post("/api/sessions", json=ON_THE_CASE).json()).id
    (first,) = wait_for_builds(api, sid, 1)
    plane = sketchable(api.get(first.mesh_url).text, first.id)[0]
    sketch = Sketch.model_validate(
        api.post(f"/api/sessions/{sid}/sketches", json=body_for(plane)).json()
    )
    design = SESSIONS.get(sid).ws.design
    original = strip_block(design.code)
    design.approve(CODE, Approval(by="user", reason="test"))
    design.apply(design.params, original)
    assert MARKER not in design.code
    snapshot = design.snapshot()
    assert MARKER in snapshot["code"] and f'"{sketch.tag}"' in snapshot["code"]


@pytest.mark.parametrize("which", ["api", "mock"])
def test_undo_takes_a_sketch_out_and_a_new_one_gets_a_new_name(which, request) -> None:
    """Restoring a build from before a sketch (undo in the history) brings back its design
    without the sketch; sketching again never reuses the name."""
    client = request.getfixturevalue(which)
    sid = Session.model_validate(client.post("/api/sessions", json=ON_THE_CASE).json()).id
    if which == "mock":
        client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
        for _ in range(500):
            if client.get(f"/api/sessions/{sid}").json()["status"] != "running":
                break
            time.sleep(0.02)
        first = Build.model_validate(client.get(f"/api/sessions/{sid}/builds").json()[-1])
    else:
        (first,) = wait_for_builds(client, sid, 1)
    plane = sketchable(client.get(first.mesh_url).text, first.id)[0]
    r = client.post(f"/api/sessions/{sid}/sketches", json=body_for(plane))
    assert r.status_code == 201, r.text
    assert [s["tag"] for s in client.get(f"/api/sessions/{sid}/sketches").json()] == ["sketch_1"]

    r = client.post(f"/api/sessions/{sid}/builds/{first.id}/restore")
    assert r.status_code == 201, r.text
    restored = Build.model_validate(r.json())
    assert client.get(f"/api/sessions/{sid}/sketches").json() == []
    source = BuildSource.model_validate(client.get(restored.source_url).json())
    assert not any(f.locked for f in source.features) and MARKER not in source.code

    r = client.post(f"/api/sessions/{sid}/sketches", json=body_for(plane, "rectangle"))
    assert r.status_code == 201, r.text
    assert [s["tag"] for s in client.get(f"/api/sessions/{sid}/sketches").json()] == ["sketch_2"]
