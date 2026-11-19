"""The agent's event stream: session events from the contract (`contracts/README.md`).

Every step of a run becomes one of the contract's server-sent events, so the session API
can forward them as they are. `docs/architecture.md` lists which step sends which event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.contracts.common import Location
from app.contracts.session import (
    Build,
    BuildReadyEvent,
    ChangesReadyEvent,
    Constraint,
    ConstraintConflict,
    ConstraintsEvent,
    ErrorEvent,
    FindResult,
    FoundEvent,
    MessageEndEvent,
    MessageStartEvent,
    NoticeEvent,
    PlanEvent,
    ProgressEvent,
    QuestionEvent,
    QuestionOption,
    RecordedEvent,
    RisksReadyEvent,
    TextEvent,
)
from app.security.scrub import LeakFilter, redact_outgoing

Listener = Callable[[int, Any], None]


class EventSink:
    """Numbers events from 1, keeps them, and hands each to the listeners and the queue."""

    def __init__(self, message_id: str = "msg-1", listeners: list[Listener] | None = None):
        self.message_id = message_id
        self.events: list[tuple[int, Any]] = []
        self.listeners = list(listeners or [])
        self.queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        self._open = False
        # Streamed text goes out a sentence at a time, checked for leaks (`app.security.scrub`).
        self._leaks = LeakFilter()
        # Text from a new message or content block of Claude's starts a new paragraph when it
        # follows text directly (`paragraph`), so "…auto." and "The new…" don't run together.
        self._after_text = ""  # the last text sent, while nothing else has been sent since
        self._new_paragraph = False

    def emit(self, event: Any) -> Any:
        if event.type != "text":
            self._flush_text()
            self._after_text = ""
        return self._emit(event)

    def _flush_text(self) -> None:
        held = self._leaks.flush()
        if held:
            self._emit(TextEvent(type="text", message_id=self.message_id, delta=held))

    def _emit(self, event: Any) -> Any:
        event_id = len(self.events) + 1
        self.events.append((event_id, event))
        self.queue.put_nowait((event_id, event))
        for listener in self.listeners:
            listener(event_id, event)
        return event

    @property
    def types(self) -> list[str]:
        return [e.type for _, e in self.events]

    def of_type(self, kind: str) -> list[Any]:
        return [e for _, e in self.events if e.type == kind]

    def recording(self) -> list[str]:
        """The stream in the recorded format of `fixtures/session-events.jsonl`."""
        return [RecordedEvent(id=i, delay_ms=0, event=e).model_dump_json() for i, e in self.events]

    # --- One method per event type --------------------------------------------------------

    def start(self) -> None:
        if not self._open:
            self._open = True
            self.emit(
                MessageStartEvent(
                    type="message_start", message_id=self.message_id, role="assistant"
                )
            )

    def paragraph(self) -> None:
        """The next text is from another message or content block of Claude's: if it follows
        text directly, a blank line comes between (QA fix brief 7)."""
        self._new_paragraph = True

    def text(self, delta: str) -> None:
        if delta:
            if self._new_paragraph and self._after_text and not self._after_text.endswith("\n"):
                if not delta[0].isspace():
                    delta = "\n\n" + delta
            self._new_paragraph = False
            self._after_text = delta
            self.start()
            ready = self._leaks.feed(delta)
            if ready:
                self._emit(TextEvent(type="text", message_id=self.message_id, delta=ready))

    def progress(self, text: str, round: int | None, rounds: int | None) -> None:
        self.start()
        self.emit(
            ProgressEvent(
                type="progress",
                message_id=self.message_id,
                text=text,
                round=round or None,
                rounds=rounds if round else None,
            )
        )

    def question(
        self,
        question_id: str,
        text: str,
        options: list[dict],
        *,
        location: dict | None = None,
        conflict: dict | None = None,
    ) -> None:
        """Claude asks. `location` is the spot on the model the question is about and
        `conflict` the constraints that can't all be met (`ConstraintConflict`), if it is
        asked because of one."""
        self.start()
        self.emit(
            QuestionEvent(
                type="question",
                message_id=self.message_id,
                question_id=question_id,
                text=text,
                options=[
                    QuestionOption(id=o["id"], label=o["label"], description=o.get("description"))
                    for o in options
                ],
                location=Location.model_validate(location) if location else None,
                conflict=ConstraintConflict.model_validate(conflict) if conflict else None,
            )
        )

    def plan(self, steps: list[str]) -> None:
        self.start()
        self.emit(PlanEvent(type="plan", message_id=self.message_id, steps=steps))

    def constraints(self, constraints: list[Constraint]) -> None:
        """The spec changed. Inside a message when one is open, else outside any."""
        self.emit(
            ConstraintsEvent(
                type="constraints",
                message_id=self.message_id if self._open else None,
                constraints=constraints,
            )
        )

    def build_ready(self, build: Build) -> None:
        self.start()
        self.emit(BuildReadyEvent(type="build_ready", message_id=self.message_id, build=build))

    def changes_ready(self, change_set: Any) -> None:
        """A change set (`change-set/1`) to review: inside the message when one is open."""
        self.emit(
            ChangesReadyEvent(
                type="changes_ready",
                message_id=self.message_id if self._open else None,
                change_set=change_set,
            )
        )

    def risks_ready(self, risk_flags: Any) -> None:
        """The run's risk flags (`risk-flags/1`), each fix to accept or ignore on its own."""
        self.emit(
            RisksReadyEvent(
                type="risks_ready",
                message_id=self.message_id if self._open else None,
                risk_flags=risk_flags,
            )
        )

    def found(self, build_id: str, result: FindResult) -> None:
        """Faces Claude found from chat (`find_in_model`), for the canvas to highlight."""
        self.start()
        self.emit(
            FoundEvent(type="found", message_id=self.message_id, build_id=build_id, result=result)
        )

    def error(self, message: str, retryable: bool = False, reason: str | None = None) -> None:
        """The run stopped: `message` in plain words (`app.agent.limits`), `reason` its code."""
        self.emit(
            ErrorEvent(
                type="error",
                message_id=self.message_id if self._open else None,
                message=redact_outgoing(message),
                retryable=retryable,
                reason=reason,  # type: ignore[arg-type]
            )
        )

    def notice(self, kind: str, text: str, **fields: Any) -> NoticeEvent:
        """A notice shown in the chat apart from Claude's text (`NoticeEvent`, one event for
        every kind): `retrying`, `waiting` or a `warning` while the run goes on, inside the
        message when one is open; a `refused` or `safety` notice opens the message."""
        if kind in ("refused", "safety"):
            self.start()
        return self.emit(
            NoticeEvent(
                type="notice",
                message_id=self.message_id if self._open else None,
                kind=kind,  # type: ignore[arg-type]
                text=text,
                **fields,
            )
        )

    def end(self) -> None:
        if self._open:
            self._open = False
            self.emit(MessageEndEvent(type="message_end", message_id=self.message_id))
