"""The existing enclosure passes every check on the board revision it was designed for.

A new session starts from the existing enclosure built on the previous revision (demo step 1).
That first build must pass every check in `standards.yaml`; only the checks without a backend
(`geometry.report.UNTESTED`) may be untested. The enclosure generator's own build of the
existing design on that revision is covered by
`test_geometry.test_older_revision_passes_every_check`; this checks what a session serves.

The scenario comes from `demos/b-parasite/` and `standards.yaml`; nothing here names a board, part
or dimension.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.agent.run import scenario_boards
from app.contracts.session import Build, Session
from app.examples import example_standards, examples
from app.main import app
from app.session_api import SESSIONS
from geometry.report import UNTESTED


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def first_build(client: TestClient) -> tuple[str, Build]:
    """A new session's first build: the existing enclosure on its own revision."""
    r = client.post("/api/sessions", json={"example": examples()[0].id})
    assert r.status_code == 201, r.text
    sid = Session.model_validate(r.json()).id
    # A real build: the sandbox, then a geometry worker that loads the CAD kernel.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        builds = client.get(f"/api/sessions/{sid}/builds").json()
        if builds:
            return sid, Build.model_validate(builds[0])
        time.sleep(0.05)
    raise AssertionError("the first build never arrived")


def test_the_existing_enclosure_passes_every_check_on_its_own_revision(client) -> None:
    previous, _ = scenario_boards()
    standards = example_standards()
    sid, build = first_build(client)
    assert (build.board.name, build.board.revision) == (previous.name, previous.revision)

    report = client.get(f"/api/sessions/{sid}/builds/{build.id}/checks").json()
    results = report["results"]
    failed = [(r["check"], r.get("summary")) for r in results if r["status"] == "fail"]
    assert not failed, failed
    untested = {r["check"] for r in results if r["status"] == "untested"}
    assert untested == set(UNTESTED)
    passed = {r["check"] for r in results if r["status"] == "pass"}
    # Every standard for an enclosure around a board passes; one for a part fitting an object
    # (object_fit) doesn't apply to it.
    applies = {s.check for s in standards.standards if s.applies(has_board=True)}
    assert applies <= passed
    na = {r["check"] for r in results if r["status"] == "not_applicable"}
    assert na == {s.check for s in standards.standards} - applies
    assert build.checks.failed == 0
