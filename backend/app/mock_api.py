"""Mock session API: serves `fixtures/` and plays its scripted event streams.

Run it in place of the real backend with `make dev MOCK_API=1` (see `docs/architecture.md`). The
routes here are the session API contract: `python -m app.contracts` writes their OpenAPI
document to `contracts/session-api.openapi.json`, and the real API must match it.

Behaviour:

- `GET /api/examples` lists the examples (`demos/<board>/examples.yaml`). `POST /api/sessions`
with
  `{"example": id}` (or the example's `board`) starts a session on the fixture board with no
  builds and the next revision to load; with neither it starts empty (no board, no next
  revision, no builds), as the real API does.
- `POST /api/demos/{demo}/session` gives the browser its own copy of a demo (`app.demos`,
  `app.sample_copies`), made the first time: the demo's conversation, parameters, board, next
  revision and builds, whose mesh, checks, source, maps and diffs are the original's files.
  `DELETE` on it resets the demo.
- The mock has one scripted run: the first message posted to any session plays
  `fixtures/session-events.jsonl` (whatever it asks for):
  progress, text, a `build_ready` with the existing enclosure's build on its own (older)
  revision and one with the fixture build on the newer revision, and a question with options.
- A message with the text of `conflict_prompt` in `fixtures/session.json` (at any point)
  plays `fixtures/conflict-events.jsonl` instead: the locks of the conflict fixture, a
  build, and a question that can't be satisfied as asked, with the conflict and its location.
- A later message that starts with "Find " is Claude finding parts of the model from chat:
  the rest of the text is resolved like `POST .../builds/{id}/find` on the latest
  build, and the reply streams it as a `found` event (the canvas highlights the matches) with a
  line that lists them, or asks with what was understood when the query is unclear.
- Answering the question (a message with `answer`) gets a short reply that ends with the
  fixture change set (`fixtures/change-set.json`) as a `changes_ready` event; any later message
  gets a one-line reply.
- `GET /changes` serves that change set (404 before it is ready). `POST /changes` accepts and
  rejects changes: their statuses change, a new build streams as `build_ready` (with no message)
  whose checks are the fixture report with each accepted change's proof as it is with the
  change alone, then the updated set as `changes_ready`.
- The run that ends with the change set also flags risks: the fixture risk flags
  (`fixtures/risk-flags.json`) follow the change set as a `risks_ready` event. `GET /risks`
  serves them (404 before they are ready). `POST /risks` accepts and ignores fixes: their
  statuses change, a new build streams as `build_ready` (with no message) with the checks of the
  latest build (a fix breaks no check), then the updated flags as `risks_ready`.
- Setting a parameter's value (`PATCH .../parameters/{name}`) is a hand edit: it locks the
  parameter and rebuilds, so a `build_ready` with no `message_id` follows on `/events`. The
  rebuilt build serves the fixture files under a new build ID.
- `GET .../builds/{id}/diff?against={other}` diffs two builds (`app.build_diffs`): between the
  two revisions, the fixture diff (`fixtures/diff.json`); within one, the parameters edited.
- `POST .../builds/{id}/restore` is undo in the history: the build's parameters come back and a
  new build of them (`restored_from` the build) follows on `/events`, as after a hand edit.
- Editing the code (`PUT .../source`) is a hand edit too: code that doesn't parse or doesn't
  define `build(params)` is refused (400, with the line); otherwise it rebuilds like a parameter
  edit, and the rebuild's source is the edited code.
- A message's text sets the spec's constraints (`app.spec`): a `constraints` event before the
  reply. Editing or removing a chip replies with a recheck: the new constraints, a progress
  line and the fixture build.
- Sketching on a face (`POST .../sketches`) is a hand edit too: the sketch becomes a
  locked feature, a rebuild follows like a parameter edit, and the rebuilt build's CAD source
  (and every later build's) has the code that applies it and lists it, locked, as a feature.
  `GET .../builds/{id}/faces/{face}` says whether a face is flat and where its plane is.
- The modeling tools (`/edits`: move, push/pull, hole, boss, rib, fillet, chamfer, shell,
  mirror, pattern) are hand edits like a sketch: each becomes a locked feature in the code and
  rebuilds; changing, suppressing or deleting one rebuilds too, and a restore brings back the
  edits the build had.
- Exports are real files: the geometry exporters (`geometry.export`) run on the enclosure
  generator's build of the design board, with the fixture check report as the build note.
- Comments are pinned to faces and located on any build like the real API's (`app.comments`).
  A message with `comments` sends them to Claude: one reply streams with a rebuild and a change
  set of fixture changes, one per comment (the change on the comment's feature if there is
  one), each linked to its comment, and a reply in every comment's thread. Each change to a
  comment streams as a `comment` event.
- The message queue (`/queue`, `/stop`) works as in the real API (`app.message_queue`): a
  message queued while a reply streams is sent when it ends, one at a time; a question (the
  scripted run ends on one) pauses the queue until it is answered or a message is sent anyway,
  which leaves the question unanswered. Stop cancels the reply streaming and the queue goes on.
- A message starting with "Take your time" works for `MOCK_SLOW_SECONDS` (default 10, not
  scaled) and then asks the recorded question, so the queue can be tried in the browser.
- The design timeline (`/history`, `app.design_history`): every change to the design is a
  step, whoever made it: a hand edit (parameter, code, sketch, modeling edit), a restore, an
  import, a review of the change set or the risk flags, and each run of Claude's that brought a
  build or a change set. `POST .../history/undo` and `.../redo` put the state back (parameters
  and locks, sketches, edits, code, the change set as it was reviewed) and rebuild it, as a
  `build_ready` with no message; refused while Claude is working.
- A message that starts with "Break the build" is a run whose rebuild fails: a failed build
  (with its plain reason, `Build.failure`) streams in the reply. A failed build serves no mesh,
  checks or maps, and comments and edits can't be made on it (404), as in the real API.
- `MOCK_API_SPEED` scales the recorded delays: 1 (default) is real time, 0 sends at once.

State is in memory and per process.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import comments as pins
from app import demos, runtime, sample_copies, samples
from app import standards_sets as std_sets
from app.agent import limits
from app.agent.services import items_from_report
from app.attachments import (
    FILE_RESPONSES,
    UPLOAD_RESPONSES,
    Attachments,
    UploadField,
    read_upload,
)
from app.boards import Board, find_board
from app.build_diffs import build_diff
from app.code_edits import (
    CODE_ERROR_RESPONSES,
    code_error,
    code_error_response,
    edited_source,
)
from app.contracts.changes import ChangeReview, ChangeSet, RiskFlags, RiskReview
from app.contracts.checks import CheckReport
from app.contracts.common import BoardRef
from app.contracts.maps import AnalysisMap, MapType
from app.contracts.mesh import MeshInfo
from app.contracts.models import MODEL_CATALOG, ModelCatalog
from app.contracts.session import (
    Attachment,
    Build,
    BuildDiff,
    BuildOrigin,
    BuildReadyEvent,
    BuildSource,
    ChangesReadyEvent,
    Comment,
    CommentCreate,
    CommentEvent,
    CommentReplyCreate,
    CommentRequest,
    CommentUpdate,
    Constraint,
    ConstraintsEvent,
    ConstraintUpdate,
    DesignHistory,
    Edit,
    EditCreate,
    EditRemoval,
    EditUpdate,
    ErrorBody,
    ErrorEvent,
    ExampleSummary,
    Export,
    ExportCreate,
    ExportFile,
    FacePlane,
    FindRequest,
    FindResult,
    FoundEvent,
    HistoryMove,
    Message,
    MessageCreate,
    MessageEndEvent,
    MessageQueue,
    MessageStartEvent,
    Parameter,
    ParameterUpdate,
    ProgressEvent,
    QuestionEvent,
    QuestionOption,
    QueuedMessage,
    QueuedMessageUpdate,
    QueueEvent,
    RecordedEvent,
    RevisionLoad,
    RisksReadyEvent,
    RunRetry,
    Session,
    SessionCreate,
    SessionStandards,
    SessionUpdate,
    Sketch,
    SketchCreate,
    SourceUpdate,
    TextEvent,
    TitleEvent,
    UserMessageEvent,
    openapi_with_events,
    sse_frame,
)
from app.contracts.telemetry import ClientTelemetry
from app.design_history import (
    History,
    edit_label,
    parameter_label,
    review_label,
    run_label,
    sketch_label,
)
from app.dimensions import measured
from app.examples import example_for_board, examples, find_example
from app.exports import (
    ExportPartsError,
    export_parts,
    fixture_face_ids,
    fixture_mesh_parts,
    fixtures_dir,
    prewarm_exports,
    session_revisions,
    write_exports,
)
from app.imports import MAX_IMPORT_BYTES, STEP_MEDIA_TYPE, check_upload
from app.modeling import (
    EditError,
    edit_params,
    new_edit,
    removal,
    source_parts,
    updated_edit,
    with_edits,
)
from app.owners import OwnerMiddleware, current_owner
from app.revisions import revision_message
from app.session_store import MemoryStandardsStore
from app.share_routes import SHARE_VIEW
from app.sketches import SketchError, face_plane, new_sketch, sketch_params, with_sketches
from app.spec import Spec, SpecError
from app.standards import load_standards
from app.standards_api import standards_router
from app.standards_changes import change_notice
from app.titles import DEFAULT_TITLE, clean_title, title_for

API = "/api"
SESSION = API + "/sessions/{session_id}"
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorBody}}
GLTF = "model/gltf+json"
EVENT_STREAM = "text/event-stream"
KEEPALIVE_SECONDS = 15.0
USER = "Demo user"  # the author of the user's comments and replies
BREAK_PREFIX = "Break the build"  # a run whose rebuild fails
MOCK_FAILURE = "the modeler couldn't make the shape with these values"
FAILED_BUILD = (
    "That version of the model couldn't be built, so there's nothing of it to show. Use the "
    "model on the canvas instead."
)


def speed() -> float:
    try:
        return max(0.0, float(os.environ.get("MOCK_API_SPEED", "1")))
    except ValueError:
        return 1.0


@cache
def _fixture_text(name: str) -> str:
    return (fixtures_dir() / name).read_text()


def _fixture(name: str) -> Any:
    return json.loads(_fixture_text(name))


def now() -> datetime:
    return datetime.now(UTC)


def _mesh() -> MeshInfo:
    """The fixture enclosure's mesh, which every mock build serves."""
    return pins.mesh_info(_fixture_text("enclosure.gltf"))


class SessionState:
    def __init__(self, session: Session):
        self.session = session
        # The browser that made it (`app.owners`): its Recents list it, other browsers' don't.
        self.owner = current_owner.get()
        self.messages: list[Message] = []
        self.events: list[tuple[int, Any]] = []
        self.changed = asyncio.Condition()
        self.builds: list[Build] = []
        self.parameters = [Parameter.model_validate(p) for p in _fixture("parameters.json")]
        self.comments: list[Comment] = []
        self.exports: dict[str, Export] = {}
        self.export_dirs: dict[str, Path] = {}
        self.attachments = Attachments()
        self.played = False
        self.question: Any = None
        self.next_message = 1
        self.next_edit = 1
        # Each build's parameters (values and locks) as they were when it was built.
        self.designs: dict[str, list[Parameter]] = {}
        self.change_set: ChangeSet | None = None
        self.risk_flags: RiskFlags | None = None
        # Builds made by reviewing the change set, with their check reports, by ID.
        self.review_builds: dict[str, tuple[Build, dict]] = {}
        self.sources: dict[str, str] = {}  # build ID -> hand-edited code it was built from
        self.spec = Spec(load_standards())
        self.sketches: list[Sketch] = []
        # The sketched features each build has (`params["sketches"]`), by build ID.
        self.build_sketches: dict[str, list[Sketch]] = {}
        self.sketches_made = 0  # names are never reused, even after an undo
        # The modeling edits in the design, and those each build has, by build ID.
        self.edits: list[Edit] = []
        self.build_edits: dict[str, list[Edit]] = {}
        self.edits_made: set[str] = set()  # names are never reused, even after an undo
        # Builds copied from a demo's original (`app.demos`), served from its files: the sample and
        # the build's ID there, by build ID.
        self.copied: dict[str, tuple[samples.Sample, str]] = {}
        # The message queue: messages sent while a reply streams, in order.
        self.queue: list[QueuedMessage] = []
        self.queue_paused = False  # what the last `queue` event said
        self.next_queued = 1
        self.queue_hold = False  # a queued message is being sent anyway: the queue waits
        self.run: asyncio.Task | None = None  # the reply streaming, if any
        self.open_message: str | None = None  # the reply's message, between start and end
        # The design timeline (`/history`): a step for every change, whoever made it.
        self.history = History()
        self.claude_changes = 0  # runs that changed the design (part of the design state)
        self.imported: set[str] = set()  # builds that are imported files

    def message_id(self) -> str:
        mid = f"msg-{self.next_message}"
        self.next_message += 1
        return mid

    async def emit(self, event: Any) -> None:
        async with self.changed:
            self.events.append((len(self.events) + 1, event))
            self._apply(event)
            self.changed.notify_all()
        if event.type == "message_start":
            self.open_message = event.message_id
        elif event.type == "message_end":
            self.open_message = None
            asyncio.get_running_loop().call_soon(_spawn, _pump(self))

    def _apply(self, event: Any) -> None:
        s = self.session
        if event.type == "message_start":
            self.messages.append(
                Message(id=event.message_id, role="assistant", text="", created_at=now())
            )
            s.status = "running"
        elif event.type == "text":
            msg = next(m for m in self.messages if m.id == event.message_id)
            msg.text += event.delta
        elif event.type == "build_ready":
            self.builds.append(event.build)
            self.designs[event.build.id] = [p.model_copy() for p in self.parameters]
            s.latest_build_id = event.build.id
            self.build_sketches[event.build.id] = list(self.sketches)
            self.build_edits[event.build.id] = list(self.edits)
        elif event.type == "question":
            self.question = event
        elif event.type == "title":
            s.title, s.title_locked = event.title, event.title_locked
        elif event.type == "changes_ready":
            self.change_set = event.change_set
        elif event.type == "risks_ready":
            self.risk_flags = event.risk_flags
        elif event.type == "message_end":
            s.status = "waiting_for_answer" if self.question else "idle"
        elif event.type == "comment":
            i = next((i for i, c in enumerate(self.comments) if c.id == event.comment.id), None)
            if i is None:
                self.comments.append(event.comment)
            else:
                self.comments[i] = event.comment

    def comment(self, comment_id: str) -> Comment:
        found = next((c for c in self.comments if c.id == comment_id), None)
        if found is None:
            raise HTTPException(404, f"no comment {comment_id}")
        return found

    async def put_comment(self, comment: Comment, message_id: str | None = None) -> Comment:
        """Store a new or changed comment: it streams as a `comment` event, located on the
        fixture mesh (every mock build serves it)."""
        located = pins.located(comment, _mesh())
        await self.emit(CommentEvent(type="comment", message_id=message_id, comment=located))
        return located

    async def comment_reply(
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
        return await self.put_comment(comment, message_id)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Export what the export menu asks for by default now, so the first export doesn't wait for
    # a geometry worker.
    threading.Thread(target=_prewarm_exports, name="export-build", daemon=True).start()
    yield


app = FastAPI(
    title="Claude Hardware session API",
    version="1",
    description="Session API contract, served by the mock in `app.mock_api`.",
    lifespan=lifespan,
)
# The demos' read-only originals: their routes match only their IDs, so they go first
# (`app.samples`).
app.include_router(samples.router)
# Each browser's standards sets are its own (`app.owners`), kept in memory here.
app.add_middleware(OwnerMiddleware)
STANDARDS = MemoryStandardsStore()
SESSIONS: dict[str, SessionState] = {}
_tasks: set[asyncio.Task] = set()


def _state(session_id: str) -> SessionState:
    view = SHARE_VIEW.get()  # a share link's snapshot, served as the session (`app.mock_shares`)
    state = view if view is not None and view.session.id == session_id else SESSIONS.get(session_id)
    if state is None:
        raise HTTPException(404, f"no session {session_id}")
    return state


def _with_urls(build: Build, session_id: str) -> Build:
    base = f"{API}/sessions/{session_id}/"
    data = build.model_dump()
    for key in ("mesh_url", "checks_url", "source_url", "board_url", "step_url"):
        if data[key] and not data[key].startswith("/"):
            data[key] = base + data[key]
    return Build.model_validate(data)


def _fixture_build(session_id: str, build_id: str) -> Build:
    """The fixture build, a rebuild after a hand edit (both serve the fixture files), or a
    build made by reviewing the session's change set."""
    view = SHARE_VIEW.get()
    state = view if view is not None and view.session.id == session_id else SESSIONS.get(session_id)
    if state is not None and build_id in state.review_builds:
        return state.review_builds[build_id][0]
    build = Build.model_validate(_fixture("build.json"))
    if build.id == build_id:
        return _with_urls(build, session_id)
    rebuilt = next((b for b in _state(session_id).builds if b.id == build_id), None)
    if rebuilt is None:
        raise HTTPException(404, f"no build {build_id}")
    return rebuilt


def _rebuild(state: SessionState, restores: Build | None = None, failed: bool = False) -> Build:
    """A rebuild after a hand edit, or of an earlier build's design (`restores`): the fixture
    build (on the restored build's revision) under a new ID, with its URLs. A `failed` one has
    its plain reason and no checks."""
    is_previous = restores is not None and _is_previous(restores)
    data = _fixture("previous-build.json" if is_previous else "build.json")
    if failed:
        zero = {"pass": 0, "fail": 0, "untested": 0, "not_applicable": 0}
        data.update(status="failed", failure=MOCK_FAILURE, checks=zero, maps=[])
    build_id = f"{_fixture('build.json')['id']}-edit-{state.next_edit}"
    state.next_edit += 1
    for key in ("mesh_url", "checks_url", "source_url", "board_url", "step_url"):
        if data[key]:
            data[key] = data[key].replace(f"/{data['id']}/", f"/{build_id}/")
    data.update(id=build_id, created_at=now().isoformat())
    if restores is not None:
        data["restored_from"] = restores.id
    return _with_urls(Build.model_validate(data), state.session.id)


def _is_previous(build: Build) -> bool:
    """Whether the build is on the older revision (`fixtures/previous-build.json`)."""
    return build.board == BoardRef.model_validate(_fixture("previous-build.json")["board"])


def _ready_build(session_id: str, build_id: str) -> Build:
    """A build that has a model: a failed build has no mesh, checks or maps to serve."""
    build = _fixture_build(session_id, build_id)
    if build.status != "ready":
        raise HTTPException(404, FAILED_BUILD)
    return build


# --- The design timeline --------------------------------------------------------------------


def _capture(state: SessionState) -> dict[str, Any]:
    """The mock design's state: what a timeline step keeps."""
    latest = state.session.latest_build_id
    return {
        "parameters": [p.model_dump(mode="json") for p in state.parameters],
        "sketches": [k.model_dump(mode="json") for k in state.sketches],
        "edits": [e.model_dump(mode="json") for e in state.edits],
        "code": state.sources.get(latest or ""),
        "change_set": state.change_set.model_dump(mode="json", by_alias=True)
        if state.change_set
        else None,
        "risk_flags": state.risk_flags.model_dump(mode="json", by_alias=True)
        if state.risk_flags
        else None,
        "standards": state.session.standards,
        "claude_changes": state.claude_changes,
        "build_id": latest,
        "import": latest if latest in state.imported else None,
    }


def _put(state: SessionState, saved: dict[str, Any]) -> None:
    state.parameters = [Parameter.model_validate(p) for p in saved["parameters"]]
    state.sketches = [Sketch.model_validate(k) for k in saved["sketches"]]
    state.edits = [Edit.model_validate(e) for e in saved["edits"]]
    state.change_set = (
        ChangeSet.model_validate(saved["change_set"]) if saved["change_set"] else None
    )
    state.risk_flags = (
        RiskFlags.model_validate(saved["risk_flags"]) if saved["risk_flags"] else None
    )
    state.session.standards = saved["standards"]
    state.claude_changes = saved["claude_changes"]


def _record(
    state: SessionState,
    before: dict[str, Any],
    kind: str,
    label: str,
    *,
    actor: str = "user",
    message_id: str | None = None,
) -> None:
    state.history.record(
        before,
        _capture(state),
        actor=actor,
        kind=kind,
        label=label,
        created_at=now(),
        message_id=message_id,
    )


async def _claude_step(state: SessionState, run: Any) -> None:
    """A run of Claude's: if it brought a build or a new change set, it is one step."""
    before, builds, change_set = _capture(state), len(state.builds), state.change_set
    messages = len(state.messages)
    await run
    if len(state.builds) == builds and state.change_set is change_set:
        return
    state.claude_changes += 1
    new = state.change_set if state.change_set is not change_set else None
    titles = [c.title for c in new.changes] if new is not None else []
    replies = [m.id for m in state.messages[messages:] if m.role == "assistant"]
    mid = replies[-1] if replies else None
    _record(state, before, "run", run_label(titles), actor="claude", message_id=mid)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": str(error.detail)}, status_code=error.status_code)


@app.get(API + "/health", include_in_schema=False)
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "version": runtime.app_version(),
        "commit": runtime.commit_sha(),
        "api_key_present": runtime.api_key_present(),
        "mock": True,
    }


# --- Standards ------------------------------------------------------------------------------


app.include_router(standards_router(lambda: STANDARDS))


@app.put(
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
    state = _state(session_id)
    if state.session.status != "idle":
        raise HTTPException(409, "wait until Claude has finished replying")
    try:
        chosen = std_sets.find_set(STANDARDS, std_sets.owner_key(), body.standards)
    except KeyError as error:
        raise HTTPException(404, f"no constraint set {body.standards}") from error
    old = state.spec.standards
    state.session.standards = chosen.id
    state.spec = state.spec.rebased(chosen)
    state.session.status = "running"
    _spawn(_restandardize(state, old, chosen))
    return state.session


async def _restandardize(state: SessionState, old: Any, new: Any) -> None:
    """The mock's recheck after a change of standards: the fixture build, with the notice of
    what changed in the rules (the fixture checks don't change)."""
    mid = state.message_id()
    events: list[Any] = [MessageStartEvent(type="message_start", message_id=mid, role="assistant")]
    if state.spec.active:
        events.append(
            ConstraintsEvent(
                type="constraints", message_id=mid, constraints=state.spec.constraints()
            )
        )
    items = None
    if state.builds:
        build = _fixture_build(state.session.id, Build.model_validate(_fixture("build.json")).id)
        events.append(BuildReadyEvent(type="build_ready", message_id=mid, build=build))
        items = items_from_report(_fixture("checks.json"))
    notice = change_notice(old, new, items, items)
    events += [
        TextEvent(type="text", message_id=mid, delta=notice),
        MessageEndEvent(type="message_end", message_id=mid),
    ]
    for event in events:
        await state.emit(event)


# --- Sessions and messages -------------------------------------------------------------------


@app.get(API + "/sessions", response_model=list[Session], tags=["sessions"])
def list_sessions() -> list[Session]:
    """The browser's own sessions, as the real API lists them (`app.owners`: every browser,
    and every test, sees only what it made), then the demos (`app.demos`).
    Unlike the real API the mock still serves any session by its ID."""
    owner = current_owner.get()
    mine = [s.session for s in SESSIONS.values() if owner is None or s.owner == owner]
    return demos.listed(mine)


@app.get(API + "/models", response_model=ModelCatalog, tags=["sessions"])
async def list_models() -> ModelCatalog:
    """The model picker's models: the catalogue (`contracts/models.json`) entries the server's
    API key can use, in the catalogue's order. The whole catalogue when that can't be checked."""
    return MODEL_CATALOG


@app.get(API + "/examples", response_model=list[ExampleSummary], tags=["sessions"])
def list_examples() -> list[ExampleSummary]:
    """Designs a new session can start from (`demos/<board>/examples.yaml`), opened with
    `POST /api/sessions` and `{"example": id}`. A session starts empty otherwise."""
    return [ExampleSummary(id=e.id, title=e.title, description=e.description) for e in examples()]


@app.post(
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
    Async, so it runs on the event loop: sessions created at once get different IDs.
    With `from_share` it copies a share link's snapshot (`app.mock_shares`)."""
    if body.from_share is not None:
        from app.mock_shares import SHARES

        return await SHARES.continue_from(body)
    sid = f"session-{len(SESSIONS) + 1}"
    if body.example is None and body.board is None:
        title = body.title or DEFAULT_TITLE
        session = Session(id=sid, title=title, created_at=now(), status="idle")
        SESSIONS[sid] = SessionState(session)
        return session
    if body.example is not None:
        try:
            example = find_example(body.example)
        except KeyError as error:
            raise HTTPException(404, f"no example {body.example}") from error
    else:
        try:
            example = example_for_board(body.board.name, body.board.revision)
        except KeyError as error:
            raise HTTPException(422, f"no board {body.board.name} {body.board.revision}") from error
    defaults = _fixture("session.json")
    session = Session(
        id=sid,
        title=body.title or example.title,
        created_at=now(),
        board=body.board or BoardRef.model_validate(defaults["board"]),
        status="idle",
        next_revision=BoardRef.model_validate(_fixture("build.json")["board"]),
    )
    state = SessionState(session)
    state.spec = Spec(example.with_standards(load_standards()))  # with what the example brings
    SESSIONS[sid] = state
    return session


RESET = "reset"  # the owner of a reset demo's copy: no browser's (owner tokens are longer)


@app.post(
    API + "/demos/{demo_id}/session",
    response_model=Session,
    responses={404: {}, 429: {}},
    tags=["sessions"],
)
async def open_demo(demo_id: str) -> Session:
    """The browser's own copy of a demo, made from its original the first time (`app.demos`).
    The handler doesn't await before it adds the copy, so two opens at once get the same one."""
    if not samples.is_sample(demo_id):
        raise HTTPException(404, f"no demo {demo_id}")
    owner = current_owner.get()
    for state in SESSIONS.values():
        if state.session.demo == demo_id and (owner is None or state.owner == owner):
            return state.session
    return _demo_copy(f"session-{len(SESSIONS) + 1}", demo_id)


@app.delete(
    API + "/demos/{demo_id}/session",
    status_code=204,
    responses={404: {}, 409: {}},
    tags=["sessions"],
)
async def reset_demo(demo_id: str) -> Response:
    """Reset a demo: the browser's copy is no longer its own, so the next open makes a fresh
    one. The mock keeps it (session IDs count the sessions), owned by nobody."""
    if not samples.is_sample(demo_id):
        raise HTTPException(404, f"no demo {demo_id}")
    owner = current_owner.get()
    for state in SESSIONS.values():
        if state.session.demo == demo_id and (owner is None or state.owner == owner):
            state.owner, state.session.demo = RESET, None
    return Response(status_code=204)


def _demo_copy(sid: str, demo_id: str) -> Session:
    """The browser's copy of a demo: its conversation, parameters and builds (served from the
    original's files), on its board, with the next revision to load if it has one."""
    start = sample_copies.continuation(demo_id)
    sample = start.sample
    ids = {bid: f"copy-{bid}" for bid in sample.builds}
    latest = start.latest()
    session = Session(
        id=sid,
        title=start.title,
        created_at=now(),
        board=start.board,
        status="idle",
        latest_build_id=ids[latest.id],
        next_revision=start.next_revision,
        standards=sample.session.standards,
        demo=demo_id,
    )
    state = SessionState(session)
    if start.example is not None:  # with the constraints the example brings
        state.spec = Spec(start.example.with_standards(load_standards()))
    state.parameters = sample_copies.copied_parameters(sample)
    state.messages = sample_copies.copied_messages(sample)
    state.next_message = sample_copies.next_message_number(state.messages)
    sample_copies.describe_spec(state.spec, state.messages)
    state.events = sample_copies.copied_events(sample, sid, ids)
    for _, event in state.events:
        if event.type == "build_ready":
            state.builds.append(event.build)
            state.designs[event.build.id] = [p.model_copy() for p in state.parameters]
        elif event.type == "risks_ready":  # the flags its chat shows, to accept and ignore
            state.risk_flags = event.risk_flags
    state.copied = {new: (sample, old) for old, new in ids.items()}
    SESSIONS[sid] = state
    return session


def _copied(session_id: str, build_id: str) -> tuple[samples.Sample, str] | None:
    """The sample and its build ID, for a build copied from a sample."""
    state = SESSIONS.get(session_id)
    return state.copied.get(build_id) if state is not None else None


def _mesh_text(session_id: str, build_id: str) -> str:
    copied = _copied(session_id, build_id)
    if copied is not None:
        return copied[0].file(copied[1], "mesh.gltf").read_text()
    return _fixture_text("enclosure.gltf")


def _map(session_id: str, build_id: str, map_type: str) -> dict:
    copied = _copied(session_id, build_id)
    if copied is not None:
        return {**copied[0].json(copied[1], f"maps/{map_type}.json"), "build_id": build_id}
    return {**_fixture(f"maps/{map_type}.json"), "build_id": build_id}


@app.get(SESSION, response_model=Session, responses=NOT_FOUND, tags=["sessions"])
def get_session(session_id: str) -> Session:
    return _state(session_id).session


@app.patch(
    SESSION,
    response_model=Session,
    responses={**NOT_FOUND, **CONFLICT, 422: {"model": ErrorBody}},
    tags=["sessions"],
)
async def rename_session(session_id: str, body: SessionUpdate) -> Session:
    """Rename the session, as the real API does: the name is cleaned (`app.titles`), locked
    (`title_locked`), and a `title` event with no `message_id` follows on `/events`."""
    state = _state(session_id)
    title = clean_title(body.title)
    if title is None:
        raise HTTPException(422, "a session name needs a letter or a digit")
    await state.emit(TitleEvent(type="title", message_id=None, title=title, title_locked=True))
    return state.session


@app.get(
    SESSION + "/messages", response_model=list[Message], responses=NOT_FOUND, tags=["messages"]
)
def list_messages(session_id: str) -> list[Message]:
    return _state(session_id).messages


@app.post(
    SESSION + "/messages",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["messages"],
)
async def post_message(session_id: str, body: MessageCreate) -> Message:
    """Send a message. The reply streams on `/events`; the response is the stored user message.
    While a reply streams it gets 409: queue it with `POST .../queue` instead."""
    return await _send(_state(session_id), body)


async def _send(state: SessionState, body: MessageCreate) -> Message:
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    if body.answer is not None:
        q = state.question
        if q is None or q.question_id != body.answer.question_id:
            raise HTTPException(409, f"no open question {body.answer.question_id}")
        if body.answer.option_id not in {o.id for o in q.options}:
            raise HTTPException(409, f"no option {body.answer.option_id}")
    elif not body.text.strip() and not body.comments:
        raise HTTPException(409, "a message needs text, comments or an answer")
    for cid in body.comments:
        if state.comment(cid).resolved:
            raise HTTPException(409, f"comment {cid} is resolved")
    try:
        chosen = std_sets.choose(
            STANDARDS,
            std_sets.owner_key(),
            state.session.standards,
            body.standards,
            not state.messages,
        )
    except KeyError as error:
        raise HTTPException(404, f"no constraint set {body.standards}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if chosen is not None:
        state.session.standards = chosen.id
        state.spec = state.spec.rebased(chosen)
    if not any(m.role == "user" for m in state.messages) and state.session.title == DEFAULT_TITLE:
        state.session.title = title_for(body.text) or DEFAULT_TITLE  # as the real API names it
    message = Message(
        id=state.message_id(),
        role="user",
        created_at=now(),
        **body.model_dump(exclude={"text"}),
        text=body.text,
    )
    state.messages.append(message)
    await state.emit(UserMessageEvent(type="user_message", message_id=message.id, message=message))
    state.session.status = "running"
    locked = {p.name: p.value for p in state.parameters if p.locked}
    if body.answer is None and state.spec.describe(body.text, message.id, locked=locked):
        await state.emit(ConstraintsEvent(type="constraints", constraints=state.spec.constraints()))
    state.run = _spawn(_claude_step(state, _respond(state, body)))
    return message


def _spawn(coro: Any) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _respond(state: SessionState, body: MessageCreate) -> None:
    if body.answer is None and body.text.startswith(SLOW_PREFIX):
        await _slow_reply(state, body)
        return
    if body.answer is None and body.text.strip() == _fixture("session.json")["conflict_prompt"]:
        state.played = True
        await _play(state, "conflict-events.jsonl")
        return
    if body.comments:
        await _answer_comments(state, body)
        return
    if body.answer is None and body.text.strip().startswith(BREAK_PREFIX):
        await _break_build(state)
        return
    if not state.played and body.answer is None:
        state.played = True
        await _play(state)
        return
    query = body.text.strip()[len(FIND_PREFIX) :].strip(" .?")
    if body.answer is None and body.text.strip().startswith(FIND_PREFIX) and query:
        await _find_reply(state, query)
        return
    change_set, risk_flags = None, None
    if body.answer is not None:
        option = next(o for o in state.question.options if o.id == body.answer.option_id)
        reply = (
            f'You chose "{option.label}". The mock API has no further scripted steps; here are '
            "the proposed changes to review."
        )
        state.question = None
        change_set = state.change_set or ChangeSet.model_validate(_fixture("change-set.json"))
        risk_flags = state.risk_flags or RiskFlags.model_validate(_fixture("risk-flags.json"))
    else:
        reply = "The mock API plays one scripted run; this message was received."
    mid = state.message_id()
    await state.emit(MessageStartEvent(type="message_start", message_id=mid, role="assistant"))
    await state.emit(TextEvent(type="text", message_id=mid, delta=reply))
    if change_set is not None:
        await state.emit(
            ChangesReadyEvent(type="changes_ready", message_id=mid, change_set=change_set)
        )
    if risk_flags is not None:
        await state.emit(RisksReadyEvent(type="risks_ready", message_id=mid, risk_flags=risk_flags))
    await state.emit(MessageEndEvent(type="message_end", message_id=mid))


SLOW_PREFIX = "Take your time"


def slow_seconds() -> float:
    """How long a "Take your time" reply works before its question (not scaled by speed)."""
    try:
        return max(0.0, float(os.environ.get("MOCK_SLOW_SECONDS", "10")))
    except ValueError:
        return 10.0


async def _slow_reply(state: SessionState, body: MessageCreate) -> None:
    """A reply that works for a while, then asks the scripted question: time to queue messages
    (and stop the run), then a question that pauses the queue."""
    mid = state.message_id()
    await state.emit(MessageStartEvent(type="message_start", message_id=mid, role="assistant"))
    await state.emit(TextEvent(type="text", message_id=mid, delta="Working on it."))
    await asyncio.sleep(slow_seconds())
    script = _script(False, "session-events.jsonl")
    recorded = next(r.event for r in script if r.event.type == "question")
    question = QuestionEvent(
        type="question",
        message_id=mid,
        question_id=recorded.question_id,
        text=recorded.text,
        options=recorded.options,
    )
    await state.emit(question)
    await state.emit(MessageEndEvent(type="message_end", message_id=mid))


async def _break_build(state: SessionState) -> None:
    """A run whose rebuild fails: the failed build streams in the reply."""
    mid = state.message_id()
    build = _rebuild(state, failed=True)
    for event in (
        MessageStartEvent(type="message_start", message_id=mid, role="assistant"),
        ProgressEvent(type="progress", message_id=mid, text="Rebuilding with the change"),
        BuildReadyEvent(type="build_ready", message_id=mid, build=build),
        TextEvent(type="text", message_id=mid, delta="I changed the design, but it didn't build."),
        MessageEndEvent(type="message_end", message_id=mid),
    ):
        await state.emit(event)


def _comment_changes(state: SessionState, comment_ids: list[str]) -> dict[str, Any]:
    """A fixture change per comment, by comment ID: the change on the comment's feature if
    there is one, else the next one left."""
    changes = list(ChangeSet.model_validate(_fixture("change-set.json")).changes)
    out: dict[str, Any] = {}
    for cid in comment_ids:
        feature = state.comment(cid).feature
        pick = next((c for c in changes if feature and c.delta.feature == feature), None)
        pick = pick or (changes[0] if changes else None)
        if pick is not None:
            changes.remove(pick)
            out[cid] = pick
    return out


async def _answer_comments(state: SessionState, body: MessageCreate) -> None:
    """A run for the comments sent to Claude: each gets a fixture change linked to it and a
    reply in its thread; the change set comes with a rebuild, as after a review."""
    mid = state.message_id()
    for cid in body.comments:
        request = CommentRequest(message_id=mid, status="running")
        await state.put_comment(state.comment(cid).model_copy(update={"request": request}))
    await state.emit(MessageStartEvent(type="message_start", message_id=mid, role="assistant"))
    if any(state.comment(cid).text.startswith(ASK_IN_THREAD) for cid in body.comments):
        await _ask_in_thread(state, mid)
        return
    count = len(body.comments)
    text = f"Working on {count} comment{'s' if count > 1 else ''}"
    await state.emit(ProgressEvent(type="progress", message_id=mid, text=text))
    picked = _comment_changes(state, body.comments)
    status = "accepted" if body.mode == "auto" else "proposed"
    linked = [c.model_copy(update={"comment": cid, "status": status}) for cid, c in picked.items()]
    if linked:
        base = ChangeSet.model_validate(_fixture("change-set.json"))
        proposed = base.model_copy(update={"mode": body.mode, "changes": linked})
        build, change_set = _reviewed(state, proposed)
        await state.emit(BuildReadyEvent(type="build_ready", message_id=mid, build=build))
    for cid in body.comments:
        change = picked.get(cid)
        reply = (
            f"{change.title}. {'Applied' if status == 'accepted' else 'Proposed'} as a tracked "
            "change answering this comment."
            if change is not None
            else "The mock API has no recorded change left for this comment."
        )
        await state.comment_reply(cid, reply, claude=True, message_id=mid)
    await state.emit(
        TextEvent(type="text", message_id=mid, delta="I replied in the comment threads.")
    )
    if linked:
        await state.emit(
            ChangesReadyEvent(type="changes_ready", message_id=mid, change_set=change_set)
        )
    for cid in body.comments:
        request = CommentRequest(message_id=mid, status="answered")
        comment = state.comment(cid).model_copy(update={"request": request})
        await state.put_comment(comment, message_id=mid)
    await state.emit(MessageEndEvent(type="message_end", message_id=mid))


# A comment starting with this gets a question from Claude in its thread instead of a change:
# the run waits for the answer, as a real run does when a request breaks a standard.
ASK_IN_THREAD = "Which way"


async def _ask_in_thread(state: SessionState, mid: str) -> None:
    text = (
        "Your comment asks for a size the constraint set doesn't allow: the overall height "
        "limit is 15.1 mm (the existing case), and the design is at that limit now. Making the "
        "part taller would break that constraint. How should I go on?"
    )
    options = [
        QuestionOption(
            id="keep",
            label="Keep the height limit",
            description="Leave the height as it is and say in the thread why the request "
            "doesn't fit the case limit. No geometry change.",
        ),
        QuestionOption(
            id="raise",
            label="Raise the limit for this design",
            description="Make the part as tall as asked and mark the height constraint as "
            "accepted for this design, so the checks report it as a known exception.",
        ),
        QuestionOption(
            id="ask",
            label="Ask the team first",
            description="Hold the change and leave a note in the thread for the team to "
            "decide on the height limit.",
        ),
    ]
    question = QuestionEvent(
        type="question", message_id=mid, question_id=f"{mid}-ask", text=text, options=options
    )
    await state.emit(question)
    state.session.status = "waiting_for_answer"


FIND_PREFIX = "Find "


async def _find_reply(state: SessionState, query: str) -> None:
    """Claude's `find_in_model` on the latest build: a `found` event and a line about it."""
    from app.find import feature_kind

    mid = state.message_id()
    build_id = state.session.latest_build_id
    await state.emit(MessageStartEvent(type="message_start", message_id=mid, role="assistant"))
    if build_id is None:
        reply = "There is no build to search yet."
    else:
        result = find(state.session.id, build_id, FindRequest(query=query))
        await state.emit(ProgressEvent(type="progress", message_id=mid, text=f"Finding {query}"))
        await state.emit(FoundEvent(type="found", message_id=mid, build_id=build_id, result=result))
        if result.status == "unclear":
            reply = f"I read that as: {result.understood}. {result.question}"
        elif result.status == "none":
            reply = f"Nothing matches. I read that as: {result.understood}."
        else:
            kinds = sorted({feature_kind(m.feature) for m in result.matches})
            features = len({m.feature for m in result.matches})
            reply = (
                f"{result.understood}: {len(result.matches)} faces in {features} features "
                f"({', '.join(kinds)}), highlighted on the model. Use them as the selection to "
                "ask about them."
            )
    await state.emit(TextEvent(type="text", message_id=mid, delta=reply))
    await state.emit(MessageEndEvent(type="message_end", message_id=mid))


@app.post(
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
    state = _state(session_id)
    body = body or RevisionLoad()
    board = state.session.next_revision
    if board is None:
        raise HTTPException(409, "no newer board revision to load")
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    if state.question is not None:
        raise HTTPException(409, "answer the open question first")
    previous = BoardRef.model_validate(_fixture("previous-build.json")["board"])
    message = Message(
        id=state.message_id(),
        role="user",
        created_at=now(),
        **body.model_dump(),
        text=revision_message(previous, board),
    )
    state.messages.append(message)
    await state.emit(UserMessageEvent(type="user_message", message_id=message.id, message=message))
    state.session.next_revision = None
    state.session.status = "running"
    state.played = True
    state.run = _spawn(_claude_step(state, _play(state, proposal=True)))
    return message


# --- Message queue ----------------------------------------------------------------------------

STOPPED = limits.stopped_text("stopped")
SENT_ANYWAY = "The question was left unanswered: your next message was sent instead."


def _queue(state: SessionState) -> MessageQueue:
    return MessageQueue(
        items=list(state.queue), paused=state.session.status == "waiting_for_answer"
    )


async def _queue_changed(
    state: SessionState, sent: QueuedMessage | None = None, message: str | None = None
) -> None:
    state.queue_paused = state.session.status == "waiting_for_answer"
    sent_id = sent.id if sent is not None else None
    await state.emit(
        QueueEvent(type="queue", queue=_queue(state), sent=sent_id, message_id=message)
    )


async def _pump(state: SessionState) -> None:
    """Send the next queued message once no reply streams and no question waits."""
    if state.queue_hold or not state.queue:
        return
    status = state.session.status
    if status != "idle":
        if state.queue_paused != (status == "waiting_for_answer"):
            await _queue_changed(state)
        return
    item = state.queue.pop(0)
    try:
        message = await _send(state, item.message)
    except HTTPException as error:
        text = f"A queued message couldn't be sent: {error.detail}"
        await state.emit(ErrorEvent(type="error", message=text))
        await _queue_changed(state)
        _spawn(_pump(state))
        return
    await _queue_changed(state, item, message.id)


def _queued(state: SessionState, item_id: str) -> QueuedMessage:
    found = next((q for q in state.queue if q.id == item_id), None)
    if found is None:
        raise HTTPException(404, f"no queued message {item_id}")
    return found


def _check_queued(body: MessageCreate) -> None:
    if body.answer is not None:
        raise HTTPException(409, "a queued message can't answer a question: answer it directly")
    if not body.text.strip() and not body.comments:
        raise HTTPException(409, "a message needs text or comments")


async def _stop(state: SessionState, note: str) -> None:
    """End the reply streaming (if any) with `note`, and any open question unanswered."""
    run = state.run
    if run is not None and not run.done():
        run.cancel()
        await asyncio.wait({run})
    mid = state.open_message
    if mid is None and state.question is not None:  # the run ended on its question: say it there
        mid = state.question.message_id
    if mid is not None:
        if note == STOPPED:  # Stop: the "stopped" error, with Retry
            ended = ErrorEvent(
                type="error", message_id=mid, message=note, retryable=True, reason="stopped"
            )
            await state.emit(ended)
        else:
            await state.emit(ProgressEvent(type="progress", message_id=mid, text=note))
        await state.emit(MessageEndEvent(type="message_end", message_id=mid))
    state.question = None
    state.session.status = "idle"


@app.get(SESSION + "/queue", response_model=MessageQueue, responses=NOT_FOUND, tags=["queue"])
def get_queue(session_id: str) -> MessageQueue:
    """The messages waiting for Claude's current run to end, in the order they will be sent."""
    return _queue(_state(session_id))


@app.post(
    SESSION + "/queue",
    response_model=QueuedMessage,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def queue_message(session_id: str, body: MessageCreate) -> QueuedMessage:
    """Send a message while Claude is working: it waits in the queue and is sent as a normal
    message once the runs before it have ended. On an idle session it is sent at once."""
    state = _state(session_id)
    _check_queued(body)
    item = QueuedMessage(id=f"queued-{state.next_queued}", created_at=now(), message=body)
    state.next_queued += 1
    state.queue.append(item)
    await _queue_changed(state)
    await _pump(state)
    return item


@app.patch(
    SESSION + "/queue/{item_id}",
    response_model=QueuedMessage,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def update_queued(session_id: str, item_id: str, body: QueuedMessageUpdate) -> QueuedMessage:
    """Edit a queued message's text, or move it (`position`, 0: next). 404 once it was sent."""
    state = _state(session_id)
    item = _queued(state, item_id)
    at = state.queue.index(item) if body.position is None else body.position
    if body.text is not None:
        edited = item.message.model_copy(update={"text": body.text})
        _check_queued(edited)
        item = item.model_copy(update={"message": edited})
    rest = [q for q in state.queue if q.id != item_id]
    rest.insert(min(at, len(rest)), item)
    state.queue = rest
    await _queue_changed(state)
    return item


@app.delete(
    SESSION + "/queue/{item_id}",
    response_model=QueuedMessage,
    responses=NOT_FOUND,
    tags=["queue"],
)
async def remove_queued(session_id: str, item_id: str) -> QueuedMessage:
    """Take a message out of the queue before it is sent. 404 once it was sent."""
    state = _state(session_id)
    item = _queued(state, item_id)
    state.queue.remove(item)
    await _queue_changed(state)
    return item


@app.post(
    SESSION + "/queue/{item_id}/send",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def send_queued(session_id: str, item_id: str) -> Message:
    """Send a queued message now, as a normal message ("Send anyway"). While Claude's question
    is open, the question is left unanswered: the paused run ends with a note saying so, and
    the message starts a new run; it is never taken as the answer. 409 while Claude is working
    on a run that isn't waiting for an answer: it is sent when the run ends."""
    state = _state(session_id)
    item = _queued(state, item_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: it is sent when the run ends")
    state.queue.remove(item)
    state.queue_hold = True
    try:
        if state.question is not None:
            await _stop(state, SENT_ANYWAY)
        message = await _send(state, item.message)
    except HTTPException:
        state.queue.insert(0, item)
        raise
    finally:
        state.queue_hold = False
    await _queue_changed(state, item, message.id)
    return message


@app.post(
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
    state = _state(session_id)
    body = body or RunRetry()
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    if state.question is not None:
        raise HTTPException(409, "answer the open question first")
    if not any(m.role == "user" for m in state.messages):
        raise HTTPException(409, "there is no reply to retry")
    message = Message(
        id=state.message_id(), role="user", created_at=now(), **body.model_dump(), text="Retry"
    )
    state.messages.append(message)
    await state.emit(UserMessageEvent(type="user_message", message_id=message.id, message=message))
    state.session.status = "running"
    _spawn(_respond(state, MessageCreate(text="Retry", **body.model_dump())))
    return message


@app.post(
    SESSION + "/stop",
    response_model=Session,
    status_code=202,
    responses=NOT_FOUND,
    tags=["messages"],
)
async def stop_run(session_id: str) -> Session:
    """Stop the reply that is running (or waiting for an answer). It ends with an `error`
    saying it was stopped (`reason` "stopped", `retryable`); the design is kept, Retry picks up
    from it, and the next queued message, if any, starts. Nothing to stop: the session as it
    is."""
    state = _state(session_id)
    if state.session.status != "idle":
        await _stop(state, STOPPED)
    await _pump(state)
    return state.session


def _script(proposal: bool, script: str) -> list[RecordedEvent]:
    """The scripted run. As a `proposal` (a loaded revision, which nobody asked about) it goes
    on without its question and ends with the fixture change set and risk flags."""
    recs = [RecordedEvent.model_validate_json(line) for line in _fixture_text(script).splitlines()]
    if not proposal:
        return recs
    recs = [r for r in recs if r.event.type != "question"]
    end = max(i for i, r in enumerate(recs) if r.event.type == "message_end")
    change_set = ChangeSet.model_validate(_fixture("change-set.json"))
    mid = recs[end].event.message_id
    ready = ChangesReadyEvent(type="changes_ready", message_id=mid, change_set=change_set)
    risk_flags = RiskFlags.model_validate(_fixture("risk-flags.json"))
    risks = RisksReadyEvent(type="risks_ready", message_id=mid, risk_flags=risk_flags)
    extra = [RecordedEvent(id=recs[end].id, delay_ms=0, event=e) for e in (ready, risks)]
    return [*recs[:end], *extra, *recs[end:]]


async def _play(
    state: SessionState, script: str = "session-events.jsonl", proposal: bool = False
) -> None:
    ids: dict[str, str] = {}
    for rec in _script(proposal, script):
        if rec.delay_ms and speed():
            await asyncio.sleep(rec.delay_ms / 1000 / speed())
        event = rec.event
        mid = getattr(event, "message_id", None)
        if mid is not None:
            if mid not in ids:
                ids[mid] = state.message_id()
            event = event.model_copy(update={"message_id": ids[mid]})
        if isinstance(event, BuildReadyEvent):
            event = event.model_copy(update={"build": _with_urls(event.build, state.session.id)})
        await state.emit(event)


@app.get(
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
    state = _state(session_id)
    start = after if after is not None else int(last_event_id or 0)

    async def stream():
        last = start
        while True:
            async with state.changed:
                pending = [e for e in state.events if e[0] > last]
                if not pending:
                    if not follow:
                        return
                    try:
                        await asyncio.wait_for(state.changed.wait(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        pending = None
            if pending is None:
                yield ": keep-alive\n\n"
                continue
            for event_id, event in pending:
                yield sse_frame(event_id, event)
                last = event_id

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type=EVENT_STREAM, headers=headers)


# --- Attachments ------------------------------------------------------------------------------


@app.post(
    SESSION + "/attachments",
    response_model=Attachment,
    status_code=201,
    responses=UPLOAD_RESPONSES,
    tags=["attachments"],
)
async def upload_attachment(session_id: str, file: UploadField) -> Attachment:
    """Upload a file to send with a message: the response goes in the message's `attachments`."""
    state = _state(session_id)
    return await state.attachments.add(f"{API}/sessions/{session_id}/attachments", file)


@app.get(
    SESSION + "/attachments/{attachment_id}",
    response_class=Response,
    responses=FILE_RESPONSES,
    tags=["attachments"],
)
def get_attachment(session_id: str, attachment_id: str) -> Response:
    return _state(session_id).attachments.response(attachment_id)


# --- Imports --------------------------------------------------------------------------------


@app.post(
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
    # The mock refuses what the real API refuses before reading the file, then serves the
    # fixture build under a new ID, as after a hand edit: it doesn't read the file.
    state = _state(session_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: import when the run has finished")
    name, _, data = await read_upload(file, MAX_IMPORT_BYTES + 1)
    check_upload(name, data)
    state.attachments.store(f"{API}/sessions/{session_id}/attachments", name, STEP_MEDIA_TYPE, data)
    before = _capture(state)
    state.change_set = None  # proposed on the design this replaces, as in the real API
    build = _rebuild(state)
    # The chat says where the build came from (`Build.origin`), as in the real API.
    build = build.model_copy(update={"origin": BuildOrigin(kind="import", file_name=name)})
    state.imported.add(build.id)
    await state.emit(BuildReadyEvent(type="build_ready", build=build))
    _record(state, before, "import", f"imported {name}")
    return build


# --- Builds ---------------------------------------------------------------------------------


@app.get(SESSION + "/builds", response_model=list[Build], responses=NOT_FOUND, tags=["builds"])
def list_builds(session_id: str) -> list[Build]:
    return _state(session_id).builds


@app.get(SESSION + "/builds/{build_id}", response_model=Build, responses=NOT_FOUND, tags=["builds"])
def get_build(session_id: str, build_id: str) -> Build:
    _state(session_id)
    return _fixture_build(session_id, build_id)


@app.get(
    SESSION + "/builds/{build_id}/mesh",
    response_class=Response,
    responses={
        200: {"description": "glTF following the mesh contract", "content": {GLTF: {}}},
        **NOT_FOUND,
    },
    tags=["builds"],
)
def get_mesh(session_id: str, build_id: str) -> Response:
    _state(session_id)
    _ready_build(session_id, build_id)
    return Response(_mesh_text(session_id, build_id), media_type=GLTF)


@app.get(
    SESSION + "/builds/{build_id}/checks",
    response_model=CheckReport,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_checks(session_id: str, build_id: str) -> dict:
    state = _state(session_id)
    build = _ready_build(session_id, build_id)
    if build_id in state.review_builds:
        return state.review_builds[build_id][1]
    if build_id in state.copied:
        sample, old = state.copied[build_id]
        return {**sample.json(old, "checks.json"), "build_id": build_id}
    name = "previous-checks.json" if _is_previous(build) else "checks.json"
    return {**_fixture(name), "build_id": build_id}


@app.get(
    SESSION + "/builds/{build_id}/source",
    response_model=BuildSource,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_source(session_id: str, build_id: str) -> dict:
    state = _state(session_id)
    _fixture_build(session_id, build_id)
    return _source(state, build_id)


def _source(state: SessionState, build_id: str) -> dict:
    """The build's source: the fixture's, or the hand-edited code it was rebuilt from."""
    copied = state.copied.get(build_id)
    source = copied[0].json(copied[1], "source.json") if copied else _fixture("build-source.json")
    code = state.sources.get(build_id)
    if code is None:
        source = {**source, "build_id": build_id}
    else:
        source = edited_source(source, build_id, code)
    sketches = state.build_sketches.get(build_id, [])
    source = with_sketches(source, [sketch_params(k) for k in sketches])
    return with_edits(source, [edit_params(e) for e in state.build_edits.get(build_id, [])])


@app.put(
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
    state = _state(session_id)
    _fixture_build(session_id, body.build_id)
    if state.session.status == "running":
        raise HTTPException(409, "the session is still running")
    error = code_error(body.code, _fixture("build-source.json")["filename"])
    if error is not None:
        return code_error_response(error)
    before = _capture(state)
    build = _rebuild(state)
    state.sources[build.id] = body.code
    await state.emit(BuildReadyEvent(type="build_ready", build=build))
    _record(state, before, "code", "edited the code")
    return build


@app.get(
    SESSION + "/builds/{build_id}/board",
    response_model=Board,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_board(session_id: str, build_id: str) -> Board:
    """The board revision the build was checked against, from `demos/<board>/`."""
    _state(session_id)
    build = _fixture_build(session_id, build_id)
    if build.board is None:
        raise HTTPException(404, f"build {build_id} has no board")
    try:
        return find_board(build.board.name, build.board.revision)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.get(
    SESSION + "/builds/{build_id}/maps/{map_type}",
    response_model=AnalysisMap,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_map(session_id: str, build_id: str, map_type: MapType) -> dict:
    _state(session_id)
    _ready_build(session_id, build_id)
    return _map(session_id, build_id, map_type)


@app.get(
    SESSION + "/builds/{build_id}/diff",
    response_model=BuildDiff,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_diff(
    session_id: str,
    build_id: str,
    against: Annotated[str, Query(description="The build to compare with: the diff's `old`")],
) -> BuildDiff:
    """What changed from build `against` to this build: the parameters changed and the
    features added, removed, moved, rotated or resized, with distances."""
    state = _state(session_id)
    old, new = _fixture_build(session_id, against), _fixture_build(session_id, build_id)
    copied_old, copied_new = state.copied.get(against), state.copied.get(build_id)
    if copied_old and copied_new and copied_old[0] is copied_new[0]:
        path = copied_new[0].file(copied_new[1], f"diffs/{copied_old[1]}.json")
        if path.is_file():
            doc = json.loads(path.read_text())
            return BuildDiff.model_validate({**doc, "old": against, "new": build_id})
    designs = None
    if old.board == new.board:  # across revisions the fixture diff has the parameters

        def design(b: Build) -> dict:
            params = {p.name: p.value for p in state.designs.get(b.id, state.parameters)}
            return {"params": params, "code": None}

        designs = (design(old), design(new))
    return build_diff(old, new, designs)


@app.post(
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
    state = _state(session_id)
    target = _fixture_build(session_id, build_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: restore when the run has finished")
    if target.status != "ready":
        raise HTTPException(
            409, "That version of the model couldn't be built, so it can't be restored."
        )
    before = _capture(state)
    saved = state.designs.get(target.id)
    state.change_set = None  # proposed on the design this replaces, as in the real API
    state.risk_flags = None  # flagged on it too
    if saved is not None:
        state.parameters = [p.model_copy() for p in saved]
    if target.id in state.build_sketches:  # its sketched features too
        state.sketches = list(state.build_sketches[target.id])
    if target.id in state.build_edits:  # and its modeling edits
        state.edits = list(state.build_edits[target.id])
    build = _rebuild(state, restores=target)
    if target.id in state.sources:  # the restored design runs the hand-edited code
        state.sources[build.id] = state.sources[target.id]
    await state.emit(BuildReadyEvent(type="build_ready", build=build))
    _record(state, before, "restore", "restored an earlier version")
    return build


# --- The design timeline ----------------------------------------------------------------------


@app.get(SESSION + "/history", response_model=DesignHistory, responses=NOT_FOUND, tags=["history"])
def get_history(session_id: str) -> DesignHistory:
    """The design timeline: every change to the design, whoever made it, oldest first, with how
    many are applied (`position`) and the steps Undo and Redo would move over."""
    return _state(session_id).history.model()


async def _move(state: SessionState, direction: str) -> HistoryMove:
    if state.session.status == "running":
        raise HTTPException(
            409, "Claude is working: undo and redo wait until the run has finished."
        )
    history = state.history
    if direction == "undo" and not history.can_undo():
        raise HTTPException(409, "There's nothing to undo.")
    if direction == "redo" and not history.can_redo():
        raise HTTPException(409, "There's nothing to redo.")
    step = history.steps[history.position - 1 if direction == "undo" else history.position]
    target = history.state_at(history.position + (-1 if direction == "undo" else 1))
    assert target is not None
    was = next((b for b in state.builds if b.id == target["build_id"]), None)
    _put(state, target)
    build = _rebuild(state, failed=was is not None and was.status != "ready")
    if target["code"] is not None:
        state.sources[build.id] = target["code"]
    if target["import"] is not None:
        state.imported.add(build.id)
    await state.emit(BuildReadyEvent(type="build_ready", build=build))
    history.moved(direction, step)
    return HistoryMove(
        direction=direction,  # type: ignore[arg-type]
        step=History.step_model(step),
        build=build,
        history=history.model(),
        change_set=state.change_set,
    )


@app.post(
    SESSION + "/history/undo",
    response_model=HistoryMove,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["history"],
)
async def undo(session_id: str) -> HistoryMove:
    """Undo the last change to the design, whoever made it (`DesignHistory.undo`): the design
    goes back to the state before it and is rebuilt; the rebuild streams as a `build_ready`
    with no message. 409 while Claude is working, with nothing to undo, or when the earlier
    design can't be rebuilt (the design stays as it is)."""
    return await _move(_state(session_id), "undo")


@app.post(
    SESSION + "/history/redo",
    response_model=HistoryMove,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["history"],
)
async def redo(session_id: str) -> HistoryMove:
    """Redo the step the last undo took back (`DesignHistory.redo`), rebuilt like an undo. A new
    change after an undo drops the steps that could be redone."""
    return await _move(_state(session_id), "redo")


# --- Changes ----------------------------------------------------------------------------------


def _change_set(state: SessionState) -> ChangeSet:
    if state.change_set is None:
        raise HTTPException(
            404, "no change set: answer the scripted run's question first (a restore retires it)"
        )
    return state.change_set


def _reviewed_report(change_set: ChangeSet, build_id: str) -> dict:
    """The fixture check report as it is with only the accepted changes: each accepted
    change's checks take their status with the change alone."""
    report = _fixture("checks.json")
    status: dict[str, str] = {}
    titles: dict[str, str] = {}
    for change in change_set.changes:
        if change.status != "accepted":
            continue
        for link in change.checks:
            if link.alone is not None and link.alone != link.before:
                status[link.id] = link.alone
                titles[link.id] = change.title
    for result in report["results"]:
        if result["id"] in status and status[result["id"]] != result["status"]:
            result["status"] = status[result["id"]]
            result["summary"] = (
                f"{'Passes' if result['status'] == 'pass' else 'Fails'} with the change: "
                f"{titles[result['id']]}."
            )
    counts = {
        s: sum(r["status"] == s for r in report["results"])
        for s in ("pass", "fail", "untested", "not_applicable")
    }
    return {**report, "build_id": build_id, "summary": counts}


@app.get(
    SESSION + "/changes",
    response_model=ChangeSet,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["changes"],
)
def get_changes(session_id: str) -> ChangeSet:
    """The change set of the latest run that changed the design."""
    return _change_set(_state(session_id))


@app.post(
    SESSION + "/changes",
    response_model=ChangeSet,
    response_model_by_alias=True,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["changes"],
)
async def review_changes(session_id: str, body: ChangeReview) -> ChangeSet:
    """Accept and reject changes by ID. The design is rebuilt and rechecked with only the
    accepted changes; the build streams as `build_ready`, then the set as `changes_ready`."""
    state = _state(session_id)
    current = _change_set(state)
    if state.session.status != "idle":
        raise HTTPException(409, "the session is still running")
    ids = {c.id for c in current.changes}
    unknown = [c for c in [*body.accept, *body.reject] if c not in ids]
    if unknown:
        raise HTTPException(404, f"no change {', '.join(unknown)}")
    both = sorted(set(body.accept) & set(body.reject))
    if both:
        raise HTTPException(409, f"both accepted and rejected: {', '.join(both)}")
    decided = {**{c: "accepted" for c in body.accept}, **{c: "rejected" for c in body.reject}}
    changes = [
        c.model_copy(update={"status": decided.get(c.id, c.status)}) for c in current.changes
    ]
    reviewed = current.model_copy(update={"changes": changes})
    before = _capture(state)
    build, change_set = _reviewed(state, reviewed)
    await state.emit(BuildReadyEvent(type="build_ready", message_id=None, build=build))
    await state.emit(
        ChangesReadyEvent(type="changes_ready", message_id=None, change_set=change_set)
    )
    _record(state, before, "review", review_label(body.accept, body.reject))
    return change_set


def _reviewed(state: SessionState, reviewed: ChangeSet) -> tuple[Build, ChangeSet]:
    """The build of a change set's accepted changes (the fixture build with their proof as
    its checks), and the set on it."""
    session_id = state.session.id
    build_id = f"build-{len(state.review_builds) + 2}"  # the fixture build is the first
    report = _reviewed_report(reviewed, build_id)
    after = {r["id"]: r["status"] for r in report["results"]}
    changes = [
        c.model_copy(
            update={"checks": [k.model_copy(update={"after": after.get(k.id)}) for k in c.checks]}
        )
        for c in reviewed.changes
    ]
    build = _with_urls(
        Build.model_validate(
            {
                **_fixture("build.json"),
                "id": build_id,
                "created_at": now(),
                "checks": report["summary"],
                "mesh_url": f"builds/{build_id}/mesh",
                "checks_url": f"builds/{build_id}/checks",
                "source_url": f"builds/{build_id}/source",
                "board_url": f"builds/{build_id}/board",
                "step_url": None,
            }
        ),
        session_id,
    )
    state.review_builds[build_id] = (build, report)
    change_set = ChangeSet.model_validate(
        {
            **reviewed.model_dump(by_alias=True),
            "build": build_id,
            "checks": report["summary"],
            "changes": [c.model_dump(by_alias=True) for c in changes],
        }
    )
    return build, change_set


@app.post(
    SESSION + "/builds/{build_id}/find",
    response_model=FindResult,
    responses=NOT_FOUND,
    tags=["builds"],
)
def find(session_id: str, build_id: str, body: FindRequest) -> FindResult:
    """Find in model: the faces a query in words describes ("all M3 holes", "faces under 1° of
    draft"), from the build's feature tags, the parameters and the analysis maps. A query that
    isn't understood comes back `unclear`, saying what was understood and asking."""
    from app.find import find_in_build
    from app.standards import load_standards

    state = _state(session_id)
    build = _fixture_build(session_id, build_id)
    maps = {t: _map(session_id, build_id, t) for t in build.maps}
    return find_in_build(
        body.query, _mesh_text(session_id, build_id), state.parameters, maps, load_standards()
    )


@app.get(
    SESSION + "/builds/{build_id}/faces/{face_id}",
    response_model=FacePlane,
    responses=NOT_FOUND,
    tags=["builds"],
)
def get_face(session_id: str, build_id: str, face_id: str) -> FacePlane:
    """A face of the build: whether it is flat, and if so the plane a sketch on it uses."""
    _state(session_id)
    _fixture_build(session_id, build_id)
    plane = face_plane(_mesh_text(session_id, build_id), build_id, face_id)
    if plane is None:
        raise HTTPException(404, f"no face {face_id}")
    return plane


# --- Sketches ---------------------------------------------------------------------------------


@app.get(SESSION + "/sketches", response_model=list[Sketch], responses=NOT_FOUND, tags=["sketches"])
def list_sketches(session_id: str) -> list[Sketch]:
    return _state(session_id).sketches


@app.post(
    SESSION + "/sketches",
    response_model=Sketch,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["sketches"],
)
async def create_sketch(session_id: str, body: SketchCreate) -> Sketch:
    """Sketch on a flat face: a circle, slot, rectangle or vent with typed size and position,
    cut into the part or extruded from it. A hand edit: the sketch becomes a named, locked
    feature (`sketches.<tag>` in the design) that Claude keeps, in the code, and the design
    rebuilds and rechecks: a `build_ready` without a `message_id` follows on `/events`.
    409 if the face isn't flat, the shape runs off it, or Claude is running."""
    state = _state(session_id)
    _ready_build(session_id, body.build_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: sketch when the run ends")
    before = _capture(state)
    try:
        made = [{"tag": f"sketch_{n + 1}"} for n in range(state.sketches_made)]
        sketch = new_sketch(body, _fixture_text("enclosure.gltf"), made)
    except SketchError as error:
        raise HTTPException(409, str(error)) from error
    if sketch is None:
        raise HTTPException(404, f"no face {body.face_id}")
    state.sketches.append(sketch)
    state.sketches_made += 1
    await state.emit(BuildReadyEvent(type="build_ready", build=_rebuild(state)))
    _record(state, before, "sketch", sketch_label(sketch))
    return sketch


# --- Modeling edits -------------------------------------------------------------------


@app.get(SESSION + "/edits", response_model=list[Edit], responses=NOT_FOUND, tags=["edits"])
def list_edits(session_id: str) -> list[Edit]:
    return _state(session_id).edits


def _editable(state: SessionState) -> None:
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: edit the model when the run ends")


def _edit(state: SessionState, tag: str) -> Edit:
    edit = next((e for e in state.edits if e.tag == tag), None)
    if edit is None:
        raise HTTPException(404, f"no edit {tag} in the design")
    return edit


@app.post(
    SESSION + "/edits",
    response_model=Edit,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def create_edit(session_id: str, body: EditCreate) -> Edit:
    """A modeling tool's edit on a build: move a part, push or pull a flat face, add a hole
    (optionally for the standard insert), boss, rib, fillet, chamfer or shell, or mirror or
    pattern a feature. It becomes a named, locked feature (`edits.<tag>`) that Claude keeps, in
    the code, and the design rebuilds and rechecks: a `build_ready` without a `message_id`
    follows on `/events`. 409 if it can't be made there (a curved face, off the face, no such
    feature) or Claude is running."""
    state = _state(session_id)
    _ready_build(session_id, body.build_id)
    _editable(state)
    before = _capture(state)
    features = source_parts(_source(state, body.build_id))
    try:
        mesh, standards = _fixture_text("enclosure.gltf"), state.spec.effective()
        edit = new_edit(body, mesh, features, state.edits_made, standards)
    except EditError as error:
        raise HTTPException(409, str(error)) from error
    if edit is None:
        what = f"edge {body.edge_id}" if body.edge_id else f"face {body.face_id}"
        raise HTTPException(404, f"no {what}")
    state.edits.append(edit)
    state.edits_made.add(edit.tag)
    await state.emit(BuildReadyEvent(type="build_ready", build=_rebuild(state)))
    _record(state, before, "edit", edit_label(edit))
    return edit


@app.patch(
    SESSION + "/edits/{tag}",
    response_model=Edit,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def update_edit(session_id: str, tag: str, body: EditUpdate) -> Edit:
    """Change a modeled feature's values, or suppress it or bring it back: still locked, and the
    design rebuilds and rechecks. A suppressed edit stays in the design but not in the build."""
    state = _state(session_id)
    _editable(state)
    old, before = _edit(state, tag), _capture(state)
    try:
        edit = updated_edit(old, body)
    except EditError as error:
        raise HTTPException(409, str(error)) from error
    state.edits = [edit if e.tag == tag else e for e in state.edits]
    await state.emit(BuildReadyEvent(type="build_ready", build=_rebuild(state)))
    if edit.suppressed != old.suppressed:
        label = f"{'suppressed' if edit.suppressed else 'brought back'} {edit_label(edit)}"
    else:
        label = f"changed {edit_label(old)} to {edit_label(edit)}"
    _record(state, before, "edit", label)
    return edit


@app.delete(
    SESSION + "/edits/{tag}",
    response_model=EditRemoval,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def delete_edit(session_id: str, tag: str) -> EditRemoval:
    """Delete a modeled feature, and the mirrors and patterns that copy it (`removed`). The
    design rebuilds and rechecks without them."""
    state = _state(session_id)
    _editable(state)
    edit, before = _edit(state, tag), _capture(state)
    removed = removal([edit_params(e) for e in state.edits], tag)
    state.edits = [e for e in state.edits if e.tag not in removed]
    await state.emit(BuildReadyEvent(type="build_ready", build=_rebuild(state)))
    _record(state, before, "edit", f"deleted {edit_label(edit)}")
    return EditRemoval(tag=tag, removed=removed)


# --- Risks ------------------------------------------------------------------------------------


def _risk_flags(state: SessionState) -> RiskFlags:
    if state.risk_flags is None:
        raise HTTPException(
            404, "no risk flags: answer the scripted run's question first (a restore retires them)"
        )
    return state.risk_flags


@app.get(
    SESSION + "/risks",
    response_model=RiskFlags,
    response_model_by_alias=True,
    responses=NOT_FOUND,
    tags=["risks"],
)
def get_risks(session_id: str) -> RiskFlags:
    """The risk flags of the latest run that flagged any, each with its fix."""
    return _risk_flags(_state(session_id))


@app.post(
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
    state = _state(session_id)
    current = _risk_flags(state)
    if state.session.status != "idle":
        raise HTTPException(409, "the session is still running")
    ids = {f.id for f in current.flags}
    unknown = [f for f in [*body.accept, *body.reject] if f not in ids]
    if unknown:
        raise HTTPException(404, f"no flag {', '.join(unknown)}")
    both = sorted(set(body.accept) & set(body.reject))
    if both:
        raise HTTPException(409, f"both accepted and rejected: {', '.join(both)}")
    decided = {**{f: "accepted" for f in body.accept}, **{f: "rejected" for f in body.reject}}
    flags = [f.model_copy(update={"status": decided.get(f.id, f.status)}) for f in current.flags]
    reviewed = current.model_copy(update={"flags": flags})
    # A fix breaks no passing check: the rebuild has the checks of the design it goes over.
    reviewed_set = state.change_set
    base = (
        state.review_builds.get(reviewed_set.build) if reviewed_set and reviewed_set.build else None
    )
    build_id = f"build-{len(state.review_builds) + 2}"  # the fixture build is the first
    report = {**(base[1] if base else _fixture("checks.json")), "build_id": build_id}
    build = _with_urls(
        Build.model_validate(
            {
                **_fixture("build.json"),
                "id": build_id,
                "created_at": now(),
                "checks": report["summary"],
                "mesh_url": f"builds/{build_id}/mesh",
                "checks_url": f"builds/{build_id}/checks",
                "source_url": f"builds/{build_id}/source",
                "board_url": f"builds/{build_id}/board",
                "step_url": None,
            }
        ),
        session_id,
    )
    state.review_builds[build_id] = (build, report)
    before = _capture(state)
    await state.emit(BuildReadyEvent(type="build_ready", message_id=None, build=build))
    await state.emit(RisksReadyEvent(type="risks_ready", message_id=None, risk_flags=reviewed))
    _record(state, before, "risks", review_label(body.accept, body.reject, "fix"))
    return reviewed


# --- Parameters and comments ------------------------------------------------------------------


@app.get(
    SESSION + "/parameters",
    response_model=list[Parameter],
    responses=NOT_FOUND,
    tags=["parameters"],
)
def list_parameters(session_id: str) -> list[Parameter]:
    """Each with where it is measured on the latest build's mesh (`app.dimensions`)."""
    state = _state(session_id)
    latest = state.session.latest_build_id
    if latest is None:
        return measured(state.parameters, None)
    copied = _copied(session_id, latest)
    design = copied[0].json(copied[1], "design.json")["params"] if copied else None
    return measured(state.parameters, _mesh_text(session_id, latest), design)


@app.patch(
    SESSION + "/parameters/{name}",
    response_model=Parameter,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["parameters"],
)
async def update_parameter(session_id: str, name: str, body: ParameterUpdate) -> Parameter:
    """A hand edit: setting `value` also locks the parameter unless `locked` says otherwise,
    and rebuilds: a `build_ready` without a `message_id` follows on `/events`. Unlocking hands
    the parameter back to Claude. Refused (409) while a run is going."""
    state = _state(session_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: edit when the run has finished")
    param = next((p for p in state.parameters if p.name == name), None)
    if param is None:  # a grouped value shown as a dimension (`boss.radius`): listed now
        param = next((p for p in list_parameters(session_id) if p.name == name), None)
        if param is not None:
            param = param.model_copy(update={"dimension": None})
            state.parameters.append(param)
    if param is None:
        raise HTTPException(404, f"no parameter {name}")
    before = _capture(state)
    if body.value is not None:
        param.value = body.value
        param.locked = True
    if body.locked is not None:
        param.locked = body.locked
    if body.value is not None:
        await state.emit(BuildReadyEvent(type="build_ready", build=_rebuild(state)))
        _record(state, before, "parameter", parameter_label(name, body.value, param.unit))
    return next((p for p in list_parameters(session_id) if p.name == name), param)


# --- Constraints ------------------------------------------------------------------------------


@app.get(
    SESSION + "/constraints",
    response_model=list[Constraint],
    responses=NOT_FOUND,
    tags=["constraints"],
)
def list_constraints(session_id: str) -> list[Constraint]:
    """The design's spec, as the chips show it. Empty until the first message describes it."""
    return _state(session_id).spec.constraints()


async def _recheck(state: SessionState, constraint: Constraint, removed: bool) -> None:
    mid = state.message_id()
    what = "Back to the set's value" if removed else "Spec changed"
    build = _fixture_build(state.session.id, Build.model_validate(_fixture("build.json")).id)
    s = build.checks
    for event in (
        MessageStartEvent(type="message_start", message_id=mid, role="assistant"),
        ConstraintsEvent(type="constraints", message_id=mid, constraints=state.spec.constraints()),
        ProgressEvent(
            type="progress",
            message_id=mid,
            text=f"{what}: {constraint.label}. Rebuilding and rechecking",
        ),
        BuildReadyEvent(type="build_ready", message_id=mid, build=build),
        TextEvent(
            type="text",
            message_id=mid,
            delta=f"Rechecked against the new spec: {s.passed} pass, {s.failed} fail, "
            f"{s.untested} untested.",
        ),
        MessageEndEvent(type="message_end", message_id=mid),
    ):
        await state.emit(event)


async def _change_constraint(state: SessionState, change: Any, removed: bool) -> Constraint:
    if state.session.status != "idle":
        raise HTTPException(409, "wait until Claude has finished replying")
    try:
        constraint = change(state.spec)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except SpecError as error:
        raise HTTPException(409, str(error)) from error
    state.session.status = "running"
    _spawn(_recheck(state, constraint, removed))
    return constraint


@app.patch(
    SESSION + "/constraints/{constraint_id}",
    response_model=Constraint,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["constraints"],
)
async def update_constraint(
    session_id: str, constraint_id: str, body: ConstraintUpdate
) -> Constraint:
    """Edit a chip: the design is rebuilt and rechecked against the new value, on `/events`."""
    return await _change_constraint(
        _state(session_id), lambda spec: spec.edit(constraint_id, body.value), False
    )


@app.delete(
    SESSION + "/constraints/{constraint_id}",
    response_model=Constraint,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["constraints"],
)
async def remove_constraint(session_id: str, constraint_id: str) -> Constraint:
    """Remove a chip from the user's words or an edit: the team standard applies again, and
    the design is rebuilt and rechecked. A team standard itself can't be removed (409)."""
    return await _change_constraint(
        _state(session_id), lambda spec: spec.remove(constraint_id), True
    )


@app.get(
    SESSION + "/comments", response_model=list[Comment], responses=NOT_FOUND, tags=["comments"]
)
def list_comments(
    session_id: str,
    build_id: Annotated[
        str | None, Query(description="Locate the pins on this build (default: the latest)")
    ] = None,
) -> list[Comment]:
    """Every comment, resolved ones included, with its pin located on the build."""
    state = _state(session_id)
    if build_id is not None:
        _fixture_build(session_id, build_id)
    return [pins.located(c, _mesh()) for c in state.comments]


@app.post(
    SESSION + "/comments",
    response_model=Comment,
    status_code=201,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def create_comment(session_id: str, body: CommentCreate) -> Comment:
    """Pin a comment to a face of a build. It is a note; send it to Claude with a message's
    `comments`."""
    state = _state(session_id)
    _ready_build(session_id, body.build_id)
    if body.face_id not in fixture_face_ids():
        raise HTTPException(404, f"no face {body.face_id}")
    comment = pins.pin(
        body,
        _mesh(),
        comment_id=f"comment-{len(state.comments) + 1}",
        author=USER,
        created_at=now(),
    )
    return await state.put_comment(comment)


@app.patch(
    SESSION + "/comments/{comment_id}",
    response_model=Comment,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def update_comment(session_id: str, comment_id: str, body: CommentUpdate) -> Comment:
    """Resolve a comment (its pin is hidden; the thread stays in the list), or reopen it."""
    state = _state(session_id)
    comment = state.comment(comment_id).model_copy(update={"resolved": body.resolved})
    return await state.put_comment(comment)


@app.post(
    SESSION + "/comments/{comment_id}/replies",
    response_model=Comment,
    status_code=201,
    responses=NOT_FOUND,
    tags=["comments"],
)
async def reply_to_comment(session_id: str, comment_id: str, body: CommentReplyCreate) -> Comment:
    """Reply in a comment's thread, as a note (it reopens a resolved thread). Send the
    comment to have Claude read the thread."""
    return await _state(session_id).comment_reply(comment_id, body.text)


# --- Exports ----------------------------------------------------------------------------------


# The export menu's default request: `DEFAULT_FORMATS` in `frontend/src/components/canvas/
# exportFormats.ts`, with the build note, in contract order.
DEFAULT_EXPORT_FORMATS = ["step", "stl", "build_note"]


def default_export_history() -> list:
    """The drawing's revision table of the first export most sessions make: of the fixture
    build, after the build on the older revision."""
    previous = Build.model_validate(_fixture("previous-build.json"))
    build = Build.model_validate(_fixture("build.json"))
    return session_revisions([previous, build], build)


def _prewarm_exports() -> None:
    report = _fixture("checks.json")
    prewarm_exports(
        {**report, "build_id": _fixture("build.json")["id"]},
        DEFAULT_EXPORT_FORMATS,
        history=default_export_history(),
    )


def _exports_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"claude-hardware-mock-exports-{os.getpid()}"


@app.post(
    SESSION + "/exports",
    response_model=Export,
    status_code=201,
    responses={**NOT_FOUND, 422: {"model": ErrorBody}},
    tags=["exports"],
)
def create_export(session_id: str, body: ExportCreate) -> Export:
    """Writes the requested formats of the chosen `parts` (default: the whole assembly) with the
    geometry exporters (`geometry.export`). The build note is written from the fixture build's
    check report, so it says what the checks panel says."""
    state = _state(session_id)
    build = _fixture_build(session_id, body.build_id)
    eid = f"export-{len(state.exports) + 1}"
    out = _exports_dir() / session_id / eid
    report = {**_fixture("checks.json"), "build_id": body.build_id}
    history = session_revisions(state.builds, build)
    try:
        parts = export_parts(body.parts, fixture_mesh_parts())
        written = write_exports(out, report, list(body.formats), parts, history, body.units)
    except ExportPartsError as error:
        raise HTTPException(422, str(error)) from error
    base = f"{API}/sessions/{session_id}/exports/{eid}/files/"
    files = [
        ExportFile(format=f.format, name=f.name, media_type=f.media_type, url=base + f.name)
        for f in written
    ]
    export = Export(id=eid, build_id=body.build_id, created_at=now(), files=files)
    state.exports[eid] = export
    state.export_dirs[eid] = out
    return export


@app.get(
    SESSION + "/exports/{export_id}/files/{name}",
    response_class=Response,
    responses={200: {"description": "The exported file", "content": {"*/*": {}}}, **NOT_FOUND},
    tags=["exports"],
)
def get_export_file(session_id: str, export_id: str, name: str) -> Response:
    state = _state(session_id)
    export = state.exports.get(export_id)
    file = next((f for f in export.files if f.name == name), None) if export else None
    if file is None:
        raise HTTPException(404, f"no file {name} in export {export_id}")
    content = (state.export_dirs[export_id] / file.name).read_bytes()
    headers = {"Content-Disposition": f'attachment; filename="{file.name}"'}
    return Response(content, media_type=file.media_type, headers=headers)


# --- Telemetry ------------------------------------------------------------------------------


@app.post(
    SESSION + "/telemetry",
    status_code=204,
    response_class=Response,
    responses={**NOT_FOUND, 422: {"model": ErrorBody}},
    tags=["telemetry"],
)
def post_telemetry(session_id: str, body: ClientTelemetry) -> Response:
    """Record a user action only the frontend sees (a view, a model/effort/mode choice, an
    accepted or rejected change) in the session's telemetry log (`docs/architecture.md`). The
    mock API keeps no log."""
    _state(session_id)
    return Response(status_code=204)


# --- Share links (`app.mock_shares`) -----------------------------------------------------------

from app.mock_shares import router as _share_router  # noqa: E402

app.include_router(_share_router)


# --- OpenAPI ----------------------------------------------------------------------------------


def contract_openapi_document() -> dict:
    """The OpenAPI document, with the server-sent event schemas added to the components."""
    return openapi_with_events(app)


def _openapi() -> dict:
    if app.openapi_schema is None:
        app.openapi_schema = contract_openapi_document()
    return app.openapi_schema


app.openapi = _openapi  # type: ignore[method-assign]
