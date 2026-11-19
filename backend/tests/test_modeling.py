"""Modeling tools on the canvas: every tool's edit changes the design's code, rebuilds and
rechecks, is a locked feature Claude keeps, and undo and redo (the history's restore) cover it.

The faces and edges come from the fixture mesh and from a real build of the enclosure generator;
the insert sizes come from `standards.yaml`. Nothing about a particular board is written here.
"""

from __future__ import annotations

import json
import math
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_sketches import face_ids, mock_events, sketchable

from app.agent.scripted import ScriptedDriver
from app.contracts.checks import validate_check_report
from app.contracts.common import FIXTURES_DIR
from app.contracts.session import (
    EDIT_TARGET,
    Build,
    BuildSource,
    Edit,
    EditCreate,
    EditRemoval,
    EditUpdate,
    Session,
)
from app.examples import examples
from app.exports import export_build_geometry
from app.modeling import (
    EditError,
    edge_point,
    edit_params,
    insert_sizes,
    mesh_parts,
    new_edit,
    removal,
    updated_edit,
    with_edits,
)
from app.standards import load_standards
from geometry import get_backend, occ
from geometry.modeling import MARKER, edits_block, strip_block
from geometry.tessellate import tessellate

# A session on the first example (demos/b-parasite/): it starts with the existing design's first
# build.
EXAMPLE = {"example": examples()[0].id}

STANDARDS = load_standards()


def fixture_mesh() -> str:
    return (FIXTURES_DIR / "enclosure.gltf").read_text()


def mesh_doc(gltf: str | dict) -> dict:
    return json.loads(gltf) if isinstance(gltf, str) else gltf


def edges_of(gltf: str | dict, part: str) -> list[dict]:
    doc = mesh_doc(gltf)
    return [
        e
        for n in doc["nodes"]
        if (n.get("extras") or {}).get("part") == part
        for e in n["extras"].get("edges", [])
    ]


def upward(gltf: str | dict, build_id: str = "b"):
    """The flat faces facing up whose centre is on the face, largest first: the first is a
    top, the inside floor of a hollow part follows."""
    return [p for p in sketchable(gltf, build_id) if p.normal[2] > 0.99]


def bodies(gltf: str | dict, features: dict[str, str], build_id: str = "b") -> dict[str, dict]:
    """One edit of every kind on the mesh: on its largest upward faces, an edge of the bottom of
    the part under them, a part and a feature of the design."""
    top, floor = upward(gltf, build_id)[:2]
    side = min(top.size) / 8
    flat = sketchable(gltf, build_id)
    bottom = next(p for p in flat if p.part == floor.part and p.normal[2] < -0.99)
    edge = next(e for e in edges_of(gltf, floor.part) if bottom.face_id in e["faces"])
    copied = next(t for t, p in features.items() if p == top.part and t != top.part)
    parts = mesh_parts(gltf)
    other = next((p for p in parts if p not in (top.part, floor.part)), floor.part)
    base = {"build_id": build_id}
    return {
        "move": {**base, "kind": "move", "part": other, "dx": 1.0, "rotation": 10.0},
        "push_pull": {**base, "kind": "push_pull", "face_id": top.face_id, "distance": -0.5},
        "hole": {**base, "kind": "hole", "face_id": top.face_id, "diameter": side, "depth": 1},
        "boss": {**base, "kind": "boss", "face_id": floor.face_id, "x": side, "insert": True},
        "rib": {
            **base,
            "kind": "rib",
            "face_id": floor.face_id,
            "length": 2 * side,
            "thickness": 1,
            "height": 2,
            "rotation": 30,
        },
        "fillet": {**base, "kind": "fillet", "edge_id": edge["id"], "radius": 0.5},
        "chamfer": {**base, "kind": "chamfer", "edge_id": edge["id"], "distance": 0.5},
        "shell": {**base, "kind": "shell", "part": other, "thickness": 0.3},
        "mirror": {**base, "kind": "mirror", "source": copied, "axis": "x"},
        "pattern": {
            **base,
            "kind": "pattern",
            "source": copied,
            "pattern": "linear",
            "count": 3,
            "axis": "y",
            "spacing": 3,
        },
    }


# --- The contract -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"kind": "hole", "face_id": "f", "diameter": 2}, "needs depth"),
        ({"kind": "hole", "diameter": 2, "depth": 1}, "needs face_id"),
        ({"kind": "hole", "face_id": "f", "diameter": 2, "depth": 1, "radius": 1}, "no radius"),
        ({"kind": "fillet", "part": "lid", "radius": 1}, "needs edge_id"),
        ({"kind": "push_pull", "face_id": "f", "distance": 0}, "other than 0"),
        ({"kind": "pattern", "source": "s", "pattern": "linear", "count": 3}, "axis, spacing"),
        ({"kind": "pattern", "source": "s", "pattern": "circular", "count": 3}, "angle"),
        ({"kind": "mirror", "source": "s"}, "needs axis"),
    ],
)
def test_each_kind_needs_its_target_and_values(body: dict, error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        EditCreate.model_validate({"build_id": "b", **body})


def test_a_hole_or_boss_for_the_insert_takes_its_sizes_from_the_standard() -> None:
    gltf = fixture_mesh()
    top = upward(gltf)[0]
    diameter, depth = insert_sizes(STANDARDS)
    hole = new_edit(
        EditCreate(build_id="b", kind="hole", face_id=top.face_id, insert=True),
        gltf,
        {},
        set(),
        STANDARDS,
    )
    assert hole is not None and (hole.diameter, hole.depth) == (diameter, depth) and hole.insert
    boss = new_edit(
        EditCreate(build_id="b", kind="boss", face_id=top.face_id, insert=True),
        gltf,
        {},
        set(),
        STANDARDS,
    )
    assert boss is not None and (boss.pocket_diameter, boss.pocket_depth) == (diameter, depth)
    assert boss.diameter > diameter and boss.height > depth
    # the pocket is tagged so the insert check pairs it with the boss
    assert boss.tag == "boss_hand_1" and boss.insert_tag == "insert_hand_1"
    # a hole for the insert can't be resized by hand
    with pytest.raises(EditError, match="insert"):
        updated_edit(hole, EditUpdate(diameter=1))


def test_an_edit_must_sit_on_a_flat_face_and_fit_on_it() -> None:
    gltf = fixture_mesh()
    top = upward(gltf)[0]
    body = {"build_id": "b", "kind": "hole", "face_id": top.face_id, "diameter": 1, "depth": 1}
    with pytest.raises(EditError, match="runs off"):
        new_edit(EditCreate(**body, x=top.size[0]), gltf, {}, set(), STANDARDS)
    from app.sketches import face_plane

    curved = next(f for f in face_ids(gltf) if not face_plane(gltf, "b", f).planar)
    with pytest.raises(EditError, match="not flat"):
        new_edit(EditCreate(**{**body, "face_id": curved}), gltf, {}, set(), STANDARDS)
    assert new_edit(EditCreate(**{**body, "face_id": "nope"}), gltf, {}, set(), STANDARDS) is None


def test_an_edge_is_found_by_a_point_halfway_along_it() -> None:
    gltf = fixture_mesh()
    part = upward(gltf)[0].part
    edge = edges_of(gltf, part)[0]
    found = edge_point(gltf, edge["id"])
    assert found is not None and found[0] == part
    assert edge_point(gltf, "nope") is None


def test_tags_are_never_reused_and_deleting_takes_the_copies_too() -> None:
    edits = [
        {"tag": "hole_hand_1", "kind": "hole"},
        {"tag": "mirror_hand_1", "kind": "mirror", "source": "hole_hand_1"},
        {"tag": "pattern_hand_1", "kind": "pattern", "source": "mirror_hand_1"},
        {"tag": "rib_hand_1", "kind": "rib"},
    ]
    assert removal(edits, "hole_hand_1") == ["hole_hand_1", "mirror_hand_1", "pattern_hand_1"]
    assert removal(edits, "rib_hand_1") == ["rib_hand_1"]
    gltf = fixture_mesh()
    top = upward(gltf)[0]
    body = EditCreate(build_id="b", kind="hole", face_id=top.face_id, diameter=1, depth=1)
    edit = new_edit(body, gltf, {}, {"hole_hand_1", "hole_hand_2"}, STANDARDS)
    assert edit is not None and edit.tag == "hole_hand_3"


# --- The code -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_build():
    """The enclosure generator's real build, its mesh and its features (tag -> part)."""
    build = export_build_geometry()
    gltf = tessellate(build.result).to_gltf({})
    features = {f.tag: f.part for f in build.result.features}
    return build, gltf, features


@pytest.mark.parametrize("kind", list(EDIT_TARGET))
def test_each_tool_changes_the_real_geometry_at_its_line(real_build, kind) -> None:
    build, gltf, features = real_build
    edit = new_edit(
        EditCreate.model_validate(bodies(gltf, features)[kind]), gltf, features, set(), STANDARDS
    )
    assert edit is not None
    params = {**build.params, "edits": [edit_params(edit)]}
    code, lines = edits_block(build.code, params["edits"], "build123d")
    result = get_backend("build123d").build(code, params)

    part = edit.part
    before, after = build.result.parts[part], result.parts[part]
    if kind == "move":
        b0, b1 = occ.bbox(before), occ.bbox(after)
        assert not math.isclose(b0[0][0], b1[0][0], abs_tol=1e-3)
        assert math.isclose(occ.volume(before), occ.volume(after), rel_tol=1e-6)
    else:
        assert not math.isclose(occ.volume(before), occ.volume(after), rel_tol=1e-6)
        # the feature is tagged on the faces it made
        assert any(ref.feature == edit.tag for ref in result.face_refs[part])
    feature = result.feature(edit.tag)
    assert feature is not None and feature.lines == lines[edit.tag]
    assert edit.tag in code.splitlines()[lines[edit.tag][0] - 1]
    if kind == "boss":  # the insert pocket, tagged for the insert check
        assert result.feature(edit.insert_tag) is not None
    # the other parts are untouched
    for name, shape in build.result.parts.items():
        if name != part:
            assert math.isclose(occ.volume(result.parts[name]), occ.volume(shape), rel_tol=1e-9)


def test_a_suppressed_edit_is_a_comment_and_leaves_the_part_as_it_was(real_build) -> None:
    build, gltf, features = real_build
    body = EditCreate.model_validate(bodies(gltf, features)["hole"])
    edit = new_edit(body, gltf, features, set(), STANDARDS)
    off = updated_edit(edit, EditUpdate(suppressed=True))
    params = {**build.params, "edits": [edit_params(off)]}
    code, lines = edits_block(build.code, params["edits"], "build123d")
    line = code.splitlines()[lines[edit.tag][0] - 1]
    assert line.strip().startswith("#") and "suppressed" in line
    result = get_backend("build123d").build(code, params)
    assert result.feature(edit.tag) is None
    assert math.isclose(
        occ.volume(result.parts[edit.part]), occ.volume(build.result.parts[edit.part]), rel_tol=1e-9
    )


def test_the_block_is_rewritten_not_added_twice_and_follows_the_sketches() -> None:
    from app.agent.design import Design
    from geometry.sketch import MARKER as SKETCHES

    code = get_backend("build123d").example("enclosure")
    design = Design(code=code, params={})
    design.add_sketch({"tag": "sketch_1", "shape": "circle", "operation": "cut"})
    design.add_edit({"tag": "hole_hand_1", "kind": "hole", "part": "lid"})
    design.add_edit({"tag": "rib_hand_1", "kind": "rib", "part": "lid"})
    assert design.code.count(MARKER) == 1 and design.code.count(SKETCHES) == 1
    assert design.code.index(SKETCHES) < design.code.index(MARKER)
    assert strip_block(design.code).count(MARKER) == 0
    # a later sketch keeps the edits after it
    design.add_sketch({"tag": "sketch_2", "shape": "vent", "operation": "cut"})
    assert design.code.count(MARKER) == 1 and '"rib_hand_1"' in design.code
    assert design.code.index('"sketch_2"') < design.code.index('"hole_hand_1"')


def test_the_source_lists_edits_locked_at_their_lines() -> None:
    fixture = json.loads((FIXTURES_DIR / "build-source.json").read_text())
    gltf = fixture_mesh()
    top = upward(gltf)[0]
    body = EditCreate(build_id="b", kind="boss", face_id=top.face_id, insert=True)
    edit = new_edit(body, gltf, {}, set(), STANDARDS)
    source = BuildSource.model_validate(with_edits(fixture, [edit_params(edit)]))
    own = [f for f in source.features if not f.locked]
    assert [f.model_dump(mode="json", exclude_defaults=True) for f in own] == fixture["features"]
    locked = {f.tag: f for f in source.features if f.locked}
    assert set(locked) == {edit.tag, edit.insert_tag}
    assert edit.tag in source.code.splitlines()[locked[edit.tag].lines[0] - 1]
    assert with_edits(fixture, []) == fixture


# --- The mock API ---------------------------------------------------------------------------


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    from app.mock_api import app

    with TestClient(app) as client:
        yield client


def mock_session(client) -> tuple[str, Build]:
    sid = Session.model_validate(client.post("/api/sessions", json={}).json()).id
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    for _ in range(500):
        if client.get(f"/api/sessions/{sid}").json()["status"] != "running":
            break
        time.sleep(0.02)
    return sid, Build.model_validate(client.get(f"/api/sessions/{sid}/builds").json()[-1])


def source_features(client, build: Build) -> dict[str, str]:
    source = BuildSource.model_validate(client.get(build.source_url).json())
    return {f.tag: f.part for f in source.features}


def test_every_tool_in_the_mock_locks_rebuilds_and_rechecks(mock) -> None:
    sid, first = mock_session(mock)
    made = bodies(mock.get(first.mesh_url).text, source_features(mock, first), first.id)
    tags = []
    for kind, body in made.items():
        before = len(mock.get(f"/api/sessions/{sid}/builds").json())
        r = mock.post(f"/api/sessions/{sid}/edits", json=body)
        assert r.status_code == 201, (kind, r.text)
        edit = Edit.model_validate(r.json())
        assert edit.kind == kind and edit.locked and edit.tag == f"{kind}_hand_1"
        tags.append(edit.tag)
        builds = [Build.model_validate(b) for b in mock.get(f"/api/sessions/{sid}/builds").json()]
        assert len(builds) == before + 1
        rebuilt = builds[-1]
        ready = [d for t, d in mock_events(mock, sid) if t == "build_ready"]
        assert ready[-1]["build"]["id"] == rebuilt.id and ready[-1].get("message_id") is None
        validate_check_report(mock.get(rebuilt.checks_url).json(), load_standards())
        source = BuildSource.model_validate(mock.get(rebuilt.source_url).json())
        (feature,) = [f for f in source.features if f.tag == edit.tag]
        assert feature.locked and edit.tag in source.code.splitlines()[feature.lines[0] - 1]
    assert [e["tag"] for e in mock.get(f"/api/sessions/{sid}/edits").json()] == tags
    old = BuildSource.model_validate(mock.get(first.source_url).json())
    assert MARKER not in old.code


def test_editing_suppressing_and_deleting_in_the_mock(mock) -> None:
    sid, first = mock_session(mock)
    made = bodies(mock.get(first.mesh_url).text, source_features(mock, first), first.id)
    hole = Edit.model_validate(mock.post(f"/api/sessions/{sid}/edits", json=made["hole"]).json())
    rebuilt = Build.model_validate(mock.get(f"/api/sessions/{sid}/builds").json()[-1])
    mirror = {**made["mirror"], "build_id": rebuilt.id, "source": hole.tag}
    r = mock.post(f"/api/sessions/{sid}/edits", json=mirror)
    assert r.status_code == 201, r.text

    r = mock.patch(f"/api/sessions/{sid}/edits/{hole.tag}", json={"diameter": hole.diameter / 2})
    assert r.status_code == 200 and r.json()["diameter"] == hole.diameter / 2
    r = mock.patch(f"/api/sessions/{sid}/edits/{hole.tag}", json={"radius": 1})
    assert r.status_code == 409
    r = mock.patch(f"/api/sessions/{sid}/edits/{hole.tag}", json={"suppressed": True})
    assert r.status_code == 200 and r.json()["suppressed"]
    latest = Build.model_validate(mock.get(f"/api/sessions/{sid}/builds").json()[-1])
    code = BuildSource.model_validate(mock.get(latest.source_url).json()).code
    assert f"# {hole.tag} is suppressed" in code

    r = mock.delete(f"/api/sessions/{sid}/edits/{hole.tag}")
    assert r.status_code == 200, r.text
    assert EditRemoval.model_validate(r.json()).removed == [hole.tag, "mirror_hand_1"]
    assert mock.get(f"/api/sessions/{sid}/edits").json() == []
    assert mock.delete(f"/api/sessions/{sid}/edits/{hole.tag}").status_code == 404
    # a feature the build doesn't have can't be copied
    r = mock.post(f"/api/sessions/{sid}/edits", json={**mirror, "source": "nope"})
    assert r.status_code == 409


def test_undo_and_redo_cover_every_tool_in_the_mock(mock) -> None:
    """Undo is restoring the build before an edit; redo is restoring the build the undo left."""
    sid, first = mock_session(mock)
    made = bodies(mock.get(first.mesh_url).text, source_features(mock, first), first.id)
    for kind, body in made.items():
        before = Build.model_validate(mock.get(f"/api/sessions/{sid}/builds").json()[-1])
        had = [e["tag"] for e in mock.get(f"/api/sessions/{sid}/edits").json()]
        edit = Edit.model_validate(mock.post(f"/api/sessions/{sid}/edits", json=body).json())
        after = Build.model_validate(mock.get(f"/api/sessions/{sid}/builds").json()[-1])
        # undo
        r = mock.post(f"/api/sessions/{sid}/builds/{before.id}/restore")
        assert r.status_code == 201, (kind, r.text)
        assert [e["tag"] for e in mock.get(f"/api/sessions/{sid}/edits").json()] == had
        undone = Build.model_validate(r.json())
        assert edit.tag not in source_features(mock, undone)
        # redo
        r = mock.post(f"/api/sessions/{sid}/builds/{after.id}/restore")
        assert r.status_code == 201, (kind, r.text)
        assert [e["tag"] for e in mock.get(f"/api/sessions/{sid}/edits").json()] == [*had, edit.tag]
        assert edit.tag in source_features(mock, Build.model_validate(r.json()))


# --- The session API: Claude keeps the edits ---------------------------------------------------


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.main import app
    from app.session_api import SESSIONS

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        yield client


def wait_for_builds(client, sid: str, count: int) -> list[Build]:
    """The session's builds once it has `count`: real builds take a while."""
    for _ in range(3000):
        builds = client.get(f"/api/sessions/{sid}/builds").json()
        if len(builds) >= count:
            return [Build.model_validate(b) for b in builds]
        time.sleep(0.05)
    raise AssertionError(f"session {sid} never had {count} builds")


def test_an_edit_is_a_locked_feature_claude_keeps(api, monkeypatch) -> None:
    from app.session_api import CONFIG, SESSIONS

    sid = Session.model_validate(api.post("/api/sessions", json=EXAMPLE).json()).id
    (first,) = wait_for_builds(api, sid, 1)
    gltf = api.get(first.mesh_url).text
    made = bodies(gltf, source_features(api, first), first.id)

    r = api.post(f"/api/sessions/{sid}/edits", json=made["hole"])
    assert r.status_code == 201, r.text
    edit = Edit.model_validate(r.json())
    path = f"edits.{edit.tag}"

    # in the design's parameters, locked, and in its code
    design = SESSIONS.get(sid).ws.design
    assert design.locked[path] == edit_params(edit)
    assert design.params["edits"] == [edit_params(edit)]
    assert MARKER in design.code and f'"{edit.tag}"' in design.code

    # rebuilt and rechecked: the new build's code lists the feature, locked
    _, rebuilt = wait_for_builds(api, sid, 2)
    assert rebuilt.status == "ready"
    validate_check_report(api.get(rebuilt.checks_url).json(), load_standards())
    source = BuildSource.model_validate(api.get(rebuilt.source_url).json())
    (feature,) = [f for f in source.features if f.tag == edit.tag]
    assert feature.locked and edit.tag in source.code.splitlines()[feature.lines[0] - 1]

    # a hand change to its values is kept, still locked
    r = api.patch(f"/api/sessions/{sid}/edits/{edit.tag}", json={"depth": edit.depth + 0.5})
    assert r.status_code == 200, r.text
    changed = Edit.model_validate(r.json())
    assert design.locked[path] == edit_params(changed)
    wait_for_builds(api, sid, 3)

    # Claude's next turn: it sees the lock, and can't change, drop or code around the edit
    steps = [
        ("read_spec", {}),
        ("set_parameter", {"name": f"{path}.depth", "value": 1}),
        ("write_design", {"params": {**design.params, "edits": []}}),
        ("write_design", {"code": "def build(params):\n    return {}\n"}),
        ("finish", {"outcome": "blocked", "summary": "Kept the hole."}),
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
    spec, change, drop, recode = drivers[0].calls[:4]
    assert json.loads(spec.text)["design"]["locked"][path] == edit_params(changed)
    for call in (change, drop, recode):
        assert call.is_error and "locked" in call.text, call.text
    assert design.params["edits"] == [edit_params(changed)]
    assert [e["tag"] for e in api.get(f"/api/sessions/{sid}/edits").json()] == [edit.tag]


def test_undo_and_redo_of_an_edit_in_the_session_api(api) -> None:
    sid = Session.model_validate(api.post("/api/sessions", json=EXAMPLE).json()).id
    (first,) = wait_for_builds(api, sid, 1)
    made = bodies(api.get(first.mesh_url).text, source_features(api, first), first.id)
    r = api.post(f"/api/sessions/{sid}/edits", json=made["rib"])
    assert r.status_code == 201, r.text
    edit = Edit.model_validate(r.json())
    _, after = wait_for_builds(api, sid, 2)

    r = api.post(f"/api/sessions/{sid}/builds/{first.id}/restore")
    assert r.status_code == 201, r.text
    assert api.get(f"/api/sessions/{sid}/edits").json() == []
    r = api.post(f"/api/sessions/{sid}/builds/{after.id}/restore")
    assert r.status_code == 201, r.text
    assert [e["tag"] for e in api.get(f"/api/sessions/{sid}/edits").json()] == [edit.tag]
    redone = Build.model_validate(r.json())
    assert redone.status == "ready" and edit.tag in source_features(api, redone)
