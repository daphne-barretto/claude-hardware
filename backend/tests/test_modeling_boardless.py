"""Modeling tools on a design with no board.

A design made from a plain request has no board. Every modeling edit there rebuilt by looking up
`latest.board.name` and crashed with a 500, after the edit was already in the design: the edit
then showed in the hand edits list without being built, and deleting it failed. Edits must
rebuild without a board, and an edit whose rebuild fails must leave the design as it was.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_modeling import upward

from app.main import app
from app.session_api import CONFIG, SESSIONS, fixture_services

BOX = "make a box 40 x 30 x 20 mm with 2 mm walls"


@pytest.fixture(autouse=True)
def _keep_config(monkeypatch):
    monkeypatch.setattr(CONFIG, "driver", CONFIG.driver)
    monkeypatch.setattr(CONFIG, "services", CONFIG.services)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import e2e_api

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def _boardless_box(client) -> tuple[str, dict]:
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": BOX}).status_code == 202
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if client.get(f"/api/sessions/{sid}").json()["status"] != "running":
            builds = client.get(f"/api/sessions/{sid}/builds").json()
            if builds:
                assert builds[-1]["board"] is None
                return sid, builds[-1]
        time.sleep(0.1)
    raise AssertionError("the box was never built")


def _push_pull(client, build: dict) -> dict:
    gltf = json.loads(client.get(build["mesh_url"]).text)
    top = upward(gltf, build["id"])[0]
    return {"build_id": build["id"], "kind": "push_pull", "face_id": top.face_id, "distance": 1.0}


def test_an_edit_on_a_design_without_a_board_rebuilds(client) -> None:
    sid, build = _boardless_box(client)
    before = len(client.get(f"/api/sessions/{sid}/builds").json())

    r = client.post(f"/api/sessions/{sid}/edits", json=_push_pull(client, build))

    assert r.status_code == 201, r.text
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    assert len(builds) == before + 1
    assert builds[-1]["board"] is None
    tag = r.json()["tag"]
    assert [e["tag"] for e in client.get(f"/api/sessions/{sid}/edits").json()] == [tag]

    d = client.delete(f"/api/sessions/{sid}/edits/{tag}")
    assert d.status_code == 200, d.text
    assert client.get(f"/api/sessions/{sid}/edits").json() == []


def test_an_edit_whose_rebuild_fails_leaves_the_design_as_it_was(client, monkeypatch) -> None:
    import app.modeling_api as modeling_api

    sid, build = _boardless_box(client)
    design = SESSIONS.get(sid).ws.design
    params_before = json.dumps(design.params, sort_keys=True)

    async def broken(*args, **kwargs):
        raise RuntimeError("the build service broke")

    monkeypatch.setattr(modeling_api, "_build_design", broken)
    r = client.post(f"/api/sessions/{sid}/edits", json=_push_pull(client, build))

    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "RuntimeError" not in detail and "Traceback" not in detail
    assert "nothing was changed" in detail.lower()
    assert client.get(f"/api/sessions/{sid}/edits").json() == []
    assert json.dumps(design.params, sort_keys=True) == params_before
