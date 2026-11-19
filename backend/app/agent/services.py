"""What the agent's tools call to build, check and analyze a design.

`Services` is the seam between the agent and the geometry. Two implementations:

- `GeometryServices`, the real one: runs the design's code in the sandboxed build runner,
  checks it with the check runner (`geometry.report`) on the exact B-rep, analyzes it, and
  stores every build in the build store (`app.builds`) with its code, parameters, check
  report, mesh and analysis maps, as a child of the build before it.
- `FixtureServices` serves the contract fixtures (`fixtures/build.json`,
  `fixtures/checks.json`, `fixtures/maps/`): every build is the sample build and every check
  report is the sample report. Frontend development and tests use it.

A check report reaches the agent as a list of `CheckItem`s, whichever service made it.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.boards import Board
from app.builds import BuildStore, default_store, new_build_id
from app.contracts.common import BoardRef
from app.contracts.session import Build
from app.standards import StandardsFile

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "fixtures"


@dataclass
class CheckItem:
    """One check result, as the agent sees it."""

    id: str
    check: str
    status: str  # pass, fail, untested or not_applicable
    reason: str | None = None  # why a not_applicable standard doesn't apply to the design
    standard: str | None = None
    component: str | None = None
    feature: str | None = None
    part: str | None = None
    parts: list[str] | None = None  # the two assembly parts a result is between
    measured: float | str | None = None
    required: float | str | None = None
    unit: str | None = None
    summary: str = ""
    point: list[float] | None = None  # where on the model, when the check says
    title: str | None = None  # the result's title on the checks strip
    faces: list[str] | None = None  # the mesh faces it is on, when the check says

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "") and k != "faces"}


@dataclass
class BuildOutcome:
    """A build of the design: the contract `Build`, or an error with the failing line."""

    build: Build | None
    error: dict | None = None
    shapes: Any = None  # whatever the service needs to check and analyze it later
    artifacts: Any = None  # GeometryServices: the build's mesh, checks and analysis

    @property
    def ok(self) -> bool:
        return self.build is not None and self.error is None


class Services(Protocol):
    def build(self, design: dict[str, Any]) -> BuildOutcome: ...

    def checks(self, outcome: BuildOutcome) -> list[CheckItem]: ...

    def analyze(self, outcome: BuildOutcome) -> list[dict]: ...


def items_from_report(report: dict) -> list[CheckItem]:
    """CheckItems from a check-results/1 report (`contracts/README.md`)."""
    items = []
    for r in report["results"]:
        measured, required = r.get("measured") or {}, r.get("required") or {}
        location = r.get("location") or {}
        items.append(
            CheckItem(
                id=r["id"],
                check=r["check"],
                status=r["status"],
                standard=r.get("standard"),
                component=r.get("component"),
                part=location.get("part"),
                parts=list(r["parts"]) if r.get("parts") else None,
                measured=measured.get("value"),
                required=required.get("value"),
                unit=measured.get("unit") or required.get("unit"),
                summary=r.get("summary", ""),
                reason=r.get("reason"),
                point=list(location["point"]) if location.get("point") else None,
                title=r.get("title"),
                faces=list(location["faces"]) if location.get("faces") else None,
            )
        )
    return items


def summary(items: list[CheckItem]) -> dict[str, int]:
    out = {"pass": 0, "fail": 0, "untested": 0, "not_applicable": 0}
    for item in items:
        out[item.status] += 1
    return out


def board_ref(board: Board | None) -> BoardRef | None:
    return BoardRef(name=board.name, revision=board.revision) if board is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def make_build(
    build_id: str,
    board: Board | None,
    *,
    status: str = "ready",
    checks: dict[str, int] | None = None,
    maps: list[str] | None = None,
    clock: Callable[[], datetime] = _now,
) -> Build:
    """A contract `Build`, checked against `board` (None for a design without one). URLs are
    relative to `/api/sessions/{id}/`, as in the fixtures."""
    return Build(
        id=build_id,
        created_at=clock(),
        board=board_ref(board),
        status=status,
        checks=checks or {"pass": 0, "fail": 0, "untested": 0, "not_applicable": 0},
        maps=maps or [],
        mesh_url=f"builds/{build_id}/mesh",
        checks_url=f"builds/{build_id}/checks",
        source_url=f"builds/{build_id}/source",
        board_url=f"builds/{build_id}/board",
        step_url=None,
    )


# --- Fixtures --------------------------------------------------------------------------------


@dataclass
class FixtureServices:
    """Every build is `fixtures/build.json`, with a new ID; every report is `checks.json`.
    Given the older revision's `board` (`fixtures/previous-build.json`), they are that build
    and `previous-checks.json` instead."""

    fixtures_dir: Path = FIXTURES_DIR
    board: BoardRef | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _json(self, name: str) -> Any:
        return json.loads((self.fixtures_dir / name).read_text())

    @property
    def _previous(self) -> bool:
        if self.board is None:
            return False
        return self.board == BoardRef.model_validate(self._json("previous-build.json")["board"])

    def build(self, design: dict[str, Any]) -> BuildOutcome:
        name = "previous-build.json" if self._previous else "build.json"
        build = Build.model_validate(self._json(name))
        build_id = f"build-{next(self._ids)}"
        data = build.model_dump()
        for key in ("mesh_url", "checks_url", "source_url", "board_url"):
            data[key] = data[key].replace(f"/{build.id}/", f"/{build_id}/")
        return BuildOutcome(Build.model_validate({**data, "id": build_id}))

    def checks(self, outcome: BuildOutcome) -> list[CheckItem]:
        return items_from_report(
            self._json("previous-checks.json" if self._previous else "checks.json")
        )

    def analyze(self, outcome: BuildOutcome) -> list[dict]:
        flagged = []
        for path in sorted((self.fixtures_dir / "maps").glob("*.json")):
            doc = json.loads(path.read_text())
            for spot in doc["flagged"]:
                flagged.append(
                    {
                        "map": doc["type"],
                        "kind": spot["kind"],
                        "value": spot["value"],
                        "unit": doc["unit"],
                        "message": spot["message"],
                        "part": spot["location"]["part"],
                        "faces": spot["location"]["faces"],
                        "point": spot["location"]["point"],
                    }
                )
        return flagged


# --- Geometry --------------------------------------------------------------------------------

# Appended to the design's code inside the sandbox: the sandbox returns named shapes, so the
# parts (with kind, placement and material) and feature tools travel in the names, with the
# lines of the statement that made each feature when the build records them (`feature_sites`).
SANDBOX_EPILOGUE = """

def _sandbox_result(_out):
    _shapes = {}
    _materials = _out.get("materials", {})
    _kinds = _out.get("kinds", {})
    _placements = _out.get("placements", {})
    _lines_of = globals().get("__feature_lines__")
    for _name, _shape in _out["parts"].items():
        _at = ",".join(repr(float(_v)) for _v in _placements.get(_name, (0, 0, 0)))
        _shapes["part|%s|%s|%s|%s" % (
            _name, _kinds.get(_name, "enclosure"), _at, _materials.get(_name, "")
        )] = _shape
    for _i, _f in enumerate(_out["features"]):
        _lines = _lines_of(_f) if _lines_of is not None else None
        _at = "%d-%d" % _lines if _lines else ""
        for _j, _tool in enumerate(_f["tools"]):
            _shapes["tool|%d|%d|%s|%s|%s" % (_i, _j, _at, _f["part"], _f["tag"])] = _tool
    return _shapes


result = _sandbox_result(build(params))
"""


Tools = dict[int, tuple[str, str, tuple[int, int] | None, list]]


def _lines(text: str) -> tuple[int, int] | None:
    """A feature's `start-end` lines from its shape names ("" when not recorded), or
    ValueError. The names come from the build, so they are checked like any of its output."""
    if not text:
        return None
    start, end = (int(v) for v in text.split("-"))
    if not 1 <= start <= end:
        raise ValueError(f"bad feature lines {text!r}")
    return start, end


def _layout(names: Any) -> tuple[dict, dict, dict, dict, Tools]:
    """Parts and features from the sandbox's shape names, or ValueError. Each value of `names`
    is carried along as is (a shape or its BREP), so this runs without the CAD kernel."""
    from geometry.parts import BOARD, ENCLOSURE, INSERT, ORIGIN

    parts, materials, kinds, placements = {}, {}, {}, {}
    tools: Tools = {}
    for name, shape in names.items():
        kind, rest = name.split("|", 1)
        if kind == "part":
            part, part_kind, at, material = rest.split("|", 3)
            parts[part] = shape
            if material:
                materials[part] = material
            if part_kind not in (ENCLOSURE, INSERT):
                raise ValueError(f"part {part!r}: unknown kind {part_kind!r}")
            if part_kind != ENCLOSURE:
                kinds[part] = part_kind
            x, y, z = (float(v) for v in at.split(","))
            if (x, y, z) != ORIGIN:
                placements[part] = (x, y, z)
        else:
            i, j, at, part, tag = rest.split("|", 4)
            tools.setdefault(int(i), (part, tag, _lines(at), []))[3].append((int(j), shape))
    if BOARD in parts:
        raise ValueError(f"{BOARD!r} is the board's part name; the board is added for you")
    return parts, materials, kinds, placements, tools


def unpack_shapes(shapes: dict[str, Any], backend: str):
    """The sandbox's named shapes back into a `geometry.interface.BuildResult`."""
    from geometry.interface import BuildResult, Feature

    parts, materials, kinds, placements, tools = _layout(shapes)
    features = []
    for i in sorted(tools):
        part, tag, lines, found = tools[i]
        ordered = tuple(s for _, s in sorted(found, key=lambda t: t[0]))
        features.append(Feature(tag, part, ordered, lines))
    return BuildResult(backend, parts, features, materials, kinds, placements)


@dataclass(frozen=True)
class SandboxShapes:
    """A sandboxed build's shapes as the server keeps them: BREP bytes by name, unparsed. Only
    a geometry worker (`geometry.worker`) turns them back into shapes."""

    backend: str
    breps: dict[str, bytes]


def _load(shapes: SandboxShapes):
    """In a geometry worker: the build's `BuildResult`."""
    import os
    import tempfile

    from geometry.sandbox import _read_brep

    loaded = {}
    with tempfile.TemporaryDirectory(prefix="breps-") as tmp:
        for i, (name, data) in enumerate(shapes.breps.items()):
            path = os.path.join(tmp, f"shape_{i}.brep")
            with open(path, "wb") as f:
                f.write(data)
            loaded[name] = _read_brep(path)
    return unpack_shapes(loaded, shapes.backend)


def flag_faces(analysis: dict) -> list[dict]:
    """The faces the analysis maps flag, from `geometry.analysis.analyze`'s result."""
    flagged = []
    for part, faces in analysis.items():
        for face in faces:
            for map_type, unit in (("wall_thickness", "mm"), ("draft", "deg")):
                fv = getattr(face, map_type)
                if fv.kind:
                    flagged.append(
                        {
                            "map": map_type,
                            "kind": fv.kind,
                            "value": round(fv.value, 3),
                            "unit": unit,
                            "part": part,
                            "feature": face.feature,
                            "point": [round(c, 3) for c in fv.point],
                        }
                    )
    return flagged


def worker_build(
    board: Board | None,
    standards: StandardsFile,
    shapes: SandboxShapes,
    build_id: str,
    previous: Any,
    store: BuildStore,
    code: str,
    params: dict[str, Any],
    parent: str | None,
) -> Artifacts:
    """In a geometry worker: mesh, check and analyze a sandboxed build, and store it."""
    result = _load(shapes)
    artifacts = make_artifacts(board, standards, result, build_id, previous, params)
    files = {MESH_FILE: json.dumps(artifacts.mesh.to_gltf(), separators=(",", ":")).encode()}
    for map_type, doc in artifacts.maps.items():
        files[map_file(map_type)] = json.dumps(doc, separators=(",", ":")).encode()
    store.save_result(
        result,
        code=code,
        parameters=params,
        board=board_ref(board),
        checks=artifacts.report,
        files=files,
        parent=parent,
        build_id=build_id,
    )
    return artifacts.portable()


def worker_feature_sources(backend: str, code: str, params: str) -> tuple[str | None, list[dict]]:
    """In a geometry worker: the name of the backend's generator whose code this is (None for
    other code), and where in it each feature is created (empty for other code)."""
    from geometry import get_backend

    engine = get_backend(backend)
    name = next((n for n, c in engine.examples().items() if c == code), None)
    if name is None:
        return None, []
    return name, engine.build(code, json.loads(params)).feature_sources()


def worker_conflicts(
    board: Board,
    standards: StandardsFile,
    params: dict[str, Any],
    locked: dict[str, Any],
    shapes: SandboxShapes,
    mesh: Any,
) -> list:
    """In a geometry worker: conflict detection with each location on the build's mesh faces."""
    from geometry.conflicts import detect_conflicts

    return detect_conflicts(board, standards, params, locked, result=_load(shapes), mesh=mesh)


@dataclass
class BuildChain:
    """The build history a run adds to: the last stored build (each new build's parent) and its
    tessellation (so face IDs stay stable from one build to the next). Services for different
    boards in one session share it."""

    parent: str | None = None
    mesh: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Artifacts:
    """What a real build made, besides its shapes: the mesh, the check results and report, and
    the per-face analysis."""

    mesh: Any  # geometry.tessellate.Tessellation
    results: list  # geometry.checks.CheckResult, the report's first results in order
    report: dict  # check-results/1
    analysis: dict  # part -> [geometry.analysis.FaceAnalysis]
    maps: dict[str, dict]  # map type -> analysis-map/1
    flagged: list[dict] = field(default_factory=list)  # the faces the maps flag

    def portable(self) -> Artifacts:
        """What the server keeps, with nothing that loads the CAD kernel when a geometry worker
        sends it back (`geometry.worker` pickles it): the mesh is plain data
        (`geometry.mesh_data`), each check result keeps its feature, and the per-face analysis,
        which only the worker uses, stays there."""
        results = [CheckFeature(r.feature) for r in self.results]
        return Artifacts(self.mesh, results, self.report, {}, self.maps, self.flagged)


@dataclass(frozen=True)
class CheckFeature:
    """The feature a check result is about: all the server needs of a `CheckResult`."""

    feature: str | None


# The files a stored build keeps (`app.builds`): the mesh, and one analysis map per type.
MESH_FILE = "mesh.gltf"


def map_file(map_type: str) -> str:
    return f"{map_type}.json"


def sandbox_build(code: str, params: dict[str, Any], backend: str):
    """Run generator code in the sandboxed build runner. Returns (BuildResult, None) or
    (None, error dict with the failing line)."""
    from geometry.sandbox import run_build

    run = run_build(code + SANDBOX_EPILOGUE, params, backend=backend, feature_sites=True)
    if not run.ok:
        assert run.error is not None
        return None, run.error.to_dict()
    try:
        return unpack_shapes(run.shapes, backend), None
    except ValueError as error:
        message = f"build(params) did not return parts and features: {error}"
        return None, {"kind": "no_result", "message": message, "line": None}


def make_artifacts(
    board: Board | None,
    standards: StandardsFile,
    result: Any,
    build_id: str,
    previous: Any = None,
    params: dict[str, Any] | None = None,
) -> Artifacts:
    """Mesh, check (the check runner) and analyze a build. `previous` is the tessellation
    of the build before, whose face IDs this one keeps where the faces match; `params` the
    design's parameters, whose `object` (if any) the object_fit check fits the part to."""
    from app.contracts.mesh import validate_mesh
    from geometry.analysis import analysis_maps, analyze
    from geometry.fit_object import FitObject
    from geometry.report import checked_report
    from geometry.tessellate import tessellate

    mesh = tessellate(result, previous=previous, source={"build_id": build_id})
    fit = FitObject.from_params(params)
    results, report = checked_report(board, standards, result, build_id, mesh, fit)
    analysis = analyze(result, standards)
    maps = analysis_maps(analysis, standards, validate_mesh(mesh.to_gltf()), build_id)
    return Artifacts(mesh, results, report, analysis, maps, flag_faces(analysis))


@dataclass
class GeometryServices:
    """The real services: every build runs in the sandbox, is checked by the check runner
    on the exact B-rep, analyzed, and stored in the build store with its code, parameters,
    check report, mesh and maps, as a child of the build before it. `board` is the board the
    design is checked against, None for a design without one (only the standards that apply
    to any design run; the rest are reported not applicable).

    The server never loads the CAD kernel: the build runs in the sandbox, which hands its
    shapes back as BREP bytes, and the meshing, checks, analysis and storing run in a geometry
    worker (`geometry.worker`). Both wait for a build slot (`geometry.slots`)."""

    board: Board | None
    standards: StandardsFile
    store: BuildStore | None = None  # None: the store in the data directory (`DATA_DIR`)
    chain: BuildChain = field(default_factory=BuildChain)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = default_store()

    def build(self, design: dict[str, Any]) -> BuildOutcome:
        from geometry.sandbox import run_build
        from geometry.worker import call

        build_id = new_build_id()
        run = run_build(
            design["code"] + SANDBOX_EPILOGUE,
            design["params"],
            backend=design["backend"],
            load_shapes=False,
            feature_sites=True,  # where each feature is made, for the build's source
        )
        if not run.ok:
            assert run.error is not None
            return BuildOutcome(
                make_build(build_id, self.board, status="failed"), error=run.error.to_dict()
            )
        try:
            _layout(run.breps)
        except ValueError as error:
            message = f"build(params) did not return parts and features: {error}"
            return BuildOutcome(
                make_build(build_id, self.board, status="failed"),
                error={"kind": "no_result", "message": message, "line": None},
            )
        shapes = SandboxShapes(design["backend"], run.breps)
        with self.chain.lock:
            previous = self.chain.mesh
        assert self.store is not None
        with self.chain.lock:
            parent = self.chain.parent
            artifacts = call(
                f"{__name__}:worker_build",
                self.board,
                self.standards,
                shapes,
                build_id,
                previous,
                self.store,
                design["code"],
                design["params"],
                parent if parent and self.store.exists(parent) else None,
            )
            self.chain.parent, self.chain.mesh = build_id, artifacts.mesh
        build = make_build(
            build_id,
            self.board,
            checks=artifacts.report["summary"],
            maps=sorted(artifacts.maps),
        )
        return BuildOutcome(build, shapes=shapes, artifacts=artifacts)

    def checks(self, outcome: BuildOutcome) -> list[CheckItem]:
        artifacts: Artifacts = outcome.artifacts
        items = items_from_report(artifacts.report)
        for item, raw in zip(items, artifacts.results, strict=False):
            item.feature = raw.feature
        return items

    def analyze(self, outcome: BuildOutcome) -> list[dict]:
        return list(outcome.artifacts.flagged)
