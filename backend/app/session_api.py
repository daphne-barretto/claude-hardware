"""The session API: sessions, messages, builds, parameters, comments and exports, with
server-sent events for live progress.

Every route is the one the session API contract declares (`contracts/session-api.openapi.json`,
the routes in `app.mock_api`); `test_session_api.py` checks that the two match.

A session holds one design agent workspace (`app.agent`). A new session starts empty: no board,
no design and no build until Claude writes the first design from what the user describes.
Opening an example (`GET /api/examples`, then `{"example": id}`) starts from it instead: for the
board revision example, the existing design made for the previous board revision, to be
updated for the new one (`demos/<board>/CHANGES.md`).

- Opening an example builds and checks its existing design. The build arrives as a
  `build_ready` event with no message.
- Posting a message starts an agent run on it. The run's events (`docs/architecture.md`) go on the
  session's event stream as they are, numbered within the session. The selection and the mode
  of the message are given to Claude with the text.
- Comments are pinned to a face and follow it through rebuilds (`app.comments`). Every
  run is told the open comments as context; a message with `comments` sends them to Claude as
  requests: one run handles them all, Claude answers each in its thread (`reply_to_comment`)
  and the change set links each change to the comment it answers. Every change to a comment
  streams as a `comment` event.
- Files attached to a message go into the workspace, where Claude reads them
  (`read_attachment`); the prompt lists them. Photos also go to Claude with the message itself
  (`app.agent.photo`).
- A `question` event leaves the session `waiting_for_answer`; posting the answer resumes the
  same run.
- A message's text sets the design's spec (`app.spec`): the constraint chips, on the stream as
  a `constraints` event. Editing or removing a chip (`/constraints`) rebuilds and rechecks.
- A session started as a new design (`new_design`) has no starting case and no first build;
  Claude proposes its build plan (`propose_plan`) before any edit. A session on the existing
  case (the revision update) edits without a plan.
- A parameter edit (`PATCH`) is a hand edit: it locks the value in the design, so Claude designs
  around it on the next run.
- Each build keeps the design it was built from: `GET .../builds/{id}/diff` diffs two builds
  (`app.builds` for stored builds, else `app.build_diffs`), and `POST .../builds/{id}/restore`
  (undo in the history) makes a build's design the current one again and rebuilds it.
- `POST .../revisions/next` loads the next board revision (the new one in
`demos/<board>/CHANGES.md`):
  it starts an agent run with a fixed prompt, so Claude starts the work without anyone typing,
  and the run's proposal arrives like any run's. It is the control the demo presses and the
  webhook a board source calls when it has a new revision.
- Sessions are saved as they go in the session store (`app.session_store`, under
  `DATA_DIR`), and loaded on demand when they aren't in memory, so a deploy or restart loses
  nothing. A run that a restart cut off comes back `idle`, with a note in the chat that the
  reply was interrupted and can be sent again.
- `POST .../imports` imports a STEP file (`app.imports`): it is read in the build sandbox and
  tessellated, checked and analyzed by the import worker, never in this process, and the build
  arrives as a `build_ready` with no message. Its mesh, checks, maps and source are the import's
  own; Claude is told about it with every message and can read the file.
- A run that changes the design leaves a change set (`app.agent.proposal`), sent as a
  `changes_ready` event and served at `/changes`. A later run that changes the design, or a
  restore, retires it; a run that leaves the design as it was (a question) keeps it.
  Accepting and rejecting its changes (`POST /changes`) rebuilds and rechecks with only the
  accepted ones; the rebuild streams as a `build_ready` with no message, then the updated set
  as `changes_ready`.
- A code edit (`PUT .../source`) is a hand edit too: the edited code is built and checked, and
  becomes the design's code if it builds. Claude's next prompt carries the change as a diff.
- A sketch on a face (`POST .../sketches`) is a hand edit too: the shape goes into the
  design's parameters (`sketches`) and code as a feature, locked (`sketches.<tag>`), and the
  design rebuilds and rechecks (a `build_ready` with no message). Claude keeps it on every run.
  The modeling tools' edits (`/edits`) work the same way; their routes are in
  `app.modeling_api`.
- A run that flags risks (`app.agent.risks`) leaves its flags, sent as a `risks_ready` event and
  served at `/risks`. Accepting or ignoring a fix (`POST /risks`, `Flags.review`) lays the
  accepted fixes over the design and rebuilds; the rebuild streams as a `build_ready` with no
  message, then the updated flags as `risks_ready`.

Builds are real: the agent's services (`GeometryServices`) build every design in the
sandbox, check it with the check runner (the standards that apply to it: a design without a
board gets the rest as not applicable) and store it in the build store (`app.builds`, under
`DATA_DIR`) with its check report, mesh and analysis maps, as a child of the build before it.
A build's mesh, checks, maps, source and diff are served from the store, and its exports are
written from a sandboxed rebuild of its stored code and parameters. Builds the store doesn't
hold (from `FixtureServices`, which frontend tests use) are served from `fixtures/` as before.

`CONFIG` is the seam: tests swap the services, the store and the driver that talks to Claude
(the default is the Claude Agent SDK, which needs `ANTHROPIC_API_KEY`).

Memory stays bounded on a small instance (the free plan has 512 MB):

- At most `MAX_AGENT_RUNS` runs talk to Claude through the SDK at once (sized from the
  machine when unset, `geometry.capacity`); each starts a Claude CLI process of about 250 MB.
  Later runs wait in line for a slot, and the chat shows each one's place in line (a
  `progress` line). The line is fair across browsers: a free slot goes to the waiting
  run whose browser has the fewest runs going, the earliest of those first
  (`geometry.slots.fair_order`), so one person's many sessions can't keep another waiting. A
  run waiting for an answer gives its slot up to a waiting run: it stops with an error, and the
  next message picks up from its design.
- Builds and geometry workers, in runs or not, take the instance's build slots
  (`geometry.slots`, `BUILD_CONCURRENCY`), shared fairly between browsers the same way.
- At most `MAX_SESSIONS` sessions (default 50) are kept: a new one drops the oldest idle ones.

Each browser sees only its own sessions: a session belongs to the browser that made it
(`app.owners`, a cookie), the session list shows only that browser's, and any other browser
gets a 404 for it. So people using the app at once never see each other's builds or history.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import json
import logging
import os
import re
import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app import comments as pins
from app import conversations as convs
from app import demos
from app import standards_sets as std_sets
from app.agent import limits
from app.agent.design import LockedCodeError, PathError, get_path, has_path, set_path
from app.agent.events import EventSink
from app.agent.proposal import Proposal, delta, title
from app.agent.risks import Flags, find_risks
from app.agent.run import (
    Driver,
    RunConfig,
    empty_workspace,
    example_workspace,
    run_agent,
)
from app.agent.services import (
    MESH_FILE,
    BuildOutcome,
    CheckItem,
    FixtureServices,
    GeometryServices,
    Services,
    make_build,
    map_file,
    summary,
)
from app.agent.tools import AttachedFile, Workspace
from app.attachments import (
    FILE_RESPONSES,
    UPLOAD_RESPONSES,
    Attachments,
    UploadField,
    read_upload,
)
from app.boards import Board, find_board
from app.build_artifacts import BuildArtifacts
from app.build_diffs import build_diff, fixture_revisions
from app.builds import BuildNotFound, BuildStore, StoredBuild, default_store, diff_builds
from app.builds import new_build_id as new_store_build_id
from app.code_edits import (
    CODE_ERROR_RESPONSES,
    build_error,
    code_edit_note,
    code_error,
    code_error_response,
    edited_source,
)
from app.contracts.changes import ChangeReview, ChangeSet, RiskFlags, RiskReview
from app.contracts.checks import CheckReport, CheckSummary
from app.contracts.common import BoardRef
from app.contracts.maps import AnalysisMap, MapType
from app.contracts.models import ModelCatalog
from app.contracts.session import (
    Attachment,
    Build,
    BuildDiff,
    BuildOrigin,
    BuildReadyEvent,
    BuildSource,
    CodeError,
    Comment,
    CommentCreate,
    CommentEvent,
    CommentReplyCreate,
    CommentRequest,
    CommentUpdate,
    Constraint,
    ConstraintsEvent,
    ConstraintUpdate,
    Conversation,
    ConversationsEvent,
    Edit,
    ErrorBody,
    ErrorEvent,
    ExampleSummary,
    Export,
    ExportCreate,
    ExportFile,
    FacePlane,
    FindRequest,
    FindResult,
    Message,
    MessageCreate,
    MessageEndEvent,
    NoticeEvent,
    Parameter,
    ParameterUpdate,
    ProgressEvent,
    QueuedMessage,
    RevisionLoad,
    RunRetry,
    Session,
    SessionCreate,
    SessionStandards,
    SessionUpdate,
    Sketch,
    SketchCreate,
    SourceUpdate,
    ThreadReportEvent,
    TitleEvent,
    UserMessageEvent,
    sse_frame,
    validate_event,
)
from app.contracts.telemetry import ClientTelemetry
from app.design_history import (
    History,
    capture,
    parameter_label,
    plain_failure,
    review_label,
    run_label,
    sketch_label,
)
from app.dimensions import grouped_values, measured
from app.edit_decisions import (
    KEEP,
    REVERT,
    EditDecision,
    accepted_note,
    adjust_prompt,
    broken_standards,
    failing_ids,
    failing_in_report,
)
from app.edit_decisions import accepted as accepted_failures
from app.edit_decisions import ask as ask_about_edit
from app.examples import Example, example_for_board, examples, find_example
from app.export_names import rename_exports
from app.exports import (
    ExportPartsError,
    export_parts,
    fixture_face_ids,
    fixture_mesh_parts,
    fixtures_dir,
    mesh_face_ids,
    session_revisions,
    store_revisions,
    write_exports,
)
from app.find import find_in_build
from app.imports import (
    MAX_IMPORT_BYTES,
    STEP_MEDIA_TYPE,
    ImportedFile,
    export_import,
    import_notes,
    import_step,
)
from app.model_availability import AVAILABILITY
from app.modeling import with_edits
from app.owners import current_owner
from app.parameter_values import value_error
from app.parameters import Approval, LockedParameterError, ParameterStore
from app.revisions import revision_message, revision_prompt
from app.runtime import data_dir
from app.security import budget, quota
from app.security import limits as rate_limits
from app.security.scrub import scrub_user_text, untrusted
from app.session_store import SessionStore, session_store
from app.share_routes import SHARE_VIEW
from app.sketches import SketchError, face_plane, new_sketch, sketch_params, with_sketches
from app.spec import Spec, SpecError
from app.standards import Standard, StandardsFile, load_standards, to_payload
from app.standards_api import standards_router
from app.standards_changes import change_notice, claude_note
from app.telemetry.session import SessionTelemetry
from app.titles import DEFAULT_TITLE, clean_title, title_for
from geometry.capacity import agent_runs
from geometry.conflicts import detect_conflicts
from geometry.params import case_step_path
from geometry.slots import fair_order

if TYPE_CHECKING:
    from app.contracts.mesh import MeshInfo
    from geometry.files import ExportedFile

API = "/api"
SESSION = API + "/sessions/{session_id}"
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorBody}}
GLTF = "model/gltf+json"
EVENT_STREAM = "text/event-stream"
KEEPALIVE_SECONDS = 15.0
CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
HAND_EDIT = Approval(by="user", reason="Changed by hand")
# Plain words for the user, never internal error text or build IDs.
FAILED_BUILD = (
    "That version of the model couldn't be built, so there's nothing of it to show. Use the "
    "model on the canvas instead."
)
RESTORE_FAILED = "Couldn't rebuild that version of the design, so nothing changed."
FAILED_RESTORE = "That version of the model couldn't be built, so it can't be restored."
PREEMPTED = limits.stopped_text("preempted")
STOPPED = limits.stopped_text("stopped")
QUEUE_POLL_SECONDS = 0.5
# The message queue (`app.message_queue`) hears when a run ends, a question opens or is
# answered, and when a session comes back after a restart, so the next queued message starts.
RUN_LISTENERS: list[Callable[[SessionState], None]] = []

REMOVED_NOTE = (
    "Some text that looked like a key, token or email address was removed from this message "
    "before you saw it (marked [removed: ...]). Don't ask for it."
)
INTERRUPTED = limits.stopped_text("interrupted")
NOT_CONFIGURED = limits.stopped_text("not_configured")
RUN_FAILED = limits.stopped_text("unknown")
# What a retry tells Claude, after the request of the reply that stopped.
ANSWERED_NOTE = (
    "(You asked the user: {question} The server restarted while you waited, so this is a new "
    "run on the same request. The user answered: {answer}. The design, its builds and the fix "
    "rounds so far are kept in the workspace: read the spec to see where things stand, carry on "
    "from there with that answer, and don't ask the same question again.)"
)
# A question left unanswered because the user sent something else: its reply ends on this note.
QUESTION_LEFT_OPEN = "The question was left unanswered: your next message was sent instead."
# What the next run is told about it, so Claude decides what the new message means for it.
OPEN_QUESTION_NOTE = (
    '(Earlier you asked the user: "{question}"{where} They didn\'t pick an option: they '
    "sent this message instead, and the question was left open. Decide whether this message "
    "answers your question, replaces it, or is about something else, and go on from there. "
    "Ask again only if you still need the answer.)"
)
RETRY_NOTE = (
    "(Your previous reply to this request stopped before you finished: {why} The user pressed "
    "Retry. The design, its builds and the fix rounds so far are kept in the workspace: read "
    "the spec to see where things stand, carry on from there, and don't redo finished steps.)"
)
# Events not worth a save of the whole session each: they are kept as events and applied
# to the messages again when the session is loaded.
STREAMED = ("text", "progress")

log = logging.getLogger(__name__)


def env_int(name: str, default: int) -> int:
    """A positive whole number from the environment, else `default`."""
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


USER = "You"  # the author of the user's comments and replies
# The parameters of the enclosure's vertical stack (`geometry.conflicts`), offered for hand edits
# with the design's others: locking one can clash with the standards, which conflict detection
# reports.
STACK_PARAMETERS = {
    "z_top": "Top of the lid",
    "ceiling": "Underside of the lid's roof",
    "floor_top": "Top of the base's floor",
    "z_bottom": "Bottom of the base",
}
MAX_IMPORT_READ = MAX_IMPORT_BYTES + 1  # one byte more tells a file over the limit


def now() -> datetime:
    return datetime.now(UTC)


def _next_count(counter: Any) -> int | None:
    """The next number an `itertools.count` gives, without taking it."""
    if not isinstance(counter, itertools.count):
        return None
    match = re.fullmatch(r"count\((-?\d+)\)", repr(counter))
    return int(match.group(1)) if match else None


# --- Fixture artifacts (builds the store doesn't hold) ---------------------------------------


def _fixture(name: str) -> Any:
    return json.loads((fixtures_dir() / name).read_text())


def _fixture_text(name: str) -> str:
    return (fixtures_dir() / name).read_text()


def build_checks(build: Build) -> dict:
    """The build's check report: the fixture report for its revision, for this build and
    board."""
    previous = build.board == fixture_revisions()[0]
    report = _fixture("previous-checks.json" if previous else "checks.json")
    report["build_id"] = build.id
    report["board"] = build.board.model_dump()
    return report


def build_source(build: Build, code: str | None = None) -> dict:
    """The build's source: the fixture's, or the hand-edited `code` it was built from."""
    source = {**_fixture("build-source.json"), "build_id": build.id}
    return source if code is None else edited_source(source, build.id, code)


def build_map(build: Build, map_type: str) -> dict:
    return {**_fixture(f"maps/{map_type}.json"), "build_id": build.id}


def build_mesh(build: Build) -> str:
    return _fixture_text("enclosure.gltf")


def build_face_ids(build: Build) -> frozenset[str]:
    return fixture_face_ids()


def build_mesh_parts(build: Build) -> tuple[str, ...]:
    return fixture_mesh_parts()


def export_files(
    build: Build,
    out: Path,
    formats: list[str],
    checks: dict | None = None,
    record: StoredBuild | None = None,
    parts: list[str] | None = None,
    history: list[Build] | None = None,
    units: str = "mm",
) -> list[ExportedFile]:
    """Write `formats` of the build's `parts` (default: the whole assembly) to `out` with the
    geometry exporters (`geometry.export`), the build note with lengths in `units`.

    A stored build (`record`) is rebuilt in the sandbox from its code and parameters and
    exported with its own check report. Any other build (a fixture one) exports the fixture
    enclosure's geometry, with the build note written from `checks`. The drawing's revision
    table lists the stored build's history, or else the session's builds (`history`, oldest
    first) up to this one. Raises `ExportPartsError` for parts the build doesn't have or can't
    export.
    """
    if record is None:
        chosen = export_parts(parts, build_mesh_parts(build))
        revisions = session_revisions(history or [], build)
        report = checks or build_checks(build)
        return write_exports(out, report, formats, chosen, revisions, units)
    board = find_board(record.board.name, record.board.revision) if record.board else None
    report = checks or record.checks
    if report is None:
        raise HTTPException(404, f"build {record.id} has no check report")
    chosen = export_parts(parts, record_mesh_parts(record))
    from geometry.worker import call

    # The rebuild and the exporters load the CAD kernel, so they run in a geometry worker.
    written = call(
        "app.exports:worker_stored_exports",
        out,
        board,
        record.code,
        record.parameters,
        record.backend,
        report,
        formats,
        chosen,
        store_revisions(CONFIG.store(), record.id),
        units,
        mesh_face_ids(stored_mesh(CONFIG.store(), record)),  # the IDs a STEP import keeps
    )
    if isinstance(written, dict):
        log.warning("export of build %s failed: %s", record.id, written)
        kind = str(written.get("kind"))
        raise HTTPException(409, limits.EXPORT_FAILED.get(kind, limits.EXPORT_UNKNOWN))
    return written


def exports_dir() -> Path:
    return data_dir() / "exports"


def build_artifacts(build: Build) -> BuildArtifacts:
    """Everything served for a fixture build."""
    return BuildArtifacts(
        mesh=build_mesh(build),
        checks=build_checks(build),
        source=build_source(build),
        maps={m: build_map(build, m) for m in build.maps},
    )


# --- Stored artifacts: real builds, from the build store --------------------------------------


def stored_checks(store: BuildStore, record: StoredBuild) -> dict:
    if record.checks is None:
        raise HTTPException(404, f"build {record.id} has no check report")
    return {**record.checks, "build_id": record.id}


def stored_mesh(store: BuildStore, record: StoredBuild) -> str:
    return store.file(record.id, MESH_FILE).read_text()


def stored_map(store: BuildStore, record: StoredBuild, map_type: str) -> dict:
    try:
        return json.loads(store.file(record.id, map_file(map_type)).read_text())
    except FileNotFoundError as error:
        raise HTTPException(404, f"build {record.id} has no {map_type} map") from error


def record_mesh_parts(record: StoredBuild) -> tuple[str, ...]:
    """The part names of a stored build's mesh."""
    doc = json.loads(stored_mesh(CONFIG.store(), record))
    names = (n.get("extras", {}).get("part") for n in doc["nodes"] if "mesh" in n)
    return tuple(dict.fromkeys(name for name in names if name))


def stored_face_ids(store: BuildStore, record: StoredBuild) -> frozenset[str]:
    doc = json.loads(stored_mesh(store, record))
    return frozenset(f["id"] for n in doc["nodes"] for f in n.get("extras", {}).get("faces", []))


def stored_source(record: StoredBuild) -> dict:
    """The build's code, and where in it each feature is created. A build records the lines as
    it runs in the sandbox (`geometry.sites`), so they are stored with it (`sources`).
    For a build stored without them, the lines of a generator that ships with a backend are
    recorded by running it in a geometry worker; other code then lists no features."""
    if record.sources:
        name, features = _example_name(record.backend, record.code), record.sources
    else:
        params = json.dumps(record.parameters)
        name, features = _feature_sources(record.backend, record.code, params)
    return {
        "build_id": record.id,
        "backend": record.backend,
        "language": "python",
        "filename": f"{name or 'design'}.py",
        "code": record.code,
        "features": features,
    }


def _example_name(backend: str, code: str) -> str | None:
    """The name of the backend's example generator whose code this is, read from its files
    (`GeometryBackend.examples`) without loading the backend."""
    import geometry

    examples = Path(geometry.__file__).parent / "backends" / backend / "examples"
    return next((p.stem for p in sorted(examples.glob("*.py")) if p.read_text() == code), None)


@lru_cache(maxsize=64)
def _feature_sources(backend: str, code: str, params: str) -> tuple[str | None, list[dict]]:
    from geometry.worker import call

    return call("app.agent.services:worker_feature_sources", backend, code, params)


def stored_artifacts(store: BuildStore, record: StoredBuild, build: Build) -> BuildArtifacts:
    """Everything served for a stored build."""
    return BuildArtifacts(
        mesh=stored_mesh(store, record),
        checks=stored_checks(store, record),
        source=stored_source(record),
        maps={m: stored_map(store, record, m) for m in build.maps},
    )


def imported_artifacts(imported: ImportedFile, build_id: str) -> BuildArtifacts:
    """What the session API serves for an imported build (or a restore of one)."""
    result = imported.result
    return BuildArtifacts(
        mesh=json.dumps(result["mesh"], separators=(",", ":")),
        checks={**result["checks"], "build_id": build_id},
        source={**result["source"], "build_id": build_id},
        maps={t: {**m, "build_id": build_id} for t, m in result["maps"].items()},
    )


# --- Configuration ----------------------------------------------------------------------------

# Given the session and the message, the driver for the run; None means the Claude Agent SDK.
DriverFactory = Callable[["SessionState", MessageCreate, str], Driver | None]


def geometry_services(board: Board | None, standards: StandardsFile) -> Services:
    """The real services, storing builds in CONFIG's store. `board` is None for a design
    without a board."""
    return GeometryServices(board, standards, store=CONFIG.store())


def fixture_services(board: Board | None, standards: StandardsFile) -> Services:
    """The contract fixtures, for frontend development and tests. They are the board
    example's enclosure, so a design without a board gets the real services: a fixture can't
    stand in for a design Claude writes from scratch."""
    if board is None:
        return geometry_services(board, standards)
    return FixtureServices(board=BoardRef(name=board.name, revision=board.revision))


@dataclass
class SessionConfig:
    # The agent's services for a session's board (None: a design without a board).
    services: Callable[[Board | None, StandardsFile], Services] = geometry_services
    driver: DriverFactory = lambda state, body, prompt: None
    store: Callable[[], BuildStore] = default_store


CONFIG = SessionConfig()


def has_credentials() -> bool:
    return any(os.environ.get(name, "").strip() for name in CREDENTIALS)


# --- Session state ----------------------------------------------------------------------------


def board_named(ws: Workspace, ref: BoardRef | None) -> Board | None:
    """The workspace's board that `ref` names (its board or the previous revision); the
    workspace's board when `ref` names neither."""
    for board in (ws.board, ws.previous):
        if (
            board is not None
            and ref is not None
            and (board.name, board.revision)
            == (
                ref.name,
                ref.revision,
            )
        ):
            return board
    return ws.board


def _saved_standards(saved: dict[str, Any] | None) -> StandardsFile:
    """A saved session's standards set (its own copy); the built-in set for older saves."""
    if saved is None:
        return load_standards()
    defaults = [Standard.model_validate(d) for d in saved.get("defaults", [])]
    rest = {k: v for k, v in saved.items() if k != "defaults"}
    return StandardsFile.model_validate(rest).model_copy(update={"defaults": defaults})


class SessionState:
    """One session: its workspace, messages, events, builds, comments and exports."""

    def __init__(
        self,
        session: Session,
        ws: Workspace,
        *,
        telemetry: SessionTelemetry | None = None,
        example: Example | None = None,
    ):
        self.session = session
        self.example = example  # the example the session opened, if any
        self.telemetry = telemetry  # the session's telemetry log, unless it is off
        self.ws = ws
        self.messages: list[Message] = []
        self.events: list[tuple[int, Any]] = []
        self.wake = asyncio.Event()
        self.builds: dict[str, Build] = {}
        # The design (`Design.snapshot()`) each build was made from, for diffs and restores.
        self.designs: dict[str, dict[str, Any]] = {}
        self.comments: list[Comment] = []
        self.exports: dict[str, Export] = {}
        self.export_dirs: dict[str, Path] = {}
        self.attachments = Attachments()
        # Imported STEP files by build ID (a restore of one shares it), in import order.
        self.imports: dict[str, ImportedFile] = {}
        self.importing = asyncio.Lock()
        self._import_ids = itertools.count(1)
        self.question: Any = None
        self.next_message = 1
        # The board example's parameters are described (`fixtures/parameters.json`); a design
        # written in the session lists its own top-level values (`parameter_list`).
        self.parameters = (
            [Parameter.model_validate(p) for p in _fixture("parameters.json")]
            if example is not None
            else []
        )
        named = {p.name for p in self.parameters}
        for name, description in STACK_PARAMETERS.items():
            value = ws.design.params.get(name)
            if name not in named and isinstance(value, int | float):
                self.parameters.append(
                    Parameter(
                        name=name, value=value, unit="mm", locked=False, description=description
                    )
                )
        self.sources: dict[str, str] = {}  # build ID -> hand-edited code it was built from
        # The code before the first hand edit Claude hasn't seen yet (None: no such edit).
        self.code_before_edits: str | None = None
        self.initial: asyncio.Task | None = None
        self.run: asyncio.Task | None = None
        self.proposal: Proposal | None = None  # the change set of the latest run with changes
        self.risks: Flags | None = None  # the risk flags of the latest run that flagged any
        # The changes the user rejected in a review, by change ID: `{id, title, at}`. Every
        # later run is told of them, so Claude doesn't propose them again unasked.
        self.rejected: dict[str, dict[str, str]] = {}
        # Each build's checks, when this process made or saw them (to tell what an edit broke).
        self.check_items: dict[str, list[CheckItem]] = {}
        # A hand edit that broke a standard, waiting for the user's decision (`edit_decisions`).
        self.edit_decision: EditDecision | None = None
        # The failing standards the user accepted by keeping their value, for later runs.
        self.accepted_failures: list[dict[str, Any]] = []
        # Named by its first request, then by Claude after the part it designs, until the user
        # renames it (`title_locked`). False for an example or a given title.
        self.auto_title = False
        ws.on_title = self.claude_title
        self.reviewing = asyncio.Lock()
        self.store: SessionStore | None = None  # where the session is saved; None: not saved
        self.preempted = False  # the run was stopped to give its slot to another session's
        # Messages sent while Claude was working, in order (`app.message_queue`).
        self.queue: list[QueuedMessage] = []
        # The run is being stopped: `STOPPED` (the user pressed Stop, `POST .../stop`: an
        # `error` with Retry) or the note its reply ends on (a queued message sent anyway).
        self.stopping: str | None = None
        self.queue_hold = False  # a queued message is being sent anyway: the queue waits
        self.queue_paused = False  # the last `queue` event said a question paused the queue
        self.closed = False  # the server is shutting down: event streams end
        self.save_failed = False  # the last save failed; the chat was told (`save`)
        # The latest run's message and prompt, for Retry (`POST .../retry`).
        self.last_run: dict[str, Any] | None = None
        # Claude's question outlived a restart: its run is gone, so the answer starts a new run
        # on the same request with the answer (`_answer_after_restart`) instead of resuming it.
        self.answer_restarts = False
        self.owner: str | None = None  # the browser the session belongs to (`app.owners`)
        self.spec = self.new_spec(ws.standards)
        # Told to Claude with the next message: the user changed the standards (PUT .../standards).
        self.standards_note: str | None = None
        self.new_design = False  # started with no starting case (`SessionCreate.new_design`)
        # Every sketch made on a face, by tag; the design's `params["sketches"]` says which are in.
        self.sketches: dict[str, Sketch] = {}
        ws.finder = self.find  # Claude's find_in_model answers as `POST .../find` does
        # Every modeling edit made (`app.modeling_api`), by tag; `params["edits"]` says
        # which are in the design.
        self.edits: dict[str, Edit] = {}
        # The design timeline (`app.design_history`): undo and redo over every change.
        self.history = History()
        # Conversations (`app.conversations`): the one the run going (or paused on its
        # question) belongs to, the questions parked while other conversations ran (by
        # conversation: the question and the run that asked), and what each run did.
        self.run_conversation: str | None = None
        self.parked: dict[str, dict[str, Any]] = {}
        self.reports: list[dict[str, Any]] = []
        self.conversations_sent: list[dict[str, Any]] | None = None

    def claude_title(self, title: str) -> str:
        """Claude names the part (`finish` `title`, already cleaned): the session takes the
        name unless the user named it, it was opened with one, or it already has it. Returns
        what came of it, for Claude."""
        s = self.session
        if s.title_locked:
            return "The user named this session, so it keeps their name."
        if not self.auto_title:
            return "This session was named when it was opened, so it keeps that name."
        if title == s.title:
            return "The session already has this name."
        mid = self.ws.sink.message_id if self.ws.sink._open else None
        self.emit(0, TitleEvent(type="title", message_id=mid, title=title))
        return f"The session is now named {title}."

    def new_spec(self, standards: StandardsFile) -> Spec:
        """A spec whose chips list the standards that apply to this session's design."""
        spec = Spec(standards)
        spec.has_board = lambda: self.ws.board is not None
        spec.has_object = lambda: self.ws.current_object() is not None
        return spec

    def idle(self) -> bool:
        """Nothing is running or waiting for an answer: dropping the session loses no work."""
        busy = [t for t in (self.initial, self.run) if t is not None and not t.done()]
        return self.session.status == "idle" and not busy and not self.reviewing.locked()

    def needs_plan(self) -> bool:
        """A new design starts with Claude's build plan; the existing case doesn't."""
        return self.new_design and self.ws.plan is None

    def apply_spec(self) -> None:
        """Give the spec's constraints to Claude, and its values to the standards the checks
        use. A changed value makes the latest build and report out of date."""
        ws = self.ws
        ws.constraints = [
            c.model_dump(mode="json", exclude_none=True) for c in self.spec.constraints()
        ]
        standards = self.spec.effective()
        if standards == ws.standards:
            return
        old = ws.services
        ws.standards = standards
        ws.services = CONFIG.services(self.checked_board, standards)
        for shared in ("_ids", "chain"):  # keep build IDs unique and the build history one chain
            if hasattr(old, shared) and hasattr(ws.services, shared):
                setattr(ws.services, shared, getattr(old, shared))
        ws._changed()

    def message_id(self) -> str:
        mid = f"msg-{self.next_message}"
        self.next_message += 1
        return mid

    def emit(self, _sink_id: int, event: Any) -> None:
        """An event from the agent (an `EventSink` listener): number it within the session."""
        if self.telemetry is not None:
            self.telemetry.event(self, event)
        if isinstance(event, BuildReadyEvent):
            event = event.model_copy(update={"build": self.with_urls(self.explained(event.build))})
        event_id = len(self.events) + 1
        self.events.append((event_id, event))
        if self.store is not None:
            try:
                self.store.append_event(self.session.id, event_id, event.model_dump(mode="json"))
            except Exception:  # saving never breaks a session
                log.exception("session %s: can't save event %s", self.session.id, event_id)
        self._apply(event)
        if event.type not in STREAMED:
            self.save()
        if event.type in ("question", "message_end"):
            self.step_done()
        self.wake.set()
        self.wake = asyncio.Event()
        if event.type == "question":
            run_changed(self)

    def end_streams(self) -> None:
        """End the session's event streams (shutdown): browsers reconnect and load it again."""
        self.closed = True
        self.wake.set()
        self.wake = asyncio.Event()  # a set event left in place would spin the streams

    def explained(self, build: Build) -> Build:
        """A failed build with why it failed in plain words (`Build.failure`)."""
        if build.status != "failed" or build.failure is not None:
            return build
        latest = self.ws.latest
        same = latest is not None and latest.build is not None and latest.build.id == build.id
        error = latest.error if same else None  # type: ignore[union-attr]
        return build.model_copy(update={"failure": plain_failure(error)})

    def step_done(self) -> None:
        """The last user action has played out: telemetry logs the canvas state."""
        if self.telemetry is not None:
            self.telemetry.step_done(self)

    # --- Build artifacts: an imported file's, stored ones for real builds, else the fixtures

    def recorded(self, build: Build) -> BuildArtifacts | None:
        imported = self.imports.get(build.id)
        return imported_artifacts(imported, build.id) if imported is not None else None

    def stored(self, build: Build) -> tuple[BuildStore, StoredBuild] | None:
        store = CONFIG.store()
        try:
            return store, store.get(build.id)
        except BuildNotFound:
            return None

    def showing_import(self) -> bool:
        """Whether the design on the canvas is an imported file, which has no parameters."""
        return self.session.latest_build_id in self.imports

    def import_list(self) -> list[ImportedFile]:
        """Each imported file once, in import order."""
        return list({id(i): i for i in self.imports.values()}.values())

    def new_build_id(self) -> str:
        """A build ID from the services' counter, so IDs stay unique within the session."""
        ids = getattr(self.ws.services, "_ids", None)
        if ids is None:  # real builds: a random ID, as the build store makes them
            build_id = new_store_build_id()
            while build_id in self.builds:
                build_id = new_store_build_id()
            return build_id
        build_id = f"build-{next(ids)}"
        while build_id in self.builds:
            build_id = f"build-{next(ids)}"
        return build_id

    def artifacts(self, build: Build) -> BuildArtifacts:
        recorded = self.recorded(build)
        if recorded is not None:
            return recorded.model_copy(deep=True)
        stored = self.stored(build)
        return stored_artifacts(*stored, build) if stored else build_artifacts(build)

    def checks(self, build: Build) -> dict:
        recorded = self.recorded(build)
        if recorded is not None:
            return {**recorded.checks, "build_id": build.id}
        stored = self.stored(build)
        return stored_checks(*stored) if stored else build_checks(build)

    def source(self, build: Build) -> dict:
        recorded = self.recorded(build)
        if recorded is not None:
            source = {**recorded.source, "build_id": build.id}
        else:
            stored = self.stored(build)
            source = stored_source(stored[1]) if stored else build_source(build)
        design = self.designs.get(build.id)
        params = design["params"] if design else {}
        source = with_sketches(source, params.get("sketches", []))
        return with_edits(source, params.get("edits", []))

    def map(self, build: Build, map_type: str) -> dict:
        recorded = self.recorded(build)
        if recorded is not None:
            if map_type not in recorded.maps:
                raise HTTPException(404, f"build {build.id} has no {map_type} map")
            return {**recorded.maps[map_type], "build_id": build.id}
        stored = self.stored(build)
        return stored_map(*stored, map_type) if stored else build_map(build, map_type)

    def find(self, build_id: str, query: str) -> FindResult:
        """Find in model on one of the session's builds: its mesh, the session's parameters and
        its analysis maps (`app.find`). The route and Claude's `find_in_model` both use it."""
        build = self.build(build_id, ready=True)
        maps = {t: self.map(build, t) for t in build.maps}
        params = self.parameter_list()
        return find_in_build(query, self.mesh(build), params, maps, self.ws.standards)

    def mesh(self, build: Build) -> str:
        recorded = self.recorded(build)
        if recorded is not None:
            return recorded.mesh
        stored = self.stored(build)
        return stored_mesh(*stored) if stored else build_mesh(build)

    def face_ids(self, build: Build) -> frozenset[str]:
        recorded = self.recorded(build)
        if recorded is not None:
            return recorded.face_ids()
        stored = self.stored(build)
        return stored_face_ids(*stored) if stored else build_face_ids(build)

    def diff(self, old: Build, new: Build) -> BuildDiff:
        """What changed from build `old` to build `new`. Two builds in the build store are
        compared as the store compares them (`app.builds.diff_builds`); any other pair (fixture
        or imported builds) gets the contract-first diff (`app.build_diffs`)."""
        old_stored, new_stored = self.stored(old), self.stored(new)
        if old_stored is not None and new_stored is not None:
            return BuildDiff.model_validate(diff_builds(old_stored[1], new_stored[1]))
        a, b = self.designs.get(old.id), self.designs.get(new.id)
        return build_diff(old, new, (a, b) if a is not None and b is not None else None)

    # --- Comments ---------------------------------------------------------------------------

    def mesh_info(self, build: Build | None) -> MeshInfo | None:
        """The build's mesh, to place comment pins on; None for a failed build."""
        if build is None or build.status != "ready":
            return None
        return pins.mesh_info(self.mesh(build))

    def latest_build(self) -> Build | None:
        bid = self.session.latest_build_id
        return self.builds.get(bid) if bid else None

    def comment(self, comment_id: str) -> Comment:
        found = next((c for c in self.comments if c.id == comment_id), None)
        if found is None:
            raise HTTPException(404, f"no comment {comment_id}")
        return found

    def located_comments(self, build: Build | None = None) -> list[Comment]:
        mesh = self.mesh_info(build or self.latest_build())
        return [pins.located(c, mesh) for c in self.comments]

    def put_comment(self, comment: Comment, message_id: str | None = None) -> Comment:
        """Store a new or changed comment: it streams as a `comment` event, on the latest
        build."""
        located = pins.located(comment, self.mesh_info(self.latest_build()))
        event = CommentEvent(type="comment", message_id=message_id, comment=located)
        EventSink(listeners=[self.emit]).emit(event)
        return located

    def comment_reply(
        self, comment_id: str, text: str, *, claude: bool = False, message_id: str | None = None
    ) -> Comment:
        comment = self.comment(comment_id)
        comment = pins.reply(
            comment,
            reply_id=f"{comment.id}.reply-{len(comment.replies) + 1}",
            role="assistant" if claude else "user",
            author=pins.CLAUDE if claude else USER,
            text=text,
            created_at=now(),
            message_id=message_id,
        )
        if not claude:
            comment = comment.model_copy(update={"resolved": False})
        return self.put_comment(comment, message_id)

    def _apply(self, event: Any) -> None:
        s = self.session
        if event.type == "message_start":
            self.messages.append(
                Message(id=event.message_id, role="assistant", text="", created_at=now())
            )
            s.status = "running"
        elif event.type == "user_message":
            # Stored as it was sent; a restart applies what was streamed since the last save.
            if not any(m.id == event.message.id for m in self.messages):
                self.messages.append(event.message)
        elif event.type == "text":
            msg = next((m for m in self.messages if m.id == event.message_id), None)
            if msg is not None:
                msg.text += event.delta
        elif event.type == "build_ready":
            self.builds[event.build.id] = event.build
            latest = self.ws.latest
            if self.ws.report is not None and latest is not None and latest.build is not None:
                if latest.build.id == event.build.id:  # the agent's checks of this build
                    self.check_items[event.build.id] = list(self.ws.report)
            # An import's builds come from a file, not the workspace's design.
            if event.build.id not in self.imports:
                self.designs.setdefault(event.build.id, self.ws.design.snapshot())
            s.latest_build_id = event.build.id
        elif event.type == "question":
            self.question = event
            s.status = "waiting_for_answer"
        elif event.type == "error" and event.message_id is not None:
            msg = next((m for m in self.messages if m.id == event.message_id), None)
            if msg is not None:  # the stored reply says why it stopped
                msg.text = f"{msg.text}\n\n{event.message}" if msg.text.strip() else event.message
        elif event.type == "message_end":
            self.question = None
            s.status = "idle"
        elif event.type == "title":
            s.title = event.title
            s.title_locked = event.title_locked
        elif event.type == "thread_report":
            # The entry is a message of the main chat: stored as it was sent.
            if not any(m.id == event.message_id for m in self.messages):
                self.messages.append(
                    Message(
                        id=event.message_id,
                        role="assistant",
                        text=event.summary,
                        created_at=now(),
                        comments=[event.comment_id],
                    )
                )
        elif event.type == "comment":
            i = next((i for i, c in enumerate(self.comments) if c.id == event.comment.id), None)
            if i is None:
                self.comments.append(event.comment)
            else:
                self.comments[i] = event.comment

    def keep_proposal(self, proposal: Proposal) -> None:
        """Hold a run's change set, and serve its `base_build` (built while proposing, never
        streamed) with the other builds."""
        self.proposal = proposal
        build = proposal.base_build.build
        if build is None or build.id in self.builds:
            self.save()
            return
        if proposal.base_build.ok and proposal.base_checks:
            checks = CheckSummary.model_validate(summary(proposal.base_checks))
            build = build.model_copy(update={"checks": checks})
        self.keep_build(build)
        self.save()

    def keep_build(self, build: Build) -> None:
        """Serve a build that wasn't streamed, with the others (not as the latest)."""
        self.builds.setdefault(build.id, self.with_urls(build))

    def with_urls(self, build: Build) -> Build:
        base = f"{API}/sessions/{self.session.id}/"
        data = build.model_dump()
        for key in ("mesh_url", "checks_url", "source_url", "board_url", "step_url"):
            if data[key] and not data[key].startswith("/"):
                data[key] = base + data[key]
        return Build.model_validate(data)

    def build(self, build_id: str, *, ready: bool = False) -> Build:
        build = self.builds.get(build_id)
        if build is None:
            raise HTTPException(404, f"no build {build_id}")
        if ready and build.status != "ready":
            raise HTTPException(404, FAILED_BUILD)
        return build

    def restore_design(self, snapshot: dict[str, Any]) -> None:
        """Make an earlier build's design the current one: a person's undo, so it needs no
        approval. Locked parameters stay locked, at the restored values."""
        design = self.ws.design
        params = copy.deepcopy(snapshot["params"])
        for path, value in design.locked.items():
            new = get_path(params, path) if has_path(params, path) else None
            if json.dumps(new, sort_keys=True) != json.dumps(value, sort_keys=True):
                design.store.set(path, json.dumps(new), by="user", by_hand=True, approval=HAND_EDIT)
        design.params = params
        design.code = snapshot["code"]
        self.ws._changed()  # the design changed: rebuild and recheck before trusting a report

    @property
    def checked_board(self) -> Board | None:
        """The board the session's builds are checked against: its own (`Session.board`). An
        example's workspace holds the newer revision too; a session still on the older one (a
        demo copy recorded before the revision arrived) is checked on that until it loads the
        newer one."""
        return board_named(self.ws, self.session.board)

    def with_example(self, chosen: StandardsFile) -> StandardsFile:
        """`chosen` plus the constraints the session's example brings, if it opened one."""
        return self.example.with_standards(chosen) if self.example is not None else chosen

    def use_standards(self, chosen: StandardsFile) -> None:
        """Design and check against the standards set `chosen` from now on."""
        self.session.standards = chosen.id
        self.ws.standards = chosen
        self.refresh_services()

    def refresh_services(self) -> None:
        """Services for the session's board and standards, keeping the build IDs and history."""
        services = CONFIG.services(self.checked_board, self.ws.standards)
        if hasattr(services, "_ids") and hasattr(self.ws.services, "_ids"):
            services._ids = self.ws.services._ids  # type: ignore[attr-defined]
        if hasattr(services, "chain") and hasattr(self.ws.services, "chain"):
            services.chain = self.ws.services.chain  # type: ignore[attr-defined]
        self.ws.services = services

    # --- The design timeline (`app.design_history`) -----------------------------------------

    def put_state(self, saved: dict[str, Any]) -> None:
        """Make a state of the timeline (`design_history.capture`) the current one: the design
        with its locks and approvals, the standards set and the change set as it was."""
        design, kept = self.ws.design, saved["design"]
        design.code, design.backend = kept["code"], kept["backend"]
        design.params = copy.deepcopy(kept["params"])
        design.store = ParameterStore.from_json(kept["store"])
        design.approvals = {k: Approval.model_validate(a) for k, a in kept["approvals"].items()}
        if saved["standards"] is not None and saved["standards"] != self.session.standards:
            with contextlib.suppress(KeyError):
                owner = self.owner or std_sets.owner_key()
                self.use_standards(std_sets.find_set(session_store(), owner, saved["standards"]))
        self.proposal = (
            Proposal.from_json(self.ws, copy.deepcopy(saved["proposal"]))
            if saved["proposal"]
            else None
        )
        self.risks = None  # flagged on another design
        self.ws._changed()  # the design changed: rebuild and recheck before trusting a report

    def record(
        self,
        before: dict[str, Any],
        kind: str,
        label: str,
        *,
        actor: str = "user",
        message_id: str | None = None,
    ) -> None:
        """A change from state `before` to now: a step in the timeline (none if nothing
        changed), saved with the session."""
        self.history.record(
            before,
            capture(self),
            actor=actor,
            kind=kind,
            label=label,
            created_at=now(),
            message_id=message_id,
        )
        self.save()

    # --- Saving and loading -----------------------------------------------------------

    def save(self) -> None:
        """Save the session to its store. Saving never breaks a session: a failure is logged."""
        if self.store is None:
            return
        try:
            self.store.save(self.session.id, self.saved())
        except Exception:
            log.exception("session %s: can't save it", self.session.id)
            if not self.save_failed:  # say so once, until a save works again
                self.save_failed = True
                self._notice_unsaved()
        else:
            self.save_failed = False

    def _notice_unsaved(self) -> None:
        """Tell the chat the session couldn't be saved; the run goes on in memory. Not saved
        itself (that is what failed): it streams, and is gone after a restart."""
        mid = self.ws.sink.message_id if self.ws.sink._open else None
        event = NoticeEvent(
            type="notice",
            message_id=mid,
            kind="warning",
            text=limits.SAVE_FAILED,
            reason="save_failed",
        )
        self.events.append((len(self.events) + 1, event))
        self.wake.set()
        self.wake = asyncio.Event()

    def saved(self) -> dict[str, Any]:
        """Everything the session needs after a restart, as JSON. The events are saved one by
        one (`emit`); `events` counts those the rest of the state already includes."""
        ws, design = self.ws, self.ws.design
        checked = ws.latest is not None and ws.latest.ok and ws.report is not None
        return {
            "session": self.session.model_dump(mode="json"),
            "owner": self.owner,
            # The session's own copy of its standards set, so an edit or delete elsewhere
            # changes nothing until the user chooses the set again.
            "standards": {
                **self.spec.standards.model_dump(mode="json"),
                "defaults": [d.model_dump(mode="json") for d in self.spec.standards.defaults],
            },
            "standards_note": self.standards_note,
            "events": len(self.events),
            "messages": [m.model_dump(mode="json") for m in self.messages],
            "builds": [b.model_dump(mode="json") for b in self.builds.values()],
            "designs": self.designs,
            "comments": [c.model_dump(mode="json") for c in self.comments],
            "exports": [e.model_dump(mode="json") for e in self.exports.values()],
            "export_dirs": {k: str(v) for k, v in self.export_dirs.items()},
            "attachments": {
                aid: a.model_dump(mode="json") for aid, (a, _) in self.attachments.files.items()
            },
            "question": self.question.model_dump(mode="json") if self.question else None,
            "queue": [q.model_dump(mode="json") for q in self.queue],
            "run_conversation": self.run_conversation,
            "parked": {
                k: {"question": p["question"].model_dump(mode="json"), "run": p["run"]}
                for k, p in self.parked.items()
            },
            "reports": self.reports,
            "next_message": self.next_message,
            "last_run": self.last_run,
            "parameters": [p.model_dump(mode="json") for p in self.parameters],
            "edits": {t: e.model_dump(mode="json") for t, e in self.edits.items()},
            "sketches": {t: k.model_dump(mode="json") for t, k in self.sketches.items()},
            "history": self.history.to_json(),
            "proposal": self.proposal.to_json() if self.proposal is not None else None,
            "risks": self.risks.to_json() if self.risks is not None else None,
            "rejected": list(self.rejected.values()),
            "accepted_failures": self.accepted_failures,
            "auto_title": self.auto_title,
            "design": {
                "code": design.code,
                "params": design.params,
                "backend": design.backend,
                "store": design.store.to_json(),
                "approvals": {k: a.model_dump(mode="json") for k, a in design.approvals.items()},
            },
            "next_build": _next_count(getattr(ws.services, "_ids", None)),
            "latest": ws.latest.build.model_dump(mode="json") if checked else None,  # type: ignore[union-attr]
            "report": [asdict(i) for i in ws.report] if checked else None,  # type: ignore[union-attr]
        }

    @classmethod
    def load(cls, store: SessionStore, session_id: str) -> SessionState | None:
        """A saved session, as it was at its last save, plus the events streamed since."""
        data = store.load(session_id)
        if data is None:
            return None
        saved_board = data["session"].get("board")
        standards = _saved_standards(data.get("standards"))
        example = None
        if saved_board is None:  # an empty session: Claude writes the first design
            ws = empty_workspace(services=CONFIG.services(None, standards), standards=standards)
        else:
            example = example_for_board(saved_board["name"], saved_board["revision"])
            ws = example_workspace(example, services=FixtureServices(), standards=standards)
            checked = board_named(ws, BoardRef.model_validate(saved_board))
            ws.services = CONFIG.services(checked, ws.standards)
        if data["next_build"] is not None and hasattr(ws.services, "_ids"):
            ws.services._ids = itertools.count(data["next_build"])  # type: ignore[attr-defined]
        saved = data["design"]
        design = ws.design
        design.code, design.params, design.backend = (
            saved["code"],
            saved["params"],
            saved["backend"],
        )
        design.store = ParameterStore.from_json(saved["store"])
        design.approvals = {k: Approval.model_validate(a) for k, a in saved["approvals"].items()}
        # The example it opened keeps its own parameter list (`design_parameters`).
        state = cls(Session.model_validate(data["session"]), ws, example=example)
        state.owner = data.get("owner")  # None: saved before sessions had owners
        state.standards_note = data.get("standards_note")
        state.messages = [Message.model_validate(m) for m in data["messages"]]
        state.builds = {b["id"]: Build.model_validate(b) for b in data["builds"]}
        state.designs = data["designs"]
        state.comments = [Comment.model_validate(c) for c in data["comments"]]
        state.exports = {e["id"]: Export.model_validate(e) for e in data["exports"]}
        state.export_dirs = {k: Path(v) for k, v in data["export_dirs"].items()}
        for aid, attachment in data["attachments"].items():
            content = store.read_attachment(session_id, aid)
            if content is not None:
                state.attachments.files[aid] = (Attachment.model_validate(attachment), content)
        if data["question"] is not None:
            state.question = validate_event(data["question"])
        state.next_message = data["next_message"]
        state.queue = [QueuedMessage.model_validate(q) for q in data.get("queue", [])]
        state.last_run = data.get("last_run")
        state.run_conversation = data.get("run_conversation")
        state.parked = {
            k: {"question": validate_event(p["question"]), "run": p["run"]}
            for k, p in (data.get("parked") or {}).items()
        }
        state.reports = data.get("reports") or []
        state.parameters = [Parameter.model_validate(p) for p in data["parameters"]]
        state.edits = {t: Edit.model_validate(e) for t, e in data.get("edits", {}).items()}
        state.sketches = {t: Sketch.model_validate(k) for t, k in data.get("sketches", {}).items()}
        state.history = History.from_json(data.get("history"))
        state.events = [(i, validate_event(e)) for i, e in store.events(session_id)]
        for _, event in state.events[data["events"] :]:  # streamed since the last save
            state._apply(event)
        ws.sink = EventSink(listeners=[state.emit])
        if data["latest"] is not None and data["report"] is not None:
            state._restore_checks(Build.model_validate(data["latest"]), data["report"])
        if data["proposal"] is not None:
            state.proposal = Proposal.from_json(ws, data["proposal"])
        if data.get("risks") is not None:  # the flags of the latest run, still to review
            state.risks = Flags.from_json(ws, data["risks"], state.proposal)
        state.rejected = {r["id"]: r for r in data.get("rejected") or []}
        state.accepted_failures = data.get("accepted_failures") or []
        state.auto_title = data.get("auto_title", False)
        state.store = store
        return state

    def _restore_checks(self, build: Build, report: list[dict]) -> None:
        """The current design's build and checks, so the change set names them. A service that
        can't analyze a build without its shapes rebuilds and rechecks on the next check."""
        ws = self.ws
        outcome = BuildOutcome(build)
        items = [CheckItem(**i) for i in report]
        try:
            analysis = ws.services.analyze(outcome)
            ws.risks = find_risks(items, analysis, ws.standards)
        except Exception:
            return
        ws.latest, ws.report = outcome, items
        ws.conflicts = (
            detect_conflicts(ws.board, ws.standards, ws.design.params, ws.design.locked)
            if ws.board is not None
            else []
        )

    def recover(self) -> None:
        """After a restart: close what the restart cut off. A run that was going ends with a
        note that the reply was interrupted; Claude's open question stays open, since nothing
        was running, and its answer carries on from the request; a first build that never
        arrived is built again."""
        first_build = not any(e.type in ("build_ready", "error") for _, e in self.events)
        q = self.question
        waiting = (
            self.session.status == "waiting_for_answer"
            and q is not None
            and self.last_run is not None
            and q.message_id == self.last_run.get("id")
        )
        if waiting:
            self.answer_restarts = True
        elif self.session.status != "idle":
            ended = {e.message_id for _, e in self.events if e.type == "message_end"}
            cut = list(
                dict.fromkeys(
                    e.message_id
                    for _, e in self.events
                    if e.type == "message_start" and e.message_id not in ended
                )
            )
            # The reply that was going says it was interrupted, and Retry picks it up.
            error = ErrorEvent(
                type="error",
                message_id=cut[-1] if cut else None,
                message=INTERRUPTED,
                retryable=self.last_run is not None,
                reason="interrupted",
            )
            if cut:
                for mid in cut[:-1]:
                    self.emit(0, MessageEndEvent(type="message_end", message_id=mid))
                self.emit(0, error)
                self.emit(0, MessageEndEvent(type="message_end", message_id=cut[-1]))
            else:
                sink = EventSink(self.message_id(), [self.emit])
                sink.start()
                self.emit(0, error.model_copy(update={"message_id": sink.message_id}))
                sink.end()
            self.session.status = "idle"
            self.question = None
            self.save()
        if first_build:
            self.initial = SESSIONS.spawn(_initial_build(self))
        if self.queue:  # the queue goes on where the restart left it
            asyncio.get_running_loop().call_soon(run_changed, self)

    # --- Parameters -------------------------------------------------------------------------

    def design_parameters(self) -> None:
        """A design written in the session: list each top-level number or text of its
        parameters that isn't listed yet, so a person can see and edit it."""
        if self.example is not None:
            return
        named = {p.name for p in self.parameters}
        for name, value in self.ws.design.params.items():
            if name in named or isinstance(value, bool) or not isinstance(value, int | float | str):
                continue
            try:
                self.parameters.append(Parameter(name=name, value=value, locked=False))
            except ValueError:  # a key that can't be a parameter name
                continue

    def parameter_list(self) -> list[Parameter]:
        """The parameters, with the design's current values and locks for those it holds. An
        imported file on the canvas has none: its geometry is the file's."""
        if self.showing_import():
            return []
        self.design_parameters()
        design = self.ws.design
        locked = design.locked
        out = []
        for p in self.parameters:
            if has_path(design.params, p.name):
                value = get_path(design.params, p.name)
                if isinstance(value, int | float | str) and not isinstance(value, bool):
                    p.value = float(value) if isinstance(value, int) else value  # 5 as 5.0
                p.locked = p.name in locked
            out.append(p)
        return out

    def measured_parameters(self) -> list[Parameter]:
        """`parameter_list()`, each with where it is measured on the latest build, then the
        design's grouped values that measure there (`app.dimensions`)."""
        params = self.parameter_list()
        build = self.latest_build()
        if not params or build is None or build.status != "ready":
            return measured(params, None)
        try:
            mesh = self.mesh(build)
        except Exception:  # noqa: BLE001 - no mesh to measure on: the values stand alone
            return measured(params, None)
        return measured(params, mesh, self.ws.design.params)

    def update_parameter(self, name: str, body: ParameterUpdate) -> Parameter:
        if self.showing_import():
            raise HTTPException(
                409, "the design on the canvas is an imported STEP file: it has no parameters"
            )
        self.design_parameters()
        param = next((p for p in self.parameters if p.name == name), None)
        if param is None:  # a grouped value shown as a dimension (`boss.radius`): listed now
            param = next((g for g in grouped_values(self.ws.design.params) if g.name == name), None)
            if param is not None:
                self.parameters.append(param)
        if param is None:
            raise HTTPException(404, f"no parameter {name}")
        if body.value is not None and (problem := value_error(param, body.value)):
            raise HTTPException(422, problem)
        design = self.ws.design
        in_design = has_path(design.params, name)
        lock = (
            body.locked if body.locked is not None else (True if body.value is not None else None)
        )
        if body.value is not None:
            param.value = body.value
            if in_design:
                if name in design.locked:
                    design.store.set(
                        name, json.dumps(body.value), by="user", by_hand=True, approval=HAND_EDIT
                    )
                    design.params = set_path(design.params, name, body.value)
                else:
                    design.lock(name, body.value)
        if lock is not None:
            param.locked = lock
            if in_design:
                if lock and name not in design.locked:
                    design.lock(name)
                elif not lock and name in design.locked:
                    design.store.unlock(name, approval=HAND_EDIT)
        if in_design and body.value is not None:
            self.ws._changed()  # the design changed: rebuild and recheck before trusting a report
        return next(p for p in self.parameter_list() if p.name == name)

    # --- Code ---------------------------------------------------------------------------------

    async def edit_code(self, code: str) -> Build | CodeError:
        """A hand edit to the generator code: build and check it, and if it builds, make it the
        design's code. The rebuild arrives as a `build_ready` with no message."""
        ws = self.ws
        filename = _fixture("build-source.json")["filename"]
        error = code_error(code, filename)
        if error is not None:
            return error
        outcome = await asyncio.to_thread(ws.services.build, {**ws.design.snapshot(), "code": code})
        if not outcome.ok:
            return build_error(outcome.error, code)
        items = await asyncio.to_thread(ws.services.checks, outcome)
        outcome.build = outcome.build.model_copy(
            update={"checks": CheckSummary.model_validate(summary(items))}
        )
        before = ws.design.code
        # A person's edit: it needs no approval, even while parameters are locked.
        ws.design.code = code
        if ws.base is not None:  # the edit is the user's, not a change Claude proposes
            ws.base = {**ws.base, "code": code}
        ws._changed()
        ws.latest = outcome
        ws.builds.append(outcome)
        if self.code_before_edits is None:
            self.code_before_edits = before
        self.sources[outcome.build.id] = code
        self.emit(0, BuildReadyEvent(type="build_ready", message_id=None, build=outcome.build))
        return self.builds[outcome.build.id]

    def review_decided(self, change_set: ChangeSet, accept: list[str], reject: list[str]) -> None:
        """Remember the changes a review rejected (and forget those accepted again)."""
        titles = {c.id: c.title for c in change_set.changes}
        for cid in accept:
            self.rejected.pop(cid, None)
        for cid in reject:
            self.rejected[cid] = {"id": cid, "title": titles.get(cid, cid), "at": now().isoformat()}

    def rejected_note(self) -> str | None:
        """What Claude is told of the rejected changes on every later run: None if none."""
        if not self.rejected:
            return None
        lines = [f"- {r['id']}: {r['title']} (rejected {r['at']})" for r in self.rejected.values()]
        return (
            "The user rejected these proposed changes in a review. Don't propose or make them "
            "again unless the user asks for them:\n" + "\n".join(lines)
        )

    def take_code_edits(self) -> str | None:
        """The note on the code edits Claude hasn't seen, once: None if there are none."""
        before, self.code_before_edits = self.code_before_edits, None
        if before is None or before == self.ws.design.code:
            return None
        return code_edit_note(
            before, self.ws.design.code, _fixture("build-source.json")["filename"]
        )

    # --- Sketches ---------------------------------------------------------------------------

    def sketch_list(self) -> list[Sketch]:
        """The sketched features in the current design (an undo can take some out)."""
        tags = [s["tag"] for s in self.ws.design.params.get("sketches", [])]
        return [self.sketches[t] for t in tags if t in self.sketches]

    def add_sketch(self, body: SketchCreate) -> tuple[Sketch, Build]:
        """A sketch on a face of a build: locked in the design's parameters and code. Returns
        it and the build it was sketched on."""
        if self.session.status == "running":
            raise HTTPException(409, "Claude is working: sketch when the run ends")
        build = self.build(body.build_id, ready=True)
        design = self.ws.design
        # Tags stay unique, even against a sketch an undo took out (its lock is still held).
        taken = [*design.params.get("sketches", [])]
        taken += [
            {"tag": p.name.split(".", 1)[1]} for p in design.store if p.name.startswith("sketches.")
        ]
        try:
            sketch = new_sketch(body, self.mesh(build), taken)
        except SketchError as error:
            raise HTTPException(409, str(error)) from error
        if sketch is None:
            raise HTTPException(404, f"no face {body.face_id}")
        design.add_sketch(sketch_params(sketch))
        self.ws._changed()  # the design changed: rebuild and recheck before trusting a report
        self.sketches[sketch.tag] = sketch
        return sketch, build


class Sessions:
    def __init__(self) -> None:
        self.by_id: dict[str, SessionState] = {}
        self.tasks: set[asyncio.Task] = set()
        self.count = 0  # sessions made since the server started
        self.running: set[SessionState] = set()  # the sessions whose runs hold a slot
        self.queue: list[SessionState] = []  # the sessions whose runs wait for one, in order
        self._loop: asyncio.AbstractEventLoop | None = None
        self._limits: dict[str, Any] = {}

    def next_id(self) -> str:
        """A new session ID: random and unguessable (`session-` and 96 random bits), so a
        session can't be found by counting."""
        self.count += 1
        taken = set(self.by_id)
        while True:
            sid = f"session-{secrets.token_hex(12)}"
            if sid not in taken and session_store().load(sid) is None:
                return sid

    def add(self, state: SessionState) -> None:
        """Keep a new session, dropping the oldest idle ones beyond `MAX_SESSIONS`."""
        cap = env_int("MAX_SESSIONS", 50)
        for sid, old in list(self.by_id.items()):
            if len(self.by_id) < cap:
                break
            # Only from memory: a saved session loads again when it is opened. One that isn't
            # saved (no store, or its last save failed) stays, so no one's work is dropped.
            if old.idle() and old.store is not None and not old.save_failed:
                del self.by_id[sid]
        self.by_id[state.session.id] = state

    def _on_loop(self, name: str, make: Callable[[], Any]) -> Any:
        """A process-wide primitive, made on the running event loop (a test server has its
        own)."""
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._limits = loop, {}
        if name not in self._limits:
            self._limits[name] = make()
        return self._limits[name]

    @contextlib.asynccontextmanager
    async def run_slot(
        self, state: SessionState, on_queued: Callable[[int], None] | None = None
    ) -> Any:
        """Hold one of the `MAX_AGENT_RUNS` slots for a run. A run that finds them all
        taken waits in line, fairly across browsers (`line`), and `on_queued` hears its
        place in line (1: next) each time it changes. While runs wait, runs waiting for an
        answer are stopped so that the line moves."""
        size = agent_runs()
        turn: asyncio.Condition = self._on_loop("run queue", asyncio.Condition)
        async with turn:
            self.queue.append(state)
            turn.notify_all()  # a run from another browser may go ahead of those waiting
            try:
                place = 0
                while (line := self.line())[0] is not state or len(self.running) >= size:
                    if place != (now_place := line.index(state) + 1):
                        place = now_place
                        if on_queued is not None:
                            on_queued(place)
                    for other in list(self.running) if len(self.running) >= size else []:
                        waiting = other.session.status == "waiting_for_answer"
                        if waiting and other.run is not None and not other.run.done():
                            other.preempted = True
                            other.run.cancel()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(turn.wait(), QUEUE_POLL_SECONDS)
            finally:
                self.queue.remove(state)
                turn.notify_all()  # the runs behind this one moved up
            self.running.add(state)
        try:
            yield
        finally:
            async with turn:
                self.running.discard(state)
                turn.notify_all()

    def line(self) -> list[SessionState]:
        """The runs waiting for a slot, in the order they will get one: each free slot goes to
        the browser with the fewest runs going, its earliest waiting run first."""
        running = Counter(s.owner for s in self.running)
        return fair_order([(s, s.owner) for s in self.queue], running)

    def place_in_line(self, state: SessionState) -> int | None:
        """Where a run waiting for a slot is in line (1: next), or None if it isn't waiting."""
        line = self.line()
        return line.index(state) + 1 if state in line else None

    def get(self, session_id: str) -> SessionState:
        """A session of the browser making the request (`app.owners`): another browser's
        session is a 404, as if it didn't exist. A share link's route (`app.share_routes`)
        serves its snapshot as the session."""
        view = SHARE_VIEW.get()
        if view is not None and view.session.id == session_id:
            return view
        state = self.by_id.get(session_id) or self.load(session_id, owned_only=True)
        if state is None or not self.owns(state):
            raise HTTPException(404, f"no session {session_id}")
        return state

    def owns(self, state: SessionState) -> bool:
        """Whether the request's browser owns the session. Outside a request, anyone does. A
        session saved without an owner (before sessions had owners) belongs to nobody: no
        browser can claim it, since anyone could (`docs/security.md`)."""
        owner = current_owner.get()
        return owner is None or state.owner == owner

    def owned_ids(self) -> list[str]:
        """The sessions of the request's browser, saved or in memory, oldest first; outside a
        request, all of them. Sessions that aren't in memory aren't loaded to find out."""
        owner = current_owner.get()
        ids = self.ids()
        if owner is None:
            return ids
        saved = session_store().owners()
        out = []
        for sid in ids:
            state = self.by_id.get(sid)
            if (state.owner if state is not None else saved.get(sid)) == owner:
                out.append(sid)
        return out

    def load(self, session_id: str, owned_only: bool = False) -> SessionState | None:
        """A session from the store, if it was saved (before a restart). `owned_only`: only
        if it is the requesting browser's, so another browser's guess loads (and recovers)
        nothing."""
        try:
            state = SessionState.load(session_store(), session_id)
        except Exception:  # a session saved in a format this version can't read
            log.exception("session %s: can't load it", session_id)
            return None
        if state is None or (owned_only and not self.owns(state)):
            return None
        self.add(state)  # loaded sessions count towards `MAX_SESSIONS` too
        state.recover()
        return state

    def ids(self) -> list[str]:
        """Every session, saved or in memory, oldest first."""
        saved = session_store().ids()
        return saved + [sid for sid in self.by_id if sid not in saved]

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self) -> None:
        for state in self.by_id.values():  # sessions left mid-run
            if state.telemetry is not None:
                state.telemetry.abandoned(state)
            # Stop saving: the saved session is as the shutdown found it, so a cut-off run
            # comes back as interrupted, not as finished by the cancellation below.
            state.store = None
            # End its event streams, so browsers reconnect (and find it again after the
            # restart) instead of waiting on a session that is gone.
            state.end_streams()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.by_id.clear()
        self.running.clear()
        self.queue.clear()
        self.count = 0


SESSIONS = Sessions()
router = APIRouter()


# --- Runs -------------------------------------------------------------------------------------


class BuildFailed(Exception):
    """A rebuild that must not leave a failed build (an undo, a restore) failed."""


async def _build_design(
    state: SessionState,
    board: Board | None,
    update: dict[str, Any] | None = None,
    *,
    failed_ok: bool = True,
) -> Build:
    """Build and check the current design against `board` (None: a design without a board),
    outside a run. The build arrives as a `build_ready` with no message. With `failed_ok`
    false, a build that fails raises `BuildFailed` instead, and nothing is streamed."""
    ws = state.ws
    snapshot = ws.design.snapshot()
    services = CONFIG.services(board, ws.standards)
    # Share the build counter so build IDs stay unique within the session, and the build
    # history, so each build is a child of the one before (the agent's first build a child of
    # the session's first).
    if hasattr(services, "_ids") and hasattr(ws.services, "_ids"):
        services._ids = ws.services._ids  # type: ignore[attr-defined]
    if hasattr(services, "chain") and hasattr(ws.services, "chain"):
        services.chain = ws.services.chain  # type: ignore[attr-defined]
    items = None
    # The build and its checks each take a build slot (`geometry.slots`) for as long as they
    # run, shared fairly with other browsers' builds.
    outcome = await asyncio.to_thread(services.build, snapshot)
    if not outcome.ok and not failed_ok:
        raise BuildFailed(plain_failure(outcome.error))
    build = outcome.build.model_copy(update=update or {})
    if not outcome.ok:
        build = build.model_copy(update={"failure": plain_failure(outcome.error)})
    if outcome.ok:
        items = await asyncio.to_thread(services.checks, outcome)
        checks = CheckSummary.model_validate(summary(items))
        build = build.model_copy(update={"checks": checks})
    state.designs[build.id] = snapshot
    if items is not None:
        state.check_items[build.id] = list(items)
    EventSink(listeners=[state.emit]).emit(
        BuildReadyEvent(type="build_ready", message_id=None, build=build)
    )
    return state.builds[build.id]


async def _initial_build(state: SessionState) -> None:
    """Build and check the existing design against the board it was designed for."""
    ws = state.ws
    sink = EventSink(listeners=[state.emit])
    try:
        # The chat says where the design came from: the existing design's file.
        origin = BuildOrigin(kind="existing", file_name=case_step_path().name)
        await _build_design(state, ws.previous, {"origin": origin})
    except Exception:  # report it on the stream rather than losing it
        log.exception("session %s: the first build failed", state.session.id)
        sink.error(FIRST_BUILD_FAILED, reason="build_crash")
    finally:
        state.step_done()
        state.save()


def run_changed(state: SessionState) -> None:
    """A run ended, paused on a question or resumed: the message queue hears it."""
    for listener in RUN_LISTENERS:
        listener(state)


def attached_files(state: SessionState, body: MessageCreate) -> list[AttachedFile]:
    """The message's attachments that were uploaded to this session, with their contents."""
    files = []
    for attachment in body.attachments:
        data = state.attachments.find(attachment)
        if data is not None:
            files.append(AttachedFile(attachment.name, attachment.media_type, data))
    return files


NEW_DESIGN_NOTE = (
    "This is a new design: propose the build plan (propose_plan) before any edit, asking only "
    "about the choices that change the geometry."
)


def prompt_for(
    body: MessageCreate,
    files: list[AttachedFile] | None = None,
    standards: StandardsFile | None = None,
    new_design: bool = False,
    comments: list[Comment] | None = None,
) -> str:
    """The message as Claude gets it: the text, then the comments sent with it and the open
    comments on the model (`comments`, located on the current build), what was selected, the
    attached files (`files`: those Claude can read) and the mode, the standards set when the
    message chose one, and for a new design that the plan comes first."""
    parts = [body.text.strip(), pins.context(comments or [], body.comments)]
    if body.selection is not None:
        selected = body.selection.model_dump(exclude_none=True)
        parts.append(f"Selected on the model: {json.dumps(selected)}")
    if body.attachments:
        readable = {f.name: f for f in files or []}
        lines = [
            f"- {a.name} ({a.media_type})"
            + (
                ": not uploaded to this session, so it can't be read"
                if a.name not in readable
                else ": a photo, shown with this message"
                if readable[a.name].is_image
                else ""
            )
            for a in body.attachments
        ]
        parts.append(
            "Attached files, in your workspace: read each with read_attachment by its name.\n"
            + "\n".join(lines)
        )
    if body.mode == "manual":
        parts.append(
            "Mode: Manual. Propose the changes and ask (ask_user) before applying any of them."
        )
    if standards is not None:
        parts.append(
            f"Constraint set: {standards.name} ({standards.id}). Design and check against "
            "these constraints (read_spec lists them)."
        )
    if new_design:
        parts.append(NEW_DESIGN_NOTE)
    return "\n\n".join(p for p in parts if p)


FIRST_BUILD_FAILED = (
    "The starting design couldn't be built just now. Nothing is lost: send your first message "
    "and Claude builds it again as it starts."
)


def queued_line(place: int) -> str:
    """The `waiting` notice of a run waiting in line for a slot (1: next)."""
    return limits.waiting_text(place)


def _queued(sink: EventSink, place: int) -> None:
    sink.notice("waiting", queued_line(place), reason="run_queue", place=place)


async def _run(
    state: SessionState, body: MessageCreate, prompt: str, files: list[AttachedFile] = ()
) -> None:
    conversation = state.run_conversation or convs.MAIN
    if state.initial is not None:
        await state.initial
    ws = state.ws
    # Any agent turn (a message or a loaded revision) sees the code edits made since the last.
    edits = state.take_code_edits()
    if edits:
        prompt = f"{prompt}\n\n{edits}"
    undone = state.history.take_note()  # the user undid changes of Claude's: like rejections
    if undone:
        prompt = f"{prompt}\n\n{undone}"
    for note in (state.rejected_note(), accepted_note(state.accepted_failures)):
        if note:
            prompt = f"{prompt}\n\n{note}"
    ws.attach(list(files))
    # Only a new design needs the build plan, and only until Claude has proposed it.
    ws.plan_required = state.needs_plan()
    ws.sink = EventSink(state.message_id(), [state.emit])
    ws.rounds, ws.finished = 0, None
    # A model found unavailable since the picker was loaded: the run goes on with the default.
    body = body.model_copy(update={"model": AVAILABILITY.usable(body.model)})
    config = RunConfig(model=body.model, effort=body.effort, mode=body.mode)
    telemetry, mid = state.telemetry, ws.sink.message_id
    _send_comments(state, body.comments, mid)
    if telemetry is not None:
        telemetry.run_start(mid, config.resolved(), body.mode)
    result, error = None, None
    earlier, design, before = None, None, None
    state.preempted, state.stopping = False, None
    state.last_run = {"message": body.model_dump(mode="json"), "prompt": str(prompt), "id": mid}
    try:
        driver = CONFIG.driver(state, body, prompt)
        if driver is None and not has_credentials():
            log.error("No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN: Claude can't run")
            error = NOT_CONFIGURED
            ws.sink.start()
            ws.sink.error(error, reason="not_configured")
            return
        # Each run proposes its own changes: from the design it starts on. Once the design
        # moves, the earlier set was proposed on an older design, so reviewing it would undo
        # this run's work. A run that leaves the design as it was (a question) keeps the set.
        ws.base, ws.edits = None, []
        earlier, design = state.proposal, ws.design.snapshot()
        before = capture(state)
        state.proposal = None
        state.risks = None
        # Only Claude itself (the SDK's CLI process) needs a slot; a scripted driver starts none
        # unless it asks for one (`uses_run_slot`, the e2e tests' busy server).
        if driver is None or getattr(driver, "uses_run_slot", False):
            slot = SESSIONS.run_slot(state, lambda place: _queued(ws.sink, place))
        else:
            slot = contextlib.nullcontext()
        async with slot:
            result = await run_agent(ws, prompt, config, driver=driver)
        # No await since run_agent sent `changes_ready` and `risks_ready`, so `/changes` and
        # `/risks` serve them from then on.
        if result.proposal is not None:
            state.keep_proposal(result.proposal)
        state.risks = result.risks
    except asyncio.CancelledError:
        if state.stopping == PARKED:
            pass  # its question stays open in its conversation (`park_question`)
        elif state.stopping is not None and state.stopping != STOPPED:
            # A queued message was sent anyway (`app.message_queue`): the reply ends on a note.
            ws.sink.progress(state.stopping, None, None)
        elif not (state.preempted or state.stopping):  # the server is shutting down
            raise
        else:
            error = PREEMPTED if state.preempted else STOPPED
            ws.sink.start()
            ws.sink.error(
                error, retryable=True, reason="preempted" if state.preempted else "stopped"
            )
    except Exception:  # run_agent reports SDK failures itself; this is anything else
        log.exception("session %s: run %s failed", state.session.id, mid)
        error = RUN_FAILED
        ws.sink.start()
        ws.sink.error(error, retryable=True, reason="unknown")
    finally:
        if result is not None:  # what the run cost, for the daily spend cap
            budget.record(result.cost_usd, result.input_tokens, result.output_tokens)
        if earlier is not None and state.proposal is None and ws.design.snapshot() == design:
            state.proposal = earlier
        titles: list[str] = []
        said = "".join(e.delta for e in ws.sink.of_type("text")).strip().split("\n")
        summary = (ws.finished or {}).get("summary") or said[-1]
        if before is not None:  # the run's applied changes are one step of the timeline
            ran = state.proposal if state.proposal is not earlier else None
            titles = [title(u, delta(u)) for u in ran.units] if ran is not None else []
            label = run_label(titles, summary)
            state.record(before, "run", label, actor="claude", message_id=mid)
        parked = state.stopping == PARKED
        if not parked:  # a parked run's thread still waits on its question
            _answer_comments(state, mid, error)
        ws.sink.end()
        if not parked:
            changed = design is not None and ws.design.snapshot() != design
            _report(state, conversation, mid, body, summary, titles, changed, error)
        state.run_conversation = None
        if state.session.status != "idle":
            state.session.status = "idle"
            state.question = None
        state.step_done()
        state.save()
        if telemetry is not None:
            telemetry.run_end(mid, config.resolved(), result, error)
        # Once this task is done: the next queued message starts.
        asyncio.get_running_loop().call_soon(run_changed, state)


def _send_comments(state: SessionState, comment_ids: list[str], mid: str) -> None:
    """The comments sent with a run: marked as sent to it, and given to the workspace, whose
    `reply_to_comment` answers them in their threads."""
    ws = state.ws
    ws.comments, ws.comment_links, ws.replied = [], {}, set()
    ws.on_reply, ws.on_end = None, None
    if not comment_ids:
        return
    for cid in comment_ids:
        request = CommentRequest(message_id=mid, status="running")
        state.put_comment(state.comment(cid).model_copy(update={"request": request}))
    ws.comments = [state.comment(cid) for cid in comment_ids]
    ws.on_reply = lambda cid, text: state.comment_reply(cid, text, claude=True, message_id=mid)
    ws.on_end = lambda error: _answer_comments(state, mid, error)


def _answer_comments(state: SessionState, mid: str, error: str | None) -> None:
    """At the end of the run, inside its message: every comment it was sent is answered. One
    Claude didn't reply to gets the run's summary (or its error) in its thread."""
    ws = state.ws
    if ws.comments and not ws.sink._open:
        ws.sink.start()  # a run that never started (no API key) still answers in the thread
    for comment in ws.comments:
        if comment.id not in ws.replied:
            said = "".join(e.delta for e in ws.sink.of_type("text"))
            summary = (ws.finished or {}).get("summary") or said
            text = f"I couldn't finish this: {error}" if error else summary.strip()
            state.comment_reply(comment.id, text or "Done.", claude=True, message_id=mid)
        status = "failed" if error else "answered"
        request = CommentRequest(message_id=mid, status=status)
        state.put_comment(
            state.comment(comment.id).model_copy(update={"request": request}), message_id=mid
        )
    ws.comments, ws.comment_links, ws.replied = [], {}, set()
    ws.on_reply, ws.on_end = None, None


def _report(
    state: SessionState,
    conversation: str,
    mid: str,
    body: MessageCreate,
    summary: str,
    titles: list[str],
    changed: bool,
    error: str | None,
) -> None:
    """A run ended: what it did goes in its conversation's history, and a thread's run reports
    back to the main chat (`ThreadReportEvent`): what changed, the checks and the build."""
    state.reports.append(
        {
            "conversation": conversation,
            "run_id": mid,
            "request": body.text.strip()[:500],
            "summary": (error or summary or "").strip()[:500],
            "changed": changed,
        }
    )
    del state.reports[: -convs.TIMELINE_ENTRIES * 4]  # bounded: only the latest are told
    if conversation == convs.MAIN or not any(c.id == conversation for c in state.comments):
        return
    build_id = state.session.latest_build_id if changed else None
    build = state.builds.get(build_id) if build_id else None
    ids = list(state.builds)
    state.emit(
        0,
        ThreadReportEvent(
            type="thread_report",
            message_id=state.message_id(),
            comment_id=conversation,
            thread_title=convs.title_of(state, conversation),
            run_id=mid,
            outcome="failed" if error else "changed" if changed else "answered",
            summary=(error or summary or "Replied in the thread.").strip()[:500],
            changes=titles[:8],
            build_id=build.id if build is not None else None,
            build_number=ids.index(build.id) + 1 if build is not None else None,
            checks=build.checks if build is not None else None,
        ),
    )


def conversations_changed(state: SessionState) -> None:
    """Stream the conversations when where any of them stands changed (a `RUN_LISTENERS`
    listener, and the message queue's)."""
    now_ = [c.model_dump(mode="json") for c in convs.conversations(state)]
    if now_ == state.conversations_sent:
        return
    if state.conversations_sent is None and len(now_) == 1:
        return  # the main chat alone: where it stands is the session's status and question
    state.conversations_sent = now_
    state.emit(
        0,
        ConversationsEvent(
            type="conversations", conversations=[Conversation.model_validate(c) for c in now_]
        ),
    )


RUN_LISTENERS.append(conversations_changed)


@router.get(
    SESSION + "/conversations",
    response_model=list[Conversation],
    responses=NOT_FOUND,
    tags=["messages"],
)
async def list_conversations(session_id: str) -> list[Conversation]:
    """The main chat and each comment thread sent to Claude, each its own conversation about
    the design: where it stands (working, queued behind another, waiting for an answer) and
    its open question. Changes stream as `conversations` events."""
    return convs.conversations(SESSIONS.get(session_id))


# --- Standards ------------------------------------------------------------------------------


# The standards sets (`app.standards_api`): the built-in set and each browser's own, kept in the
# session store.
router.include_router(standards_router(session_store))


RECHECK_BUILD_FAILED = (
    "The design doesn't build with the new value. Your earlier builds are kept: change the "
    "value back, or ask Claude to adjust the design to it."
)
RECHECK_FAILED = (
    "Rechecking against the new value failed on our side. Your design and builds are kept; "
    "change the value again to retry."
)


async def _recheck(state: SessionState, constraint: Constraint, removed: bool) -> None:
    """A chip was edited or removed: rebuild the design and recheck it against the new spec."""
    if state.initial is not None:
        await state.initial
    ws = state.ws
    ws.sink = EventSink(state.message_id(), [state.emit])
    try:
        ws.sink.start()
        ws.sink.constraints(state.spec.constraints())
        what = "Back to the set's value" if removed else "Spec changed"
        ws.sink.progress(f"{what}: {constraint.label}. Rebuilding and rechecking", None, None)
        ws._changed()
        report = await ws.check()
        if report.get("status") == "failed":
            limit = limits.BUILD_LIMITS.get(str((report.get("error") or {}).get("kind")))
            ws.sink.text(limit[1] if limit else RECHECK_BUILD_FAILED)
        else:
            s = report["summary"]
            ws.sink.text(
                f"Rechecked against the new spec: {s['pass']} pass, {s['fail']} fail, "
                f"{s['untested']} untested."
            )
    except Exception:  # report it on the stream rather than losing it
        log.exception("session %s: the recheck failed", state.session.id)
        ws.sink.error(RECHECK_FAILED, reason="unknown")
    finally:
        ws.sink.end()
        state.session.status = "idle"


@router.put(
    SESSION + "/standards",
    response_model=Session,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["standards"],
)
async def set_session_standards(session_id: str, body: SessionStandards) -> Session:
    """Design and check the session against this standards set from now on. Choosing a set
    (or choosing it again after editing it) rechecks the current design in a geometry worker
    and posts what changed as a chat notice on `/events`; Claude gets the new set from the next
    message. Only the user changes a session's standards."""
    state = SESSIONS.get(session_id)
    if state.session.status != "idle" or state.reviewing.locked() or state.importing.locked():
        raise HTTPException(409, "wait until Claude has finished replying")
    owner = state.owner or std_sets.owner_key()
    try:
        chosen = await asyncio.to_thread(std_sets.find_set, session_store(), owner, body.standards)
    except KeyError as error:
        raise HTTPException(404, f"no constraint set {body.standards}") from error
    chosen = state.with_example(chosen)  # an example's own constraints stay with its sessions
    ws = state.ws
    old = state.spec.standards
    before = list(ws.report) if ws.report is not None else None
    state.session.standards = chosen.id
    state.spec = state.spec.rebased(chosen)
    state.apply_spec()
    state.standards_note = claude_note(old, chosen)
    state.session.status = "running"
    state.save()
    state.run = SESSIONS.spawn(_restandardize(state, old, chosen, before))
    return state.session


async def _restandardize(
    state: SessionState, old: StandardsFile, new: StandardsFile, before: list | None
) -> None:
    """The session's standards changed: recheck the current design and say what changed."""
    if state.initial is not None:
        await state.initial
    ws = state.ws
    ws.sink = EventSink(state.message_id(), [state.emit])
    try:
        ws.sink.start()
        if state.spec.active:
            ws.sink.constraints(state.spec.constraints())
        if not ws.has_design:
            ws.sink.text(change_notice(old, new, before, None))
        else:
            ws.sink.progress(f"Constraint set: {new.name}. Rechecking the design", None, None)
            ws._changed()
            report = await ws.check()
            if report.get("status") == "failed":
                ws.sink.text(f"The build failed: {json.dumps(report.get('error'))}")
            else:
                ws.sink.text(change_notice(old, new, before, ws.report))
    except Exception as exc:  # report it on the stream rather than losing it
        ws.sink.error(f"The recheck failed: {type(exc).__name__}: {exc}", retryable=True)
    finally:
        ws.sink.end()
        state.session.status = "idle"
        state.save()


# --- Examples -------------------------------------------------------------------------------


@router.get(API + "/models", response_model=ModelCatalog, tags=["sessions"])
async def list_models() -> ModelCatalog:
    """The model picker's models: the catalogue (`contracts/models.json`) entries the server's
    API key can use, in the catalogue's order. The whole catalogue when that can't be checked."""
    await AVAILABILITY.refresh_if_stale()
    return AVAILABILITY.catalog()


@router.get(API + "/examples", response_model=list[ExampleSummary], tags=["sessions"])
async def list_examples() -> list[ExampleSummary]:
    """Designs a new session can start from (`demos/<board>/examples.yaml`), opened with
    `POST /api/sessions` and `{"example": id}`. A session starts empty otherwise."""
    return [ExampleSummary(id=e.id, title=e.title, description=e.description) for e in examples()]


def _example_for(body: SessionCreate) -> Example | None:
    """The example a new session opens: by ID, or the board revision example whose new
    revision is `board`; None for an empty session."""
    if body.example is not None:
        try:
            example = find_example(body.example)
        except KeyError as error:
            raise HTTPException(404, f"no example {body.example}") from error
        if body.board is not None:
            _, new = example.boards()
            if body.board != BoardRef(name=new.name, revision=new.revision):
                raise HTTPException(422, f"example {example.id} is not on {body.board.name}")
        return example
    if body.board is not None:
        try:
            return example_for_board(body.board.name, body.board.revision)
        except KeyError as error:
            raise HTTPException(422, f"no board {body.board.name} {body.board.revision}") from error
    return None


# --- Sessions and messages -------------------------------------------------------------------


@router.get(API + "/sessions", response_model=list[Session], tags=["sessions"])
async def list_sessions() -> list[Session]:
    """The browser's own sessions (`app.owners`), oldest first, then the demos every browser
    sees (`app.demos`): its own copy of each, or the demo's original until it opens it."""
    out = []
    for sid in SESSIONS.owned_ids():
        try:
            out.append(SESSIONS.get(sid).session)
        except HTTPException:  # saved in a format this version can't read
            continue
    return demos.listed(out)


@router.post(
    API + "/sessions",
    response_model=Session,
    status_code=201,
    responses={**NOT_FOUND, 422: {"model": ErrorBody}},
    tags=["sessions"],
)
async def create_session(body: SessionCreate) -> Session:
    """Start a session. With no example it starts empty: no board, no design and no build until
    Claude writes the first design. With `example` (or the `board` of an example's new
    revision) it opens that example; its existing design's first build streams on `/events`.
    With `from_share` it starts from a share link's snapshot (`app.shares`). At most
    `SESSIONS_PER_DAY` per owner and per IP (`app.security.limits`)."""
    rate_limits.check(rate_limits.SESSIONS)
    if body.from_share is not None:
        from app.shares import SHARES

        made = await SHARES.continue_from(body)
        rate_limits.record(rate_limits.SESSIONS)
        return made
    example = _example_for(body)
    standards = await asyncio.to_thread(load_standards)
    if example is None:
        ws = empty_workspace(services=CONFIG.services(None, standards), standards=standards)
        board, title = None, body.title or DEFAULT_TITLE
    else:
        # The services are swapped for CONFIG's once the boards are known.
        ws = await asyncio.to_thread(
            example_workspace, example, services=FixtureServices(), standards=standards
        )
        assert ws.board is not None
        ws.services = CONFIG.services(ws.board, ws.standards)
        board = BoardRef(name=ws.board.name, revision=ws.board.revision)
        title = body.title or example.title
    sid = SESSIONS.next_id()
    session = Session(
        id=sid, title=title, created_at=now(), board=board, status="idle", next_revision=board
    )
    state = SessionState(
        session,
        ws,
        telemetry=SessionTelemetry.open(sid),
        example=example,
    )
    if state.telemetry is not None:
        ws.observer = state.telemetry.observe
        state.telemetry.session_start(state)
    state.owner = current_owner.get()
    rate_limits.record(rate_limits.SESSIONS)
    state.auto_title = example is None and not body.title
    SESSIONS.add(state)
    state.store = session_store()
    state.new_design = body.new_design
    state.save()
    if ws.has_design:
        state.initial = SESSIONS.spawn(_initial_build(state))
    else:  # nothing to build until Claude writes the first design
        state.step_done()
    return session


@router.get(SESSION, response_model=Session, responses=NOT_FOUND, tags=["sessions"])
async def get_session(session_id: str) -> Session:
    return SESSIONS.get(session_id).session


@router.patch(
    SESSION,
    response_model=Session,
    responses={**NOT_FOUND, **CONFLICT, 422: {"model": ErrorBody}},
    tags=["sessions"],
)
async def rename_session(session_id: str, body: SessionUpdate) -> Session:
    """Rename the session (the browser that owns it). The name is the user's from then on:
    `title_locked`, and Claude no longer renames it. A `title` event (no `message_id`) tells
    every open view. A sample can't be renamed (409)."""
    state = SESSIONS.get(session_id)
    title = clean_title(body.title)
    if title is None:
        raise HTTPException(422, "a session name needs a letter or a digit")
    state.emit(0, TitleEvent(type="title", message_id=None, title=title, title_locked=True))
    state.save()
    return state.session


@router.get(
    SESSION + "/messages", response_model=list[Message], responses=NOT_FOUND, tags=["messages"]
)
async def list_messages(session_id: str) -> list[Message]:
    return SESSIONS.get(session_id).messages


@router.post(
    SESSION + "/messages",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["messages"],
)
async def post_message(session_id: str, body: MessageCreate) -> Message:
    """Send a message, or answer the open question. The reply streams on `/events`; the
    response is the stored user message. While Claude is working a message gets 409: queue it
    with `POST .../queue` instead. While Claude's question is open, a message that isn't its
    answer is sent all the same: the question is left unanswered (`leave_question_open`) and
    Claude is told about it with the message. Secrets and email addresses in the text are
    replaced with markers first (`app.security.scrub`); a message that starts Claude counts
    against the rate limits and the daily budget (`app.security`)."""
    rate_limits.take(rate_limits.MESSAGES)
    state = SESSIONS.get(session_id)
    return await send_now(state, body)


async def send_now(state: SessionState, body: MessageCreate) -> Message:
    """`POST .../messages` once the session is found: a question open in another conversation
    is parked first (`park_question`); one open in this message's conversation is left
    unanswered (`leave_question_open`), and Claude is told about it."""
    if state.session.status != "waiting_for_answer" or state.question is None:
        return send_message(state, body)
    live = convs.live_conversation(state)
    if body.answer is not None:
        if body.answer.question_id == state.question.question_id or not _parked_entry(
            state, body.answer.question_id
        ):
            return send_message(state, body)
    else:
        _sendable(state, body)  # refused before the question is touched
    state.queue_hold = True  # the queue waits: this message goes first
    try:
        if body.answer is not None or convs.conversation_of(body) != live:
            await park_question(state)
            return send_message(state, body)
        note = await leave_question_open(state, QUESTION_LEFT_OPEN)
        return send_message(state, body, note)
    finally:
        state.queue_hold = False


PARKED = "parked"  # `state.stopping` of a run parked on its question (`park_question`)
# What a run started by the answer to a parked question is told, after the request it asked on.
PARKED_ANSWER_NOTE = (
    "(You asked the user: {question} While the question waited, other conversations' runs "
    "went on with the same design, so this is a new run on the same request. The user "
    "answered: {answer}. Read the spec to see where the design stands now, carry on from there "
    "with that answer, and don't ask the same question again.)"
)


def _parked_entry(state: SessionState, question_id: str) -> tuple[str, dict[str, Any]] | None:
    return next(
        ((k, p) for k, p in state.parked.items() if p["question"].question_id == question_id),
        None,
    )


async def park_question(state: SessionState) -> None:
    """Another conversation's run is to start while Claude's question waits: the run that asked
    ends, and the question stays open in its own conversation; its answer starts a new run on
    the same request (`_answer_parked`). A hand edit's decision isn't a run: it is closed."""
    q = state.question
    if state.session.status != "waiting_for_answer" or q is None:
        return
    decision = state.edit_decision
    if decision is not None and decision.question_id == q.question_id:
        await leave_question_open(state, QUESTION_LEFT_OPEN)
        return
    conversation = convs.live_conversation(state) or convs.MAIN
    state.parked[conversation] = {"question": q, "run": dict(state.last_run or {})}
    if (state.run is None or state.run.done()) and state.answer_restarts:
        # Kept across a restart: there is no run to end, only its reply.
        state.emit(0, MessageEndEvent(type="message_end", message_id=q.message_id))
        state.answer_restarts = False
        state.session.status = "idle"
        state.question = None
        state.run_conversation = None
        state.save()
        return
    await end_paused_run(state, PARKED)


def _sendable(state: SessionState, body: MessageCreate) -> None:
    """The checks a message that starts Claude passes, whatever the session is doing."""
    if state.reviewing.locked():
        raise HTTPException(409, "the accepted changes are still rebuilding")
    if state.importing.locked():
        raise HTTPException(409, "the imported file is still being read")
    if not body.text.strip() and not body.comments:
        raise HTTPException(409, "a message needs text, comments or an answer")
    for cid in body.comments:
        if state.comment(cid).resolved:
            raise HTTPException(409, f"comment {cid} is resolved")
    rate_limits.check(rate_limits.RUNS)
    budget.check()


def open_question_note(state: SessionState, q: Any = None) -> str | None:
    """What the next run is told about Claude's open question (`q`, else the session's): what
    it asked, and in which comment thread when its run answered comments."""
    q = q or state.question
    if q is None:
        return None
    threads = [
        c for c in state.comments if c.request is not None and c.request.message_id == q.message_id
    ]
    where = "."
    if threads:
        named = "\n".join(f"- comment {c.id}: {c.text[:200]}" for c in threads)
        where = " in the comment thread it was about:\n" + untrusted("comment threads", named)
        where += "\n"
    return OPEN_QUESTION_NOTE.format(question=q.text, where=where)


async def end_paused_run(state: SessionState, note: str) -> None:
    """Cancel the session's run (going, or paused on its question); its reply ends with
    `note` and the session is idle again."""
    run = state.run
    if (run is None or run.done()) and state.answer_restarts and state.question is not None:
        # A question kept across a restart has no run to cancel: its reply ends on the note.
        mid = state.question.message_id
        state.emit(0, ProgressEvent(type="progress", message_id=mid, text=note))
        state.emit(0, MessageEndEvent(type="message_end", message_id=mid))
        state.answer_restarts = False
        state.session.status = "idle"
        state.question = None
        state.save()
        return
    if run is None or run.done():
        raise HTTPException(409, "Claude isn't working: there is nothing to stop")
    state.stopping = note
    run.cancel()
    await asyncio.wait({run})
    if state.session.status != "idle":  # stopped before its reply started
        state.session.status = "idle"
        state.question = None
        state.save()


async def leave_question_open(state: SessionState, note: str) -> str | None:
    """Close Claude's open question unanswered, because the user sent something else: its
    reply ends with `note` (a hand edit's decision keeps the edit as it is). Returns what the
    next run is told about the question; None when no question was open."""
    q = state.question
    if state.session.status != "waiting_for_answer" or q is None:
        return None
    told = open_question_note(state)
    decision = state.edit_decision
    if decision is not None and decision.question_id == q.question_id:
        _close_edit_decision(state, note)
        state.question = None
        state.session.status = "idle"
        state.save()
    else:
        await end_paused_run(state, note)
    return told


def send_message(state: SessionState, body: MessageCreate, note: str | None = None) -> Message:
    """Store the message and start its run (or resume the paused run with the answer): the
    checks and start of `POST .../messages`, also used to send a queued message. `note` goes
    to Claude after the message (a question it left open)."""
    status = state.session.status
    parked = _parked_entry(state, body.answer.question_id) if body.answer is not None else None
    if parked is not None:  # a question parked in its conversation (`park_question`)
        assert body.answer is not None
        if body.answer.option_id not in {o.id for o in parked[1]["question"].options}:
            raise HTTPException(409, f"no option {body.answer.option_id}")
        if status != "idle":
            raise HTTPException(409, "the session is still running")
    elif body.answer is not None:
        q = state.question
        if status != "waiting_for_answer" or q is None or q.question_id != body.answer.question_id:
            raise HTTPException(409, f"no open question {body.answer.question_id}")
        if body.answer.option_id not in {o.id for o in q.options}:
            raise HTTPException(409, f"no option {body.answer.option_id}")
    elif status == "running":
        raise HTTPException(409, "the session is still running")
    elif state.reviewing.locked():
        raise HTTPException(409, "the accepted changes are still rebuilding")
    elif state.importing.locked():
        raise HTTPException(409, "the imported file is still being read")
    elif status == "waiting_for_answer":
        raise HTTPException(409, "answer the open question first")
    elif not body.text.strip() and not body.comments:
        raise HTTPException(409, "a message needs text, comments or an answer")
    for cid in body.comments:
        if state.comment(cid).resolved:
            raise HTTPException(409, f"comment {cid} is resolved")
    if body.answer is None:  # this message starts Claude
        rate_limits.check(rate_limits.RUNS)
        budget.check()
    text, removed = scrub_user_text(body.text)
    body = body.model_copy(update={"text": text})
    try:
        chosen = std_sets.choose(
            session_store(),
            state.owner or std_sets.owner_key(),
            state.session.standards,
            body.standards,
            not state.messages,
        )
    except KeyError as error:
        raise HTTPException(404, f"no constraint set {body.standards}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if chosen is not None:
        chosen = state.with_example(chosen)  # an example's own constraints stay with it
        # The agent reads these standards (read_spec) and the checks run against them.
        state.use_standards(chosen)
    first = not any(m.role == "user" for m in state.messages)
    message = _user_message(state, body)
    if first and state.auto_title and not state.session.title_locked:
        named = title_for(body.text) if state.session.title == DEFAULT_TITLE else None
        if named:  # named from its first request, in the user's words
            state.session.title = named
    if parked is not None:
        return _answer_parked(state, parked[0], body, message)
    decision = state.edit_decision
    if body.answer is not None and decision and decision.question_id == body.answer.question_id:
        return _decide_edit(state, decision, body, message)
    if body.answer is not None and state.answer_restarts:
        return _answer_after_restart(state, body, message)
    if body.answer is not None:
        try:
            state.ws.answer_question(body.answer.question_id, body.answer.option_id)
        except KeyError as error:
            state.messages.remove(message)
            raise HTTPException(409, f"no open question {body.answer.question_id}") from error
        state.question = None
        state.session.status = "running"
        state.save()
        run_changed(state)  # the queue isn't paused any more
        return message
    if chosen is not None:  # the spec starts from the chosen standards set
        state.spec = state.new_spec(chosen)
    if state.spec.describe(body.text, message.id, locked=state.ws.design.locked):
        state.apply_spec()
        state.emit(0, ConstraintsEvent(type="constraints", constraints=state.spec.constraints()))
    state.session.status = "running"
    state.save()
    files = attached_files(state, body)
    prompt = prompt_for(body, files, chosen, state.needs_plan(), state.located_comments())
    if state.standards_note is not None:  # the user changed the standards since the last run
        prompt += "\n\n" + state.standards_note
        state.standards_note = None
    if removed:
        prompt += "\n\n" + REMOVED_NOTE
    conversation = convs.conversation_of(body)
    if conversation in state.parked:  # its parked question is left unanswered
        left = state.parked.pop(conversation)
        note = "\n\n".join(n for n in (note, open_question_note(state, left["question"])) if n)
    if note:
        prompt += "\n\n" + note
    told = convs.context_for(state, conversation)
    if told:
        prompt += "\n\n" + told
    state.run_conversation = conversation
    notes = import_notes(state.import_list(), state.session.latest_build_id)
    if notes:
        prompt += "\n\n" + notes
    rate_limits.record(rate_limits.RUNS)
    state.run = SESSIONS.spawn(_run(state, body, prompt, files))
    run_changed(state)  # the conversations show the run
    return message


def _answer_parked(
    state: SessionState, conversation: str, body: MessageCreate, message: Message
) -> Message:
    """The answer to a question parked in its conversation: the run that asked has ended, so a
    new run on the same request carries on with the answer, from the design as it is now."""
    entry = state.parked[conversation]
    q, last = entry["question"], entry["run"]
    assert body.answer is not None
    label = next(o.label for o in q.options if o.id == body.answer.option_id)
    rate_limits.check(rate_limits.RUNS)
    budget.check()
    del state.parked[conversation]
    state.session.status = "running"
    state.save()
    asked = MessageCreate.model_validate(last["message"]) if last.get("message") else body
    prompt = "\n\n".join(
        p
        for p in (
            last.get("prompt", ""),
            PARKED_ANSWER_NOTE.format(question=q.text, answer=label),
            convs.context_for(state, conversation),
        )
        if p
    )
    files = attached_files(state, asked)
    state.run_conversation = conversation
    rate_limits.record(rate_limits.RUNS)
    state.run = SESSIONS.spawn(_run(state, asked, prompt, files))
    run_changed(state)
    return message


def _answer_after_restart(state: SessionState, body: MessageCreate, message: Message) -> Message:
    """The answer to a question that outlived a restart: the run that asked is gone, so its
    reply ends and a new run on the same request carries on with the answer."""
    q, last = state.question, state.last_run
    assert q is not None and last is not None and body.answer is not None
    label = next(o.label for o in q.options if o.id == body.answer.option_id)
    rate_limits.check(rate_limits.RUNS)
    budget.check()
    state.answer_restarts = False
    state.emit(0, MessageEndEvent(type="message_end", message_id=q.message_id))
    state.session.status = "running"
    state.save()
    asked = MessageCreate.model_validate(last["message"])
    prompt = f"{last['prompt']}\n\n{ANSWERED_NOTE.format(question=q.text, answer=label)}"
    files = attached_files(state, asked)
    rate_limits.record(rate_limits.RUNS)
    state.run = SESSIONS.spawn(_run(state, asked, prompt, files))
    run_changed(state)  # the queue isn't paused any more
    return message


def _user_message(state: SessionState, body: MessageCreate) -> Message:
    """Store the user's message, and log and record it."""
    message = Message(
        id=state.message_id(),
        role="user",
        created_at=now(),
        **body.model_dump(exclude={"text"}),
        text=body.text,
    )
    state.messages.append(message)
    # Every tab shows it, not only the one that sent it.
    state.emit(0, UserMessageEvent(type="user_message", message_id=message.id, message=message))
    if state.telemetry is not None:
        state.telemetry.message(message, body)
    return message


@router.post(
    SESSION + "/revisions/next",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["messages"],
)
async def load_next_revision(session_id: str, body: RevisionLoad | None = None) -> Message:
    """Load the next board revision (`next_revision`): Claude starts the work on it without
    being asked. The response is the user message that stands for the load; the run streams
    on `/events` and ends with its change set (`changes_ready`). Also the webhook a board
    source calls when a new revision is ready."""
    state = SESSIONS.get(session_id)
    body = body or RevisionLoad()
    if state.session.next_revision is None:
        raise HTTPException(409, "no newer board revision to load")
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    if state.reviewing.locked():
        raise HTTPException(409, "the accepted changes are still rebuilding")
    if state.importing.locked():
        raise HTTPException(409, "the imported file is still being read")
    if state.session.status == "waiting_for_answer":
        raise HTTPException(409, "answer the open question first")
    ws = state.ws
    if ws.previous is None or ws.board is None:
        raise HTTPException(409, "no newer board revision to load")
    # Claude starts: rate limits and the daily budget
    rate_limits.check(rate_limits.RUNS)
    budget.check()
    message_body = MessageCreate(text=revision_message(ws.previous, ws.board), **body.model_dump())
    state.session.next_revision = None
    # From now on the session is on the newer revision (a demo copy may still be on the older).
    state.session.board = BoardRef(name=ws.board.name, revision=ws.board.revision)
    state.refresh_services()
    message = _user_message(state, message_body)
    state.session.status = "running"
    state.save()
    prompt = revision_prompt(ws.previous, ws.board)
    rate_limits.record(rate_limits.RUNS)
    state.run = SESSIONS.spawn(_run(state, message_body, prompt))
    return message


@router.post(
    SESSION + "/retry",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["messages"],
)
async def retry_run(session_id: str, body: RunRetry | None = None) -> Message:
    """Retry the reply that stopped (an `error` with `retryable`, or one a restart
    interrupted): a new run on the same request picks up from the design and builds so far.
    The response is the user message that stands for the retry; the run streams on
    `/events`. 409 while a run is going or nothing has run yet."""
    state = SESSIONS.get(session_id)
    body = body or RunRetry()
    last = state.last_run
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    if state.session.status == "waiting_for_answer":
        raise HTTPException(409, "answer the open question first")
    if state.reviewing.locked() or state.importing.locked():
        raise HTTPException(409, "the session is still busy")
    if last is None:
        raise HTTPException(409, "there is no reply to retry")
    # Claude starts: rate limits and the daily budget
    rate_limits.check(rate_limits.RUNS)
    budget.check()
    stopped = MessageCreate.model_validate(last["message"])
    message_body = stopped.model_copy(
        update={**body.model_dump(), "text": "Retry", "answer": None, "comments": []}
    )
    message = _user_message(state, message_body)
    state.session.status = "running"
    state.save()
    why = next(
        (e.message for _, e in reversed(state.events) if e.type == "error"),
        "it was cut off.",
    )
    prompt = f"{last['prompt']}\n\n{RETRY_NOTE.format(why=why)}"
    files = attached_files(state, stopped)
    run_body = stopped.model_copy(update=body.model_dump())
    rate_limits.record(rate_limits.RUNS)
    state.run = SESSIONS.spawn(_run(state, run_body, prompt, files))
    return message


@router.post(
    SESSION + "/stop",
    response_model=Session,
    status_code=202,
    responses=NOT_FOUND,
    tags=["messages"],
)
async def stop_run(session_id: str) -> Session:
    """Stop the reply that is running (or waiting in line, retrying, or waiting for an
    answer). It ends with an `error` saying it was stopped (`reason` "stopped", `retryable`);
    the design and builds so far are kept, Retry picks up from them, and the next queued
    message, if any, starts (`app.message_queue`). Nothing to stop: the session as it is."""
    state = SESSIONS.get(session_id)
    run = state.run
    if run is not None and not run.done():
        state.stopping = STOPPED
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(run), 5)
        if state.session.status != "idle":  # stopped before its reply started
            state.session.status = "idle"
            state.question = None
            state.save()
    run_changed(state)  # the next queued message, if any, starts (`app.message_queue`)
    return state.session


@router.get(
    SESSION + "/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-sent events: `id`, `event` (the type) and `data` (JSON).",
            "content": {EVENT_STREAM: {"schema": {"$ref": "#/components/schemas/SessionEvent"}}},
        },
        **NOT_FOUND,
    },
    tags=["events"],
)
async def events(
    session_id: str,
    after: Annotated[int | None, Query(description="Send events after this id")] = None,
    follow: Annotated[bool, Query(description="Keep the stream open for new events")] = True,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Live progress for a session, from the start or after `after` / `Last-Event-ID`."""
    state = SESSIONS.get(session_id)
    try:
        start = after if after is not None else int(last_event_id or 0)
    except ValueError:
        start = 0

    async def stream():
        last = start
        if follow and not state.closed:
            # Sent at once, so a browser (and any proxy) sees the stream open even when no
            # event is due: a reconnect shows as connected straight away.
            yield ": connected\n\n"
        while True:
            wake = state.wake
            pending = [e for e in state.events if e[0] > last]
            if not pending:
                if not follow or state.closed:
                    return
                try:
                    await asyncio.wait_for(wake.wait(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                continue
            for event_id, event in pending:
                yield sse_frame(event_id, event)
                last = event_id

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type=EVENT_STREAM, headers=headers)


# --- Attachments ------------------------------------------------------------------------------


@router.post(
    SESSION + "/attachments",
    response_model=Attachment,
    status_code=201,
    responses=UPLOAD_RESPONSES,
    tags=["attachments"],
)
async def upload_attachment(session_id: str, file: UploadField) -> Attachment:
    """Upload a file to send with a message: the response goes in the message's `attachments`."""
    state = SESSIONS.get(session_id)
    _check_quota(state)
    attachment = await state.attachments.add(f"{API}/sessions/{session_id}/attachments", file)
    if state.store is not None:
        aid = attachment.url.rsplit("/", 1)[-1]
        await asyncio.to_thread(
            state.store.write_attachment, session_id, aid, state.attachments.files[aid][1]
        )
        state.save()
    return attachment


@router.get(
    SESSION + "/attachments/{attachment_id}",
    response_class=Response,
    responses=FILE_RESPONSES,
    tags=["attachments"],
)
async def get_attachment(session_id: str, attachment_id: str) -> Response:
    return SESSIONS.get(session_id).attachments.response(attachment_id)


# --- Imports --------------------------------------------------------------------------------


def session_board(state: SessionState) -> Board | None:
    """The board revision the session is on: the previous one until the next is loaded (None
    for a design without a board)."""
    ws = state.ws
    return ws.previous if state.session.next_revision is not None else ws.board


@router.post(
    SESSION + "/imports",
    response_model=Build,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, 413: {"model": ErrorBody}, 422: {"model": ErrorBody}},
    tags=["builds"],
)
async def import_file(session_id: str, file: UploadField) -> Build:
    """Import a STEP file (multipart, field `file`) as a build: start from an existing design
    or bring in a part. The file is read in the build sandbox, never in the server; its faces
    get stable IDs and feature tags (holes, bosses and openings where recognized, else the
    part's name), and it is checked against the session's board and standards. The build also
    arrives as a `build_ready` without a `message_id`; Claude is told about it with the next
    message and can read the file. 413 over the size limit; 422 for a file that isn't STEP,
    can't be read, or takes too long."""
    state = SESSIONS.get(session_id)
    _check_quota(state)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: import when the run has finished")
    if state.importing.locked():
        raise HTTPException(409, "another import is still being read")
    if state.reviewing.locked():
        raise HTTPException(409, "the accepted changes are still rebuilding")
    async with state.importing:
        if state.initial is not None:
            await state.initial
        name, _, data = await read_upload(file, MAX_IMPORT_READ)
        board = session_board(state)
        imported = await import_step(
            name,
            data,
            (board.name, board.revision) if board is not None else None,
            to_payload(state.ws.standards),
            state.new_build_id(),
        )
        checked = imported.result["checks"].get("board")
        if board is None and checked:  # a design exported with its board, into a boardless session
            board = find_board(checked["name"], checked["revision"])
        attachment = state.attachments.store(
            f"{API}/sessions/{session_id}/attachments", name, STEP_MEDIA_TYPE, data
        )
        state.ws.attach([AttachedFile(attachment.name, attachment.media_type, data)])
        before = capture(state)
        # The change set was proposed on the design the import replaces on the canvas.
        state.proposal = None
        origin = BuildOrigin(kind="import", file_name=name)
        build = _emit_import(state, imported, board, imported.build_id, origin=origin)
        state.record(before, "import", f"imported {name}")
        return build


def _emit_import(
    state: SessionState, imported: ImportedFile, board: Board | None, build_id: str, **update: Any
) -> Build:
    """Stream an imported file as build `build_id` (`build_ready` with no message)."""
    state.imports[build_id] = imported
    checks = CheckSummary.model_validate(imported.result["checks"]["summary"])
    maps = list(imported.result["maps"])
    build = make_build(build_id, board, checks=checks.model_dump(), maps=maps)
    build = build.model_copy(update=update)
    EventSink(listeners=[state.emit]).emit(
        BuildReadyEvent(type="build_ready", message_id=None, build=build)
    )
    state.step_done()
    return state.builds[build_id]


# --- Builds ---------------------------------------------------------------------------------


@router.get(SESSION + "/builds", response_model=list[Build], responses=NOT_FOUND, tags=["builds"])
async def list_builds(session_id: str) -> list[Build]:
    return list(SESSIONS.get(session_id).builds.values())


@router.get(
    SESSION + "/builds/{build_id}", response_model=Build, responses=NOT_FOUND, tags=["builds"]
)
async def get_build(session_id: str, build_id: str) -> Build:
    return SESSIONS.get(session_id).build(build_id)


@router.get(
    SESSION + "/builds/{build_id}/mesh",
    response_class=Response,
    responses={
        200: {"description": "glTF following the mesh contract", "content": {GLTF: {}}},
        **NOT_FOUND,
    },
    tags=["builds"],
)
async def get_mesh(session_id: str, build_id: str) -> Response:
    state = SESSIONS.get(session_id)
    # Off the event loop (a stored mesh is read from disk), so other sessions keep streaming.
    mesh = await asyncio.to_thread(state.mesh, state.build(build_id, ready=True))
    return Response(mesh, media_type=GLTF)


@router.get(
    SESSION + "/builds/{build_id}/checks",
    response_model=CheckReport,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_checks(session_id: str, build_id: str) -> dict:
    state = SESSIONS.get(session_id)
    return state.checks(state.build(build_id, ready=True))


@router.get(
    SESSION + "/builds/{build_id}/source",
    response_model=BuildSource,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_source(session_id: str, build_id: str) -> dict:
    state = SESSIONS.get(session_id)
    build = state.build(build_id, ready=True)
    code = state.sources.get(build_id)
    if code is None or state.stored(build) is not None:  # a stored build keeps its own code
        # Off the event loop: an older stored build's feature lines come from a geometry
        # worker, which may wait for a build slot.
        return await asyncio.to_thread(state.source, build)
    return build_source(build, code)


@router.put(
    SESSION + "/source",
    response_model=Build,
    responses=CODE_ERROR_RESPONSES,
    tags=["builds"],
)
async def update_source(session_id: str, body: SourceUpdate) -> Any:
    """A hand edit to the generator code of `build_id`: rebuild and recheck with `code`. The
    response is the rebuild, which also arrives as a `build_ready` without a `message_id`.
    Code that doesn't parse or doesn't define `build(params)`, or that fails to build, is
    refused with a `CodeError` (400), and nothing changes. Claude gets the edit on its next
    turn."""
    state = SESSIONS.get(session_id)
    state.build(body.build_id)
    if state.session.status != "idle":
        raise HTTPException(409, "the session is still running")
    if state.initial is not None:
        await state.initial
    before = capture(state)
    result = await state.edit_code(body.code)
    if isinstance(result, CodeError):
        return code_error_response(result)
    state.record(before, "code", "edited the code")
    return result


@router.get(
    SESSION + "/builds/{build_id}/board",
    response_model=Board,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_board(session_id: str, build_id: str) -> Board:
    """The board revision the build was checked against, from `demos/<board>/`; 404 for a build
    of a design without a board."""
    build = SESSIONS.get(session_id).build(build_id)
    if build.board is None:
        raise HTTPException(404, f"build {build_id} has no board")
    try:
        return find_board(build.board.name, build.board.revision)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.get(
    SESSION + "/builds/{build_id}/maps/{map_type}",
    response_model=AnalysisMap,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_map(session_id: str, build_id: str, map_type: MapType) -> dict:
    state = SESSIONS.get(session_id)
    build = state.build(build_id, ready=True)
    if map_type not in build.maps:
        raise HTTPException(404, f"build {build_id} has no {map_type} map")
    return state.map(build, map_type)


@router.get(
    SESSION + "/builds/{build_id}/diff",
    response_model=BuildDiff,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_diff(
    session_id: str,
    build_id: str,
    against: Annotated[str, Query(description="The build to compare with: the diff's `old`")],
) -> BuildDiff:
    """What changed from build `against` to this build: the parameters changed and the
    features added, removed, moved, rotated or resized, with distances."""
    state = SESSIONS.get(session_id)
    old, new = state.build(against), state.build(build_id)
    if old.id in state.imports or new.id in state.imports:
        if state.imports.get(old.id) is state.imports.get(new.id):  # the same imported file
            return BuildDiff(old=old.id, new=new.id, parameters=[], features=[])
        imported = old if old.id in state.imports else new
        raise HTTPException(
            409, f"build {imported.id} is an imported STEP file: it can't be diffed with another"
        )
    return await asyncio.to_thread(state.diff, old, new)


@router.post(
    SESSION + "/builds/{build_id}/restore",
    response_model=Build,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["builds"],
)
async def restore_build(session_id: str, build_id: str) -> Build:
    """Undo in the history: bring back an earlier build's design (its parameters, and their
    locks) and rebuild it on the build's revision. The new build, `restored_from` this one,
    also arrives as a `build_ready` without a `message_id`. Refused while a run is going, like
    a message; allowed while a question waits, like a hand edit."""
    state = SESSIONS.get(session_id)
    target = state.build(build_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: restore when the run has finished")
    if target.status != "ready":
        raise HTTPException(409, FAILED_RESTORE)
    if state.initial is not None:
        await state.initial
    before = capture(state)
    imported = state.imports.get(target.id)
    if imported is not None:  # an imported file: the same file is the design again
        state.proposal = None
        board = find_board(target.board.name, target.board.revision) if target.board else None
        build = _emit_import(state, imported, board, state.new_build_id(), restored_from=target.id)
        state.record(before, "restore", "restored an earlier version")
        return build
    snapshot = state.designs.get(target.id)
    if snapshot is None:
        raise HTTPException(
            409, "There's no saved design for that version, so it can't be restored."
        )
    try:
        board = find_board(target.board.name, target.board.revision) if target.board else None
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    # The change set was proposed on the design this replaces: reviewing it afterwards would
    # put that design back over the restore.
    state.proposal = None
    risks, state.risks = state.risks, None  # flagged on the design this replaces, like the set
    state.restore_design(snapshot)
    # The rebuild of the stored design: if it fails, the design stays as it was (no failed
    # build replaces the model on the canvas), and the user is told so plainly.
    try:
        restored = await _build_design(state, board, {"restored_from": target.id}, failed_ok=False)
    except Exception as error:
        state.put_state(before)
        state.risks = risks
        log.warning("session %s: restoring %s failed: %s", session_id, target.id, error)
        raise HTTPException(409, RESTORE_FAILED) from error
    if target.id in state.sources:  # the restored design runs the hand-edited code
        state.sources[restored.id] = state.sources[target.id]
    state.record(before, "restore", "restored an earlier version")
    return restored


# --- Changes ----------------------------------------------------------------------------------


class _ReviewSink(EventSink):
    """Events of a review outside a run: the rebuild is a `build_ready` with no message, like
    the session's first build, so the chat gets no empty assistant message."""

    def start(self) -> None:
        pass

    def build_ready(self, build: Build) -> None:
        self.emit(BuildReadyEvent(type="build_ready", message_id=None, build=build))


def _proposal(state: SessionState) -> Proposal:
    if state.proposal is None:
        raise HTTPException(404, "no change set: no run has changed the design yet")
    return state.proposal


@router.get(
    SESSION + "/changes",
    response_model=ChangeSet,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["changes"],
)
async def get_changes(session_id: str) -> ChangeSet:
    """The change set of the latest run that changed the design."""
    return _proposal(SESSIONS.get(session_id)).change_set()


@router.post(
    SESSION + "/changes",
    response_model=ChangeSet,
    response_model_by_alias=True,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["changes"],
)
async def review_changes(session_id: str, body: ChangeReview) -> ChangeSet:
    """Accept and reject changes by ID. The design is rebuilt and rechecked with only the
    accepted changes; the build streams as `build_ready`, then the set as `changes_ready`."""
    state = SESSIONS.get(session_id)
    proposal = _proposal(state)
    if state.session.status != "idle":
        raise HTTPException(409, "the session is still running")
    if state.reviewing.locked():
        raise HTTPException(409, "another review is still rebuilding")
    async with state.reviewing:
        sink = _ReviewSink(listeners=[state.emit])
        state.ws.sink = sink
        before = capture(state)
        try:
            change_set = await proposal.review(accept=body.accept, reject=body.reject)
        except KeyError as error:
            raise HTTPException(404, str(error.args[0]) if error.args else str(error)) from error
        except LockedParameterError as error:
            name = error.parameter.name
            raise HTTPException(409, f"refused by the lock on {name}: {error}") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        state.review_decided(change_set, body.accept, body.reject)
        sink.changes_ready(change_set)
        state.record(before, "review", review_label(body.accept, body.reject))
        return change_set


@router.post(
    SESSION + "/builds/{build_id}/find",
    response_model=FindResult,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def find(session_id: str, build_id: str, body: FindRequest) -> FindResult:
    """Find in model: the faces a query in words describes, from the build's feature tags, the
    session's parameters and the build's analysis maps (`app.find`)."""
    state = SESSIONS.get(session_id)
    return await asyncio.to_thread(state.find, build_id, body.query)  # off the event loop


@router.get(
    SESSION + "/builds/{build_id}/faces/{face_id}",
    response_model=FacePlane,
    responses=NOT_FOUND,
    tags=["builds"],
)
async def get_face(session_id: str, build_id: str, face_id: str) -> FacePlane:
    """A face of the build: whether it is flat, and if so the plane a sketch on it uses."""
    state = SESSIONS.get(session_id)
    build = state.build(build_id, ready=True)
    plane = face_plane(state.mesh(build), build.id, face_id)
    if plane is None:
        raise HTTPException(404, f"no face {face_id}")
    return plane


# --- Sketches ---------------------------------------------------------------------------------


@router.get(
    SESSION + "/sketches", response_model=list[Sketch], responses=NOT_FOUND, tags=["sketches"]
)
async def list_sketches(session_id: str) -> list[Sketch]:
    return SESSIONS.get(session_id).sketch_list()


@router.post(
    SESSION + "/sketches",
    response_model=Sketch,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["sketches"],
)
async def create_sketch(session_id: str, body: SketchCreate) -> Sketch:
    """Sketch on a flat face, cut or extruded: a named, locked feature Claude keeps. The design
    rebuilds and rechecks: a `build_ready` without a `message_id` follows on `/events`."""
    state = SESSIONS.get(session_id)
    if state.initial is not None:
        await state.initial
    before = capture(state)
    sketch, build = state.add_sketch(body)
    try:
        # A design without a board rebuilds without one.
        board = find_board(build.board.name, build.board.revision) if build.board else None
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    await _build_design(state, board)
    state.record(before, "sketch", sketch_label(sketch))
    return sketch


# --- Risks ------------------------------------------------------------------------------------


def _risks(state: SessionState) -> Flags:
    if state.risks is None:
        raise HTTPException(404, "no risk flags: no run has flagged a risk yet")
    return state.risks


@router.get(
    SESSION + "/risks",
    response_model=RiskFlags,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["risks"],
)
async def get_risks(session_id: str) -> RiskFlags:
    """The risk flags of the latest run that flagged any, each with its fix."""
    return _risks(SESSIONS.get(session_id)).risk_flags()


@router.post(
    SESSION + "/risks",
    response_model=RiskFlags,
    response_model_by_alias=True,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["risks"],
)
async def review_risks(session_id: str, body: RiskReview) -> RiskFlags:
    """Accept and ignore (reject) fixes by flag ID, each on its own. The accepted fixes are laid
    over the design (and the run's accepted changes), which is rebuilt and rechecked; the build
    streams as `build_ready`, then the flags as `risks_ready`."""
    state = SESSIONS.get(session_id)
    flags = _risks(state)
    if state.session.status != "idle":
        raise HTTPException(409, "the session is still running")
    if state.reviewing.locked():
        raise HTTPException(409, "another review is still rebuilding")
    async with state.reviewing:
        sink = _ReviewSink(listeners=[state.emit])
        state.ws.sink = sink
        before = capture(state)
        try:
            risk_flags = await flags.review(accept=body.accept, reject=body.reject)
        except (PathError, LockedCodeError) as error:  # the fix no longer fits the design
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, str(error.args[0]) if error.args else str(error)) from error
        except LockedParameterError as error:
            name = error.parameter.name
            raise HTTPException(409, f"refused by the lock on {name}: {error}") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        sink.risks_ready(risk_flags)
        state.record(before, "risks", review_label(body.accept, body.reject, "fix"))
        return risk_flags


# --- Parameters and comments ------------------------------------------------------------------


@router.get(
    SESSION + "/parameters",
    response_model=list[Parameter],
    responses=NOT_FOUND,
    tags=["parameters"],
)
async def list_parameters(session_id: str) -> list[Parameter]:
    # Off the event loop: measuring reads the latest build's mesh (`app.dimensions`).
    return await asyncio.to_thread(SESSIONS.get(session_id).measured_parameters)


@router.patch(
    SESSION + "/parameters/{name}",
    response_model=Parameter,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["parameters"],
)
async def update_parameter(session_id: str, name: str, body: ParameterUpdate) -> Parameter:
    """A hand edit: setting `value` also locks the parameter unless `locked` says otherwise,
    and rebuilds and rechecks the design with it (in the geometry worker, in a build slot): a
    `build_ready` without a `message_id` follows on `/events`. Claude designs around locked
    values and can change one only with the user's approval; it reworks the rest of the design
    around the edit on the next message. Refused while a run is going, like a message; allowed
    while a question waits."""
    state = SESSIONS.get(session_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: edit when the run has finished")
    if state.initial is not None:
        await state.initial
    before = capture(state)  # the timeline's step (`app.design_history`)
    design_before = state.ws.design.snapshot()
    known = [*state.parameter_list(), *grouped_values(state.ws.design.params)]
    old = next((p for p in known if p.name == name), None)
    previous, was_locked = (old.value, old.locked) if old is not None else (None, False)
    param = state.update_parameter(name, body)
    state.save()
    if state.telemetry is not None:
        state.telemetry.parameter_edit(state, name, body)
    if body.value is not None and state.ws.design.snapshot() != design_before:
        # A new edit replaces an open decision; what failed before that edit still counts.
        failing = state.edit_decision.before if state.edit_decision else _failing_before(state)
        if state.edit_decision is not None:
            if state.edit_decision.name == name:  # a revert still goes back to before both
                previous = state.edit_decision.previous
                was_locked = state.edit_decision.was_locked
            _close_edit_decision(state, "A newer edit replaces this one.")
        await _rebuild_after_edit(state, name, body.value, previous, was_locked, failing)
    if body.value is not None:  # one step of the timeline, with the build the edit made
        state.record(before, "parameter", parameter_label(name, body.value, param.unit))
        state.save()
    state.step_done()  # telemetry logs the canvas the edit left
    # Measured on the build the edit made, as the list has it (`app.dimensions`).
    listed = await asyncio.to_thread(state.measured_parameters)
    return next((p for p in listed if p.name == name), param)


def _latest_board(state: SessionState) -> Board | None:
    """The board of the latest build (the session's board otherwise)."""
    latest = state.builds.get(state.session.latest_build_id or "")
    if latest is not None and latest.board is not None:
        try:
            return find_board(latest.board.name, latest.board.revision)
        except LookupError:
            pass
    return state.ws.board


def _failing_before(state: SessionState) -> set[str]:
    """The failing checks of the latest build, before an edit."""
    latest = state.builds.get(state.session.latest_build_id or "")
    if latest is None or latest.status != "ready":
        return set()
    items = state.check_items.get(latest.id)
    if items is not None:
        return failing_ids(items)
    try:
        return failing_in_report(state.checks(latest))
    except Exception:  # no report to compare with: every failing standard is news
        return set()


async def _rebuild_after_edit(
    state: SessionState, name: str, value: Any, previous: Any, was_locked: bool, before: set[str]
) -> None:
    """Build and check the design after a hand edit. A failed build arrives as a failed
    `build_ready`; a crash is reported on the stream, and the edit stands either way. If the
    edit makes a check against a standard fail that didn't in `before`, the user decides
    (`edit_decisions`), unless a run is waiting on its own question: its answer goes on."""
    try:
        build = await _build_design(state, _latest_board(state))
        broken = broken_standards(before, state.check_items.get(build.id) or [])
        run_waiting = state.run is not None and not state.run.done()
        if broken and not run_waiting:
            sink = EventSink(state.message_id(), [state.emit])
            decision = EditDecision(
                f"edit-{sink.message_id}", sink, name, value, previous, was_locked, broken, before
            )
            state.edit_decision = decision
            ask_about_edit(decision, state.ws.standards)
    except Exception as exc:  # report it on the stream rather than losing it
        if state.edit_decision is not None:  # the question never made it: no decision waits
            state.edit_decision.sink.end()
            state.edit_decision = None
        EventSink(listeners=[state.emit]).error(
            f"The rebuild after the edit failed: {type(exc).__name__}: {exc}", retryable=True
        )
    finally:
        state.save()


def _close_edit_decision(state: SessionState, text: str) -> None:
    decision, state.edit_decision = state.edit_decision, None
    if decision is not None:
        decision.sink.text(text)
        decision.sink.end()


def _decide_edit(
    state: SessionState, decision: EditDecision, body: MessageCreate, message: Message
) -> Message:
    """The user's answer to a hand edit that broke a standard: keep the value and accept the
    failure, let Claude adjust the rest around it, or revert it."""
    assert body.answer is not None
    state.edit_decision = None
    state.question = None
    choice, sink = body.answer.option_id, decision.sink
    if choice == KEEP:
        state.accepted_failures += accepted_failures(decision)
        sink.text(
            f"Kept {decision.name} at {decision.value}. The failing check stays in the "
            "report, and Claude keeps your value."
        )
        sink.end()
    elif choice == REVERT:
        state.session.status = "running"
        state.run = SESSIONS.spawn(_revert_edit(state, decision))
    else:  # ADJUST: Claude reworks the rest of the design around the value
        sink.text(f"Claude reworks the rest of the design around {decision.name}.")
        sink.end()
        prompt = adjust_prompt(decision, state.ws.standards)
        run_body = body.model_copy(update={"text": prompt, "answer": None})
        state.session.status = "running"
        state.run = SESSIONS.spawn(_run(state, run_body, prompt))
    state.save()
    return message


async def _revert_edit(state: SessionState, decision: EditDecision) -> None:
    """Put the edited value back as it was (and its lock), and rebuild."""
    sink = decision.sink
    try:
        update = ParameterUpdate(value=decision.previous, locked=decision.was_locked)
        state.update_parameter(decision.name, update)
        sink.text(f"Put {decision.name} back to {decision.previous}.")
        await _build_design(state, _latest_board(state))
    except Exception as exc:  # report it on the stream rather than losing it
        sink.error(f"The revert failed: {type(exc).__name__}: {exc}", retryable=True)
    finally:
        sink.end()
        state.session.status = "idle"
        state.save()


# --- Constraints ------------------------------------------------------------------------------


@router.get(
    SESSION + "/constraints",
    response_model=list[Constraint],
    responses=NOT_FOUND,
    tags=["constraints"],
)
async def list_constraints(session_id: str) -> list[Constraint]:
    """The design's spec, as the chips show it. Empty until the first message describes it."""
    return SESSIONS.get(session_id).spec.constraints()


def _change_constraint(session_id: str, constraint_id: str, change: Callable) -> Constraint:
    state = SESSIONS.get(session_id)
    if state.session.status != "idle":
        raise HTTPException(409, "wait until Claude has finished replying")
    try:
        constraint = change(state.spec)
    except KeyError as error:
        raise HTTPException(404, f"no constraint {constraint_id}") from error
    except SpecError as error:
        raise HTTPException(409, str(error)) from error
    state.apply_spec()
    state.session.status = "running"
    removed = change.__name__ == "remove"
    state.run = SESSIONS.spawn(_recheck(state, constraint, removed))
    return constraint


@router.patch(
    SESSION + "/constraints/{constraint_id}",
    response_model=Constraint,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["constraints"],
)
async def update_constraint(
    session_id: str, constraint_id: str, body: ConstraintUpdate
) -> Constraint:
    """Edit a chip: the design is rebuilt and rechecked against the new value, on `/events`."""

    def edit(spec: Spec) -> Constraint:
        return spec.edit(constraint_id, body.value)

    return _change_constraint(session_id, constraint_id, edit)


@router.delete(
    SESSION + "/constraints/{constraint_id}",
    response_model=Constraint,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["constraints"],
)
async def remove_constraint(session_id: str, constraint_id: str) -> Constraint:
    """Remove a chip from the user's words or an edit: the team standard applies again, and
    the design is rebuilt and rechecked. A team standard itself can't be removed (409)."""

    def remove(spec: Spec) -> Constraint:
        return spec.remove(constraint_id)

    return _change_constraint(session_id, constraint_id, remove)


@router.get(
    SESSION + "/comments", response_model=list[Comment], responses=NOT_FOUND, tags=["comments"]
)
async def list_comments(
    session_id: str,
    build_id: Annotated[
        str | None, Query(description="Locate the pins on this build (default: the latest)")
    ] = None,
) -> list[Comment]:
    """Every comment, resolved ones included, with its pin located on the build."""
    state = SESSIONS.get(session_id)
    build = state.build(build_id) if build_id else None
    return state.located_comments(build)


@router.post(
    SESSION + "/comments",
    response_model=Comment,
    status_code=201,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def create_comment(session_id: str, body: CommentCreate) -> Comment:
    """Pin a comment to a face of a build. It is a note; send it to Claude with a message's
    `comments`."""
    state = SESSIONS.get(session_id)
    body = body.model_copy(update={"text": scrub_user_text(body.text)[0]})
    build = state.build(body.build_id, ready=True)
    mesh = state.mesh_info(build)
    if mesh is None or body.face_id not in state.face_ids(build):
        raise HTTPException(404, f"no face {body.face_id}")
    try:
        comment = pins.pin(
            body,
            mesh,
            comment_id=f"comment-{secrets.token_hex(8)}",
            author=USER,
            created_at=now(),
        )
    except KeyError as error:
        raise HTTPException(404, f"no face {body.face_id}") from error
    comment = state.put_comment(comment)
    if state.telemetry is not None:
        state.telemetry.comment(state, comment)
    return comment


@router.patch(
    SESSION + "/comments/{comment_id}",
    response_model=Comment,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def update_comment(session_id: str, comment_id: str, body: CommentUpdate) -> Comment:
    """Resolve a comment (its pin is hidden; the thread stays in the list), or reopen it."""
    state = SESSIONS.get(session_id)
    comment = state.comment(comment_id).model_copy(update={"resolved": body.resolved})
    return state.put_comment(comment)


@router.post(
    SESSION + "/comments/{comment_id}/replies",
    response_model=Comment,
    status_code=201,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def reply_to_comment(session_id: str, comment_id: str, body: CommentReplyCreate) -> Comment:
    """Reply in a comment's thread, as a note (it reopens a resolved thread). Send the
    comment to have Claude read the thread."""
    return SESSIONS.get(session_id).comment_reply(comment_id, scrub_user_text(body.text)[0])


# --- Telemetry --------------------------------------------------------------------------------


@router.post(
    SESSION + "/telemetry",
    status_code=204,
    response_class=Response,
    responses={**NOT_FOUND, 422: {"model": ErrorBody}},
    tags=["telemetry"],
)
async def post_telemetry(session_id: str, body: ClientTelemetry) -> Response:
    """Record a user action only the frontend sees (a view, a model/effort/mode choice, an
    accepted or rejected change) in the session's telemetry log (`docs/architecture.md`)."""
    state = SESSIONS.get(session_id)
    check_client_telemetry(body)
    if state.telemetry is not None:
        state.telemetry.client(body)
    return Response(status_code=204)


def check_client_telemetry(body: ClientTelemetry) -> None:
    needs = {
        "view": ("view",),
        "choice": ("setting", "value"),
        "change_decision": ("change_id", "decision"),
    }[body.kind]
    missing = [name for name in needs if getattr(body, name) is None]
    if missing:
        raise HTTPException(422, f"a {body.kind} needs {', '.join(missing)}")


# --- Exports ----------------------------------------------------------------------------------


def _check_quota(state: SessionState) -> None:
    """Refuse a new file when the session's owner is at their disk quota (`app.security.quota`)."""
    builds = [b for s in list(SESSIONS.by_id.values()) if s.owner == state.owner for b in s.builds]
    quota.check(SESSIONS.owned_ids(), builds)


@router.post(
    SESSION + "/exports",
    response_model=Export,
    status_code=201,
    responses={**NOT_FOUND, 422: {"model": ErrorBody}},
    tags=["exports"],
)
async def create_export(session_id: str, body: ExportCreate) -> Export:
    """Writes the requested formats of the chosen `parts` (default: the whole assembly) with the
    geometry exporters (`geometry.export`)."""
    state = SESSIONS.get(session_id)
    build = state.build(body.build_id, ready=True)
    await asyncio.to_thread(quota.cleanup_exports)
    _check_quota(state)
    eid = f"export-{secrets.token_hex(8)}"
    out = exports_dir() / session_id / eid
    imported = state.imports.get(build.id)
    if imported is not None:  # the import worker exports the file's own geometry
        board = (build.board.name, build.board.revision) if build.board else None
        report = CheckReport.model_validate(state.checks(build)).model_dump(mode="json")
        written = await export_import(
            imported, board, to_payload(state.ws.standards), report, list(body.formats), out
        )
    else:
        stored = state.stored(build)
        try:
            exported = await asyncio.to_thread(
                export_files,
                build,
                out,
                list(body.formats),
                state.checks(build),
                stored[1] if stored else None,
                body.parts,
                list(state.builds.values()),
                body.units,
            )
        except ExportPartsError as error:
            raise HTTPException(422, str(error)) from error
        except (OSError, RuntimeError) as error:  # out of disk, or the worker died or timed out
            log.exception("session %s: export of %s failed", session_id, build.id)
            raise HTTPException(503, limits.EXPORT_UNKNOWN) from error
        written = [
            {"format": f.format, "name": f.name, "media_type": f.media_type} for f in exported
        ]
    add_safety_notes(state, out, written)
    number = list(state.builds).index(build.id) + 1 if build.id in state.builds else 1
    rename_exports(out, written, state.session.title, number)
    base = f"{API}/sessions/{session_id}/exports/{eid}/files/"
    files = [ExportFile(**f, url=base + f["name"]) for f in written]
    export = Export(id=eid, build_id=build.id, created_at=now(), files=files)
    state.exports[eid] = export
    state.export_dirs[eid] = out
    step = next((f for f in files if f.format == "step"), None)
    if step is not None:
        state.builds[build.id] = build.model_copy(update={"step_url": step.url})
    state.save()
    if state.telemetry is not None:
        state.telemetry.export(state, export, body.parts)
    return export


def add_safety_notes(state: SessionState, out: Path, written: list[dict]) -> None:
    """The session's safety notices in the exported build note, if there is one."""
    from app.agent.safety import with_safety_section

    notices = [e for _, e in state.events if e.type == "notice" and e.kind == "safety"]
    for f in written:
        note = out / f["name"]
        if notices and f["format"] == "build_note" and note.is_file():
            note.write_text(with_safety_section(note.read_text(), notices))


@router.get(
    SESSION + "/exports/{export_id}/files/{name}",
    response_class=Response,
    responses={200: {"description": "The exported file", "content": {"*/*": {}}}, **NOT_FOUND},
    tags=["exports"],
)
async def get_export_file(session_id: str, export_id: str, name: str) -> Response:
    state = SESSIONS.get(session_id)
    export = state.exports.get(export_id)
    file = next((f for f in export.files if f.name == name), None) if export else None
    if export is None or file is None:
        raise HTTPException(404, f"no file {name} in export {export_id}")
    path = state.export_dirs[export_id] / file.name
    if not path.is_file():
        raise HTTPException(
            404, f"{name} was cleared after a while to save space. Export the design again."
        )
    content = path.read_bytes()
    headers = {"Content-Disposition": f'attachment; filename="{file.name}"'}
    return Response(content, media_type=file.media_type, headers=headers)


__all__ = [
    "CONFIG",
    "SESSIONS",
    "SessionConfig",
    "SessionState",
    "fixture_services",
    "geometry_services",
    "prompt_for",
    "router",
]
