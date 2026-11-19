"""A new session starts empty: no board and no build until the user asks for one. The
board revision in `demos/b-parasite/` is one example, opened only when asked for."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session_api import SESSIONS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_a_new_session_without_a_body_has_no_board_and_no_build(client) -> None:
    r = client.post("/api/sessions", json={})
    assert r.status_code == 201, r.text
    session = r.json()
    assert session["board"] is None
    time.sleep(0.5)  # the scenario used to build the existing design right after creation
    assert client.get(f"/api/sessions/{session['id']}/builds").json() == []
    assert client.get(f"/api/sessions/{session['id']}").json()["board"] is None
