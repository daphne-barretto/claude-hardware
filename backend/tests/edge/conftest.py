"""Shared setup for the edge-case suite (`docs/security.md`, `make edge`).

The suite runs the real app (`app.main`) over HTTP with the model scripted: never the real
Claude. `client` serves the board example on the contract fixtures (fast) and builds a design
without a board for real in the sandbox. `claude` is the scripted model: by default it plays
the e2e backend's script (`app.e2e_api.script`), and a test can queue its own steps.

Each `Browser` is one owner (`app.owners`): its own cookie, as a second browser would have.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.scripted import ScriptedDriver
from app.examples import examples
from app.main import app
from app.owners import OWNER_COOKIE, new_owner
from app.session_api import CONFIG, SESSIONS, fixture_services

# Importing the e2e backend swaps CONFIG's driver and services for its own; the suite uses
# its stand-in Claude (`Claude` below) but keeps CONFIG as the tests set it.
_saved = (CONFIG.driver, CONFIG.services)
from app import e2e_api  # noqa: E402

CONFIG.driver, CONFIG.services = _saved

EXAMPLE = examples()[0].id
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _sandbox_without_kernel_limits(monkeypatch):
    """The build sandbox's kernel limits exist only on Linux (`docs/architecture.md`); elsewhere the
    builds run without them, as local development does."""
    if not sys.platform.startswith("linux"):
        monkeypatch.setenv("GEOMETRY_SANDBOX_KERNEL_LIMITS", "optional")


class Claude:
    """The scripted model. Each run plays the next queued script (a list of steps, or a callable
    of the workspace); with none queued it plays `app.e2e_api.script`. Every prompt and the
    agent's options are recorded."""

    def __init__(self) -> None:
        self.scripts: list[Any] = []
        self.prompts: list[str] = []
        self.options: list[Any] = []
        self.drivers: list[ScriptedDriver] = []
        self.delay = 0.0  # seconds before each step: a slow model

    def __call__(self, state, body, prompt):
        self.prompts.append(prompt)
        recorder = self

        if self.scripts:
            steps = self.scripts.pop(0)

            class Queued(ScriptedDriver):
                def __call__(self, prompt_: str, options: Any):
                    recorder.options.append(options)
                    if callable(steps):
                        self.steps = steps(state.ws)
                    return self._slow(super().__call__(prompt_, options))

                async def _slow(self, stream):
                    async for message in stream:
                        if recorder.delay:
                            import asyncio

                            await asyncio.sleep(recorder.delay)
                        yield message

            driver: ScriptedDriver = Queued(state.ws, steps if not callable(steps) else [])
        else:

            class StandIn(e2e_api.StandInClaude):
                def __call__(self, prompt_: str, options: Any):
                    recorder.options.append(options)
                    return self._slow(super().__call__(prompt_, options))

                async def _slow(self, stream):
                    async for message in stream:
                        if recorder.delay:
                            import asyncio

                            await asyncio.sleep(recorder.delay)
                        yield message

            driver = StandIn(state, body, prompt)
        self.drivers.append(driver)
        return driver


@pytest.fixture
def claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch, claude):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


class Browser:
    """One browser: requests carry its own owner cookie."""

    def __init__(self, client: TestClient, owner: str | None = None) -> None:
        self.client = client
        self.owner = owner or new_owner()

    def request(self, method: str, url: str, **kwargs: Any):
        self.client.cookies.clear()
        self.client.cookies.set(OWNER_COOKIE, self.owner)
        return self.client.request(method, url, **kwargs)

    def get(self, url: str, **kw: Any):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any):
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any):
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw: Any):
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw: Any):
        return self.request("DELETE", url, **kw)

    # --- Sessions -------------------------------------------------------------------------

    def new_session(self, **body: Any) -> str:
        r = self.post("/api/sessions", json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def example_session(self) -> str:
        """The board example, once its first build has arrived."""
        sid = self.new_session(example=EXAMPLE)
        wait_for(lambda: any(t == "build_ready" for _, t, _ in self.events(sid)), "first build")
        return sid

    def events(self, sid: str, after: int = 0) -> list[tuple[int, str, dict]]:
        r = self.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
        assert r.status_code == 200, r.text
        return parse_sse(r.text)

    def status(self, sid: str) -> str:
        return self.get(f"/api/sessions/{sid}").json()["status"]

    def send(self, sid: str, text: str, **body: Any):
        return self.post(f"/api/sessions/{sid}/messages", json={"text": text, **body})

    def settle(self, sid: str, timeout: float = 300, keep: bool = True) -> str:
        """Wait until nothing runs in the session; its status then. `keep`: the stand-in asks
        before finishing with a failing standard (`app.e2e_api`, a box has no draft); keep the
        design, as a user would, so the session goes idle."""
        status = self._settle(sid, timeout)
        while keep and status == "waiting_for_answer":
            question = [d for _, t, d in self.events(sid) if t == "question"][-1]
            if not question["text"].startswith("The design fails"):
                break
            answer = {"question_id": question["question_id"], "option_id": "keep"}
            r = self.post(f"/api/sessions/{sid}/messages", json={"text": "", "answer": answer})
            assert r.status_code == 202, r.text
            status = self._settle(sid, timeout)
        return status

    def _settle(self, sid: str, timeout: float) -> str:

        def settled() -> bool:
            if self.status(sid) == "running":
                return False
            found = self.events(sid)
            started = {d["message_id"] for _, t, d in found if t == "message_start"}
            ended = {d["message_id"] for _, t, d in found if t == "message_end"}
            asked = {d["message_id"] for _, t, d in found if t == "question"}
            return started <= ended | asked

        wait_for(settled, f"session {sid} to settle", timeout)
        return self.status(sid)

    def builds(self, sid: str) -> list[dict]:
        return self.get(f"/api/sessions/{sid}/builds").json()


@pytest.fixture
def browser(client) -> Browser:
    return Browser(client)


@pytest.fixture
def other(client) -> Browser:
    """A second browser, with its own owner cookie."""
    return Browser(client)


def parse_sse(text: str) -> list[tuple[int, str, dict]]:
    out = []
    for frame in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in frame.splitlines() if line and not line.startswith(":")
        )
        if fields:
            out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return out


def wait_for(ready: Callable[[], bool], what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def box_session(browser: Browser) -> tuple[str, dict]:
    """A design without a board, built for real in the sandbox: the scripted model's box."""
    sid = browser.new_session()
    r = browser.send(sid, "make a box 40 x 30 x 20 mm with 2 mm walls")
    assert r.status_code == 202, r.text
    browser.settle(sid)
    builds = browser.builds(sid)
    assert builds, browser.events(sid)
    return sid, builds[-1]
