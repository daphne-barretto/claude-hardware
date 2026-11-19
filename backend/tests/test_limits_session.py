"""Limits and failures in the session API: Stop and Retry, a restart mid-run, a save that
fails, and the session cap. Each says plainly what happened and keeps the work.

Claude is played by `ScriptedDriver` (`test_session_api.Claude`); a restart is a new app
instance on the same `DATA_DIR`, as in `test_session_store.py`.
"""

from __future__ import annotations

import asyncio
import time

import test_session_api
from test_session_api import Claude, events, send, start, wait_until
from test_session_store import backend, data  # noqa: F401 (fixture)

from app.agent import limits
from app.session_api import CONFIG, INTERRUPTED, RETRY_NOTE, SESSIONS, STOPPED, SessionState

claude = test_session_api.claude
client = test_session_api.client


class Hangs:
    """A driver that never answers: the run is still going when it is stopped or cut off."""

    def __init__(self, *args) -> None:
        pass

    async def __call__(self, prompt, options):
        await asyncio.Event().wait()
        yield  # pragma: no cover


def errors(found) -> list[dict]:
    return [d for _, t, d in found if t == "error"]


def test_stop_ends_the_run_plainly_and_retry_picks_up_from_the_design(client, monkeypatch):
    fake = Claude()
    prompts: list[str] = []

    def driver(state, body, prompt):
        prompts.append(prompt)
        return Hangs() if len(prompts) == 1 else fake(state, body, prompt)

    monkeypatch.setattr(CONFIG, "driver", driver)
    sid, found = start(client)
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    send(client, sid, text="Make the walls thicker.")
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "running"

    r = client.post(f"/api/sessions/{sid}/stop")
    assert r.status_code == 202, r.text
    stopped = wait_until(client, sid, "idle", found[-1][0])
    (error,) = errors(stopped)
    assert error == {
        "type": "error",
        "message_id": error["message_id"],
        "message": STOPPED,
        "retryable": True,
        "reason": "stopped",
    }
    assert STOPPED == "You stopped this reply. Your design and builds so far are kept."
    assert client.get(f"/api/sessions/{sid}/builds").json() == builds, "nothing lost"

    fake.scripts.append([("text", "Picked up.")])
    r = client.post(f"/api/sessions/{sid}/retry", json={"mode": "auto"})
    assert r.status_code == 202, r.text
    assert r.json()["role"] == "user" and r.json()["text"] == "Retry"
    after = wait_until(client, sid, "idle", stopped[-1][0])
    assert not errors(after)
    assert "Make the walls thicker." in prompts[1], "the stopped message's request"
    assert RETRY_NOTE.format(why=STOPPED) in prompts[1]


def test_retry_needs_a_reply_that_stopped_and_nothing_running(client, claude) -> None:
    sid, _ = start(client)
    r = client.post(f"/api/sessions/{sid}/retry")
    assert r.status_code == 409 and "no reply to retry" in r.json()["detail"]
    assert client.post("/api/sessions/nope/retry").status_code == 404
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 202  # nothing to stop: fine


def test_a_restart_mid_run_says_it_was_interrupted_and_retry_carries_on(data, monkeypatch):  # noqa: F811
    fake = Claude()
    prompts: list[str] = []

    def driver(state, body, prompt):
        prompts.append(prompt)
        return Hangs() if len(prompts) == 1 else fake(state, body, prompt)

    monkeypatch.setattr(CONFIG, "driver", driver)
    with backend() as c:
        sid, found = start(c)
        builds = c.get(f"/api/sessions/{sid}/builds").json()
        send(c, sid, text="Move the USB opening.")
        for _ in range(500):
            running = events(c, sid, found[-1][0])
            if any(t == "message_start" for _, t, _ in running):
                break
            time.sleep(0.02)
        cut = next(d["message_id"] for _, t, d in running if t == "message_start")

    with backend() as c:
        session = c.get(f"/api/sessions/{sid}").json()
        assert session["status"] == "idle"
        after = events(c, sid)
        (error,) = errors(after)
        # In the reply that was cut off, which ends after it.
        assert error["message_id"] == cut and error["reason"] == "interrupted"
        assert error["retryable"] and error["message"] == INTERRUPTED
        assert "server restarted" in INTERRUPTED and "kept" in INTERRUPTED
        assert "Retry" in INTERRUPTED
        kinds = [t for _, t, _ in after]
        assert kinds.count("message_start") == kinds.count("message_end")
        assert c.get(f"/api/sessions/{sid}/builds").json() == builds, "builds kept"

        fake.scripts.append([("text", "Moved it.")])
        r = c.post(f"/api/sessions/{sid}/retry", json={})
        assert r.status_code == 202, r.text
        wait_until(c, sid, "idle", after[-1][0])
        assert "Move the USB opening." in prompts[-1] and "pressed Retry" in prompts[-1]


def test_a_restart_while_claude_waits_for_an_answer_keeps_the_question_open(data, monkeypatch):  # noqa: F811
    """Regression: a restart while Claude's question was open said the reply was interrupted
    (an error, with Retry), though nothing was running. The question stays open instead, and
    answering it carries on from the request with the answer."""
    fake = Claude()
    prompts: list[str] = []

    def driver(state, body, prompt):
        prompts.append(prompt)
        return fake(state, body, prompt)

    monkeypatch.setattr(CONFIG, "driver", driver)
    fake.scripts.append(
        [
            ("read_spec", {}),
            (
                "ask_user",
                {
                    "question": "The lid can't be 1 ft tall under the height limit. Which way?",
                    "options": [
                        {"id": "keep", "label": "Keep the 15.1 mm limit"},
                        {"id": "tall", "label": "Make it 1 ft"},
                    ],
                },
            ),
            ("finish", {"outcome": "done", "summary": "Answered."}),
        ]
    )
    with backend() as c:
        sid, found = start(c)
        send(c, sid, text="Make the lid 1 ft tall.")
        asked = wait_until(c, sid, "waiting_for_answer", found[-1][0])
        question = [d for _, t, d in asked if t == "question"][-1]

    with backend() as c:
        session = c.get(f"/api/sessions/{sid}").json()
        assert session["status"] == "waiting_for_answer"
        after = events(c, sid)
        assert errors(after) == [], "nothing was running: no interrupted note"
        open_question = [d for _, t, d in after if t == "question"][-1]
        assert open_question["question_id"] == question["question_id"]

        fake.scripts.append([("text", "Kept the lid at 15.1 mm.")])
        send(c, sid, answer={"question_id": question["question_id"], "option_id": "keep"})
        done = wait_until(c, sid, "idle", after[-1][0])
        assert errors(done) == []
        assert "Make the lid 1 ft tall." in prompts[-1]
        assert "Which way?" in prompts[-1] and "Keep the 15.1 mm limit" in prompts[-1]
        kinds = [t for _, t, _ in events(c, sid)]
        assert kinds.count("message_start") == kinds.count("message_end")


def test_a_failed_save_is_said_once_and_the_run_goes_on(client, claude, monkeypatch) -> None:
    sid, found = start(client)
    state = SESSIONS.get(sid)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state.store, "save", full)
    claude.scripts.append([("text", "Done anyway.")])
    send(client, sid, text="Look at the board.")
    after = wait_until(client, sid, "idle", found[-1][0])
    notices = [d for _, t, d in after if t == "notice"]
    assert [(n["kind"], n["reason"], n["text"]) for n in notices] == [
        ("warning", "save_failed", limits.SAVE_FAILED)
    ], "said once, not on every save"
    assert "out of space" in limits.SAVE_FAILED and "keeps working" in limits.SAVE_FAILED
    assert not errors(after)
    assert any(t == "text" and "Done anyway." in d["delta"] for _, t, d in after)

    # A session whose save failed is never dropped from memory to make room.
    monkeypatch.setenv("MAX_SESSIONS", "1")
    start(client)
    assert sid in SESSIONS.by_id


def test_the_session_cap_drops_only_saved_sessions_from_memory(client, claude, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSIONS", "1")
    first, _ = start(client)
    builds = client.get(f"/api/sessions/{first}/builds").json()
    second, _ = start(client)
    assert first not in SESSIONS.by_id and second in SESSIONS.by_id
    # Dropped from memory, not lost: it loads again, with its builds.
    assert client.get(f"/api/sessions/{first}").status_code == 200
    assert client.get(f"/api/sessions/{first}/builds").json() == builds


def test_shutting_down_ends_the_event_streams(client, claude) -> None:
    """At shutdown (`Sessions.close`) a session's streams end instead of waiting on it, so
    browsers reconnect and find it again after the restart."""
    sid, _ = start(client)
    state: SessionState = SESSIONS.get(sid)
    state.closed = True
    try:
        r = client.get(f"/api/sessions/{sid}/events", params={"after": 10_000})  # follows
        assert r.status_code == 200 and r.text == ""
    finally:
        state.closed = False


def test_an_attachment_over_the_limit_is_refused_plainly(client, monkeypatch) -> None:
    import io

    from app import attachments

    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 1024**2)
    sid, _ = start(client)
    files = {"file": ("drawing.png", io.BytesIO(b"x" * (1024**2 + 1)), "image/png")}
    r = client.post(f"/api/sessions/{sid}/attachments", files=files)
    assert r.status_code == 413
    assert r.json()["detail"] == (
        "drawing.png is larger than 1 MB, the most an attached file can be. Nothing was sent: "
        "attach a smaller file, or only the pages or parts Claude needs."
    )


def test_an_export_that_fails_says_so_plainly(client, monkeypatch) -> None:
    import app.session_api as api

    sid, _ = start(client)
    build_id = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]

    def dies(*args, **kwargs):
        raise api_worker_error("the geometry worker died")

    def api_worker_error(text):
        from geometry.worker import WorkerError

        return WorkerError(text)

    monkeypatch.setattr(api, "export_files", dies)
    r = client.post(f"/api/sessions/{sid}/exports", json={"build_id": build_id, "formats": ["stl"]})
    assert r.status_code == 503
    assert r.json()["detail"] == limits.EXPORT_UNKNOWN
    assert "worker" not in r.json()["detail"]
    assert limits.EXPORT_FAILED["output"] == (
        "This export is too large for the server to write. Your design is unchanged: export "
        "fewer parts, or one format at a time."
    )
