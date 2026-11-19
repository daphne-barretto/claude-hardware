"""Fake API failures, for tests and the e2e server: what the Claude Code CLI sends when an
API call fails, without calling a model.

```python
driver = FlakyDriver("rate_limit", then=ScriptedDriver(ws, steps), failures=1)
await run_agent(ws, "make a box", driver=driver)  # fails once, then plays the script
```

Each fault (`FAULTS`) is either the messages the CLI sends after its own retries gave up (an
assistant message with an `error`, then an error result with the HTTP status) or, for
`network`, the exception the SDK raises when the CLI's process dies. `cli_retries` adds the
CLI's `system`/`api_retry` messages before it, as when the CLI retries a call itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Fault:
    status: int | None  # the failing call's HTTP status (`ResultMessage.api_error_status`)
    sdk_error: str  # `AssistantMessage.error`
    text: str  # what the CLI says in the assistant message and the result


FAULTS: dict[str, Fault] = {
    "rate_limit": Fault(
        429,
        "rate_limit",
        'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Number '
        'of request tokens has exceeded your per-minute rate limit"}} retry-after: 20',
    ),
    "overloaded": Fault(
        529,
        "server_error",
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":'
        '"Overloaded"}}',
    ),
    "billing": Fault(
        400,
        "billing_error",
        'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":'
        '"Your credit balance is too low to access the Anthropic API. Please go to Plans & '
        'Billing to upgrade or purchase credits."}}',
    ),
    "auth": Fault(401, "authentication_failed", "Invalid API key · Please run /login"),
    "too_large": Fault(
        400,
        "invalid_request",
        'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":'
        '"prompt is too long: 215000 tokens > 200000 maximum"}}',
    ),
    "model_not_found": Fault(
        404,
        "invalid_request",
        'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: '
        'claude-unknown-1"}}',
    ),
    "timeout": Fault(None, "unknown", "API Error: Request timed out."),
    "network": Fault(None, "unknown", "Connection error: read ECONNRESET"),
}


def retry_message(fault: str, attempt: int, attempts: int, delay_ms: float) -> Any:
    """The CLI's `system`/`api_retry` message: it retries the failed call in `delay_ms`."""
    from claude_agent_sdk import SystemMessage

    f = FAULTS[fault]
    data = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": attempts,
        "retry_delay_ms": delay_ms,
        "error_status": f.status,
        "error": f.sdk_error,
    }
    return SystemMessage(subtype="api_retry", data=data)


def failure_messages(fault: str) -> list[Any]:
    """What the CLI sends when it gives up on a call: the error, then an error result."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    f = FAULTS[fault]
    return [
        AssistantMessage(content=[TextBlock(f.text)], model="<synthetic>", error=f.sdk_error),  # type: ignore[arg-type]
        ResultMessage(
            subtype="success",
            duration_ms=0,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="faulty",
            result=f.text,
            api_error_status=f.status,
        ),
    ]


class CLIConnectionError(Exception):
    """Stands in for the SDK's error of the same name: the CLI's process died."""


Driver = Callable[[str, Any], AsyncIterator[Any]]


class FlakyDriver:
    """Fails the first `failures` tries with `fault`, then hands each later try to `then` (a
    driver; None: an empty reply). With `cli_retries`, each failing try first sends that many
    `api_retry` messages, `retry_delay_ms` apart (waited with `sleep`, if given); with
    `recover`, the CLI's retries work and the same try goes on with `then`."""

    def __init__(
        self,
        fault: str,
        then: Driver | None = None,
        *,
        failures: int = 1,
        cli_retries: int = 0,
        retry_delay_ms: float = 20_000,
        recover: bool = False,
        sleep: Callable[[float], Any] | None = None,
        before: list[Any] | None = None,
    ):
        self.fault, self.then = fault, then
        self.failures, self.cli_retries, self.retry_delay_ms = failures, cli_retries, retry_delay_ms
        self.recover, self.sleep = recover, sleep
        self.before = before or []  # messages sent before the failure (e.g. some text)
        self.prompts: list[str] = []
        self.uses_run_slot = False

    def __call__(self, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.prompts.append(prompt)
        failing = len(self.prompts) <= self.failures
        return self._play(prompt, options, failing)

    async def _play(self, prompt: str, options: Any, failing: bool) -> AsyncIterator[Any]:
        if failing:
            for message in self.before:
                yield message
            for attempt in range(1, self.cli_retries + 1):
                yield retry_message(self.fault, attempt, self.cli_retries, self.retry_delay_ms)
                if self.sleep is not None:
                    await self.sleep(self.retry_delay_ms / 1000)
            if not self.recover:
                if self.fault == "network":
                    raise CLIConnectionError(FAULTS["network"].text)
                for message in failure_messages(self.fault):
                    yield message
                return
        if self.then is not None:
            async for message in self.then(prompt, options):
                yield message


async def real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
