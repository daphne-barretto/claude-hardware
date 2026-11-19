"""Importing a STEP file into a session with no board, or one checked against the browser's own
standards set (`app.standards_sets`): both were refused by a crash (`session_board` returned
None; the import worker could only find the built-in set). The import runs as the app runs it,
in the import worker."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from build123d import Box, export_step
from fastapi.testclient import TestClient

from app.contracts.session import Build
from app.imports import import_notes
from app.main import app
from app.session_api import SESSIONS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


@pytest.fixture
def block(tmp_path) -> bytes:
    path = tmp_path / "block.step"
    export_step(Box(20, 10, 5), str(path))
    return Path(path).read_bytes()


def imported(client, sid: str, data: bytes) -> Build:
    r = client.post(f"/api/sessions/{sid}/imports", files={"file": ("block.step", data)})
    assert r.status_code == 201, r.text
    return Build.model_validate(r.json())


def test_a_step_file_imports_into_a_session_with_no_board(client, block) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    build = imported(client, sid, block)
    assert build.status == "ready" and build.board is None
    checks = client.get(build.checks_url).json()
    assert checks["build_id"] == build.id and checks.get("board") is None
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == build.id
    # What Claude is told about it with the next message.
    notes = import_notes(SESSIONS.get(sid).import_list(), build.id)
    assert "checked against no board" in notes


def test_a_step_file_is_checked_against_the_browsers_own_standards_set(client, block) -> None:
    built_in = next(s for s in client.get("/api/standards").json() if s["built_in"])
    own = client.post("/api/standards", json={"name": "Mine", "duplicate_of": built_in["id"]})
    assert own.status_code == 201, own.text
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.put(f"/api/sessions/{sid}/standards", json={"standards": own.json()["id"]})
    assert r.status_code in (200, 202), r.text
    deadline = time.monotonic() + 30  # the recheck it starts ends first
    while client.get(f"/api/sessions/{sid}").json()["status"] != "idle":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    build = imported(client, sid, block)
    checks = client.get(build.checks_url).json()
    assert checks["build_id"] == build.id
