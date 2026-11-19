"""A build no message made says where it came from (`Build.origin`), so the chat can show it
where it happened: a file the user imported, or the existing design an example opens on. It
comes back from the stored session and is in the demos recorded on the example. The scenario
comes from the examples list and `demos/`; nothing here names a board or file."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app import samples
from app.examples import examples
from geometry.params import case_step_path


def test_an_example_session_says_its_first_build_is_the_existing_design(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.main import app
    from app.session_api import CONFIG, SESSIONS, fixture_services

    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    expected = {"kind": "existing", "file_name": case_step_path().name}
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"example": examples()[0].id}).json()["id"]
        for _ in range(200):
            builds = client.get(f"/api/sessions/{sid}/builds").json()
            if builds:
                break
            time.sleep(0.05)
        assert builds[0]["origin"] == expected
        SESSIONS.by_id.clear()  # a restart: the build comes back from the session store
        assert client.get(f"/api/sessions/{sid}/builds").json()[0]["origin"] == expected
    SESSIONS.by_id.clear()


def test_claudes_builds_have_no_origin() -> None:
    for sample in samples.catalogue().values():
        for _, event in sample.events:
            if event.type == "build_ready" and event.message_id is not None:
                assert event.build.origin is None


def test_the_demos_on_the_example_show_where_their_first_build_came_from() -> None:
    """The demos recorded on the example open on the existing design: its build says so."""
    on_board = [s for s in samples.catalogue().values() if s.session.board is not None]
    assert on_board
    for sample in on_board:
        first = next(e for _, e in sample.events if e.type == "build_ready")
        assert first.message_id is None
        assert first.build.origin is not None
        assert first.build.origin.kind == "existing"
        assert first.build.origin.file_name == case_step_path().name
        assert sample.builds[first.build.id].origin == first.build.origin
