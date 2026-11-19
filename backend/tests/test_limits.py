"""Limits and failures, said plainly (`app.agent.limits`): every API error a run can hit ends in
a plain message (no status codes, raw errors or internal names) that says what happened,
whether anything was lost and what to do; temporary ones are retried first, with a visible
notice and backoff, and the work so far is kept.

Claude is never called: `app.agent.faults.FlakyDriver` sends what the Claude Code CLI sends
when an API call fails, and the backoff runs on a fake clock.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_agent import workspace

from app.agent import limits
from app.agent.faults import FAULTS, FlakyDriver, failure_messages
from app.agent.limits import RetryPolicy, classify_exception, classify_message, stopped_text
from app.agent.run import RESUME_NOTE, RunConfig, run_agent
from app.agent.scripted import ScriptedDriver
from app.contracts.session import validate_recording

# Words that must never reach the chat for a limit or failure.
INTERNAL = (
    "API Error",
    "429",
    "529",
    "401",
    "rate_limit_error",
    "overloaded_error",
    "invalid_request_error",
    "x-api-key",
    "/login",
    "ECONNRESET",
    "slot",
    "worker",
    "sandbox",
    "Traceback",
    "Exception",
    "Error:",
)


class FakeClock:
    """`RetryPolicy.sleep`: records each wait instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def policy(attempts: int = 2, clock: FakeClock | None = None) -> RetryPolicy:
    return RetryPolicy(
        attempts=attempts, cli_retries=4, base_s=10, cap_s=60, sleep=clock or FakeClock()
    )


def said(ws) -> str:
    """Everything the chat shows for the run, apart from Claude's own text."""
    shown = [e.message for e in ws.sink.of_type("error")] + [
        e.text for e in ws.sink.of_type("notice")
    ]
    return "\n".join(shown)


def assert_plain(text: str) -> None:
    for word in INTERNAL:
        assert word not in text, f"{word!r} reached the chat: {text}"


def run(ws, driver, config: RunConfig | None = None, prompt: str = "Make the lid 2 mm thicker."):
    return asyncio.run(run_agent(ws, prompt, config or RunConfig(retry=policy()), driver=driver))


DONE = [("text", "Back on it."), ("finish", {"outcome": "done", "summary": "Done."})]


# --- Classifying what the CLI reports ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("rate_limit", "rate_limited"),
        ("overloaded", "overloaded"),
        ("billing", "billing"),
        ("auth", "not_configured"),
        ("too_large", "too_large"),
        ("timeout", "network"),
    ],
)
def test_each_api_error_is_classified(fault, reason) -> None:
    assistant, result = failure_messages(fault)
    assert classify_message(assistant).reason == reason
    assert classify_message(result).reason == reason


def test_a_dead_cli_or_dropped_connection_is_a_temporary_network_failure() -> None:
    for exc in (TimeoutError("read timed out"), ConnectionResetError("reset by peer")):
        failure = classify_exception(exc)
        assert failure.reason == "network" and failure.transient
    assert classify_exception(ValueError("bad")).reason == "unknown"


def test_retry_after_is_read_from_the_error() -> None:
    (assistant, _) = failure_messages("rate_limit")
    assert classify_message(assistant).retry_after == 20


# --- What the chat says for each API error ----------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "reason", "retryable", "words"),
    [
        ("billing", "billing", True, "run out of credits"),
        ("auth", "not_configured", False, "isn't set up on this server"),
        ("too_large", "too_large", False, "grown too long"),
    ],
)
def test_a_lasting_api_error_stops_at_once_with_a_plain_message(fault, reason, retryable, words):
    ws, clock = workspace(), FakeClock()
    result = run(ws, FlakyDriver(fault), RunConfig(retry=policy(clock=clock)))
    (error,) = ws.sink.of_type("error")
    assert error.message == stopped_text(reason) and words in error.message
    assert error.reason == reason and error.retryable is retryable
    assert "kept" in error.message  # whether anything was lost
    assert clock.waits == [] and not ws.sink.of_type("notice"), "not retried"
    assert result.error == error.message
    assert_plain(said(ws))
    assert ws.sink.types[-2:] == ["error", "message_end"]
    validate_recording(ws.sink.recording())


def test_the_cli_retrying_a_rate_limit_shows_each_retry_and_then_goes_through() -> None:
    ws = workspace()
    then = ScriptedDriver(ws, DONE)
    driver = FlakyDriver("rate_limit", then, cli_retries=2, retry_delay_ms=20_000, recover=True)
    result = run(ws, driver)
    notices = ws.sink.of_type("notice")
    assert [(n.kind, n.reason, n.retry_in_s, n.attempt, n.attempts) for n in notices] == [
        ("retrying", "rate_limited", 20.0, 1, 2),
        ("retrying", "rate_limited", 20.0, 2, 2),
    ]
    assert notices[0].text == limits.retrying_text("rate_limited", 1, 2)
    assert "try 1 of 2" in notices[0].text and "Nothing is lost" in notices[0].text
    assert result.error is None and result.finished["outcome"] == "done"
    assert not ws.sink.of_type("error")
    assert_plain(said(ws))


def test_when_the_cli_gives_up_on_a_rate_limit_the_run_stops_with_retry() -> None:
    ws, clock = workspace(), FakeClock()
    driver = FlakyDriver("rate_limit", cli_retries=4, retry_delay_ms=5000)
    run(ws, driver, RunConfig(retry=policy(clock=clock)))
    assert len(ws.sink.of_type("notice")) == 4
    (error,) = ws.sink.of_type("error")
    # The CLI already waited and retried: the run doesn't add retries of its own.
    assert clock.waits == [] and len(driver.prompts) == 1
    assert error.message == stopped_text("rate_limited", 5)
    assert "after 5 tries" in error.message and "Retry" in error.message and error.retryable
    assert_plain(said(ws))


def test_an_overload_the_cli_didnt_retry_is_retried_with_backoff() -> None:
    ws, clock = workspace(), FakeClock()
    then = ScriptedDriver(ws, DONE)
    driver = FlakyDriver("overloaded", then, failures=2)
    result = run(ws, driver, RunConfig(retry=policy(attempts=3, clock=clock)))
    assert clock.waits == [10, 20]  # doubled each time
    notices = ws.sink.of_type("notice")
    assert [(n.attempt, n.attempts, n.retry_in_s) for n in notices] == [(1, 3, 10), (2, 3, 20)]
    assert result.error is None and result.finished["outcome"] == "done"
    assert len(driver.prompts) == 3 and all(RESUME_NOTE in p for p in driver.prompts[1:])


def test_backoff_is_capped_and_honours_retry_after() -> None:
    p = RetryPolicy(attempts=5, base_s=10, cap_s=60, sleep=FakeClock())
    assert [p.delay(n) for n in range(1, 6)] == [10, 20, 40, 60, 60]
    assert p.delay(1, retry_after=45) == 45  # the server asked for longer
    assert p.delay(3, retry_after=5) == 40

    ws, clock = workspace(), FakeClock()
    run(ws, FlakyDriver("rate_limit", failures=9), RunConfig(retry=policy(attempts=2, clock=clock)))
    assert clock.waits == [20, 20]  # retry-after: 20 beats 10, and ties 20


def test_after_n_tries_the_run_stops_with_a_message_and_retry() -> None:
    ws, clock = workspace(), FakeClock()
    driver = FlakyDriver("network", failures=9)
    result = run(ws, driver, RunConfig(retry=policy(attempts=2, clock=clock)))
    assert len(driver.prompts) == 3 and len(clock.waits) == 2
    (error,) = ws.sink.of_type("error")
    assert error.message == stopped_text("network", 3) == result.error
    assert error.reason == "network" and error.retryable
    assert error.message == (
        "The connection to Claude kept dropping after 3 tries, so this reply stopped. Your "
        "design and builds so far are kept. Press Retry to pick up where it left off."
    )
    assert_plain(said(ws))


def test_a_retry_keeps_the_rounds_and_builds_so_far() -> None:
    """The CLI dies after Claude's first edit: the next try picks up from that design."""
    ws = workspace()
    first = ScriptedDriver(
        ws, [("set_parameter", {"name": "draft", "value": 2.5}), ("run_checks", {})]
    )

    class CutOff:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def __call__(self, prompt, options):
            self.prompts.append(prompt)
            return self._play(prompt, options)

        async def _play(self, prompt, options):
            if len(self.prompts) == 1:
                async for m in first(prompt, options):
                    if type(m).__name__ != "ResultMessage":
                        yield m
                raise ConnectionResetError("the CLI's connection dropped")
            async for m in ScriptedDriver(ws, DONE)(prompt, options):
                yield m

    driver = CutOff()
    result = run(ws, driver)
    assert result.error is None and result.finished["outcome"] == "done"
    assert ws.design.params["draft"] == 2.5, "the first try's edit is kept"
    assert result.rounds == 1 and len(ws.builds) >= 1, "its round and build are kept"
    assert RESUME_NOTE in driver.prompts[1]
    assert [b.build.id for b in ws.builds][0] in json.dumps(
        [e.build.model_dump(mode="json") for e in ws.sink.of_type("build_ready")]
    )


def test_the_cli_is_told_how_many_times_to_retry(monkeypatch) -> None:
    from app.agent.run import options_for

    ws = workspace()
    options = options_for(ws, RunConfig().resolved(), None, RetryPolicy(cli_retries=3))
    assert options.env["CLAUDE_CODE_MAX_RETRIES"] == "3"


def test_every_api_fault_says_something_plain() -> None:
    for fault in FAULTS:
        ws = workspace()
        run(ws, FlakyDriver(fault, failures=9), RunConfig(retry=policy(attempts=1)))
        (error,) = ws.sink.of_type("error")
        assert error.reason is not None and error.reason != "unknown", fault
        assert "kept" in error.message, fault
        assert_plain(said(ws))


def test_a_build_that_hits_a_limit_says_so_and_the_run_goes_on() -> None:
    from app.agent.services import BuildOutcome, make_build

    ws = workspace()

    class OutOfMemory:
        def build(self, design):
            error = {"kind": "memory", "message": "the build ran out of memory", "line": None}
            return BuildOutcome(make_build("build-9", ws.board, status="failed"), error=error)

        def checks(self, outcome):
            return []

        def analyze(self, outcome):
            return []

    ws.services = OutOfMemory()
    steps = [("run_checks", {}), ("text", "That build ran out of memory; simplifying.")]
    run(ws, ScriptedDriver(ws, steps))
    (notice,) = ws.sink.of_type("notice")
    assert (notice.kind, notice.reason) == ("warning", "build_memory")
    assert notice.text == limits.BUILD_LIMITS["memory"][1]
    assert "earlier builds are kept" in notice.text
    assert_plain(notice.text)
