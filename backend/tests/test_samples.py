"""Sample sessions: `demos/sessions/`, recorded runs (`app.fixtures.from_session`) or
made from `demos/b-parasite/` and `standards.yaml`, served read-only by the real session API and the
mock at the session API's paths."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import samples
from app.boards import BOARDS_DIR, find_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import validate_check_report
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from app.contracts.session import Build, BuildSource, Message, Parameter, RecordedEvent, Session
from app.examples import example_standards
from app.fixtures.samples import REQUESTS_DIR, SAMPLES_DIR, stale
from app.standards import load_standards

BACKEND = Path(__file__).resolve().parents[1]
CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
CHANGED = {row["Component"].strip("`") for row in CHANGES.rows}


@cache
def index() -> dict:
    return json.loads((SAMPLES_DIR / "index.json").read_text())


def sample_ids() -> list[str]:
    return index()["samples"]


def build_dirs():
    for sid in sample_ids():
        for path in sorted((SAMPLES_DIR / sid / "builds").iterdir()):
            yield sid, path


def board_build_dirs():
    """The builds checked against a board revision."""
    for sid, path in build_dirs():
        if read(path / "build.json")["board"] is not None:
            yield sid, path


def read(path) -> dict:
    return json.loads(path.read_text())


# --- The catalogue ------------------------------------------------------------------------------


def test_samples_are_current() -> None:
    problems = stale()
    assert not problems, f"run: cd backend && .venv/bin/python -m app.fixtures.samples {problems}"


def test_there_is_a_sample_for_each_step_of_the_board_revision() -> None:
    """The existing case on its own board, on the new board, and updated for the new board."""
    old = find_board(*_name_rev(CHANGES.old_file))
    new = find_board(*_name_rev(CHANGES.new_file))
    seen = set()
    for _, path in board_build_dirs():
        design = read(path / "design.json")["params"]["source"]
        board = read(path / "build.json")["board"]
        seen.add((design["revision"], board["revision"]))
    assert seen == {
        (old.revision, old.revision),
        (old.revision, new.revision),
        (new.revision, new.revision),
    }


def _name_rev(file: str) -> tuple[str, str]:
    doc = json.loads((BOARDS_DIR / file).read_text())
    return doc["name"], doc["revision"]


def test_check_results_match_the_board_changes() -> None:
    """A design passes every check on the board it was designed for; the old design on the new
    board fails at exactly the components `demos/b-parasite/CHANGES.md` lists, and at every one."""
    for sid, path in board_build_dirs():
        report = read(path / "checks.json")
        design = read(path / "design.json")["params"]["source"]
        failing = {r["component"] for r in report["results"] if r["status"] == "fail"}
        where = f"{sid}/{path.name}"
        if design["revision"] == report["board"]["revision"]:
            assert failing == set(), f"{where} fails on its own board: {failing}"
        else:
            assert failing == CHANGED, f"{where} fails at {failing}, CHANGES.md lists {CHANGED}"
        assert report["summary"]["untested"] > 0  # strength, drop and heat are never claimed


def spec_standards(doc: dict):
    """The standards a sample's checks ran against: the built-in ones with the values its
    messages gave (`app.spec`), as in the session that was recorded."""
    from app.spec import Spec

    on_board = doc["session"]["board"] is not None
    # A demo on the board example has the constraints the example brings too. The demos without
    # a board were recorded while the example's height limit was still a default rule (reported
    # not applicable there), so they are read with it as well until they are recorded again.
    spec = Spec(example_standards())
    spec.has_board = lambda: on_board
    for m in doc["messages"]:
        if m["role"] == "user":
            spec.describe(m["text"], m["id"])
    return spec.effective()


def test_samples_follow_the_contracts() -> None:
    for sid in sample_ids():
        doc = read(SAMPLES_DIR / sid / "session.json")
        session = Session.model_validate(doc["session"])
        assert session.id == sid and session.sample
        messages = [Message.model_validate(m) for m in doc["messages"]]
        # Turns: each user message has Claude's reply.
        assert [m.role for m in messages] == ["user", "assistant"] * (len(messages) // 2)
        recorded = [RecordedEvent.model_validate({**e, "delay_ms": 0}) for e in doc["events"]]
        assert [r.id for r in recorded] == list(range(1, len(recorded) + 1))
        events = [r.event for r in recorded]
        built = [e.build.id for e in events if e.type == "build_ready"]
        assert built[-1] == session.latest_build_id
        for reply in messages[1::2]:
            texts = [e.delta for e in events if e.type == "text" and e.message_id == reply.id]
            assert "".join(texts) == reply.text, (sid, reply.id)
        for p in doc["parameters"]:
            Parameter.model_validate(p)
    for sid, path in build_dirs():
        standards = spec_standards(read(SAMPLES_DIR / sid / "session.json"))
        build = Build.model_validate(read(path / "build.json"))
        assert build.id == path.name and build.status == "ready"
        mesh = validate_mesh(path / "mesh.gltf")
        report = validate_check_report(read(path / "checks.json"), standards, mesh)
        assert report.build_id == build.id
        assert report.summary.model_dump() == build.checks.model_dump()
        for map_type in build.maps:
            amap = validate_analysis_map(read(path / "maps" / f"{map_type}.json"), mesh, standards)
            assert amap.build_id == build.id
        source = BuildSource.model_validate(read(path / "source.json"))
        assert {f.tag for f in source.features} <= set(mesh.features), sid


def test_titles_and_text_name_only_the_boards() -> None:
    """Every board a sample names is a board in `demos/b-parasite/`."""
    names = {json.loads(p.read_text())["name"] for p in BOARDS_DIR.glob("*.json")}
    for sid in sample_ids():
        session = read(SAMPLES_DIR / sid / "session.json")["session"]
        if session["board"] is None:
            continue
        assert any(n in session["title"] for n in names), session["title"]
        find_board(session["board"]["name"], session["board"]["revision"])


def test_samples_from_a_plain_request() -> None:
    """At least three samples are designs made from a plain request with no board, one per
    folder of `demos/requests/`, checked against the standards for any design."""
    requests = sorted(p for p in REQUESTS_DIR.iterdir() if (p / "request.json").is_file())
    assert len(requests) >= 3
    standards = load_standards()
    board_only = {s.check for s in standards.standards if s.applies_to == "board"}
    general = []
    for sid in sample_ids():
        doc = read(SAMPLES_DIR / sid / "session.json")
        if doc["session"]["board"] is not None:
            continue
        for path in sorted((SAMPLES_DIR / sid / "builds").iterdir()):
            report = read(path / "checks.json")
            assert report["board"] is None
            na = {r["check"] for r in report["results"] if r["status"] == "not_applicable"}
            assert board_only <= na, (sid, board_only - na)
        # Recorded from the request in its folder (`demos/README.md`): the same words.
        first = next(m for m in doc["messages"] if m["role"] == "user")
        general.append(first["text"])
    assert sorted(general) == sorted(
        json.loads((p / "request.json").read_text())["prompt"] for p in requests
    )


# --- Served by both APIs ---------------------------------------------------------------------


@pytest.fixture(params=["real", "mock"])
def client(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    if request.param == "real":
        from app.main import app
        from app.session_api import CONFIG, SESSIONS, fixture_services

        monkeypatch.setattr(CONFIG, "services", fixture_services)
        SESSIONS.by_id.clear()
    else:
        from app.mock_api import SESSIONS, app

        SESSIONS.clear()
    with TestClient(app) as c:
        yield c


def test_sessions_list_the_samples_after_real_sessions(client) -> None:
    created = client.post("/api/sessions", json={}).json()
    listed = client.get("/api/sessions").json()
    assert listed[0]["id"] == created["id"] and not listed[0].get("sample")
    assert [s["id"] for s in listed[1:]] == sample_ids()
    assert all(s["sample"] is True for s in listed[1:])
    # The real session's own routes are untouched.
    assert client.get(f"/api/sessions/{created['id']}").json()["id"] == created["id"]


def test_every_read_of_a_sample(client) -> None:
    for sid in sample_ids():
        base = f"/api/sessions/{sid}"
        session = client.get(base).json()
        assert session["sample"] is True
        messages = client.get(base + "/messages").json()
        assert messages == read(SAMPLES_DIR / sid / "session.json")["messages"]
        stream = client.get(base + "/events", params={"follow": "false"})
        assert stream.headers["content-type"].startswith("text/event-stream")
        frames = [f for f in stream.text.split("\n\n") if f.strip()]
        assert len(frames) == len(read(SAMPLES_DIR / sid / "session.json")["events"])
        after = client.get(base + "/events", params={"follow": "false", "after": 1}).text
        assert [f for f in after.split("\n\n") if f.strip()] == frames[1:]
        builds = client.get(base + "/builds").json()
        assert builds[-1]["id"] == session["latest_build_id"]
        for b in builds:
            one = f"{base}/builds/{b['id']}"
            assert client.get(one).json() == b
            mesh = client.get(one + "/mesh")
            assert mesh.headers["content-type"].startswith("model/gltf+json")
            validate_mesh(mesh.content)
            assert client.get(one + "/checks").json()["summary"] == b["checks"]
            assert client.get(one + "/source").json()["build_id"] == b["id"]
            board = client.get(one + "/board")
            if b["board"] is None:
                assert board.status_code == 404
            else:
                assert board.json()["revision"] == b["board"]["revision"]
            for map_type in b["maps"]:
                assert client.get(f"{one}/maps/{map_type}").json()["type"] == map_type
        if len(builds) > 1:
            first, last = builds[0]["id"], builds[-1]["id"]
            diff = client.get(f"{base}/builds/{last}/diff", params={"against": first}).json()
            assert diff["old"] == first and diff["new"] == last
        assert client.get(base + "/parameters").status_code == 200
        assert client.get(base + "/comments").json() == []
        assert client.get(base + "/changes").status_code == 404
        found = client.post(
            f"{base}/builds/{builds[-1]['id']}/find", json={"query": "walls thinner than 3 mm"}
        )
        assert found.status_code == 200, found.text


def test_a_sample_is_read_only(client) -> None:
    sid = sample_ids()[0]
    base = f"/api/sessions/{sid}"
    build = client.get(base + "/builds").json()[-1]["id"]
    attempts = [
        ("post", "/messages", {"text": "Make it bigger", "mode": "auto"}),
        ("post", "/revisions/next", {"mode": "auto"}),
        ("patch", "/parameters/wall", {"value": 3}),
        ("post", "/comments", {"text": "x", "build_id": build, "face": "x"}),
        ("post", f"/builds/{build}/restore", None),
        ("post", "/changes", {"accept": [], "reject": []}),
        # The modeling tools: an edit is refused as read-only, not as a missing session.
        ("post", "/edits", {"kind": "hole", "build_id": build}),
        ("patch", "/edits/hole_1", {"params": {}}),
        ("delete", "/edits/hole_1", None),
    ]
    for method, path, body in attempts:
        if method == "delete":
            response = client.request("DELETE", base + path)
        else:
            response = getattr(client, method)(base + path, json=body)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["detail"] == samples.READ_ONLY
    # Telemetry is accepted and dropped: views on a sample are not an error.
    assert client.post(base + "/telemetry", json={"kind": "view", "view": "map"}).status_code == 204
    assert client.get("/api/sessions/sample-that-does-not-exist").status_code == 404


def test_a_sample_lists_no_modeling_edits(client) -> None:
    for sid in sample_ids():
        response = client.get(f"/api/sessions/{sid}/edits")
        assert response.status_code == 200, response.text
        assert response.json() == []


URL_KEYS = ("mesh_url", "checks_url", "source_url", "board_url", "step_url")


def test_sample_builds_have_absolute_urls_like_real_sessions(client) -> None:
    """Every `*_url` of a sample build is the absolute path a real session serves, wherever the
    build is read: the build list, one build, and the recorded `build_ready` events."""
    for sid in sample_ids():
        base = f"/api/sessions/{sid}"
        builds = client.get(base + "/builds").json()
        stream = client.get(base + "/events", params={"follow": "false"}).text
        streamed = [
            json.loads(line[len("data: ") :])["build"]
            for frame in stream.split("\n\n")
            if "event: build_ready" in frame
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        assert streamed, sid
        for b in [
            *builds,
            *(client.get(f"{base}/builds/{x['id']}").json() for x in builds),
            *streamed,
        ]:
            prefix = f"{base}/builds/{b['id']}/"
            for key in URL_KEYS:
                url = b[key]
                if url is None:
                    continue
                assert url.startswith(prefix), (sid, key, url)
                if key != "board_url" or b["board"] is not None:
                    assert client.get(url).status_code == 200, (sid, key, url)


def test_exports_of_a_sample(client) -> None:
    sid = sample_ids()[0]
    base = f"/api/sessions/{sid}"
    build = client.get(base + "/builds").json()[-1]["id"]
    export = client.post(base + "/exports", json={"build_id": build, "formats": ["stl"]})
    assert export.status_code == 201, export.text
    (file,) = export.json()["files"]
    assert client.get(file["url"]).content


def test_reading_samples_never_loads_the_cad_kernel() -> None:
    """The server reads samples, diffs and finds in them without the CAD kernel."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """\
        import json, sys
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as client:
            for s in client.get("/api/sessions").json():
                if not s.get("sample"):
                    continue
                base = f"/api/sessions/{s['id']}"
                builds = client.get(base + "/builds").json()
                for b in builds:
                    for part in ("", "/mesh", "/checks", "/source", "/maps/draft"):
                        assert client.get(f"{base}/builds/{b['id']}{part}").status_code == 200
                    r = client.post(f"{base}/builds/{b['id']}/find", json={"query": "thin walls"})
                    assert r.status_code == 200, r.text
                if len(builds) > 1:
                    r = client.get(
                        f"{base}/builds/{builds[-1]['id']}/diff",
                        params={"against": builds[0]["id"]},
                    )
                    assert r.status_code == 200, r.text
        print(json.dumps(sorted(m for m in ("OCP", "build123d", "cadquery") if m in sys.modules)))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=BACKEND
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
