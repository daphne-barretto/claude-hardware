"""Lifecycle and resources (`docs/security.md`): restarts mid-run and mid-build, stream
resumes, very long sessions, missing sessions, the session cap, a full disk and a slow model."""

from __future__ import annotations

import errno
import time

import pytest
from conftest import Browser, box_session, wait_for
from fastapi.testclient import TestClient

from app.session_api import SESSIONS


def restart(client: TestClient) -> None:
    """What a server restart leaves: only the store. The app's shutdown runs (as on SIGTERM),
    memory is cleared, and the next request loads sessions from the store."""
    client.__exit__(None, None, None)
    SESSIONS.by_id.clear()
    client.__enter__()


def test_a_restart_mid_run_leaves_the_session_idle_with_a_note(
    client, browser: Browser, claude
) -> None:
    claude.delay = 0.3
    sid = browser.new_session()
    assert browser.send(sid, "make a box 40 x 30 x 20 mm").status_code == 202
    wait_for(lambda: len(browser.events(sid)) >= 2, "the run to start")
    restart(client)
    session = browser.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "idle"
    kinds = [t for _, t, _ in browser.events(sid)]
    starts, ends = kinds.count("message_start"), kinds.count("message_end")
    assert starts == ends  # every message closed
    # Said as the "interrupted" error, with Retry (`app.agent.limits`).
    said = [d for _, t, d in browser.events(sid) if t == "error"]
    assert [d["reason"] for d in said] == ["interrupted"] and "interrupted" in said[0]["message"]
    claude.delay = 0
    assert browser.send(sid, "make a box 40 x 30 x 20 mm").status_code == 202
    assert browser.settle(sid) == "idle"


def test_a_restart_mid_build_keeps_no_half_written_build(client, browser: Browser, claude) -> None:
    """Claude's edit is building when the server goes down: afterwards every build the session
    lists is whole (mesh and checks served), the session is idle, and it takes a new message."""
    from app.agent.scripted import box_design

    sid, build = box_session(browser)
    code, params = box_design(60, 40, 30, 2, "")
    claude.scripts.append(
        [
            ("write_design", {"code": code, "params": params}),
            ("finish", {"outcome": "done", "summary": "Bigger."}),
        ]
    )
    assert browser.send(sid, "make it bigger").status_code == 202
    wait_for(
        lambda: any("checks" in str(d.get("text")) for _, t, d in browser.events(sid)[-3:]),
        "the build to start",
    )
    restart(client)
    assert browser.status(sid) == "idle"
    for b in browser.builds(sid):
        assert browser.get(f"/api/sessions/{sid}/builds/{b['id']}/mesh").status_code == 200
        assert browser.get(f"/api/sessions/{sid}/builds/{b['id']}/checks").status_code == 200
    assert browser.send(sid, "make a box 20 x 20 x 20 mm").status_code == 202
    assert browser.settle(sid) == "idle"


def test_the_stream_resumes_after_a_disconnect_without_gaps_or_repeats(
    browser: Browser,
) -> None:
    sid = browser.example_session()
    assert browser.send(sid, "check it").status_code == 202
    browser.settle(sid)
    everything = browser.events(sid)
    middle = everything[len(everything) // 2][0]
    first = [e for e in everything if e[0] <= middle]
    rest = browser.get(
        f"/api/sessions/{sid}/events",
        params={"follow": "false"},
        headers={"Last-Event-ID": str(middle)},
    )
    from conftest import parse_sse

    assert first + parse_sse(rest.text) == everything
    for bad in ["abc", "-5", "99999999999999999999", ""]:
        r = browser.get(
            f"/api/sessions/{sid}/events",
            params={"follow": "false"},
            headers={"Last-Event-ID": bad},
        )
        assert r.status_code == 200, bad
    r = browser.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": "abc"})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/session-999999",
        "/api/sessions/does-not-exist/messages",
        "/api/sessions/..%2F..%2Fetc%2Fpasswd",
        "/api/sessions/session-1%00/builds",
        "/api/sessions/" + "x" * 5000,
    ],
)
def test_missing_or_odd_session_urls_are_404_with_a_message(browser: Browser, path: str) -> None:
    r = browser.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert r.json()["detail"]


def test_a_long_session_still_loads_fast(client, browser: Browser) -> None:
    """200 messages and 50 builds: listing them, and loading the session after a restart,
    stays well under a second each."""
    from app.contracts.session import Message
    from app.session_api import now

    sid, build = box_session(browser)
    state = SESSIONS.by_id[sid]
    for i in range(200):
        role = "user" if i % 2 == 0 else "assistant"
        state.messages.append(
            Message(id=f"msg-x{i}", role=role, created_at=now(), text=f"message {i} " * 40)
        )
    for i in range(50):
        copy = build | {"id": f"build-x{i}"}
        state.builds[copy["id"]] = type(state.builds[build["id"]]).model_validate(copy)
    state.save()
    for path in ("", "/messages", "/builds", "/events?follow=false"):
        t = time.monotonic()
        r = browser.get(f"/api/sessions/{sid}{path}")
        assert r.status_code == 200
        assert time.monotonic() - t < 1.0, path
    restart(client)
    t = time.monotonic()
    assert len(browser.get(f"/api/sessions/{sid}/messages").json()) >= 200
    assert time.monotonic() - t < 2.0
    assert len(browser.builds(sid)) >= 51


# --- Resources --------------------------------------------------------------------------------


def test_past_max_sessions_old_sessions_are_dropped_from_memory_but_not_lost(
    browser: Browser, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_SESSIONS", "3")
    ids = [browser.new_session(title=f"s{i}") for i in range(6)]
    assert len(SESSIONS.by_id) <= 3
    for i, sid in enumerate(ids):  # every one still opens, from the store
        assert browser.get(f"/api/sessions/{sid}").json()["title"] == f"s{i}"
    listed = [s["id"] for s in browser.get("/api/sessions").json() if not s.get("sample")]
    assert listed == ids


def test_a_full_disk_on_upload_is_a_clear_error_not_a_crash(browser: Browser, monkeypatch) -> None:
    sid = browser.new_session()
    state = SESSIONS.by_id[sid]

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(state.store, "write_attachment", full)
    r = browser.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("a.png", b"x", "image/png")}
    )
    assert r.status_code in (503, 507), r.status_code
    assert "space" in r.json()["detail"].lower()
    assert browser.get("/api/health").status_code == 200


def test_a_full_disk_while_saving_a_run_leaves_the_session_usable(
    browser: Browser, claude, monkeypatch
) -> None:
    sid = browser.new_session()
    state = SESSIONS.by_id[sid]
    real = state.store.save if hasattr(state.store, "save") else None
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    if real is None:
        pytest.skip("the store has no save method to fail")
    monkeypatch.setattr(state.store, "save", flaky)
    r = browser.send(sid, "make a box 10 x 10 x 10 mm")
    assert r.status_code in (202, 503, 507), r.status_code
    assert browser.settle(sid) == "idle"
    assert browser.get("/api/health").status_code == 200


def test_a_slow_model_never_slows_other_requests(browser: Browser, other: Browser, claude) -> None:
    claude.delay = 0.5  # half a second per step: a slow API
    sid = browser.new_session()
    assert browser.send(sid, "make a box 40 x 30 x 20 mm").status_code == 202
    theirs = other.new_session()
    slowest = 0.0
    while browser.status(sid) == "running":
        for path in ("/api/health", f"/api/sessions/{sid}", "/api/sessions"):
            t = time.monotonic()
            assert browser.get(path).status_code == 200
            slowest = max(slowest, time.monotonic() - t)
        t = time.monotonic()
        assert other.get(f"/api/sessions/{theirs}").status_code == 200
        slowest = max(slowest, time.monotonic() - t)
        time.sleep(0.05)
    assert slowest < 0.5, slowest
