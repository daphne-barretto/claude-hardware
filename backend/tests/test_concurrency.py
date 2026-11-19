"""Sessions run at the same time, up to the instance's limits, and share them fairly.

One person can run several sessions at once, and several people can each run several. Runs take
the `MAX_AGENT_RUNS` run slots and builds the `BUILD_CONCURRENCY` build slots
(`geometry.capacity`); nothing else makes them wait for each other. When the slots are full the
line is fair across browsers (`geometry.slots.fair_order`): a free slot goes to the browser with
the fewest going, so one person's many sessions can't starve another's, and the chat's
`waiting` notice (`place`, fed by that fair order) says where each run is.

Claude is the scripted stand-in of the end-to-end backend (`app.e2e_api`), timed so the tests
can see which runs overlapped.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_concurrent_users import EXAMPLE, Browser
from test_session_memory import event_data, settle

from app import e2e_api
from app.agent.services import FixtureServices
from app.contracts.common import BoardRef
from app.main import app
from app.owners import OWNER_COOKIE, new_owner
from app.session_api import CONFIG, SESSIONS, fixture_services, queued_line
from geometry import slots
from geometry.slots import FairSlots, fair_order

HOLD = 0.8  # how long each timed run keeps its run slot (s)


class Windows:
    """When each run held its slot: (session ID, start, end)."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, float, float]] = []
        self.started: list[str] = []
        self.lock = threading.Lock()

    def of(self, sids: list[str]) -> list[tuple[float, float]]:
        by_sid = {sid: (start, end) for sid, start, end in self.runs}
        return [by_sid[sid] for sid in sids]


def overlap(windows: list[tuple[float, float]]) -> bool:
    """Whether every window was open at one moment."""
    return max(start for start, _ in windows) < min(end for _, end in windows)


@pytest.fixture
def windows(monkeypatch) -> Windows:
    found = Windows()

    class TimedClaude(e2e_api.StandInClaude):
        """The e2e stand-in, taking a run slot as Claude does and holding it for `HOLD`."""

        def __init__(self, state, body, prompt):
            super().__init__(state, body, prompt)
            self.sid = state.session.id
            self.uses_run_slot = True

        async def _play(self):
            start = time.monotonic()
            with found.lock:
                found.started.append(self.sid)
            await asyncio.sleep(HOLD)
            with found.lock:
                found.runs.append((self.sid, start, time.monotonic()))
            async for message in super()._play():
                yield message

    monkeypatch.setattr(CONFIG, "driver", TimedClaude)
    return found


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)  # fast builds
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


def browser(client: TestClient) -> Browser:
    br = Browser(client)
    br.cookies[OWNER_COOKIE] = new_owner()
    return br


def sessions(br: Browser, n: int, example: bool = False) -> list[str]:
    """`n` new sessions of the browser: empty ones (no first build), or the first example's."""
    body = EXAMPLE if example else {}
    return [br.post("/api/sessions", json=body).json()["id"] for _ in range(n)]


def send(br: Browser, sid: str) -> None:
    r = br.post(f"/api/sessions/{sid}/messages", json={"text": e2e_api.TAKE_SLOT})
    assert r.status_code == 202, r.text


def health_seconds(client: TestClient) -> float:
    start = time.monotonic()
    assert client.get("/api/health").status_code == 200
    return time.monotonic() - start


def test_one_browser_runs_three_sessions_at_once(client, windows, monkeypatch) -> None:
    monkeypatch.setenv("MAX_AGENT_RUNS", "3")
    me = browser(client)
    sids = sessions(me, 3)
    settle(client)
    for sid in sids:
        send(me, sid)
    settle(client)
    assert overlap(windows.of(sids)), windows.runs
    # Each ran to the end, and none waited in line.
    me.get("/api/health")  # the test client's cookies are this browser's
    for sid in sids:
        events = event_data(client, sid)
        assert not [d for t, d in events if t == "notice" and d["kind"] == "waiting"]
        assert any(t == "message_end" for t, _ in events)


def test_two_browsers_run_two_sessions_each_at_once(client, windows, monkeypatch) -> None:
    monkeypatch.setenv("MAX_AGENT_RUNS", "4")
    a, b = browser(client), browser(client)
    mine, theirs = sessions(a, 2), sessions(b, 2)
    settle(client)
    for br, sid in [(a, mine[0]), (b, theirs[0]), (a, mine[1]), (b, theirs[1])]:
        send(br, sid)
    # The server answers at once while all four run.
    time.sleep(HOLD / 4)
    assert max(health_seconds(client) for _ in range(5)) < HOLD / 4
    settle(client)
    assert overlap(windows.of(mine + theirs)), windows.runs


def test_the_line_is_fair_across_browsers(client, windows, monkeypatch) -> None:
    """Two run slots. One browser starts three runs, then another starts one: the second
    browser's run goes next, ahead of the first's third, and each chat says where it is."""
    monkeypatch.setenv("MAX_AGENT_RUNS", "2")
    a, b = browser(client), browser(client)
    mine, theirs = sessions(a, 3), sessions(b, 1)
    settle(client)
    for sid in mine:
        send(a, sid)
        time.sleep(0.05)  # in this order
    deadline = time.monotonic() + 5
    while SESSIONS.place_in_line(SESSIONS.get(mine[2])) != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    send(b, theirs[0])
    deadline = time.monotonic() + 5
    while SESSIONS.place_in_line(SESSIONS.get(theirs[0])) != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert SESSIONS.place_in_line(SESSIONS.get(theirs[0])) == 1
    assert SESSIONS.place_in_line(SESSIONS.get(mine[2])) == 2
    settle(client)
    assert windows.started == [mine[0], mine[1], theirs[0], mine[2]]
    # Never more than two at once.
    edges = sorted([(s, 1) for _, s, _ in windows.runs] + [(e, -1) for _, _, e in windows.runs])
    running = peak = 0
    for _, step in edges:
        running += step
        peak = max(peak, running)
    assert peak == 2

    def queued(br: Browser, sid: str) -> list[str]:
        br.get("/api/health")  # the test client's cookies are this browser's
        events = event_data(client, sid)
        waiting = [d for t, d in events if t == "notice" and d["kind"] == "waiting"]
        assert all(d["place"] and d["reason"] == "run_queue" for d in waiting)
        return [d["text"] for d in waiting]

    # The first browser's third run was next, then moved back one for the other browser's.
    assert queued(a, mine[2]) == [queued_line(1), queued_line(2), queued_line(1)]
    assert queued(b, theirs[0]) == [queued_line(1)]
    assert queued(a, mine[0]) == queued(a, mine[1]) == []


def test_fair_order_round_robins_across_owners() -> None:
    waiting = [("a3", "a"), ("a4", "a"), ("b1", "b"), ("a5", "a"), ("c1", "c"), ("b2", "b")]
    # "a" already has two going, "b" one, "c" none.
    order = fair_order(waiting, Counter({"a": 2, "b": 1}))
    assert order == ["c1", "b1", "a3", "b2", "a4", "a5"]
    # One owner alone: first come, first served.
    assert fair_order([(n, "a") for n in range(4)], Counter()) == [0, 1, 2, 3]


def _hold(pool: FairSlots, owner: str, log: list[str], name: str, go: threading.Event) -> None:
    pool.acquire(owner)
    log.append(name)
    go.wait(5)
    pool.release(owner)


def test_build_slots_go_to_the_owner_with_the_fewest_builds() -> None:
    pool = FairSlots(2)
    log: list[str] = []
    gates = {n: threading.Event() for n in ("a1", "a2", "a3", "a4", "b1")}
    threads = []

    def start(owner: str, name: str) -> None:
        t = threading.Thread(target=_hold, args=(pool, owner, log, name, gates[name]))
        t.start()
        threads.append(t)
        deadline = time.monotonic() + 2
        while name not in log and pool.waiting() < sum(1 for t in threads) - pool.taken:
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)

    for name in ("a1", "a2", "a3", "a4"):
        start("a", name)
    start("b", "b1")
    assert log == ["a1", "a2"] and pool.waiting() == 3
    gates["a1"].set()
    deadline = time.monotonic() + 2
    while len(log) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert log == ["a1", "a2", "b1"], "the other owner's build goes before a third of a's"
    for gate in gates.values():
        gate.set()
    for t in threads:
        t.join(5)
    assert log == ["a1", "a2", "b1", "a3", "a4"] and pool.taken == 0


def test_a_change_sets_parallel_builds_share_the_slots_with_another_browser(monkeypatch) -> None:
    """A change set builds each change in parallel (`app.agent.proposal`), every build in a
    build slot (`geometry.slots`). Another browser's build waiting meanwhile goes next."""
    monkeypatch.setenv(slots.CONCURRENCY_ENV, "2")
    started: list[str] = []

    def build(name: str) -> None:
        with slots.build_slot():
            started.append(name)
            # Long enough that the other browser's build is waiting before the first two end,
            # even on a busy machine.
            time.sleep(1.0)

    async def builds_for(owner: str, names: list[str], after: int = 0) -> None:
        while slots._pool().waiting() < after:  # the other browser's builds wait first
            await asyncio.sleep(0.01)
        slots.slot_owner.set(owner)  # as `app.owners` sets it for a request
        await asyncio.gather(*(asyncio.to_thread(build, n) for n in names))

    async def both() -> None:
        await asyncio.gather(
            builds_for("a", [f"a{n}" for n in range(1, 7)]), builds_for("b", ["b1"], after=4)
        )

    asyncio.run(both())
    assert len(started) == 7
    assert all(n.startswith("a") for n in started[:2]), started
    # a1 and a2 end together, freeing two slots: one goes to b1, the other to a3. First come,
    # first served would have kept b1 waiting behind all six.
    assert started.index("b1") in (2, 3), started
    assert started.index("b1") < started.index("a4"), started


class SlotServices(FixtureServices):
    """The fixture builds, each in a build slot as a real build is (`geometry.sandbox`)."""

    def build(self, design):
        with slots.build_slot():
            return super().build(design)


def test_a_browsers_builds_carry_its_owner(client, monkeypatch) -> None:
    """`app.owners` makes the request's browser the owner of the builds it starts, so the
    build slots can be shared fairly between browsers."""
    seen: list[Any] = []
    original = slots.FairSlots.acquire

    def acquire(self, owner, on_wait=None):
        seen.append(owner)
        return original(self, owner, on_wait)

    monkeypatch.setattr(slots.FairSlots, "acquire", acquire)
    monkeypatch.setattr(
        CONFIG,
        "services",
        lambda board, _: SlotServices(board=BoardRef(name=board.name, revision=board.revision)),
    )
    a, b = browser(client), browser(client)
    sessions(a, 1, example=True), sessions(b, 1, example=True)
    settle(client)
    assert set(seen) == {a.cookies[OWNER_COOKIE], b.cookies[OWNER_COOKIE]}


def test_real_builds_run_at_once_and_the_server_keeps_answering(client, monkeypatch) -> None:
    """Two browsers design from scratch in two sessions each: real sandboxed builds and their
    checks in geometry workers (`geometry_services`). Two build slots: two builds run at once,
    and the server answers a health check at once all the while: nothing in the server
    process waits on geometry or loads the CAD kernel on the event loop."""
    from app.session_api import geometry_services

    monkeypatch.setenv(slots.CONCURRENCY_ENV, "2")
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(CONFIG, "services", geometry_services)
    held, peak, lock = Counter(), [0], threading.Lock()
    acquire, release = slots.FairSlots.acquire, slots.FairSlots.release

    def counting_acquire(self, owner, on_wait=None):
        acquire(self, owner, on_wait)
        with lock:
            held[owner] += 1
            peak[0] = max(peak[0], sum(held.values()))

    def counting_release(self, owner):
        with lock:
            held[owner] -= 1
        release(self, owner)

    monkeypatch.setattr(slots.FairSlots, "acquire", counting_acquire)
    monkeypatch.setattr(slots.FairSlots, "release", counting_release)
    a, b = browser(client), browser(client)
    work = [(br, sid) for br in (a, b) for sid in sessions(br, 2)]
    for n, (br, sid) in enumerate(work):
        text = f"make a box {30 + n} x 20 x 10 mm with 2 mm walls"
        assert br.post(f"/api/sessions/{sid}/messages", json={"text": text}).status_code == 202
    slowest, deadline = 0.0, time.monotonic() + 300
    while time.monotonic() < deadline:
        slowest = max(slowest, health_seconds(client))
        states = [SESSIONS.by_id[sid] for _, sid in work]
        # Each run ends, or asks whether to keep a box that fails a standard (no draft): the
        # builds are made either way.
        if all(
            (s.run is not None and s.run.done()) or s.session.status == "waiting_for_answer"
            for s in states
        ):
            break
        time.sleep(0.05)
    for br, sid in work:
        br.get("/api/health")  # the test client's cookies are this browser's
        assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"], sid
    assert peak[0] == 2, f"{peak[0]} builds ran at once"
    assert slowest < 1.0, f"a health check took {slowest:.2f} s while builds ran"
