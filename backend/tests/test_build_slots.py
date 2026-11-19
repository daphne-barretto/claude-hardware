"""Builds per instance and their memory: at most `BUILD_CONCURRENCY` builds run at
once and the rest queue, saying so in the chat; a build may use `BUILD_MEMORY_MB` and fails
with "ran out of memory" past it, without taking the service down."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import threading
import time

import pytest

from app.agent.events import EventSink
from app.agent.run import scenario_workspace
from app.agent.services import BuildOutcome, make_build
from app.agent.tools import QUEUED
from geometry import capacity, slots
from geometry.sandbox import run_build

BOX = "from build123d import Box\nresult = Box(1, 1, 1)\n"
# Allocates 300 MB, then returns a shape.
BIG = "blob = bytearray(300 * 1024 * 1024)\n" + BOX

# A build out of memory is stopped by its address-space cap (RLIMIT_AS), which macOS doesn't
# enforce (local builds there run with GEOMETRY_SANDBOX_KERNEL_LIMITS=optional).
LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="macOS has no address-space cap: Linux only"
)


def test_the_limits_come_from_configuration(monkeypatch):
    monkeypatch.delenv(slots.CONCURRENCY_ENV, raising=False)
    monkeypatch.delenv(slots.MEMORY_ENV, raising=False)
    # Unset: sized from the machine (`geometry.capacity`, tests/test_capacity.py).
    assert slots.concurrency() == capacity.capacity().build_slots >= 1
    assert slots.build_memory_mb() == slots.DEFAULT_MEMORY_MB
    # A 2 GB instance also holds the server, Next.js, the agent CLI and the build's libraries.
    assert slots.DEFAULT_MEMORY_MB <= 1024
    monkeypatch.setenv(slots.CONCURRENCY_ENV, "3")
    monkeypatch.setenv(slots.MEMORY_ENV, "512")
    assert slots.concurrency() == 3
    assert slots.build_memory_mb() == 512
    for bad in ("0", "-1", "lots"):
        monkeypatch.setenv(slots.MEMORY_ENV, bad)
        with pytest.raises(ValueError, match=slots.MEMORY_ENV):
            slots.build_memory_mb()


class SlowServices:
    """Builds that take a while inside a build slot, counting how many run at once."""

    def __init__(self, board) -> None:
        self.board = board
        self.running = 0
        self.most = 0
        self.lock = threading.Lock()

    def build(self, design) -> BuildOutcome:
        with slots.build_slot():
            with self.lock:
                self.running += 1
                self.most = max(self.most, self.running)
            time.sleep(0.4)
            with self.lock:
                self.running -= 1
        return BuildOutcome(make_build("build-1", self.board), shapes=design)


@pytest.fixture(scope="module")
def workspace():
    return scenario_workspace()


def _two_builds(workspace, services) -> list[list[str]]:
    sinks = [EventSink("msg-1"), EventSink("msg-2")]
    spaces = [
        dataclasses.replace(workspace, services=services, sink=sink, _lock=asyncio.Lock())
        for sink in sinks
    ]

    async def both():
        return await asyncio.gather(*(ws.build({}) for ws in spaces))

    for out in asyncio.run(both()):
        assert not out.get("is_error"), out
    # The chat's lines: progress, and the `waiting` notice of a build in line (by its text).
    return [[e.text for _, e in sink.events if e.type in ("progress", "notice")] for sink in sinks]


def test_one_build_runs_at_a_time_and_the_next_is_queued_in_the_chat(workspace, monkeypatch):
    monkeypatch.setenv(slots.CONCURRENCY_ENV, "1")
    services = SlowServices(workspace.board)
    progress = _two_builds(workspace, services)
    assert services.most == 1
    queued = [p for p in progress if QUEUED in p]
    assert len(queued) == 1, progress
    # The chat says it is waiting after saying it is building, and before the build is ready.
    assert queued[0].index("Building") < queued[0].index(QUEUED)
    assert QUEUED == "Waiting for another build to finish. This one starts right after."


def test_the_configured_number_of_builds_run_at_once(workspace, monkeypatch):
    monkeypatch.setenv(slots.CONCURRENCY_ENV, "2")
    services = SlowServices(workspace.board)
    progress = _two_builds(workspace, services)
    assert services.most == 2
    assert not any(QUEUED in p for p in progress)


def test_a_sandboxed_build_waits_for_a_free_slot(monkeypatch):
    monkeypatch.setenv(slots.CONCURRENCY_ENV, "1")
    queued = threading.Event()
    results = []

    def build():
        slots.on_queued.set(queued.set)
        results.append(run_build(BOX))

    with slots.build_slot():
        thread = threading.Thread(target=build)
        thread.start()
        assert queued.wait(10), "the build never said it was queued"
        time.sleep(0.5)
        assert thread.is_alive() and not results, "the build ran while another held the slot"
    thread.join(60)
    assert results and results[0].ok, results


@LINUX_ONLY
def test_the_build_memory_limit_comes_from_build_memory_mb(monkeypatch):
    monkeypatch.setenv(slots.MEMORY_ENV, "200")
    result = run_build(BIG, timeout_s=20)
    assert result.error is not None and result.error.kind == "memory", result.error
    assert result.error.message == "the build ran out of memory"
    monkeypatch.setenv(slots.MEMORY_ENV, "1024")
    result = run_build(BIG, timeout_s=20)
    assert result.ok, result.error


@LINUX_ONLY
def test_a_build_out_of_memory_fails_in_the_chat_and_the_service_carries_on(workspace, monkeypatch):
    monkeypatch.setenv(slots.MEMORY_ENV, "200")
    sink = EventSink("msg-1")
    ws = dataclasses.replace(
        workspace,
        design=dataclasses.replace(workspace.design),
        sink=sink,
        _lock=asyncio.Lock(),
    )
    ws.design.code = workspace.design.code + "\n_blob = bytearray(300 * 1024 * 1024)\n"
    out = asyncio.run(ws.build({}))
    assert out.get("is_error")
    assert "the build ran out of memory" in out["content"][0]["text"]
    (ready,) = sink.of_type("build_ready")
    assert ready.build.status == "failed"
    # The next build, within the limit, works.
    ok = dataclasses.replace(workspace, sink=EventSink("msg-2"), _lock=asyncio.Lock())
    assert not asyncio.run(ok.build({})).get("is_error")
