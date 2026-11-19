"""Telemetry contract: one append-only event log per session, to learn from it.

A log is JSON Lines: one `TelemetryEvent` per line, in order (`seq` from 1). Every event
carries the schema version `v`; a breaking change bumps `TELEMETRY_VERSION`.

- **User actions**: `session_start`, `message` (with the selection sent as context, the
  attachments, model, effort and Manual/Auto mode), `answer`, `change_decision` (accept or
  reject one proposed change), `parameter_edit` (a hand edit or a lock), `comment`, `export`,
  `view` (maps, section, compare, history, camera...) and `choice` (model, effort, mode).
- **Agent steps**: `run_start`, `stream` (every session event of the run, with its
  timing), `tool_call` (inputs, outputs, latency), `build` (code and parameter hashes,
  shapes hash, the build's artifacts), `checks`, `fix_round`, `question`, `conflict`,
  `proposal` and `run_end` (outcome, tokens, cost, model and effort).
- **Errors and abandonments**: `error` (failed builds, sandbox stops, API errors) and
  `abandoned` (a session left mid-run).
- **Canvas**: `canvas` is the canvas state after each step: what the user saw.

`docs/architecture.md` describes the log, where it is kept and what it never holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from app.contracts.checks import CheckSummary
from app.contracts.common import BoardRef, Contract, ContractError, Point3
from app.contracts.session import Effort, Mode, Parameter, Selection, SessionEvent

TELEMETRY_VERSION = 1

Ms = Annotated[int, Field(ge=0)]


class _Event(Contract):
    v: Literal[1] = TELEMETRY_VERSION
    session_id: str = Field(min_length=1)
    seq: int = Field(ge=1, description="Position in the log, from 1")
    at: datetime
    t_ms: Ms = Field(description="Milliseconds since the session started")


# --- User actions ----------------------------------------------------------------------------


class SessionStartTelemetry(_Event):
    kind: Literal["session_start"]
    title: str
    board: BoardRef | None = None
    app_version: str
    commit: str


class MessageTelemetry(_Event):
    kind: Literal["message"]
    message_id: str
    text: str
    selection: Selection | None = Field(default=None, description="Sent to Claude as context")
    attachments: list[str] = Field(default_factory=list, description="File names")
    model: str | None = None
    effort: Effort | None = None
    mode: Mode
    comments: list[str] = Field(default_factory=list, description="Comments sent to Claude")


class AnswerTelemetry(_Event):
    kind: Literal["answer"]
    message_id: str
    question_id: str
    option_id: str
    since_question_ms: Ms | None = None


class ChangeDecisionTelemetry(_Event):
    kind: Literal["change_decision"]
    change_id: str
    decision: Literal["accept", "reject"]
    since_proposal_ms: Ms | None = Field(
        default=None, description="Time from the proposal to this decision"
    )


class ParameterEditTelemetry(_Event):
    kind: Literal["parameter_edit"]
    name: str
    value: float | str | None = None
    locked: bool | None = None


class CommentTelemetry(_Event):
    kind: Literal["comment"]
    comment_id: str
    build_id: str
    face_id: str
    point: Point3
    text: str


class ExportTelemetry(_Event):
    kind: Literal["export"]
    export_id: str
    build_id: str
    formats: list[str]
    parts: list[str] | None = Field(
        default=None,
        description="The parts exported (`ExportCreate.parts`); absent for the whole assembly",
    )


ViewName = Literal["map", "section", "compare", "history", "camera", "feature_tree", "units"]


class ViewTelemetry(_Event):
    kind: Literal["view"]
    view: ViewName
    value: str | None = Field(default=None, description="The map type, camera view, ...")


class ChoiceTelemetry(_Event):
    kind: Literal["choice"]
    setting: Literal["model", "effort", "mode"]
    value: str


# --- Agent steps -----------------------------------------------------------------------------


class RunStartTelemetry(_Event):
    kind: Literal["run_start"]
    message_id: str
    model: str | None = None
    effort: Effort | None = None
    mode: Mode


class StreamTelemetry(_Event):
    """One session event, as the agent emitted it (build URLs relative to the session), with
    the delay since the previous one."""

    kind: Literal["stream"]
    delay_ms: Ms
    event: SessionEvent


class ToolCallTelemetry(_Event):
    kind: Literal["tool_call"]
    message_id: str | None = None
    tool: str
    input: dict
    output: Any = None
    is_error: bool = False
    denied: str | None = Field(default=None, description="Why a hook refused the call")
    latency_ms: Ms


class BuildTelemetry(_Event):
    kind: Literal["build"]
    message_id: str | None = None
    build_id: str
    status: Literal["ready", "failed"]
    code_hash: str
    parameters: dict
    shapes_hash: str | None = Field(default=None, description="sha256 of the build's mesh")
    checks: CheckSummary | None = None
    artifacts: str | None = Field(
        default=None, description="File under the log's `artifacts/` with what was served"
    )


class ChecksTelemetry(_Event):
    kind: Literal["checks"]
    build_id: str
    summary: CheckSummary
    failing: list[str]
    untested: list[str]


class FixRoundTelemetry(_Event):
    kind: Literal["fix_round"]
    message_id: str | None = None
    round: int = Field(ge=1)
    rounds: int = Field(ge=1)
    tool: str


class QuestionTelemetry(_Event):
    kind: Literal["question"]
    message_id: str
    question_id: str
    text: str
    options: list[str]


class ConflictTelemetry(_Event):
    kind: Literal["conflict"]
    message_id: str | None = None
    conflicts: list[dict]


class ProposalTelemetry(_Event):
    kind: Literal["proposal"]
    message_id: str
    mode: Mode
    changes: list[str]
    accepted: list[str]


class RunEndTelemetry(_Event):
    kind: Literal["run_end"]
    message_id: str
    outcome: Literal["done", "blocked", "error", "unfinished"]
    rounds: int = Field(ge=0)
    passing: bool
    questions: int = Field(ge=0)
    changes: int = Field(ge=0, description="Proposed changes")
    latency_ms: Ms
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    effort: Effort | None = None
    error: str | None = None


# --- Errors and canvas -----------------------------------------------------------------------


class ErrorTelemetry(_Event):
    kind: Literal["error"]
    message_id: str | None = None
    source: Literal["build", "sandbox", "api", "run", "client"]
    message: str


class AbandonedTelemetry(_Event):
    kind: Literal["abandoned"]
    message_id: str | None = None
    status: str = Field(description="The session's status when it was left")


class CanvasState(Contract):
    status: Literal["idle", "running", "waiting_for_answer"]
    latest_build_id: str | None = None
    checks: CheckSummary | None = None
    parameters: list[Parameter]
    comments: list[str] = Field(default_factory=list, description="Comment IDs on the model")


class CanvasTelemetry(_Event):
    kind: Literal["canvas"]
    step: int = Field(ge=0, description="The step (0 is the session start)")
    after: Literal["session_start", "message", "answer", "parameter_edit", "comment", "export"] = (
        Field(description="The kind of user action the step began with")
    )
    state: CanvasState


TelemetryEvent = Annotated[
    SessionStartTelemetry
    | MessageTelemetry
    | AnswerTelemetry
    | ChangeDecisionTelemetry
    | ParameterEditTelemetry
    | CommentTelemetry
    | ExportTelemetry
    | ViewTelemetry
    | ChoiceTelemetry
    | RunStartTelemetry
    | StreamTelemetry
    | ToolCallTelemetry
    | BuildTelemetry
    | ChecksTelemetry
    | FixRoundTelemetry
    | QuestionTelemetry
    | ConflictTelemetry
    | ProposalTelemetry
    | RunEndTelemetry
    | ErrorTelemetry
    | AbandonedTelemetry
    | CanvasTelemetry,
    Field(discriminator="kind"),
]

USER_KINDS = (
    "session_start",
    "message",
    "answer",
    "change_decision",
    "parameter_edit",
    "comment",
    "export",
    "view",
    "choice",
)
AGENT_KINDS = (
    "run_start",
    "stream",
    "tool_call",
    "build",
    "checks",
    "fix_round",
    "question",
    "conflict",
    "proposal",
    "run_end",
)
OTHER_KINDS = ("error", "abandoned", "canvas")

_adapter: TypeAdapter[Any] = TypeAdapter(TelemetryEvent)


def validate_telemetry_event(data: dict) -> Any:
    try:
        return _adapter.validate_python(data)
    except ValidationError as error:
        raise ContractError(f"telemetry event: {error}") from error


def telemetry_json_schema() -> dict:
    return _adapter.json_schema()


# --- What the frontend reports ---------------------------------------------------------------


class ClientTelemetry(Contract):
    """A user action only the frontend sees, for the session's telemetry log
    (`POST /api/sessions/{id}/telemetry`)."""

    kind: Literal["view", "choice", "change_decision"]
    view: ViewName | None = None
    setting: Literal["model", "effort", "mode"] | None = None
    change_id: str | None = None
    decision: Literal["accept", "reject"] | None = None
    value: str | None = None


__all__ = [
    "AGENT_KINDS",
    "OTHER_KINDS",
    "TELEMETRY_VERSION",
    "USER_KINDS",
    "CanvasState",
    "ClientTelemetry",
    "TelemetryEvent",
    "telemetry_json_schema",
    "validate_telemetry_event",
]
