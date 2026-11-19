"""Undo and redo over one timeline of design changes, whoever made them (`app.design_history`,
`app.history_api`).

The board example on the contract fixtures (`FixtureServices`), with Claude scripted
(`ScriptedDriver`): a hand edit, a Claude run, an accept and a sketch are four steps, undone and
redone in exactly that order.
"""

from __future__ import annotations

import pytest
from test_failed_builds import failing
from test_session_api import Claude, last_id, send, start, wait_until
from test_sketches import body_for, sketchable

from app.contracts.session import Build, DesignHistory, HistoryMove
from app.session_api import CONFIG, SESSIONS, fixture_services


@pytest.fixture
def claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def history(client, sid: str) -> DesignHistory:
    r = client.get(f"/api/sessions/{sid}/history")
    assert r.status_code == 200, r.text
    return DesignHistory.model_validate(r.json())


def move(client, sid: str, direction: str) -> HistoryMove:
    r = client.post(f"/api/sessions/{sid}/history/{direction}")
    assert r.status_code == 200, r.text
    return HistoryMove.model_validate(r.json())


def params(client, sid: str) -> dict[str, tuple]:
    return {
        p["name"]: (p["value"], p["locked"])
        for p in client.get(f"/api/sessions/{sid}/parameters").json()
    }


def statuses(client, sid: str) -> dict[str, str] | None:
    r = client.get(f"/api/sessions/{sid}/changes")
    if r.status_code == 404:
        return None
    return {c["id"]: c["status"] for c in r.json()["changes"]}


def sketches(client, sid: str) -> list[str]:
    return [s["tag"] for s in client.get(f"/api/sessions/{sid}/sketches").json()]


def builds(client, sid: str) -> list[Build]:
    return [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]


def four_steps(client, claude) -> tuple[str, list[dict]]:
    """A hand edit, a Claude run (Manual: it proposes two changes), an accept of one of them and
    a sketch. Returns the session and the state after each (parameters, change statuses,
    sketches), the start first."""
    sid, _ = start(client)
    seen = [(params(client, sid), statuses(client, sid), sketches(client, sid))]

    r = client.patch(f"/api/sessions/{sid}/parameters/clearance", json={"value": 0.7})
    assert r.status_code == 200, r.text
    seen.append((params(client, sid), statuses(client, sid), sketches(client, sid)))

    claude.scripts.append(
        [
            ("set_parameter", {"name": "draft", "value": 2.0}),
            ("set_parameter", {"name": "wall", "value": 3.0}),
            ("run_checks", {}),
            ("finish", {"outcome": "done", "summary": "Proposed a thicker wall and draft."}),
        ]
    )
    after = last_id(client, sid)
    send(client, sid, text="Make it sturdier", mode="manual")
    wait_until(client, sid, "idle", after)
    proposed = statuses(client, sid)
    assert proposed and set(proposed.values()) == {"proposed"}
    seen.append((params(client, sid), proposed, sketches(client, sid)))

    first = sorted(proposed)[0]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first]})
    assert r.status_code == 200, r.text
    seen.append((params(client, sid), statuses(client, sid), sketches(client, sid)))

    shown = builds(client, sid)[-1]
    plane = sketchable(client.get(shown.mesh_url).text, shown.id)[0]
    r = client.post(f"/api/sessions/{sid}/sketches", json=body_for(plane))
    assert r.status_code == 201, r.text
    seen.append((params(client, sid), statuses(client, sid), sketches(client, sid)))
    return sid, seen


def test_undo_and_redo_walk_hand_and_claude_changes_in_order(client, claude) -> None:
    sid, seen = four_steps(client, claude)
    h = history(client, sid)
    texts = [s.text for s in h.steps]
    assert len(texts) == 4 and h.position == 4
    assert texts[0] == "You: set clearance to 0.7 mm"
    assert texts[1].startswith("Claude: ") and texts[1].endswith("and 1 more")
    assert texts[2] == "You: accepted 1 change"
    assert texts[3] == "You: sketch a circle cut"
    assert [s.actor for s in h.steps] == ["user", "claude", "user", "user"]
    assert h.steps[1].message_id is not None and h.undo == h.steps[3] and h.redo is None
    count = len(builds(client, sid))

    # Undo takes back the sketch, the accept, Claude's run and the hand edit, in that order,
    # each to the state before it, rebuilt.
    for i in (3, 2, 1, 0):
        moved = move(client, sid, "undo")
        assert moved.step.text == texts[i]
        assert moved.history.position == i
        assert (params(client, sid), statuses(client, sid), sketches(client, sid)) == seen[i]
        assert moved.build.status == "ready"
        assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == moved.build.id
    r = client.post(f"/api/sessions/{sid}/history/undo")
    assert r.status_code == 409 and "nothing to undo" in r.json()["detail"].lower()

    # Redo brings them back in the order they were made.
    for i in (1, 2, 3, 4):
        moved = move(client, sid, "redo")
        assert moved.step.text == texts[i - 1]
        assert (params(client, sid), statuses(client, sid), sketches(client, sid)) == seen[i]
    r = client.post(f"/api/sessions/{sid}/history/redo")
    assert r.status_code == 409 and "nothing to redo" in r.json()["detail"].lower()
    # Undo never deletes builds: each move added one.
    assert len(builds(client, sid)) == count + 8


def test_a_new_change_after_an_undo_drops_the_redo_branch(client, claude) -> None:
    sid, seen = four_steps(client, claude)
    move(client, sid, "undo")  # the sketch
    move(client, sid, "undo")  # the accept
    assert history(client, sid).redo.text == "You: accepted 1 change"

    r = client.patch(f"/api/sessions/{sid}/parameters/clearance", json={"value": 0.8})
    assert r.status_code == 200, r.text

    h = history(client, sid)
    assert [s.text for s in h.steps][-1] == "You: set clearance to 0.8 mm"
    assert len(h.steps) == 3 and h.position == 3 and h.redo is None
    assert client.post(f"/api/sessions/{sid}/history/redo").status_code == 409
    moved = move(client, sid, "undo")
    assert moved.step.text == "You: set clearance to 0.8 mm"
    assert (params(client, sid), statuses(client, sid), sketches(client, sid)) == seen[2]


def test_the_timeline_is_saved_with_the_session(client, claude) -> None:
    sid, seen = four_steps(client, claude)
    move(client, sid, "undo")
    saved = history(client, sid)

    SESSIONS.by_id.clear()  # as after a restart: the session loads from the store
    assert history(client, sid) == saved
    moved = move(client, sid, "redo")
    assert moved.step.text == "You: sketch a circle cut"
    assert sketches(client, sid) == seen[4][2]
    SESSIONS.by_id.clear()
    assert history(client, sid).position == 4


def test_an_undo_whose_rebuild_fails_keeps_the_design(client, claude, monkeypatch) -> None:
    sid, seen = four_steps(client, claude)
    latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    monkeypatch.setattr(CONFIG, "services", failing)

    r = client.post(f"/api/sessions/{sid}/history/undo")

    assert r.status_code == 409
    assert "nothing changed" in r.json()["detail"].lower() and "build-" not in r.json()["detail"]
    assert history(client, sid).position == 4
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == latest
    assert (params(client, sid), statuses(client, sid), sketches(client, sid)) == seen[4]


def test_claude_is_told_about_its_undone_changes_once(client, claude) -> None:
    sid, _ = four_steps(client, claude)
    for _ in range(3):  # the sketch, the accept, then Claude's run
        moved = move(client, sid, "undo")
    assert moved.step.actor == "claude"
    label = moved.step.label

    claude.scripts.append([("finish", {"outcome": "done", "summary": "Noted."})])
    after = last_id(client, sid)
    send(client, sid, text="What's next?")
    wait_until(client, sid, "idle", after)
    assert "The user undid these changes of yours" in claude.prompts[-1]
    assert label in claude.prompts[-1]

    claude.scripts.append([("finish", {"outcome": "done", "summary": "Noted."})])
    after = last_id(client, sid)
    send(client, sid, text="And now?")
    wait_until(client, sid, "idle", after)
    assert "The user undid" not in claude.prompts[-1]


def test_redo_of_a_claude_step_takes_back_the_note(client, claude) -> None:
    sid, _ = four_steps(client, claude)
    for _ in range(3):
        move(client, sid, "undo")
    move(client, sid, "redo")  # Claude's run is back

    claude.scripts.append([("finish", {"outcome": "done", "summary": "Noted."})])
    after = last_id(client, sid)
    send(client, sid, text="What's next?")
    wait_until(client, sid, "idle", after)
    assert "The user undid" not in claude.prompts[-1]


def test_undo_and_redo_wait_for_claude(client, claude) -> None:
    sid, _ = four_steps(client, claude)
    state = SESSIONS.get(sid)
    state.session.status = "running"
    try:
        r = client.post(f"/api/sessions/{sid}/history/undo")
        assert r.status_code == 409 and "Claude is working" in r.json()["detail"]
    finally:
        state.session.status = "idle"


def test_each_undo_and_redo_carries_the_change_set_it_moved_to(client, claude) -> None:
    """The canvas shows the change set of the state it moved to from the move itself: no
    `GET /changes` after each step, which is a 404 (a console error) where there is none (QA brief
    14). Same set as `/changes`; null where `/changes` is a 404."""
    sid, _ = four_steps(client, claude)
    carried = []
    for direction in ("undo",) * 4 + ("redo",) * 4:
        r = client.post(f"/api/sessions/{sid}/history/{direction}")
        assert r.status_code == 200, r.text
        assert "change_set" in r.json()
        served = client.get(f"/api/sessions/{sid}/changes")
        expected = served.json() if served.status_code == 200 else None
        assert r.json()["change_set"] == expected
        carried.append(r.json()["change_set"] is not None)
    # None before Claude's run (the hand edit and the start), its set after it.
    assert carried == [True, True, False, False, False, True, True, True]
