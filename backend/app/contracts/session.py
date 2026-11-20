"""Session API contract: the resources the session API serves and the chat calls,
and the server-sent events for live progress.

The routes are declared once, in `app.mock_api`, against these models; the OpenAPI document
generated from them is checked in as `contracts/session-api.openapi.json`.
`assert_matches_session_api()` checks that another app (the real API) serves the same thing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from app.contracts.changes import ChangeSet, RiskFlags
from app.contracts.checks import CheckSummary
from app.contracts.common import (
    CONTRACTS_DIR,
    BoardRef,
    Contract,
    ContractError,
    Location,
    Point3,
    StableId,
)
from app.contracts.maps import MapType
from app.contracts.models import check_model_id

OPENAPI_PATH = CONTRACTS_DIR / "session-api.openapi.json"

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Mode = Literal["manual", "auto"]

# --- Resources -----------------------------------------------------------------------------


class SessionCreate(Contract):
    """A new session. With neither `example` nor `board` it starts empty: no
    board, no design and no build until Claude writes the first design."""

    title: str | None = None
    example: StableId | None = Field(
        default=None, description="The example to open (`GET /api/examples`), if any"
    )
    board: BoardRef | None = Field(
        default=None,
        description="Board revision to start from: opens the example whose new revision it is",
    )
    new_design: bool = Field(
        default=False,
        description="Start a new design: Claude proposes a build plan before its first edit",
    )
    from_share: str | None = Field(
        default=None,
        description="A share link's token (`/share/<token>`) to start from: the new session is "
        "the browser's own. A read-only link (`view`) copies the snapshot's latest state, chat "
        "included; a design link (`copy`) copies the design alone. Not with `example` or `board`",
    )


ShareKind = Literal["view", "copy"]


class ShareCreate(Contract):
    """Share a session by link, or update the link of that kind to the session as it is now."""

    kind: ShareKind = Field(
        default="view",
        description="`view`: a read-only snapshot of the chat and the model. `copy`: the latest "
        "design alone, which anyone with the link can copy into a session of their own",
    )
    attachments: bool = Field(
        default=False,
        description="`view` only: include the files attached to messages. By default the "
        "snapshot keeps their names only",
    )


class ShareRevoke(Contract):
    """Stop sharing: the link of that kind is turned off for good."""

    kind: ShareKind = "view"


class ShareLink(Contract):
    """A session's share link, as its owner sees it."""

    token: str = Field(min_length=32, description="Random, unguessable; the link's only key")
    kind: ShareKind
    url: str = Field(description="The page to share: `/share/<token>` (a path)")
    active: bool = Field(description="False once sharing is stopped: the link is turned off")
    attachments: bool = Field(description="Whether attached files are in the snapshot")
    created_at: datetime
    updated_at: datetime = Field(description="When the snapshot was last taken")
    up_to_date: bool = Field(
        description="Whether the snapshot has everything in the session: false once there are "
        "new messages or builds, which aren't shared until the link is updated"
    )


class ExampleSummary(Contract):
    """A design a new session can start from (`demos/<board>/examples.yaml`)."""

    id: StableId
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Session(Contract):
    id: StableId
    title: str
    created_at: datetime
    board: BoardRef | None = None
    status: Literal["idle", "running", "waiting_for_answer"] = Field(
        description="`waiting_for_answer` while a question event is unanswered"
    )
    latest_build_id: StableId | None = None
    next_revision: BoardRef | None = Field(
        default=None,
        description="The board revision `POST .../revisions/next` loads; null once it is loaded",
    )
    standards: StableId | None = Field(
        default=None,
        description="The standards set chosen with the session's first message, if any",
    )
    sample: bool = Field(
        default=False,
        description="A demo's hardcoded original (`demos/sessions/`), listed until the "
        "browser opens the demo: open it with `POST /api/demos/{demo}/session`, which gives "
        "the browser its own copy. The original itself is read-only",
    )
    demo: StableId | None = Field(
        default=None,
        description="The demo this session is (`demos/sessions/`): its hardcoded original "
        "(`sample: true`) or the browser's own copy of it, which works like any session. "
        "Null for every other session",
    )
    shared: ShareKind | None = Field(
        default=None,
        description="Served from a share link (`/api/shares/<token>`): `view` is a read-only "
        "snapshot of a session, `copy` a design to make an editable copy of. Null for a "
        "session of one's own",
    )
    title_locked: bool = Field(
        default=False,
        description="The user renamed the session: the name is theirs and Claude no longer "
        "renames it",
    )


class SessionUpdate(Contract):
    """A rename by the user (`PATCH /api/sessions/{id}`). The title is trimmed, made one line
    without control characters, HTML or markdown, and cut to 60 characters; nothing left is
    refused."""

    title: str = Field(min_length=1, max_length=1000)


class StandardSummary(Contract):
    """One standard (rule) of a set, as the standards picker and editor list it."""

    id: StableId = Field(description="Fixed by the rule's kind (`CheckKind.standard_id`)")
    title: str
    description: str
    check: StableId = Field(description="The check that enforces it")
    kind: Literal["min", "max", "tolerance", "spec"]
    value: float | str = Field(description="A number for min, max and tolerance; text for spec")
    unit: Literal["mm", "deg"] | None = None
    applies_to: Literal["any", "board", "object"]
    enabled: bool = Field(description="False: turned off in the set, so not checked")
    aliases: list[str] = Field(description="Words that name the rule in a description")
    dimensions: dict[str, float] | None = Field(
        default=None, description="Named sizes in mm that make a spec measurable"
    )
    material: str | None = Field(default=None, description="Material of the bought-in part")
    label: str = Field(description='The rule in plain language, e.g. "Walls at least 2 mm"')


class StandardsSet(Contract):
    """A set of team standards a session can be designed and checked against: the built-in set
    (`standards.yaml`, read-only) or one the browser made (`app.standards_sets`)."""

    id: StableId
    name: str
    built_in: bool = Field(description="The team's set from standards.yaml: read-only")
    duplicated_from: StableId | None = Field(
        default=None, description="The set this one was duplicated from, if any"
    )
    standards: list[StandardSummary]


class CheckKindDimension(Contract):
    """A named size a rule of a kind must give (e.g. an insert's pocket diameter)."""

    name: StableId
    label: str
    unit: Literal["mm", "deg"]
    min: float
    max: float
    description: str


class CheckKindInfo(Contract):
    """One kind of rule the app can enforce: the check behind it, its parameters, unit and
    valid range (`GET /api/standards/kinds`). Rules can be made of these kinds only."""

    check: StableId = Field(description="The check that enforces a rule of this kind")
    standard_id: StableId = Field(description="The ID every rule of this kind has")
    title: str = Field(description="A new rule's default title")
    kind: Literal["min", "max", "tolerance", "spec"]
    unit: Literal["mm", "deg"] | None = None
    min: float | None = Field(default=None, description="Smallest value the check accepts")
    max: float | None = Field(default=None, description="Largest value the check accepts")
    phrase: str = Field(description="The rule in plain language, `{value}` for its value")
    description: str = Field(description="What the check measures, in plain words")
    applies_to: Literal["any", "board", "object"] = Field(
        description="Which designs a new rule of this kind applies to"
    )
    value_label: str = Field(description="What the value is, for its input")
    dimensions: list[CheckKindDimension] = Field(default_factory=list)
    material: bool = Field(default=False, description="A rule of this kind names a material")
    suggested: StandardSummary | None = Field(
        default=None, description="The built-in set's rule of this kind, to start from"
    )


class StandardInput(Contract):
    """A rule as the editor sends it. Its ID, comparison and unit follow from its `check`
    (the catalogue's kind); a missing title or description takes the kind's."""

    check: str = Field(description="The kind of rule: a check from `GET /api/standards/kinds`")
    title: str | None = None
    description: str | None = None
    value: float | str | None = None
    unit: str | None = Field(default=None, description="Must be the kind's unit when given")
    applies_to: str | None = Field(default=None, description="any, board or object")
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)
    dimensions: dict[str, float | str | None] | None = None
    material: str | None = None


class StandardsSetCreate(Contract):
    """A new set: empty, or a copy of `duplicate_of` (any set the browser can see)."""

    name: str
    duplicate_of: StableId | None = None


class StandardsSetUpdate(Contract):
    """A change to a set the browser made: a new name and/or its whole list of rules."""

    name: str | None = None
    standards: list[StandardInput] | None = None


class StandardsProblem(Contract):
    """One thing wrong with a set, in words the editor can show next to the rule."""

    rule: int | None = Field(default=None, description="Index of the rule in the request")
    field: str | None = Field(default=None, description="The rule's field, or the set's name")
    message: str


class StandardsErrorBody(Contract):
    """A set the API refused, with each problem."""

    detail: str
    problems: list[StandardsProblem] = Field(default_factory=list)


class SessionStandards(Contract):
    """The standards set a session is designed and checked against from now on. Choosing (or
    choosing again after editing) rechecks the current design and posts what changed."""

    standards: StableId


class Selection(Contract):
    """What the user selected on the model when they sent the message."""

    part: StableId | None = Field(
        default=None, description="The part everything selected is on; null across parts"
    )
    parts: list[StableId] = Field(default_factory=list, description="Whole parts selected")
    faces: list[StableId] = Field(default_factory=list)
    edges: list[StableId] = Field(default_factory=list)
    components: list[StableId] = Field(
        default_factory=list,
        description=(
            "Board components selected (reference designators). Their opening faces are not"
            " added to `faces`"
        ),
    )
    point: Point3 | None = None


class Answer(Contract):
    """An answer to a question event: one of its options."""

    question_id: StableId
    option_id: StableId


class Attachment(Contract):
    name: str
    media_type: str
    url: str


class MessageCreate(Contract):
    text: str = Field(default="", description="May be empty when `answer` is given")
    model: str | None = Field(
        default=None, description="Model ID from `contracts/models.json`; the server picks if null"
    )
    effort: Effort | None = None
    mode: Mode = Field(default="auto", description="Auto applies changes; Manual proposes them")
    selection: Selection | None = None
    answer: Answer | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    standards: StableId | None = Field(
        default=None,
        description="A standards set ID (`GET /api/standards`) for the agent and checks to use. "
        "Given with the session's first message; a later message may repeat it but not change it",
    )
    comments: list[StableId] = Field(
        default_factory=list,
        description='Comments sent to Claude ("Send to Claude"): one run handles them all and '
        "answers each in its own thread. `text` may be empty",
    )

    @field_validator("model")
    @classmethod
    def _known_model(cls, model: str | None) -> str | None:
        return check_model_id(model)


class QueuedMessage(Contract):
    """A message sent while Claude was working: it waits in the session's queue and is sent,
    as a normal message, once the runs before it have ended (`GET .../queue`)."""

    id: StableId
    created_at: datetime
    message: MessageCreate = Field(
        description="What is sent when its turn comes (text, attachments, selection, standards, "
        "model, effort and mode); never an answer"
    )


class MessageQueue(Contract):
    """The messages waiting for the current run to end, in the order they will be sent."""

    items: list[QueuedMessage]
    paused: bool = Field(
        description="Claude's question is open: the next message waits until it is answered "
        "(it is never taken as the answer), or until it is sent anyway "
        "(`POST .../queue/{item}/send`)"
    )


class QueuedMessageUpdate(Contract):
    """Edit a queued message before it is sent, or move it in the queue."""

    text: str | None = Field(default=None, description="The new text; it can't be left empty")
    position: int | None = Field(
        default=None, ge=0, description="Its new place in the queue (0: next); past the end: last"
    )


class RunRetry(Contract):
    """Retry the reply that stopped (`POST .../retry`): a new run picks up from the design and
    builds so far, with the stopped message's request. The choices are those a message carries."""

    model: str | None = Field(
        default=None, description="Model ID from `contracts/models.json`; the server picks if null"
    )
    effort: Effort | None = None
    mode: Mode = Field(default="auto", description="Auto applies changes; Manual proposes them")

    @field_validator("model")
    @classmethod
    def _known_model(cls, model: str | None) -> str | None:
        return check_model_id(model)


class RevisionLoad(Contract):
    """Loading the next board revision: Claude starts the work on it with a fixed prompt, so
    nobody types. The choices are those a message would carry."""

    model: str | None = Field(
        default=None, description="Model ID from `contracts/models.json`; the server picks if null"
    )
    effort: Effort | None = None
    mode: Mode = Field(default="auto", description="Auto applies changes; Manual proposes them")

    @field_validator("model")
    @classmethod
    def _known_model(cls, model: str | None) -> str | None:
        return check_model_id(model)


class Message(Contract):
    id: StableId
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime
    model: str | None = None
    effort: Effort | None = None
    mode: Mode | None = None
    selection: Selection | None = None
    answer: Answer | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    standards: StableId | None = None
    comments: list[StableId] = Field(default_factory=list)


class BuildOrigin(Contract):
    """Where a build that no message made came from, so the chat can say so where it happened."""

    kind: Literal["import", "existing"] = Field(
        description="`import`: a file the user imported; `existing`: the existing design an "
        "example opens on, read from its file"
    )
    file_name: str = Field(min_length=1, max_length=255, description="The file's name")


class Build(Contract):
    id: StableId
    created_at: datetime
    board: BoardRef | None = Field(
        description="The board revision the build was checked against; null for a design "
        "without a board"
    )
    status: Literal["ready", "failed"]
    checks: CheckSummary
    maps: list[MapType] = Field(description="Analysis maps available for the build")
    mesh_url: str = Field(description="glTF following the mesh contract")
    checks_url: str = Field(description="JSON following the check-result contract")
    source_url: str = Field(description="The CAD code that produced the build (`BuildSource`)")
    board_url: str = Field(
        description="The board revision named by `board`, in the board JSON format "
        "(`backend/app/boards/SCHEMA.md`); 404 when `board` is null"
    )
    step_url: str | None = Field(description="STEP of the build, null until exported")
    restored_from: StableId | None = Field(
        default=None,
        description="The earlier build whose design this build restores (undo in the history)",
        # The same schema in the API and in `build_ready` events (FastAPI leaves the default out).
        json_schema_extra=lambda schema: schema.pop("default", None),
    )
    failure: str | None = Field(
        default=None,
        description="Why a failed build couldn't be built, in plain words for the user (never "
        "internal error text); null for a ready build",
        json_schema_extra=lambda schema: schema.pop("default", None),
    )
    origin: BuildOrigin | None = Field(
        default=None,
        description="The file the build came from, for a build no message made (an import, or "
        "the existing design an example opens on); null otherwise",
        json_schema_extra=lambda schema: schema.pop("default", None),
    )


class FeatureSummary(Contract):
    """Where one feature is: the geometry it added or cut (`geometry.diff.feature_summary`)."""

    tag: StableId
    part: StableId
    centroid: Point3 | None = Field(description="Centroid of the feature's volume, mm")
    size: Point3 | None = Field(description="Axis-aligned size of the feature, mm")
    volume: float = Field(description="mm³")
    rotation: float | None = Field(
        description="Direction of the long axis in the board plane, degrees in [0, 180); null "
        "for a shape without one"
    )


class ParameterChange(Contract):
    """One design parameter added, removed or changed, by path (`openings[<id>].x`)."""

    name: str
    change: Literal["added", "removed", "changed"]
    old: Any = Field(description="The value before, null when added")
    new: Any = Field(description="The value after, null when removed")


class FeatureChange(Contract):
    """One feature added, removed, moved, rotated or resized (`geometry.diff.diff_features`)."""

    tag: StableId
    part: StableId
    change: str = Field(
        description="`added`, `removed`, or what happened joined with ` and `: `moved`, "
        "`rotated`, `resized`, `changed part` (e.g. `moved and rotated`)"
    )
    distance: float | None = Field(description="How far the centroid moved, mm")
    offset: Point3 | None = Field(description="The move as [dx, dy, dz], mm")
    rotation: float | None = Field(description="How far the long axis turned, degrees")
    old: FeatureSummary | None
    new: FeatureSummary | None


class BuildDiff(Contract):
    """What changed from one build (`old`) to another (`new`): any two builds of a session."""

    old: StableId
    new: StableId
    parameters: list[ParameterChange]
    features: list[FeatureChange]


LineNumber = Annotated[int, Field(ge=1)]


class FeatureSource(Contract):
    """Where in a build's code one feature was created."""

    tag: StableId = Field(description="The feature tag, as on the mesh faces (`feature`)")
    part: StableId = Field(description="The part the feature belongs to")
    lines: tuple[LineNumber, LineNumber] = Field(
        description="First and last line (1-based, inclusive) of the statement in `code` that "
        "created the feature: the generator's `feature(...)` call, or the statement that "
        "appends it to `features`. Features made by one statement in a loop share its lines."
    )
    locked: bool = Field(
        default=False,
        description="A feature made by hand (a sketch on a face): Claude keeps it as it is",
    )

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.lines[0] > self.lines[1]:
            raise ValueError(f"feature {self.tag}: lines {list(self.lines)} end before they start")
        return self


class BuildSource(Contract):
    """The generator code that produced a build, as run by the geometry backend."""

    build_id: StableId
    backend: str = Field(description="Geometry backend that ran the code, e.g. `build123d`")
    language: Literal["python"] = "python"
    filename: str = Field(description="A file name to show or save the code under")
    code: str = Field(description="Defines `build(params) -> dict` (geometry/interface.py)")
    features: list[FeatureSource] = Field(
        description="Every feature of the build, in the order the features were applied"
    )

    @model_validator(mode="after")
    def _lines_in_code(self) -> Self:
        count = len(self.code.splitlines())
        for f in self.features:
            if f.lines[1] > count:
                raise ValueError(f"feature {f.tag}: line {f.lines[1]} is past the end of `code`")
        return self


class SourceUpdate(Contract):
    """A hand edit to the generator code: rebuild and recheck with `code`."""

    build_id: StableId = Field(description="The build whose code was edited")
    code: str = Field(min_length=1, description="Must define `build(params)`")


class CodeError(Contract):
    """Why edited code was refused: it doesn't parse, or it doesn't build."""

    detail: str
    line: LineNumber | None = Field(default=None, description="1-based line in `code`, if known")
    column: LineNumber | None = Field(default=None, description="1-based column, if known")


class DimensionEnd(Contract):
    """One end of a dimension: a point and the face it lies on."""

    point: Point3 = Field(description="Board coordinates (mm), on `face`")
    part: StableId | None = Field(default=None, description="Part of `face`")
    face: StableId | None = Field(
        default=None,
        description="Stable face ID, as on the mesh faces; null for a point off the model "
        "(the centre of a radius in open space)",
    )

    @model_validator(mode="after")
    def _face_has_part(self) -> Self:
        if (self.face is None) != (self.part is None):
            raise ValueError("a dimension end names both its face and its part, or neither")
        return self


class Dimension(Contract):
    """Where a parameter's value is measured on the model of the build it came with."""

    kind: Literal["distance", "radius"] = Field(
        description="`distance`: the value is the length from `start` to `end`. `radius`: "
        "`start` is the centre and `end` a point on the curved face at the radius"
    )
    start: DimensionEnd
    end: DimensionEnd


class Drag(Contract):
    """How a parameter is changed by dragging faces of the model.

    Moving the faces `d` mm along `axis` changes the value by `d` mm (negative `d` makes it
    smaller). Faces not named by any parameter's `drag` don't drag.
    """

    faces: list[StableId] = Field(min_length=1, description="Stable face IDs that move together")
    axis: Point3 = Field(description="Unit vector in board coordinates the faces move along")

    @model_validator(mode="after")
    def _unit_axis(self) -> Self:
        length = sum(c * c for c in self.axis) ** 0.5
        if abs(length - 1) > 1e-6:
            raise ValueError(f"drag axis {list(self.axis)} is not a unit vector")
        return self


class Parameter(Contract):
    name: StableId
    value: float | str
    unit: Literal["mm", "deg"] | None = None
    locked: bool = Field(description="Set by a hand edit; Claude designs around it")
    min: float | None = None
    max: float | None = None
    description: str = ""
    feature: StableId | None = Field(default=None, description="Feature the parameter drives")
    dimension: Dimension | None = Field(
        default=None,
        description="Where the value is measured on the model; null when it has no single place "
        "(an angle applied everywhere, a gap, a material)",
    )
    drag: Drag | None = Field(
        default=None, description="Faces that change the value when dragged; null if none do"
    )

    @model_validator(mode="after")
    def _shapes_are_lengths(self) -> Self:
        if (self.dimension or self.drag) and self.unit != "mm":
            raise ValueError(f"parameter {self.name}: only lengths have a dimension or drag")
        return self


class ParameterUpdate(Contract):
    value: float | str | None = None
    locked: bool | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _not_a_bool(cls, value: object) -> object:
        if isinstance(value, bool):  # JSON true would pass as 1.0
            raise ValueError("a parameter value is a number or text, not true or false")
        return value


SketchShape = Literal["circle", "slot", "rectangle", "vent"]
SketchOperation = Literal["cut", "extrude"]
PositiveMm = Annotated[float, Field(gt=0)]


class FacePlane(Contract):
    """A face of a build and, when it is flat, its plane: where a sketch on it goes."""

    build_id: StableId
    face_id: StableId
    part: StableId
    feature: StableId = Field(description="The feature that created the face")
    planar: bool = Field(description="Only a flat face can be sketched on")
    origin: Point3 | None = Field(
        default=None, description="The face's centre in board coordinates (mm): sketch x = y = 0"
    )
    normal: Point3 | None = Field(default=None, description="Unit normal, out of the part")
    x_dir: Point3 | None = Field(
        default=None,
        description="The sketch's x axis, in the face; its y axis is `normal × x_dir`. Level "
        "on a wall (y points up), board x on a top or bottom face",
    )
    size: tuple[float, float] | None = Field(
        default=None, description="The face's extent along the sketch's x and y axes (mm)"
    )


class SketchCreate(Contract):
    """A shape on a flat face, with typed size and position, cut into or extruded from it."""

    build_id: StableId = Field(description="The build whose face is sketched on")
    face_id: StableId
    shape: SketchShape
    operation: SketchOperation
    width: PositiveMm = Field(
        description="Circle: diameter. Slot: overall length. Rectangle, vent: width (sketch x)"
    )
    height: PositiveMm | None = Field(
        default=None, description="Slot, rectangle, vent: height (sketch y). Not for a circle"
    )
    count: int | None = Field(
        default=None, ge=2, le=50, description="Vent: the number of slots across the width"
    )
    x: float = Field(default=0, description="Centre on the face, mm along the sketch x axis")
    y: float = Field(default=0, description="Centre on the face, mm along the sketch y axis")
    depth: PositiveMm = Field(description="How far the cut goes in, or the extrusion stands out")

    @model_validator(mode="after")
    def _shape_sizes(self) -> Self:
        if self.shape == "circle":
            if self.height is not None or self.count is not None:
                raise ValueError("a circle has a diameter (width) only")
        elif self.height is None:
            raise ValueError(f"a {self.shape} needs a height")
        if self.shape == "slot" and self.height is not None and self.height >= self.width:
            raise ValueError("a slot's height must be less than its length (width)")
        if self.shape == "vent" and self.count is None:
            raise ValueError("a vent needs a count of slots")
        if self.shape != "vent" and self.count is not None:
            raise ValueError(f"a {self.shape} has no count")
        return self


class Sketch(SketchCreate):
    """A sketched feature: named, locked, and kept in the design's code and parameters."""

    tag: StableId = Field(description="The feature tag, e.g. `sketch_1`")
    part: StableId
    plane: FacePlane
    locked: bool = Field(default=True, description="Claude keeps it as it is")


EditKind = Literal[
    "move", "push_pull", "hole", "boss", "rib", "fillet", "chamfer", "shell", "mirror", "pattern"
]
EditAxis = Literal["x", "y", "z"]
PatternKind = Literal["linear", "circular"]

# The values each kind of edit reads (`geometry.modeling`); the others must be left out.
EDIT_VALUES: dict[str, tuple[str, ...]] = {
    "move": ("dx", "dy", "dz", "rotation"),
    "push_pull": ("distance",),
    "hole": ("x", "y", "diameter", "depth", "insert"),
    "boss": ("x", "y", "diameter", "height", "insert"),
    "rib": ("x", "y", "length", "thickness", "height", "rotation"),
    "fillet": ("radius",),
    "chamfer": ("distance",),
    "shell": ("thickness",),
    "mirror": ("axis",),
    "pattern": ("pattern", "count", "axis", "spacing", "angle"),
}
# The values an edit can't do without (a hole or boss with an insert takes its sizes from the
# standard; a pattern's own values depend on its kind).
EDIT_REQUIRED: dict[str, tuple[str, ...]] = {
    "move": (),
    "push_pull": ("distance",),
    "hole": ("diameter", "depth"),
    "boss": ("diameter", "height"),
    "rib": ("length", "thickness", "height"),
    "fillet": ("radius",),
    "chamfer": ("distance",),
    "shell": ("thickness",),
    "mirror": ("axis",),
    "pattern": ("pattern", "count"),
}
# What each kind of edit is made on: a flat face, an edge, a part or another feature.
EDIT_TARGET: dict[str, Literal["face_id", "edge_id", "part", "source"]] = {
    "move": "part",
    "push_pull": "face_id",
    "hole": "face_id",
    "boss": "face_id",
    "rib": "face_id",
    "fillet": "edge_id",
    "chamfer": "edge_id",
    "shell": "part",
    "mirror": "source",
    "pattern": "source",
}
EDIT_VALUE_FIELDS = (
    "x",
    "y",
    "diameter",
    "depth",
    "height",
    "length",
    "thickness",
    "radius",
    "distance",
    "insert",
    "dx",
    "dy",
    "dz",
    "rotation",
    "axis",
    "pattern",
    "count",
    "spacing",
    "angle",
)


class EditValues(Contract):
    """The typed values of a modeling edit; each kind reads its own (`EDIT_VALUES`)."""

    x: float | None = Field(default=None, description="Hole, boss, rib: mm along the face's x")
    y: float | None = Field(default=None, description="Hole, boss, rib: mm along the face's y")
    diameter: PositiveMm | None = Field(default=None, description="Hole, boss")
    depth: PositiveMm | None = Field(default=None, description="Hole: how deep it goes")
    height: PositiveMm | None = Field(default=None, description="Boss, rib: how far it stands")
    length: PositiveMm | None = Field(default=None, description="Rib")
    thickness: PositiveMm | None = Field(default=None, description="Rib; shell: the walls")
    radius: PositiveMm | None = Field(default=None, description="Fillet")
    distance: float | None = Field(
        default=None,
        description="Push/pull: mm the face moves out (negative: in). Chamfer: its size",
    )
    insert: bool | None = Field(
        default=None,
        description="Hole, boss: sized for the standard heat-set insert (`standards.yaml`)",
    )
    dx: float | None = Field(default=None, description="Move: mm along board x")
    dy: float | None = Field(default=None, description="Move: mm along board y")
    dz: float | None = Field(default=None, description="Move: mm along board z")
    rotation: float | None = Field(
        default=None,
        description="Move: degrees about the vertical axis through the part's centre. Rib: "
        "degrees from the face's x axis",
    )
    axis: EditAxis | None = Field(
        default=None,
        description="Mirror: the plane through the part's centre normal to this axis. "
        "Linear pattern: the direction",
    )
    pattern: PatternKind | None = None
    count: int | None = Field(
        default=None, ge=2, le=50, description="Pattern: copies in all, the original included"
    )
    spacing: float | None = Field(default=None, description="Linear pattern: mm between copies")
    angle: float | None = Field(
        default=None, description="Circular pattern: degrees it spans (360: all round)"
    )


class EditCreate(EditValues):
    """A modeling edit on a build: what it is made on (`EDIT_TARGET`) and its typed values
    (`EDIT_VALUES`)."""

    build_id: StableId = Field(description="The build the edit is made on")
    kind: EditKind
    part: StableId | None = Field(default=None, description="Move, shell: the part")
    face_id: StableId | None = Field(
        default=None,
        description="Push/pull, hole, boss, rib: the flat face. Shell: the face left open",
    )
    edge_id: StableId | None = Field(default=None, description="Fillet, chamfer: the edge")
    source: StableId | None = Field(
        default=None, description="Mirror, pattern: the tag of the feature to copy"
    )

    @model_validator(mode="after")
    def _kind_values(self) -> Self:
        kind = self.kind
        target = EDIT_TARGET[kind]
        if getattr(self, target) is None:
            raise ValueError(f"a {kind} needs {target}")
        extra = [
            f
            for f in EDIT_VALUE_FIELDS
            if f not in EDIT_VALUES[kind] and getattr(self, f) is not None
        ]
        if extra:
            raise ValueError(f"a {kind} has no {', '.join(extra)}")
        insert = bool(self.insert)
        missing = [
            f
            for f in EDIT_REQUIRED[kind]
            if getattr(self, f) is None and not (insert and f in ("diameter", "depth", "height"))
        ]
        if kind == "pattern" and self.pattern == "linear":
            missing += [f for f in ("axis", "spacing") if getattr(self, f) is None]
        if kind == "pattern" and self.pattern == "circular" and self.angle is None:
            missing.append("angle")
        if missing:
            raise ValueError(f"a {kind} needs {', '.join(missing)}")
        if kind == "push_pull" and self.distance == 0:
            raise ValueError("a push/pull needs a distance other than 0")
        if kind == "chamfer" and self.distance is not None and self.distance <= 0:
            raise ValueError("a chamfer's distance must be more than 0")
        return self


class Edit(EditCreate):
    """A modeling edit in the design: named, locked, and kept in its code and parameters."""

    tag: StableId = Field(description="The feature tag, e.g. `hole_hand_1`")
    part: StableId
    plane: FacePlane | None = Field(default=None, description="The face's plane, if on a face")
    point: Point3 | None = Field(
        default=None, description="Fillet, chamfer: a point on the edge, board coordinates"
    )
    pocket_diameter: PositiveMm | None = Field(
        default=None, description="Boss with an insert: the insert pocket (`standards.yaml`)"
    )
    pocket_depth: PositiveMm | None = None
    insert_tag: StableId | None = Field(
        default=None, description="Boss with an insert: the pocket's feature tag"
    )
    suppressed: bool = Field(default=False, description="Left out of the build, kept in the design")
    locked: bool = Field(default=True, description="Claude keeps it as it is")


class EditUpdate(EditValues):
    """Change an edit's typed values, or suppress it (`suppressed`) or bring it back. What it is
    made on can't change: delete it and make a new one."""

    suppressed: bool | None = None


class EditRemoval(Contract):
    """An edit deleted, with the edits that copied it (mirrors and patterns of it)."""

    tag: StableId
    removed: list[StableId] = Field(description="Every edit deleted: `tag` and its dependents")


class CommentCreate(Contract):
    """A comment pinned to a face of a build. It is a note until it is sent to Claude
    (`MessageCreate.comments`)."""

    build_id: StableId
    face_id: StableId
    point: Point3 = Field(description="Where on the face it is pinned (mm, board coordinates)")
    text: str = Field(min_length=1)


class CommentReplyCreate(Contract):
    """A reply in a comment's thread: a note, like the comment, until the thread is sent."""

    text: str = Field(min_length=1)


class CommentUpdate(Contract):
    resolved: bool = Field(description="Resolve closes the thread and hides the pin")


class CommentReply(Contract):
    id: StableId
    role: Literal["user", "assistant"]
    author: str
    text: str
    created_at: datetime
    message_id: StableId | None = Field(
        default=None, description="The run that wrote it, for Claude's replies"
    )


class CommentRequest(Contract):
    """The comment was sent to Claude: the run that handles it."""

    message_id: StableId = Field(description="The run's assistant message")
    status: Literal["running", "answered", "failed"]


class Comment(Contract):
    id: StableId
    build_id: StableId = Field(description="The build it was pinned on")
    face_id: StableId = Field(description="Stable face ID: the comment survives rebuilds")
    point: Point3 = Field(description="Where it was pinned, on the build it was pinned on")
    text: str
    author: str
    created_at: datetime
    part: StableId | None = Field(default=None, description="The part of the face")
    feature: StableId | None = Field(
        default=None,
        description="The feature that made the face: if a rebuild drops the face ID, the pin "
        "moves to the nearest face of the same feature",
    )
    anchor: Point3 | None = Field(
        default=None,
        description="Centre of the face on the build it was pinned on. On a later build the "
        "pin keeps its offset from the face's centre, so it follows the face when it moves",
    )
    location: Location | None = Field(
        default=None,
        description="Where the pin is on the build the list was asked for (the latest by "
        "default); null when the build no longer has the face or its feature",
    )
    replies: list[CommentReply] = Field(default_factory=list)
    request: CommentRequest | None = Field(
        default=None, description="Set once the comment is sent to Claude; null for a note"
    )
    resolved: bool = False


# The formats of `geometry.export.FORMATS` (test_export checks they match). `source` is two
# files: the generator code and the parameters it ran with.
ExportFormat = Literal["step", "stl", "3mf", "glb", "obj", "dxf", "pdf", "source", "build_note"]


LengthUnit = Literal["mm", "in"]


class ExportCreate(Contract):
    build_id: StableId
    formats: list[ExportFormat] = Field(min_length=1)
    parts: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Parts to export: any of the build's mesh parts and `board` (only the STEP "
        "carries the board). Default: the whole assembly. An unknown part is a 422.",
    )
    units: LengthUnit = Field(
        default="mm",
        description="Length unit of the build note (the display unit). Every other format "
        "stays in its native unit, millimetres",
    )


class ExportFile(Contract):
    format: ExportFormat
    name: str
    media_type: str
    url: str


class Export(Contract):
    id: StableId
    build_id: StableId
    created_at: datetime
    files: list[ExportFile]


class FindRequest(Contract):
    """A search for parts of the model in words, e.g. "all M3 holes" or "faces under 1° of
    draft"."""

    query: str = Field(min_length=1, max_length=500)


class FindMatch(Contract):
    face: StableId = Field(description="A face ID of the build's mesh")
    part: StableId
    feature: StableId = Field(description="The feature that created the face")
    value: float | None = Field(
        default=None, description="The measured value the query compared, if it compared one"
    )
    unit: Literal["mm", "deg"] | None = None


class FindResult(Contract):
    query: str
    status: Literal["found", "none", "unclear"] = Field(
        description="`found` with matches, `none` when the query was understood and nothing "
        "matches, `unclear` when part of it wasn't understood (see `question`)"
    )
    understood: str = Field(description="What the query was read as, in words")
    question: str | None = Field(
        default=None, description="Set when `unclear`: what to ask the user, with suggestions"
    )
    matches: list[FindMatch] = Field(default_factory=list)


class ErrorBody(Contract):
    detail: str


HistoryActor = Literal["user", "claude"]
HistoryKind = Literal[
    "parameter", "edit", "sketch", "code", "run", "review", "risks", "restore", "import"
]


class HistoryStep(Contract):
    """One change to the design in the session's timeline (`DesignHistory`), whoever made it: a
    hand edit, a Claude run's applied changes (one step per run), a change-set or risk review, a
    restore or an import."""

    id: StableId
    actor: HistoryActor = Field(description="Who made the change: the user or Claude")
    kind: HistoryKind
    label: str = Field(description='What changed, e.g. "push/pull 1 mm"')
    text: str = Field(description='Who and what, as shown: "You: push/pull 1 mm"')
    created_at: datetime
    build_id: StableId | None = Field(description="The build the change made")
    message_id: StableId | None = Field(description="The run's message, for a Claude step")


class DesignHistory(Contract):
    """The session's timeline of design states: Undo moves back one step, Redo forward. A new
    change after an undo drops the steps that were ahead (the redo branch). Undo never deletes
    builds."""

    steps: list[HistoryStep] = Field(description="Every step, oldest first, the redo branch too")
    position: int = Field(
        ge=0, description="How many of `steps` are applied: 0 is the start, before any step"
    )
    undo: HistoryStep | None = Field(description="The step Undo takes back; null when none")
    redo: HistoryStep | None = Field(description="The step Redo makes again; null when none")


class HistoryMove(Contract):
    """An undo or redo: the step it moved over, the rebuild of the design it moved to (also a
    `build_ready` with no message), and the timeline after it."""

    direction: Literal["undo", "redo"]
    step: HistoryStep
    build: Build
    history: DesignHistory
    change_set: ChangeSet | None = Field(
        default=None,
        description="The change set of the state it moved to, as `GET .../changes` serves it "
        "then; null where that is a 404 (no run had changed the design yet)",
    )


ConstraintSource = Literal["standard", "user", "edit"]


class Constraint(Contract):
    """One constraint of the design's spec, shown as a chip above the chat input.

    Every constraint is a team standard (`standards.yaml`) with the value the design must
    meet, so its check enforces it. The value is the standard's own (`source: standard`), one
    the user gave in a message (`user`, with their words in `quote`) or a chip edit (`edit`).
    """

    id: StableId = Field(description="The standard's ID")
    title: str = Field(description="The standard's title")
    label: str = Field(description='The chip text, e.g. "Minimum wall thickness ≥ 2.5 mm"')
    kind: Literal["min", "max", "tolerance", "spec"]
    value: float | str
    unit: Literal["mm", "deg"] | None = Field(description="Null for a spec")
    check: StableId = Field(description="The check that enforces it")
    source: ConstraintSource = Field(
        description="`standard`: from standards.yaml; `user`: from the user's words; "
        "`edit`: the chip was edited"
    )
    quote: str | None = Field(description="The user's words it came from")
    message_id: StableId | None = Field(description="The message the user's words are in")
    standard_value: float | str = Field(description="The value in standards.yaml")


class ConstraintUpdate(Contract):
    value: float | str = Field(description="A number for min, max and tolerance; text for spec")


# --- Server-sent events ----------------------------------------------------------------------
#
# `GET /api/sessions/{id}/events` streams `text/event-stream`. Each event has `id:` (an integer
# that increases within the session), `event:` (the `type` below) and `data:` (the JSON object,
# which repeats `type`). Reconnect with `Last-Event-ID` (or `?after=`) to resume.


class MessageStartEvent(Contract):
    type: Literal["message_start"]
    message_id: StableId
    role: Literal["assistant"]


class TextEvent(Contract):
    type: Literal["text"]
    message_id: StableId
    delta: str = Field(description="Text tokens to append to the message")


class ProgressEvent(Contract):
    type: Literal["progress"]
    message_id: StableId
    text: str = Field(description='A progress line, e.g. "Fixing the cutout · round 1 of 4"')
    round: int | None = Field(default=None, ge=1)
    rounds: int | None = Field(default=None, ge=1)


class QuestionOption(Contract):
    id: StableId
    label: str
    description: str | None = None


class ConflictConstraint(Contract):
    """One side of a conflict: a locked parameter, a team standard or a board part."""

    kind: Literal["locked", "standard", "board"]
    id: StableId = Field(description="Parameter path, standard ID or component reference")
    label: str = Field(min_length=1, description="What to call it, e.g. the standard's title")
    relation: Literal["=", ">=", "<="] = Field(
        description="How `value` binds: a lock or a part's height is `=`, a `min` standard "
        "`>=`, a `max` standard `<=`"
    )
    value: float = Field(description="The locked value, the standard's value, or the height")
    unit: Literal["mm", "deg"] = "mm"
    best: float | None = Field(
        description="Best achievable value when only this constraint gives way; null when no "
        "value of it resolves the conflict (a board part can't change)"
    )
    locations: list[Location] = Field(min_length=1, description="Where on the model it acts")


class ConstraintConflict(Contract):
    """Locked values, standards and the board that can't all be met (conflict detection)."""

    id: StableId
    message: str
    over_by: float = Field(ge=0, description="How far apart the constraints are, in mm")
    constraints: list[ConflictConstraint] = Field(min_length=2)
    location: Location = Field(description="Where the conflict is worst")


class QuestionEvent(Contract):
    type: Literal["question"]
    message_id: StableId
    question_id: StableId
    text: str
    options: list[QuestionOption] = Field(min_length=2)
    location: Location | None = Field(
        default=None, description="The spot on the model the question is about, to pin it at"
    )
    conflict: ConstraintConflict | None = Field(
        default=None, description="Set when Claude asks because the constraints can't all be met"
    )


class BuildReadyEvent(Contract):
    type: Literal["build_ready"]
    message_id: StableId | None = None
    build: Build


class ConstraintsEvent(Contract):
    """The design's spec changed: every constraint, as the chips show them."""

    type: Literal["constraints"]
    message_id: StableId | None = None
    constraints: list[Constraint]


class PlanEvent(Contract):
    """Claude's build plan for a new design, shown before any edit."""

    type: Literal["plan"]
    message_id: StableId
    steps: list[str] = Field(min_length=1)


class MessageEndEvent(Contract):
    type: Literal["message_end"]
    message_id: StableId


class ChangesReadyEvent(Contract):
    type: Literal["changes_ready"]
    message_id: StableId | None = Field(
        default=None, description="The run's message; null for a review outside a run"
    )
    change_set: ChangeSet = Field(description="Same shape as `GET .../changes`")


class RisksReadyEvent(Contract):
    type: Literal["risks_ready"]
    message_id: StableId | None = Field(
        default=None, description="The run's message; null for a review outside a run"
    )
    risk_flags: RiskFlags = Field(description="Same shape as `GET .../risks`")


class FoundEvent(Contract):
    """Claude looked for parts of the model from chat (`find_in_model`): the canvas
    highlights the matched faces, and the chat lists them, ready to send back as the
    selection. An unclear query has no matches: the chat shows what was understood and the
    question."""

    type: Literal["found"]
    message_id: StableId
    build_id: StableId = Field(description="The build whose mesh faces the matches name")
    result: FindResult = Field(description="Same shape as `POST .../builds/{id}/find`")


# Why a run stopped or waits (`app.agent.limits`): a machine-readable code next to the plain
# text, so the chat can pick an icon or a control without parsing the words.
LimitReason = Literal[
    "rate_limited",  # the API's rate limit
    "overloaded",  # the API is overloaded or had a server error
    "network",  # the connection to the API timed out or dropped
    "billing",  # the API account is out of credits
    "not_configured",  # no or an invalid API key: the server isn't set up
    "too_large",  # the request or conversation is too long for the model
    "model_unavailable",  # the chosen model isn't available to the server's key
    "run_queue",  # waiting for other people's runs to finish
    "build_queue",  # waiting for another build to finish
    "build_memory",  # a build ran out of memory
    "build_time",  # a build ran over its time limit
    "build_crash",  # a build process crashed
    "preempted",  # the run gave its place up while waiting for an answer
    "stopped",  # the user stopped the run
    "interrupted",  # the server restarted mid-run
    "save_failed",  # the session couldn't be saved (disk full); the run goes on in memory
    "unknown",
]


class ErrorEvent(Contract):
    """The run stopped. `message` says plainly what happened, whether anything was lost and
    what to do; `retryable` offers Retry (`POST .../retry`), which picks up from the design
    and builds so far."""

    type: Literal["error"]
    message_id: StableId | None = None
    message: str
    retryable: bool = False
    reason: LimitReason | None = Field(default=None, description="Why, as a code; null: unknown")


class NoticeEvent(Contract):
    """A notice the chat shows apart from Claude's text (`text` is plain and complete). One
    event for every kind:

    - `retrying`: a temporary API error, retried automatically in `retry_in_s` seconds
      (`attempt` of `attempts`);
    - `waiting`: in line behind other runs (`place`, 1: next) or another build;
    - `warning`: something failed but nothing stopped (a build hit a limit, the session
      couldn't be saved);
    - `refused`: Claude won't design this (a weapon, a harmful part, or a part made to
      defeat a safety protection or evade a legal requirement), and `text` says why;
    - `safety`: Claude helps with a regulated or safety-critical product, and `text` says
      requirements vary by region and none were checked; `regulations` are the kinds of rules
      that may apply, never a claim that the design meets them.

    A later event of the same message replaces a `retrying` or `waiting` notice."""

    type: Literal["notice"]
    message_id: StableId | None = None
    kind: Literal["retrying", "waiting", "warning", "refused", "safety"]
    text: str = Field(min_length=1)
    reason: LimitReason | None = None
    retry_in_s: float | None = Field(default=None, ge=0, description="retrying: seconds to wait")
    attempt: int | None = Field(default=None, ge=1, description="retrying: this retry's number")
    attempts: int | None = Field(default=None, ge=1, description="retrying: retries allowed")
    place: int | None = Field(default=None, ge=1, description="waiting: place in line, 1: next")
    categories: list[str] = Field(
        default_factory=list,
        description='safety: what kind of product it is, e.g. "a product for children"',
    )
    regulations: list[str] = Field(
        default_factory=list,
        description="safety: kinds of regulations that may apply; none were checked",
    )
    region: str | None = Field(
        default=None, description="safety: the region the user named; null when unknown"
    )


class TitleEvent(Contract):
    """The session was renamed: by Claude, naming the part it designs (`message_id` of its run),
    or by the user (`message_id` null). The header and Recents show the new name."""

    type: Literal["title"]
    message_id: StableId | None = None
    title: str = Field(min_length=1)
    title_locked: bool = False


class CommentEvent(Contract):
    """A comment was pinned, replied to, sent, answered or resolved: the whole comment as it
    is now, located on the latest build."""

    type: Literal["comment"]
    message_id: StableId | None = Field(
        default=None, description="The run, for Claude's replies while it runs"
    )
    comment: Comment


class UserMessageEvent(Contract):
    """The user sent a message (typed, an answer, a queued message sent, a revision load, a
    Retry): the message as stored, so every tab shows it, not only the one that sent it."""

    type: Literal["user_message"]
    message_id: StableId = Field(description="The stored message's ID (`GET .../messages`)")
    message: Message


class QueueEvent(Contract):
    """The session's message queue changed (a message queued, edited, moved, removed or sent,
    or a question pausing it): the whole queue as it is now, so every tab shows the same."""

    type: Literal["queue"]
    queue: MessageQueue
    sent: StableId | None = Field(
        default=None, description="The queued message this change sent, if it sent one"
    )
    message_id: StableId | None = Field(
        default=None, description="The stored user message `sent` became (`GET .../messages`)"
    )


class Conversation(Contract):
    """One conversation with Claude about the session's design: the main chat, or a comment
    thread sent to Claude. Each has its own history and its own question; all of them edit the
    one design, so their runs take turns (`GET .../conversations`)."""

    id: StableId = Field(description="`main` for the main chat, else the comment's ID")
    kind: Literal["main", "thread"]
    title: str = Field(description="The main chat's name, or the start of the comment's text")
    status: Literal["idle", "working", "queued", "waiting_for_answer"] = Field(
        description="`working`: its run is going; `queued`: its next run waits for another "
        "conversation's run (`queued_behind`); `waiting_for_answer`: Claude's question in it is "
        "open (`question`), which waits on this conversation only"
    )
    queued_behind: StableId | None = Field(
        default=None, description="The conversation whose run it waits for, when `queued`"
    )
    queued_behind_title: str | None = None
    question: QuestionEvent | None = Field(
        default=None,
        description="Claude's open question in this conversation: answer it with "
        "`POST .../messages` (`answer`) from the thread or the main chat",
    )
    summary: str | None = Field(default=None, description="What its latest run did")


class ConversationsEvent(Contract):
    """A conversation's status changed (a run started, queued, asked, ended): every
    conversation as it is now, so the main chat and the threads show the same."""

    type: Literal["conversations"]
    conversations: list[Conversation]


class ThreadReportEvent(Contract):
    """A comment thread's run reports back to the main chat: what it changed, the checks and
    the build, with a link to the thread. `message_id` is the entry's own message in the main
    chat (`GET .../messages`)."""

    type: Literal["thread_report"]
    message_id: StableId
    comment_id: StableId = Field(description="The thread: open it from the entry")
    thread_title: str
    run_id: StableId = Field(description="The thread's run that reports (its assistant message)")
    outcome: Literal["changed", "answered", "failed"] = Field(
        description="`changed`: the run changed the design; `answered`: it replied without a "
        "change; `failed`: it stopped with an error"
    )
    summary: str
    changes: list[str] = Field(default_factory=list, description="The changes it made, by title")
    build_id: StableId | None = Field(default=None, description="The build it ended on")
    build_number: int | None = Field(
        default=None, ge=1, description="The build's place in the session's builds (1: first)"
    )
    checks: CheckSummary | None = Field(default=None, description="That build's check results")


SessionEvent = Annotated[
    MessageStartEvent
    | TextEvent
    | ProgressEvent
    | QuestionEvent
    | BuildReadyEvent
    | ChangesReadyEvent
    | ConstraintsEvent
    | PlanEvent
    | RisksReadyEvent
    | FoundEvent
    | MessageEndEvent
    | ErrorEvent
    | CommentEvent
    | QueueEvent
    | NoticeEvent
    | TitleEvent
    | UserMessageEvent
    | ConversationsEvent
    | ThreadReportEvent,
    Field(discriminator="type"),
]
EVENT_TYPES = (
    "message_start",
    "text",
    "progress",
    "question",
    "build_ready",
    "changes_ready",
    "constraints",
    "plan",
    "risks_ready",
    "found",
    "message_end",
    "error",
    "comment",
    "queue",
    "notice",
    "title",
    "user_message",
    "conversations",
    "thread_report",
)

_event_adapter: TypeAdapter[Any] = TypeAdapter(SessionEvent)


class RecordedEvent(Contract):
    """One line of a recorded event stream (`fixtures/session-events.jsonl`)."""

    id: int = Field(ge=1)
    delay_ms: int = Field(ge=0, description="Wait before sending, when played back")
    event: SessionEvent


def validate_event(data: dict) -> Any:
    try:
        return _event_adapter.validate_python(data)
    except ValidationError as error:
        raise ContractError(f"session event: {error}") from error


def event_json_schema() -> dict:
    return _event_adapter.json_schema()


def validate_recording(lines: list[str]) -> list[RecordedEvent]:
    """A recorded stream: valid events, increasing IDs, messages opened before use and closed."""
    events = []
    for n, line in enumerate(lines, 1):
        try:
            events.append(RecordedEvent.model_validate_json(line))
        except ValidationError as error:
            raise ContractError(f"line {n}: {error}") from error
    open_messages: set[str] = set()
    last = 0
    for rec in events:
        if rec.id <= last:
            raise ContractError(f"event {rec.id}: ids must increase")
        last = rec.id
        ev = rec.event
        if ev.type == "message_start":
            open_messages.add(ev.message_id)
        elif getattr(ev, "message_id", None) and ev.message_id not in open_messages:
            raise ContractError(f"event {rec.id}: message {ev.message_id} is not open")
        if ev.type == "message_end":
            open_messages.discard(ev.message_id)
    if open_messages:
        raise ContractError(f"messages never ended: {sorted(open_messages)}")
    return events


def sse_frame(event_id: int, event: Any) -> str:
    """Format one event as a server-sent event frame."""
    data = event.model_dump_json() if hasattr(event, "model_dump_json") else json.dumps(event)
    return f"id: {event_id}\nevent: {event.type}\ndata: {data}\n\n"


# --- OpenAPI ----------------------------------------------------------------------------------


def contract_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text())


def openapi_with_events(app: Any) -> dict:
    """`app`'s OpenAPI document, with the server-sent event schemas added to the components
    (as `SessionEvent` and the event types it refers to)."""
    from fastapi.openapi.utils import get_openapi

    doc = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    schema = event_json_schema()
    defs = schema.pop("$defs", {})
    text = json.dumps(schema).replace("#/$defs/", "#/components/schemas/")
    components = doc.setdefault("components", {}).setdefault("schemas", {})
    for name, value in defs.items():
        value = json.loads(json.dumps(value).replace("#/$defs/", "#/components/schemas/"))
        # A model shared with the API (such as `Location`) may appear there as a response
        # (no defaults) and here as input; anything else must be identical.
        if name in components and _without_defaults(components[name]) != _without_defaults(value):
            raise ValueError(f"event schema {name} differs from the API schema of the same name")
        components[name] = value
    components["SessionEvent"] = json.loads(text)
    return dict(sorted(doc.items()))


def _without_defaults(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_defaults(v) for k, v in value.items() if k != "default"}
    if isinstance(value, list):
        return [_without_defaults(v) for v in value]
    return value


def assert_matches_session_api(openapi: dict, contract: dict | None = None) -> None:
    """Every operation in the contract exists in `openapi` with the same parameters, request body
    and responses, and every schema they use is identical. Extra operations are allowed."""
    contract = contract or contract_openapi()
    problems = []
    for path, ops in contract["paths"].items():
        for method, op in ops.items():
            other = openapi.get("paths", {}).get(path, {}).get(method)
            where = f"{method.upper()} {path}"
            if other is None:
                problems.append(f"{where}: missing")
                continue
            for key in ("parameters", "requestBody", "responses"):
                if _strip(op.get(key)) != _strip(other.get(key)):
                    problems.append(f"{where}: {key} differ")
    theirs = openapi.get("components", {}).get("schemas", {})
    for name, schema in contract.get("components", {}).get("schemas", {}).items():
        if name not in theirs:
            problems.append(f"schema {name}: missing")
        elif _strip(schema) != _strip(theirs[name]):
            problems.append(f"schema {name}: differs")
    if problems:
        raise ContractError("session API does not match the contract:\n  " + "\n  ".join(problems))


HUMAN_ONLY = ("description", "title", "summary", "operationId", "tags")


def _strip(value: Any, names: bool = False) -> Any:
    """Drop human-only text (descriptions, titles, summaries) before comparing. Under
    `properties` the keys are field names, which are kept."""
    if isinstance(value, dict):
        return {
            k: _strip(v, names=k == "properties" and not names)
            for k, v in value.items()
            if names or k not in HUMAN_ONLY
        }
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value
