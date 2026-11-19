"""New chats keep the server's memory bounded.

QA's `Fail the run if a new chat fails on staging` step runs `.github/scripts/session_smoke.py`
against staging: create a session, post a message. On the free plan (512 MB) the backend died
after a few back-to-back new chats, because every message started its own Claude CLI process
(about 250 MB each) and every new session built at the same time as the others. These tests run
the same smoke test against the app, with a stand-in for the CLI that counts how many are alive.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.agent.run as agent_run
from app.agent.services import FixtureServices
from app.contracts.common import REPO_ROOT
from app.examples import examples
from app.main import app
from app.session_api import CONFIG, SESSIONS
from geometry import slots

ASK = "ask me something"
# A session on the first example, so it has a first build (an empty session has none).
EXAMPLE = {"example": examples()[0].id}


def _session_smoke() -> Any:
    spec = importlib.util.spec_from_file_location(
        "session_smoke", REPO_ROOT / ".github" / "scripts" / "session_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session_smoke = _session_smoke()


@dataclass
class FakeCLI:
    """Stands in for the Claude CLI the SDK starts for each run: counts the live ones."""

    seconds: float = 1.0  # longer than a new chat takes to start, so runs would overlap
    live: int = 0
    peak: int = 0
    runs: int = 0
    asker: Any = None  # the workspace whose run asks a question and waits for the answer
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def __call__(self, prompt: str, options: Any):
        with self.lock:
            self.live += 1
            self.runs += 1
            self.peak = max(self.peak, self.live)
        try:
            if ASK in prompt and self.asker is not None:
                options_ = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
                await self.asker.ask_user({"question": "Which?", "options": options_})
            else:
                await asyncio.sleep(self.seconds)
        finally:
            with self.lock:
                self.live -= 1
        if False:  # an async generator, like the SDK's driver
            yield None


@pytest.fixture
def cli(monkeypatch) -> FakeCLI:
    fake = FakeCLI()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(CONFIG, "driver", lambda state, body, prompt: None)  # the SDK
    monkeypatch.setattr(agent_run, "sdk_driver", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAX_AGENT_RUNS", "1")  # the free plan's (unset, sized from the machine)
    monkeypatch.setenv("BUILD_CONCURRENCY", "1")
    monkeypatch.delenv("MAX_SESSIONS", raising=False)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def sender(client: TestClient):
    """`session_smoke.request`, through the test client."""

    def send(method: str, url: str, body: Any) -> tuple[int, str]:
        r = client.request(method, url, json=body)
        return r.status_code, r.text

    return send


def settle(client: TestClient, seconds: float = 30) -> list[dict]:
    """The sessions, once none is running."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sessions = client.get("/api/sessions").json()
        tasks = [t for st in SESSIONS.by_id.values() for t in (st.initial, st.run) if t]
        if all(s["status"] == "idle" for s in sessions) and all(t.done() for t in tasks):
            return sessions
        time.sleep(0.05)
    raise AssertionError(f"sessions still running: {client.get('/api/sessions').json()}")


def event_data(client: TestClient, sid: str) -> list[tuple[str, dict]]:
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"})
    out = []
    for frame in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_back_to_back_new_chats_run_one_claude_at_a_time(client, cli) -> None:
    """What the QA step runs, ten times on one instance (QA, e2e and the demo walk add up):
    every new chat starts and takes its message, and only one Claude CLI is alive at once."""
    for _ in range(10):
        assert session_smoke.smoke("http://testserver", send=sender(client)) == []
        assert client.get("/api/health").status_code == 200
    settle(client)
    assert cli.runs == 10
    assert cli.peak == 1, f"{cli.peak} Claude CLI processes ran at once"


@pytest.mark.parametrize("build_slots", [1, 3])
def test_concurrent_new_chats_build_within_the_build_slots(client, monkeypatch, build_slots):
    """New chats' first builds take the build slots (`BUILD_CONCURRENCY`) as every build does,
    so no more than that many run at once, and nothing else holds them to fewer."""
    monkeypatch.setenv("BUILD_CONCURRENCY", str(build_slots))
    live, peak, lock = [0], [0], threading.Lock()

    class SlowBuilds(FixtureServices):
        def build(self, design):
            with slots.build_slot():  # as a sandboxed build does (`geometry.sandbox`)
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                try:
                    time.sleep(1.0)  # long enough to overlap on a busy machine
                    return super().build(design)
                finally:
                    with lock:
                        live[0] -= 1

    monkeypatch.setattr(CONFIG, "services", lambda board, standards: SlowBuilds())
    with ThreadPoolExecutor(5) as pool:
        codes = list(
            pool.map(lambda _: client.post("/api/sessions", json=EXAMPLE).status_code, range(5))
        )
    assert codes == [201] * 5
    sessions = settle(client)
    assert all(s["latest_build_id"] for s in sessions if not s.get("sample"))
    # Never more than the slots; with more than one, builds overlap (each new chat's geometry
    # worker for the example's parameters takes a slot too, so not always all of them).
    assert peak[0] <= build_slots, f"{peak[0]} builds ran at once"
    assert peak[0] >= min(build_slots, 2), f"only {peak[0]} build ran at once"


def test_a_run_waiting_for_an_answer_gives_its_slot_up(client, cli) -> None:
    first = client.post("/api/sessions", json={}).json()["id"]
    second = client.post("/api/sessions", json={}).json()["id"]
    settle(client)
    cli.asker = SESSIONS.get(first).ws
    assert client.post(f"/api/sessions/{first}/messages", json={"text": ASK}).status_code == 202
    for _ in range(200):
        if client.get(f"/api/sessions/{first}").json()["status"] == "waiting_for_answer":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("the first run never asked")
    r = client.post(f"/api/sessions/{second}/messages", json={"text": "make a box"})
    assert r.status_code == 202
    settle(client)
    assert cli.runs == 2 and cli.peak == 1
    (error,) = [d for t, d in event_data(client, first) if t == "error"]
    assert error["reason"] == "preempted" and "someone else's request" in error["message"]
    # The stopped session takes a new message.
    r = client.post(f"/api/sessions/{first}/messages", json={"text": "make a box"})
    assert r.status_code == 202
    settle(client)


def test_sessions_beyond_the_cap_drop_the_oldest_idle_ones(client, monkeypatch) -> None:
    from app.session_api import SESSIONS

    monkeypatch.setenv("MAX_SESSIONS", "3")
    made = []
    for _ in range(5):
        made.append(client.post("/api/sessions", json={}).json()["id"])
        settle(client)
    assert len(set(made)) == 5
    # Only the newest are kept in memory; the dropped ones stay saved and load on demand.
    assert len(SESSIONS.by_id) <= 3
    listed = [s["id"] for s in client.get("/api/sessions").json() if not s.get("sample")]
    assert listed == made
    assert len(SESSIONS.by_id) <= 3
    assert client.get(f"/api/sessions/{made[0]}").status_code == 200
