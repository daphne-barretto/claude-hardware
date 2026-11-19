"""Sessions survive a restart: the session API keeps them in `DATA_DIR`, so a deploy or a
restart of the backend loses no chat, build or change set.

A restart is a new app instance on the same `DATA_DIR`: the test client's lifespan ends (the
running tasks are cancelled and the sessions dropped from memory), then a new one starts.
Claude is played by `ScriptedDriver`, as in `test_session_api.py`, whose helpers these reuse.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import contextmanager

import pytest
import test_session_api
from fastapi.testclient import TestClient
from test_session_api import Claude, events, run_with_changes, send, start, wait_until

from app.contracts.session import Build, Message, Session
from app.main import app
from app.owners import OWNER_COOKIE, new_owner
from app.session_api import SESSIONS

claude = test_session_api.claude
openings = test_session_api.openings


@pytest.fixture
def data(tmp_path, monkeypatch):
    path = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(path))
    SESSIONS.by_id.clear()
    yield path
    SESSIONS.by_id.clear()


# One browser across the restarts: it keeps its sessions (`app.owners`).
BROWSER = {OWNER_COOKIE: new_owner()}


@contextmanager
def backend():
    """One app instance; leaving it is the shutdown of a deploy or restart."""
    with TestClient(app, cookies=BROWSER) as client:
        yield client
    SESSIONS.by_id.clear()


def finishing(text: str) -> list:
    return [("text", text), ("finish", {"outcome": "blocked", "summary": text})]


def asking() -> list:
    options = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    return [
        ("text", "One question first."),
        ("ask_user", {"question": "Which?", "options": options}),
        ("finish", {"outcome": "blocked", "summary": "Answered."}),
    ]


def test_a_session_survives_a_restart(data, claude: Claude) -> None:
    claude.scripts.append(asking())
    with backend() as client:
        sid, found = start(client)
        send(client, sid, text="Look at the board.")
        asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
        (question,) = [d for _, t, d in asked if t == "question"]
        send(client, sid, answer={"question_id": question["question_id"], "option_id": "a"})
        wait_until(client, sid, "idle", asked[-1][0])
        session = client.get(f"/api/sessions/{sid}").json()
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        builds = client.get(f"/api/sessions/{sid}/builds").json()
        before = events(client, sid)
        assert builds, "the session has a build"

    with backend() as client:
        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200, r.text
        assert Session.model_validate(r.json()) == Session.model_validate(session)
        r = client.get(f"/api/sessions/{sid}/messages")
        assert r.status_code == 200, r.text
        assert r.json() == messages
        r = client.get(f"/api/sessions/{sid}/builds")
        assert r.status_code == 200, r.text
        assert r.json() == builds
        for build in [Build.model_validate(b) for b in builds]:
            assert client.get(build.mesh_url).status_code == 200
            assert client.get(build.checks_url).status_code == 200
            assert client.get(build.source_url).status_code == 200
        assert events(client, sid) == before
        assert sid in [s["id"] for s in client.get("/api/sessions").json()]

        # The conversation goes on.
        claude.scripts.append(finishing("Still here."))
        reply = send(client, sid, text="Carry on.")
        assert reply.id not in {m["id"] for m in messages}
        after = wait_until(client, sid, "idle", before[-1][0])
        assert "Still here." in "".join(d["delta"] for _, t, d in after if t == "text")

        # A new session gets a new ID, not one a stored session has.
        r = client.post("/api/sessions", json={})
        assert r.status_code == 201 and r.json()["id"] != sid


def test_hand_edits_comments_attachments_and_exports_survive_a_restart(data, claude) -> None:
    with backend() as client:
        sid, _ = start(client)
        params = client.get(f"/api/sessions/{sid}/parameters").json()
        param = next(p for p in params if isinstance(p["value"], int | float))
        value = param["value"] + 1
        r = client.patch(f"/api/sessions/{sid}/parameters/{param['name']}", json={"value": value})
        assert r.status_code == 200, r.text
        build_id = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
        files = {"file": ("notes.png", io.BytesIO(b"keep me"), "image/png")}
        attachment = client.post(f"/api/sessions/{sid}/attachments", files=files).json()
        export = client.post(
            f"/api/sessions/{sid}/exports", json={"build_id": build_id, "formats": ["step"]}
        ).json()
        params = client.get(f"/api/sessions/{sid}/parameters").json()

    with backend() as client:
        assert client.get(f"/api/sessions/{sid}/parameters").json() == params
        edited = next(p for p in params if p["name"] == param["name"])
        assert edited["value"] == value and edited["locked"]
        r = client.get(attachment["url"])
        assert r.status_code == 200 and r.content == b"keep me"
        for file in export["files"]:
            assert client.get(file["url"]).status_code == 200
        # The locked value holds in the design Claude works on.
        assert SESSIONS.get(sid).ws.design.locked


def test_a_change_set_survives_a_restart_and_can_be_reviewed(data, claude, openings) -> None:
    with backend() as client:
        sid, _ = run_with_changes(client, claude, "manual")
        change_set = client.get(f"/api/sessions/{sid}/changes").json()

    with backend() as client:
        r = client.get(f"/api/sessions/{sid}/changes")
        assert r.status_code == 200, r.text
        assert r.json() == change_set
        first = change_set["changes"][0]["id"]
        r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first]})
        assert r.status_code == 200, r.text
        status = {c["id"]: c["status"] for c in r.json()["changes"]}
        assert status[first] == "accepted"
        builds = {b["id"] for b in client.get(f"/api/sessions/{sid}/builds").json()}
        assert r.json()["build"] in builds


def interrupted(client, sid: str) -> list[Message]:
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "idle"
    found = client.get(f"/api/sessions/{sid}/messages").json()
    messages = [Message.model_validate(m) for m in found]
    assert "interrupted" in messages[-1].text and messages[-1].role == "assistant"
    kinds = [t for _, t, _ in events(client, sid)]
    assert kinds.count("message_start") == kinds.count("message_end"), "no message left open"
    return messages


def test_a_question_waiting_for_an_answer_stays_open_after_a_restart(data, claude) -> None:
    """Nothing was running while Claude waited, so the restart cuts nothing off: the question is
    still there to answer, with no interrupted note, and the answer carries on."""
    claude.scripts.append(asking())
    with backend() as client:
        sid, found = start(client)
        send(client, sid, text="Look at the board.")
        asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
        (question,) = [d for _, t, d in asked if t == "question"]

    with backend() as client:
        assert client.get(f"/api/sessions/{sid}").json()["status"] == "waiting_for_answer"
        after = events(client, sid)
        assert not [d for _, t, d in after if t == "error"]
        claude.scripts.append(finishing("Again."))
        send(client, sid, answer={"question_id": question["question_id"], "option_id": "a"})
        wait_until(client, sid, "idle", after[-1][0])


def test_a_run_cut_off_by_a_restart_comes_back_idle_with_a_note(data, monkeypatch) -> None:
    from app.session_api import CONFIG

    class Hangs:
        """A driver that never answers: the run is still going at the restart."""

        def __init__(self, *args) -> None:
            pass

        async def __call__(self, prompt, options):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    monkeypatch.setattr(CONFIG, "driver", lambda state, body, prompt: Hangs())
    with backend() as client:
        sid, _ = start(client)
        send(client, sid, text="Look at the board.")
        assert client.get(f"/api/sessions/{sid}").json()["status"] == "running"

    with backend() as client:
        messages = interrupted(client, sid)
        assert [m.role for m in messages[:1]] == ["user"]


def test_the_architecture_doc_says_where_sessions_live_and_what_survives_a_deploy() -> None:
    from app.contracts.common import REPO_ROOT

    doc = (REPO_ROOT / "docs" / "architecture.md").read_text()
    section = doc.split("### Storage and what survives a deploy", 1)[1].split("\n### ", 1)[0]
    render = (REPO_ROOT / "render.yaml").read_text()
    data_dir = render.split("key: DATA_DIR", 1)[1].split("value:", 1)[1].split()[0]
    assert data_dir in section, "where staging and demo keep DATA_DIR"
    assert "sessions.db" in section
    for event in ("restart", "deploy", "persistent disk"):
        assert event in section, f"what survives a {event}"


def test_a_restored_build_says_what_it_was_restored_from_after_a_restart(data, claude) -> None:
    """QA fix brief 15: History says "Restored from …" for a restore on the real backend, also
    once the session is read back from `DATA_DIR`."""
    with backend() as client:
        sid, found = start(client)
        first = found[-1][2]["build"]["id"]
        params = client.get(f"/api/sessions/{sid}/parameters").json()
        param = next(p for p in params if isinstance(p["value"], int | float))
        edit = {"value": param["value"] + 1}
        r = client.patch(f"/api/sessions/{sid}/parameters/{param['name']}", json=edit)
        assert r.status_code == 200, r.text
        r = client.post(f"/api/sessions/{sid}/builds/{first}/restore")
        assert r.status_code == 201, r.text
        restored = r.json()
        assert restored["restored_from"] == first
        assert client.get(f"/api/sessions/{sid}/builds").json()[-1] == restored

    with backend() as client:
        listed = client.get(f"/api/sessions/{sid}/builds").json()
        assert listed[-1]["id"] == restored["id"]
        assert listed[-1]["restored_from"] == first
