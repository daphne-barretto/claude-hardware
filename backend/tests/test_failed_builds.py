"""Failed builds and restores (bugs found in a session with undo).

1. Undo produced a failed build: restoring an earlier build (`POST .../builds/{id}/restore`)
   whose rebuild failed made a new, failed build the latest one, with no mesh or checks (404s),
   in place of the model. A restore must rebuild the stored design and, if that fails, say so
   plainly and keep the current design and model.
2. A failed build captured actions: a comment on the newest build, which had failed, was refused
   with the raw text "build build-… failed". The user is told in plain words, never with IDs or
   internal error text, and a failed build carries its plain reason (`Build.failure`).

The board example on the contract fixtures (`FixtureServices`); a build is made to fail by
swapping in services whose builds fail.
"""

from __future__ import annotations

import json
import re

import pytest
from test_session_api import start
from test_sketches import body_for, sketchable

from app.agent.services import BuildOutcome, FixtureServices, make_build
from app.contracts.session import Build
from app.session_api import CONFIG, SESSIONS, fixture_services

RAW = re.compile(r"build-[\w-]+|Traceback|Error\b")


class FailingServices(FixtureServices):
    """Fixture services whose builds fail, as a geometry kernel error does."""

    def build(self, design):
        build_id = f"build-{next(self._ids)}"
        error = {"kind": "runtime", "message": "Standard_ConstructionError: BRep_API", "line": 3}
        return BuildOutcome(make_build(build_id, None, status="failed"), error=error)


def failing(board, standards):
    return FailingServices()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def _builds(client, sid: str) -> list[Build]:
    return [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]


def test_a_restore_whose_rebuild_fails_keeps_the_design_and_says_so(client, monkeypatch) -> None:
    sid, found = start(client)
    first = Build.model_validate(found[-1][2]["build"])
    r = client.patch(f"/api/sessions/{sid}/parameters/clearance", json={"value": 0.7})
    assert r.status_code == 200, r.text
    design = SESSIONS.get(sid).ws.design
    params = json.dumps(design.params, sort_keys=True)
    latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    count = len(_builds(client, sid))

    monkeypatch.setattr(CONFIG, "services", failing)
    r = client.post(f"/api/sessions/{sid}/builds/{first.id}/restore")

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "nothing changed" in detail.lower() and not RAW.search(detail)
    # No failed build replaces the model, and the design is the one before the restore.
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == latest
    assert len(_builds(client, sid)) == count
    assert json.dumps(design.params, sort_keys=True) == params
    clearance = next(
        p for p in client.get(f"/api/sessions/{sid}/parameters").json() if p["name"] == "clearance"
    )
    assert clearance["value"] == 0.7 and clearance["locked"]


def test_a_failed_build_says_why_plainly_and_takes_no_comment(client, monkeypatch) -> None:
    sid, found = start(client)
    good = Build.model_validate(found[-1][2]["build"])
    plane = sketchable(client.get(good.mesh_url).text, good.id)[0]

    monkeypatch.setattr(CONFIG, "services", failing)
    r = client.post(f"/api/sessions/{sid}/sketches", json=body_for(plane))
    assert r.status_code == 201, r.text
    failed = _builds(client, sid)[-1]
    assert failed.status == "failed"
    assert failed.failure and not RAW.search(failed.failure)

    face, point = plane.face_id, list(plane.origin)
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": failed.id, "face_id": face, "point": point, "text": "Thicker here"},
    )
    assert r.status_code == 404
    assert not RAW.search(r.json()["detail"]), r.json()["detail"]
    # The model shown (the last good build) takes it.
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": good.id, "face_id": face, "point": point, "text": "Thicker here"},
    )
    assert r.status_code == 201, r.text
    # A failed build can't be restored: there's no model of it to bring back.
    r = client.post(f"/api/sessions/{sid}/builds/{failed.id}/restore")
    assert r.status_code == 409 and not RAW.search(r.json()["detail"])
