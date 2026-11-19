"""What the session API (`app.session_api`) records in a session's telemetry log.

The session API calls one method per user action and hands over every agent event; the agent's
workspace reports its tool calls, fix rounds and conflicts through `Workspace.observer`. After
each user action has played out (the reply ended, a question came, an edit was made), the
canvas state is logged as a `canvas` event: what the user saw at each step.
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

from app import runtime
from app.contracts.telemetry import CanvasState, ClientTelemetry
from app.telemetry.log import TelemetryLog, open_log

if TYPE_CHECKING:
    from app.agent.run import RunConfig, RunResult
    from app.contracts.session import Comment, Export, Message, MessageCreate, ParameterUpdate
    from app.session_api import SessionState


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canvas_state(state: SessionState) -> CanvasState:
    """What the canvas shows: the latest build and its checks, the parameters and comments.

    The parameters are the list GET /parameters serves (`measured_parameters`: each with its
    dimension on the latest build, then the grouped values that measure there), so the log
    holds the list the user saw at every step."""
    s = state.session
    latest = state.builds.get(s.latest_build_id) if s.latest_build_id else None
    return CanvasState(
        status=s.status,
        latest_build_id=s.latest_build_id,
        checks=latest.checks if latest is not None else None,
        parameters=state.measured_parameters(),
        comments=[c.id for c in state.comments],
    )


# Why a run stopped (`app.agent.limits`), where the reason says what the words may not: the
# chat's plain words for an API problem ("Claude isn't set up on this server yet") name no API.
API_REASONS = frozenset(
    {
        "rate_limited",
        "overloaded",
        "network",
        "billing",
        "not_configured",
        "too_large",
        "model_unavailable",
    }
)
SANDBOX_REASONS = frozenset({"build_memory", "build_time", "build_crash"})


def error_source(message: str, reason: str | None = None) -> str:
    if reason in SANDBOX_REASONS:
        return "sandbox"
    if reason in API_REASONS:
        return "api"
    text = message.lower()
    if "sandbox" in text:
        return "sandbox"
    if "build" in text and "failed" in text:
        return "build"
    if text.startswith(("the run stopped", "the server has no")) or "api" in text:
        return "api"
    return "run"


class SessionTelemetry:
    def __init__(self, log: TelemetryLog):
        self.log = log
        self.pending: str | None = None  # the user action whose canvas state is due
        self.run_began: float | None = None
        self.question_at: float | None = None
        self.proposal_at: float | None = None
        self.run_questions = 0  # questions asked in the current run

    @classmethod
    def open(cls, session_id: str) -> SessionTelemetry | None:
        log = open_log(session_id)
        return cls(log) if log is not None else None

    # --- User actions -----------------------------------------------------------------------

    def session_start(self, state: SessionState) -> None:
        s = state.session
        self.log.write(
            "session_start",
            title=s.title,
            board=s.board,
            app_version=runtime.app_version(),
            commit=runtime.commit_sha(),
        )
        self.pending = "session_start"

    def message(self, message: Message, body: MessageCreate) -> None:
        self.log.last_stream = time.monotonic()  # a turn's delays count from its message
        if body.answer is not None:
            self.log.write(
                "answer",
                message_id=message.id,
                question_id=body.answer.question_id,
                option_id=body.answer.option_id,
                since_question_ms=self.log.ms_since(self.question_at),
            )
            self.pending = "answer"
            return
        self.log.write(
            "message",
            message_id=message.id,
            text=body.text,
            selection=body.selection,
            attachments=[a.name for a in body.attachments],
            model=body.model,
            effort=body.effort,
            mode=body.mode,
            comments=body.comments,
        )
        self.pending = "message"

    def parameter_edit(self, state: SessionState, name: str, body: ParameterUpdate) -> None:
        locked = (
            body.locked if body.locked is not None else (True if body.value is not None else None)
        )
        self.log.write("parameter_edit", name=name, value=body.value, locked=locked)
        # The canvas once the edit's rebuild has played out (`step_done`).
        self.pending = "parameter_edit"

    def comment(self, state: SessionState, comment: Comment) -> None:
        self.log.write(
            "comment",
            comment_id=comment.id,
            build_id=comment.build_id,
            face_id=comment.face_id,
            point=comment.point,
            text=comment.text,
        )
        self.log.canvas("comment", canvas_state(state))

    def export(self, state: SessionState, export: Export, parts: list[str] | None = None) -> None:
        fields: dict[str, Any] = {"parts": parts} if parts is not None else {}
        self.log.write(
            "export",
            export_id=export.id,
            build_id=export.build_id,
            formats=[f.format for f in export.files],
            **fields,
        )
        self.log.canvas("export", canvas_state(state))

    def client(self, body: ClientTelemetry) -> None:
        """A user action only the frontend sees."""
        if body.kind == "view":
            self.log.write("view", view=body.view, value=body.value)
        elif body.kind == "choice":
            self.log.write("choice", setting=body.setting, value=body.value)
        else:
            self.log.write(
                "change_decision",
                change_id=body.change_id,
                decision=body.decision,
                since_proposal_ms=self.log.ms_since(self.proposal_at),
            )

    def step_done(self, state: SessionState) -> None:
        """The user action's effects have played out: log what the canvas shows."""
        if self.pending is not None:
            after, self.pending = self.pending, None
            self.log.canvas(after, canvas_state(state))

    def abandoned(self, state: SessionState) -> None:
        if state.session.status != "idle":
            mid = getattr(state.ws.sink, "message_id", None)
            self.log.write("abandoned", message_id=mid, status=state.session.status)

    # --- Agent steps ------------------------------------------------------------------------

    def run_start(self, message_id: str, config: RunConfig, mode: str) -> None:
        self.run_began = time.monotonic()
        self.run_questions = 0
        self.log.write(
            "run_start",
            message_id=message_id,
            model=config.model,
            effort=config.effort,
            mode=mode,
        )

    def run_end(
        self,
        message_id: str,
        config: RunConfig,
        result: RunResult | None,
        error: str | None = None,
    ) -> None:
        if result is not None and result.proposal is not None:
            self.proposal_at = time.monotonic()
            self.log.write(
                "proposal",
                message_id=message_id,
                mode=result.proposal.mode,
                changes=[u.path for u in result.proposal.units],
                accepted=sorted(result.proposal.accepted),
            )
        error = error or (result.error if result is not None else None)
        finished = result.finished if result is not None else None
        outcome = (finished or {}).get("outcome") or ("error" if error else "unfinished")
        self.log.write(
            "run_end",
            message_id=message_id,
            outcome=outcome,
            rounds=result.rounds if result else 0,
            passing=result.passing if result else False,
            questions=self.run_questions,
            changes=len(result.proposal.units) if result and result.proposal else 0,
            latency_ms=self.log.ms_since(self.run_began) or 0,
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
            cost_usd=result.cost_usd if result else None,
            model=(result.model if result and result.model else None) or config.model,
            effort=config.effort,
            error=error,
        )
        self.run_began = None

    def observe(self, kind: str, data: dict) -> None:
        """A workspace step (`Workspace.observer`): tool calls, fix rounds, conflicts."""
        self.log.write(kind, **data)

    def event(self, state: SessionState, event: Any) -> None:
        """An agent event as it was emitted (build URLs relative to the session)."""
        self.log.stream(event)
        mid = getattr(event, "message_id", None)
        if event.type == "build_ready":
            self._build(state, event.build, mid)
        elif event.type == "question":
            self.question_at = time.monotonic()
            self.run_questions += 1
            self.log.write(
                "question",
                message_id=mid,
                question_id=event.question_id,
                text=event.text,
                options=[o.id for o in event.options],
            )
        elif event.type == "error":
            self.log.write(
                "error",
                message_id=mid,
                source=error_source(event.message, getattr(event, "reason", None)),
                message=event.message,
            )

    def _build(self, state: SessionState, build: Any, message_id: str | None) -> None:
        design = state.ws.design.snapshot()
        fields: dict[str, Any] = {
            "message_id": message_id,
            "build_id": build.id,
            "status": build.status,
            "code_hash": sha256(design.get("code") or ""),
            "parameters": design.get("params") or {},
        }
        if build.status != "ready":
            self.log.write("build", **fields)
            self.log.write(
                "error", message_id=message_id, source="build", message=f"Build {build.id} failed"
            )
            return
        artifacts = state.artifacts(build)
        fields["shapes_hash"] = sha256(artifacts.mesh)
        fields["checks"] = build.checks
        fields["artifacts"] = self.log.save_artifacts(build.id, artifacts)
        self.log.write("build", **fields)
        report = artifacts.checks
        results = report.get("results") or []
        self.log.write(
            "checks",
            build_id=build.id,
            summary=report.get("summary") or build.checks,
            failing=[r["id"] for r in results if r.get("status") == "fail"],
            untested=[r["id"] for r in results if r.get("status") == "untested"],
        )
