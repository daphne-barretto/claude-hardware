"""Several people use the app at once.

Each browser has its own sessions (`app.owners`, a cookie): another browser can't list, open or
change them. Runs that find every run slot taken (`MAX_AGENT_RUNS`) wait in line, and the chat
shows each one's place in line (fair across browsers: `test_concurrency.py`). The browser test
of the same, with two browser contexts running the demo, is
`frontend/e2e/live-users.spec.ts`.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_session_memory import cli, event_data, settle

from app.examples import examples
from app.main import app
from app.owners import OWNER_COOKIE, new_owner, owner_from
from app.session_api import CONFIG, SESSIONS, fixture_services, queued_line, session_store

__all__ = ["cli"]  # the stand-in for the Claude CLI, a fixture

# A session on the first example (demos/b-parasite/), so it has an existing design and a first
# build.
EXAMPLE = {"example": examples()[0].id}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAX_AGENT_RUNS", "1")  # unset, it is sized from the machine
    monkeypatch.setattr(CONFIG, "services", fixture_services)  # fast builds
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


class Browser:
    """One browser on the shared test client: its own cookie jar, as a browser has."""

    def __init__(self, client: TestClient):
        self.client = client
        self.cookies: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.client.cookies.clear()
        for name, value in self.cookies.items():
            self.client.cookies.set(name, value)
        r = self.client.request(method, url, **kwargs)
        if OWNER_COOKIE in r.cookies:
            self.cookies[OWNER_COOKIE] = r.cookies[OWNER_COOKIE]
        return r

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


def own(browser: Browser) -> list[str]:
    """The sessions the browser lists as its own: every browser also sees the read-only samples
    (`app.samples`), which belong to nobody."""
    return [s["id"] for s in browser.get("/api/sessions").json() if not s.get("sample")]


def test_every_browser_lists_the_samples_but_only_its_own_sessions(client) -> None:
    a, b = Browser(client), Browser(client)
    mine = a.post("/api/sessions", json=EXAMPLE).json()["id"]
    settle(client)
    samples = [s["id"] for s in b.get("/api/sessions").json() if s.get("sample")]
    assert samples == [s["id"] for s in a.get("/api/sessions").json() if s.get("sample")]
    assert own(a) == [mine] and own(b) == []


def test_each_browser_gets_an_owner_cookie(client) -> None:
    r = client.get("/api/health")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{OWNER_COOKIE}=") and "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    owner = r.cookies[OWNER_COOKIE]
    assert owner_from(f"{OWNER_COOKIE}={owner}") == owner
    # A browser that has one keeps it.
    assert "set-cookie" not in client.get("/api/health").headers
    assert owner_from(f"{OWNER_COOKIE}=../../etc") is None
    assert owner_from('bad"cookie') is None


def test_browsers_never_see_each_others_sessions(client) -> None:
    a, b = Browser(client), Browser(client)
    mine = a.post("/api/sessions", json=EXAMPLE).json()["id"]
    theirs = b.post("/api/sessions", json=EXAMPLE).json()["id"]
    settle(client)
    assert own(a) == [mine]
    assert own(b) == [theirs]
    assert a.get(f"/api/sessions/{mine}/builds").json(), "the session has its first build"

    for url in ("", "/messages", "/builds", "/parameters", "/changes", "/comments"):
        assert b.get(f"/api/sessions/{mine}{url}").status_code == 404, url
    events = b.get(f"/api/sessions/{mine}/events", params={"follow": "false"})
    assert events.status_code == 404
    r = b.post(f"/api/sessions/{mine}/messages", json={"text": "make a box"})
    assert r.status_code == 404
    build = a.get(f"/api/sessions/{mine}/builds").json()[-1]["id"]
    assert b.get(f"/api/sessions/{mine}/builds/{build}/mesh").status_code == 404
    assert a.get(f"/api/sessions/{mine}/builds/{build}/mesh").status_code == 200

    # The owner is saved with the session: after it leaves memory, the same holds.
    SESSIONS.by_id.clear()
    assert session_store().owners()[mine] == a.cookies[OWNER_COOKIE]
    assert own(b) == [theirs]
    assert mine not in SESSIONS.by_id, "listing doesn't load other browsers' sessions"
    assert own(a) == [mine]
    assert b.get(f"/api/sessions/{mine}").status_code == 404
    assert a.get(f"/api/sessions/{mine}").status_code == 200
    # A browser with no cookie yet is a new browser: it sees nothing.
    assert own(Browser(client)) == []


def test_a_session_saved_without_an_owner_is_nobodys(client) -> None:
    """It used to go to the first browser that opened it; on a public demo anyone could, so it
    belongs to nobody (`docs/security.md`)."""
    a, b = Browser(client), Browser(client)
    sid = a.post("/api/sessions", json=EXAMPLE).json()["id"]
    settle(client)
    state = SESSIONS.get(sid)
    state.owner = None  # saved before sessions had owners
    state.save()
    SESSIONS.by_id.clear()
    assert own(b) == [] and own(a) == []
    assert b.get(f"/api/sessions/{sid}").status_code == 404
    assert a.get(f"/api/sessions/{sid}").status_code == 404
    assert own(b) == []


def test_runs_beyond_the_slots_wait_in_line_and_show_their_place(client, cli) -> None:
    cli.seconds = 0.6
    browsers = [Browser(client) for _ in range(3)]
    sids = [br.post("/api/sessions", json=EXAMPLE).json()["id"] for br in browsers]
    settle(client)
    for n, (br, sid) in enumerate(zip(browsers, sids, strict=True)):
        r = br.post(f"/api/sessions/{sid}/messages", json={"text": f"make box {n}"})
        assert r.status_code == 202
        time.sleep(0.1)  # in this order
    # While the first runs, the others wait in line: 1st and 2nd.
    places = {}
    deadline = time.monotonic() + 10
    while len(places) < 2 and time.monotonic() < deadline:
        for sid in sids[1:]:
            place = SESSIONS.place_in_line(SESSIONS.get(sid))
            if place is not None:
                places[sid] = place
        time.sleep(0.02)
    assert places == {sids[1]: 1, sids[2]: 2}
    settle(client)
    assert cli.runs == 3 and cli.peak == 1

    queued, ends, errors = [], [], []
    for br, sid in zip(browsers, sids, strict=True):
        br.get("/api/health")  # the test client's cookies are this browser's
        events = event_data(client, sid)
        waiting = [d for t, d in events if t == "notice" and d["kind"] == "waiting"]
        assert all(d["reason"] == "run_queue" for d in waiting)
        queued.append([d["text"] for d in waiting])
        ends.append(sum(t == "message_end" for t, _ in events))
        errors += [d for t, d in events if t == "error"]
    assert queued == [[], [queued_line(1)], [queued_line(2), queued_line(1)]]
    assert queued_line(2) == (
        "Waiting for 1 request ahead of you. Claude starts on this when it's your turn; you "
        "move up as they finish."
    )
    # Every queued run ran, and the queue is empty again.
    assert all(ends) and not errors
    assert SESSIONS.queue == [] and SESSIONS.running == set()


def test_a_run_stopped_while_in_line_leaves_the_line(client, cli) -> None:
    cli.seconds = 1.0
    a, b, c = Browser(client), Browser(client), Browser(client)
    first, second, third = (br.post("/api/sessions", json=EXAMPLE).json()["id"] for br in (a, b, c))
    settle(client)
    a.post(f"/api/sessions/{first}/messages", json={"text": "one"})
    time.sleep(0.1)
    b.post(f"/api/sessions/{second}/messages", json={"text": "two"})
    time.sleep(0.1)
    c.post(f"/api/sessions/{third}/messages", json={"text": "three"})
    deadline = time.monotonic() + 5
    while SESSIONS.place_in_line(SESSIONS.get(third)) != 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    run = SESSIONS.get(second).run
    assert run is not None
    client.portal.call(run.cancel)  # type: ignore[union-attr]
    deadline = time.monotonic() + 5
    while SESSIONS.place_in_line(SESSIONS.get(third)) != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert SESSIONS.place_in_line(SESSIONS.get(third)) == 1
    deadline = time.monotonic() + 10
    while SESSIONS.get(third).session.status != "idle" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert SESSIONS.queue == [], "the stopped run left the line"
    assert cli.runs == 2


def test_each_browser_has_a_distinct_owner() -> None:
    assert len({new_owner() for _ in range(100)}) == 100


def test_the_e2e_backend_queues_scripted_runs_that_take_a_slot(client, monkeypatch) -> None:
    """What `frontend/e2e/live-users.spec.ts` does in two browsers: one run holds the slot
    until another waits in line, which shows its place, then runs."""
    monkeypatch.setattr(CONFIG, "driver", CONFIG.driver)  # importing e2e_api swaps it
    from app import e2e_api

    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(e2e_api, "HOLD_SECONDS", 0.5)
    a, b = Browser(client), Browser(client)
    first, second = (br.post("/api/sessions", json=EXAMPLE).json()["id"] for br in (a, b))
    settle(client)
    a.post(f"/api/sessions/{first}/messages", json={"text": e2e_api.HOLD_SLOT})
    deadline = time.monotonic() + 10
    while not SESSIONS.running and time.monotonic() < deadline:
        time.sleep(0.02)
    b.post(f"/api/sessions/{second}/messages", json={"text": e2e_api.TAKE_SLOT})
    settle(client)
    b.get("/api/health")
    events = event_data(client, second)
    lines = [d["text"] for t, d in events if t == "notice"]
    assert queued_line(1) in lines
    assert any("Had a run slot." in d["delta"] for t, d in events if t == "text")
    a.get("/api/health")
    texts = [d["delta"] for t, d in event_data(client, first) if t == "text"]
    assert e2e_api.HOLDING in texts and any("Had a run slot." in t for t in texts)
    # Other scripted runs take no slot.
    assert not e2e_api.StandInClaude(SESSIONS.get(first), _body("hi"), "hi").uses_run_slot


def _body(text: str) -> Any:
    from app.contracts.session import MessageCreate

    return MessageCreate(text=text)
