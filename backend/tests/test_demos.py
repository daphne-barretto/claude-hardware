"""Demos (`app.demos`): hardcoded saved sessions every browser sees in its list, each opened as
that browser's own copy (`POST /api/demos/{demo}/session`), which works like any session. The
demos are read from `demos/sessions/`; nothing here names one."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.fixtures.samples import SAMPLES_DIR


def demo_ids() -> list[str]:
    return json.loads((SAMPLES_DIR / "index.json").read_text())["samples"]


def original_doc(demo: str) -> dict:
    return json.loads((SAMPLES_DIR / demo / "session.json").read_text())


@pytest.fixture(params=["real", "mock"])
def app(request, tmp_path, monkeypatch):
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
    return app


@pytest.fixture
def real(app):
    from app.main import app as real_app

    if app is not real_app:
        pytest.skip("real API only")
    return app


def open_demo(client: TestClient, demo: str) -> dict:
    r = client.post(f"/api/demos/{demo}/session")
    assert r.status_code == 200, r.text
    return r.json()


def a_face(client: TestClient, base: str, build_id: str) -> str:
    """A face ID of the build's mesh."""
    mesh = client.get(f"{base}/builds/{build_id}/mesh").json()
    return next(
        n["extras"]["faces"][0]["id"] for n in mesh["nodes"] if "faces" in n.get("extras", {})
    )


def listed(client: TestClient) -> list[dict]:
    return client.get("/api/sessions").json()


def test_the_list_shows_every_demo_before_it_is_opened(app) -> None:
    with TestClient(app) as client:
        own = client.post("/api/sessions", json={}).json()
        sessions = listed(client)
        assert sessions[0]["id"] == own["id"] and sessions[0].get("demo") is None
        assert [s["demo"] for s in sessions[1:]] == demo_ids()
        assert all(s["sample"] for s in sessions[1:])


def test_the_same_browser_gets_the_same_copy(app) -> None:
    demo = demo_ids()[0]
    with TestClient(app) as client:
        first = open_demo(client, demo)
        assert first["demo"] == demo and first["id"] != demo and not first.get("sample")
        assert first["title"] == original_doc(demo)["session"]["title"]
        assert open_demo(client, demo)["id"] == first["id"]
        # The list shows the copy in the demo's place, once, and nowhere else.
        sessions = listed(client)
        assert [s["demo"] for s in sessions] == demo_ids()
        assert [s["id"] for s in sessions].count(first["id"]) == 1
        assert sessions[0]["id"] == first["id"] and not sessions[0].get("sample")
        assert all(s["sample"] for s in sessions[1:])


def test_each_browser_gets_its_own_copy(app) -> None:
    demo = demo_ids()[0]
    with TestClient(app) as mine, TestClient(app) as theirs:
        a = open_demo(mine, demo)
        assert [s["id"] for s in listed(theirs) if s["demo"] == demo] == [demo]  # the original
        b = open_demo(theirs, demo)
        assert a["id"] != b["id"]
        assert [s["id"] for s in listed(mine) if s["demo"] == demo] == [a["id"]]
        assert [s["id"] for s in listed(theirs) if s["demo"] == demo] == [b["id"]]


def test_unknown_demos_are_refused_and_make_nothing(app) -> None:
    with TestClient(app) as client:
        assert client.post("/api/demos/sample-nope/session").status_code == 404
        own = client.post("/api/sessions", json={}).json()
        assert client.post(f"/api/demos/{own['id']}/session").status_code == 404
        assert client.delete("/api/demos/sample-nope/session").status_code == 404
        assert [s["id"] for s in listed(client) if not s.get("sample")] == [own["id"]]


def test_editing_the_copy_leaves_the_original_and_others_alone(real) -> None:
    """The mock pins comments to its own fixture mesh, so this runs on the real API."""
    demo = demo_ids()[0]
    doc = original_doc(demo)
    with TestClient(real) as mine, TestClient(real) as theirs:
        copy = open_demo(mine, demo)
        base = f"/api/sessions/{copy['id']}"
        renamed = mine.patch(base, json={"title": "My bracket"})
        assert renamed.status_code == 200, renamed.text
        build = copy["latest_build_id"]
        comment = mine.post(
            base + "/comments",
            json={
                "text": "Check this",
                "build_id": build,
                "face_id": a_face(mine, base, build),
                "point": [0, 0, 0],
            },
        )
        assert comment.status_code in (200, 201, 202), comment.text
        # Renamed in the demo's place, still a demo.
        mine_listed = [s for s in listed(mine) if s["demo"] == demo]
        assert [(s["id"], s["title"]) for s in mine_listed] == [(copy["id"], "My bracket")]

        original = mine.get(f"/api/sessions/{demo}").json()
        assert original["title"] == doc["session"]["title"] and original["sample"] is True
        assert mine.get(f"/api/sessions/{demo}/comments").json() == []
        assert len(mine.get(f"/api/sessions/{demo}/messages").json()) == len(doc["messages"])
        # Another browser's copy starts from the original, not from the edited copy.
        other = open_demo(theirs, demo)
        assert other["title"] == doc["session"]["title"]
        assert theirs.get(f"/api/sessions/{other['id']}/comments").json() == []


def test_two_tabs_opening_a_demo_at_once_make_one_copy(app) -> None:
    demo = demo_ids()[-1]
    with TestClient(app) as client:
        client.get("/api/sessions")  # the browser's cookie, before the tabs race
        with ThreadPoolExecutor(4) as pool:
            ids = {s["id"] for s in pool.map(lambda _: open_demo(client, demo), range(4))}
        assert len(ids) == 1
        assert [s["id"] for s in listed(client) if not s.get("sample")] == list(ids)


def test_reset_gives_a_fresh_copy(app) -> None:
    demo = demo_ids()[0]
    with TestClient(app) as client:
        first = open_demo(client, demo)
        client.patch(f"/api/sessions/{first['id']}", json={"title": "Changed"})
        assert client.delete(f"/api/demos/{demo}/session").status_code == 204
        assert [s["id"] for s in listed(client) if s["demo"] == demo] == [demo]
        fresh = open_demo(client, demo)
        assert fresh["id"] != first["id"]
        assert fresh["title"] == original_doc(demo)["session"]["title"]
        # Resetting again with no copy is fine.
        assert client.delete(f"/api/demos/{demo}/session").status_code == 204
        assert client.delete(f"/api/demos/{demo}/session").status_code == 204


def test_opening_a_demo_counts_against_the_session_limit(real, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONS_PER_DAY", "1")
    with TestClient(real) as client:
        first = open_demo(client, demo_ids()[0])
        assert open_demo(client, demo_ids()[0])["id"] == first["id"]  # no new copy: not counted
        assert client.post(f"/api/demos/{demo_ids()[1]}/session").status_code == 429
        assert client.post("/api/sessions", json={}).status_code == 429


def test_the_copy_is_a_saved_session_with_its_timeline(real) -> None:
    """After a restart the copy loads like any saved session, still the browser's demo, and
    has the steps Claude's replies made, as a session that ran the conversation would."""
    from app.session_api import SESSIONS

    with TestClient(real) as client:
        for demo in demo_ids():
            copy = open_demo(client, demo)
            history = client.get(f"/api/sessions/{copy['id']}/history").json()
            builds = [
                e["event"]
                for e in original_doc(demo)["events"]
                if e["event"]["type"] == "build_ready"
            ]
            if all(b["message_id"] for b in builds):  # Claude made the first design
                assert history["steps"], demo
            for step in history["steps"]:
                assert step["actor"] == "claude" and step["kind"] == "run"
                assert step["build_id"] in {
                    b["id"] for b in client.get(f"/api/sessions/{copy['id']}/builds").json()
                }
            assert history["position"] == len(history["steps"])

            SESSIONS.by_id.clear()  # a restart
            assert open_demo(client, demo)["id"] == copy["id"]
            reloaded = client.get(f"/api/sessions/{copy['id']}/history").json()
            assert reloaded == history
