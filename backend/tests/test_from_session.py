"""Turning a saved session into a sample (`app.fixtures.from_session`): the demos are recorded
runs, made with it from sessions the session API saved."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import samples
from app.fixtures.from_session import Refused, check_private, cut, sample_files, write
from app.fixtures.samples import stale

# A recorded run with two turns' worth of builds and a next revision it hasn't loaded yet.
SAMPLE = "sample-b-parasite-v2-0-arrives"


@pytest.fixture
def saved(tmp_path, monkeypatch):
    """A saved session of the real API: a browser's copy of SAMPLE, whose builds are in the
    build store and whose state and events are in the session store, as a run leaves them."""
    data = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data))
    from app.main import app
    from app.session_api import CONFIG, SESSIONS, fixture_services

    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        response = client.post(f"/api/demos/{SAMPLE}/session")
        assert response.status_code == 200, response.text
        yield response.json()["id"], data


def test_a_saved_session_becomes_a_sample(saved, tmp_path) -> None:
    session_id, data = saved
    files, recorded = sample_files(session_id, SAMPLE, data, title="Recorded")
    out = tmp_path / "samples"
    write(files, SAMPLE, recorded, out)

    made = samples._load(out / SAMPLE)
    original = samples.catalogue()[SAMPLE]
    assert made.session.id == SAMPLE and made.session.sample and made.session.title == "Recorded"
    assert made.session.board == original.session.board
    assert made.session.next_revision == original.session.next_revision
    assert [(m.role, m.text) for m in made.messages] == [
        (m.role, m.text) for m in original.messages
    ]
    # The builds the session made, under the sample's IDs, with their own files.
    assert list(made.builds) == list(original.builds)
    for bid, build in made.builds.items():
        assert build.checks == original.builds[bid].checks
        assert build.mesh_url == f"/api/sessions/{SAMPLE}/builds/{bid}/mesh"
        assert made.json(bid, "checks.json")["build_id"] == bid
        for name in ("mesh.gltf", "source.json", "design.json"):
            mine, theirs = made.file(bid, name).read_text(), original.file(bid, name).read_text()
            if name == "source.json":
                mine, theirs = json.loads(mine)["code"], json.loads(theirs)["code"]
            if name == "design.json":
                mine, theirs = json.loads(mine)["params"], json.loads(theirs)["params"]
            assert mine == theirs, (bid, name)
    first, second = list(made.builds)
    diff = json.loads((out / SAMPLE / "builds" / second / "diffs" / f"{first}.json").read_text())
    assert (diff["old"], diff["new"]) == (first, second)
    assert made.session.latest_build_id == second

    # Nothing names the saved session or its builds.
    for path in (out / SAMPLE).rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert session_id not in text, path
            assert not re.search(r"build-[0-9a-f]{12}", text), path
    assert "owner" not in json.loads((out / SAMPLE / "session.json").read_text())

    # Recorded: --check holds it to its own files, not the generators' inputs.
    index = json.loads((out / "index.json").read_text())
    assert index["samples"] == [SAMPLE]
    entry = index["recorded"][SAMPLE]
    assert entry["builds"] == 2 and entry["turns"] == 1
    assert entry["models"] == sorted({m.model for m in original.messages if m.model})
    assert stale(out) == []
    (out / SAMPLE / "builds" / second / "checks.json").write_text("{}\n")
    assert any("changed since it was recorded" in p for p in stale(out))


def test_a_cut_keeps_the_first_turns(saved) -> None:
    session_id, data = saved
    files, recorded = sample_files(session_id, SAMPLE, data, turns=0)
    doc = json.loads(files["session.json"])
    assert doc["messages"] == [] and recorded["turns"] == 0
    assert [e["event"]["type"] for e in doc["events"]] == ["build_ready"]
    assert doc["session"]["latest_build_id"] == "build-1"
    # The board the opened case was checked on, with the next revision still to load.
    assert doc["session"]["board"] == json.loads(files["builds/build-1/build.json"])["board"]
    assert doc["session"]["next_revision"] is not None
    assert "builds/build-2/build.json" not in files


def test_cut_by_user_turns() -> None:
    state = {
        "messages": [
            {"id": "msg-1", "role": "user", "text": "a"},
            {"id": "msg-2", "role": "assistant", "text": "b"},
            {"id": "msg-3", "role": "user", "text": "c"},
            {"id": "msg-4", "role": "assistant", "text": "d"},
        ]
    }
    events = [
        (1, {"type": "build_ready"}),
        (2, {"type": "user_message"}),
        (3, {"type": "message_end"}),
        (4, {"type": "user_message"}),
        (5, {"type": "message_end"}),
    ]
    messages, kept = cut(state, events, 1)
    assert [m["id"] for m in messages] == ["msg-1", "msg-2"]
    assert [i for i, _ in kept] == [1, 2, 3]
    assert cut(state, events, None) == (state["messages"], events)
    assert cut(state, events, 0)[1] == [(1, {"type": "build_ready"})]


def test_nothing_private_is_written(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-but-secret")
    with pytest.raises(Refused, match="secret"):
        check_private({"session.json": "it says not-a-real-key-but-secret"})
    with pytest.raises(Refused, match="private"):
        check_private({"source.json": "open('/Users/someone/notes.txt')"})
    with pytest.raises(Refused, match="private"):
        check_private({"session.json": "hardware_owner=abc"})
    check_private({"session.json": "a box 60 x 40 mm"})
