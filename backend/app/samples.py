"""The demos' hardcoded originals: read-only sessions with a recorded conversation and pre-made
builds, served at the session API's paths by the real API (`app.main`) and the mock
(`app.mock_api`) alike. Users never work in an original: opening a demo gives the browser its
own copy (`app.demos`), and the original stays as it was.

The samples are files in `demos/sessions/` (`demos/README.md`), recorded by
`python -m app.fixtures.from_session`; nothing here names one. `GET /api/sessions` lists each as a
demo (`"sample": true`) until the browser has its own copy. Every read of a session works on
them: its messages, its event stream (the recorded conversation, then keep-alives), its builds
with their mesh, checks, source, board and maps (at absolute URLs, as a real session's), diffs
between them, parameters, comments and modeling edits (none). Find in model and exports work
too, since they change nothing. Anything else that would change a
sample (a message, an edit, a restore, a review, an upload) gets 409 with `READ_ONLY`.

The routes match only the IDs of the samples on disk (the `sample_session` path convertor), so
they sit in front of the session API's own routes without touching them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from starlette.convertors import Convertor, register_url_convertor

from app.boards import Board, find_board
from app.contracts.common import DEMOS_DIR
from app.contracts.maps import MapType
from app.contracts.session import (
    Build,
    BuildDiff,
    DesignHistory,
    Export,
    ExportCreate,
    ExportFile,
    FindRequest,
    FindResult,
    Message,
    Parameter,
    Session,
    sse_frame,
)
from app.runtime import data_dir
from app.standards import load_standards

API = "/api"
SESSION = API + "/sessions/{session_id:sample_session}"
GLTF = "model/gltf+json"
EVENT_STREAM = "text/event-stream"
KEEPALIVE_SECONDS = 15.0
READ_ONLY = "A demo's original is read-only. Open the demo to work in your own copy of it."


def demos_dir() -> Path:
    """Where the demos are read from: `DEMOS_DIR` if set, else `demos/`."""
    return Path(os.environ.get("DEMOS_DIR") or DEMOS_DIR)


def samples_dir() -> Path:
    return demos_dir() / "sessions"


# --- Catalogue ---------------------------------------------------------------------------------


@dataclass
class Sample:
    root: Path
    session: Session
    messages: list[Message]
    events: list[tuple[int, Any]]
    parameters: list[Parameter]
    builds: dict[str, Build]
    exports: dict[str, tuple[Export, Path]] = field(default_factory=dict)

    def build(self, build_id: str) -> Build:
        build = self.builds.get(build_id)
        if build is None:
            raise HTTPException(404, f"no build {build_id} in session {self.session.id}")
        return build

    def file(self, build_id: str, name: str) -> Path:
        self.build(build_id)
        return self.root / "builds" / build_id / name

    def json(self, build_id: str, name: str) -> Any:
        return json.loads(self.file(build_id, name).read_text())


def _load(root: Path) -> Sample:
    from app.contracts.session import _event_adapter

    doc = json.loads((root / "session.json").read_text())
    builds = {}
    for path in sorted((root / "builds").glob("*/build.json")):
        build = Build.model_validate_json(path.read_text())
        builds[build.id] = build
    return Sample(
        root=root,
        session=Session.model_validate(doc["session"]),
        messages=[Message.model_validate(m) for m in doc["messages"]],
        events=[(e["id"], _event_adapter.validate_python(e["event"])) for e in doc["events"]],
        parameters=[Parameter.model_validate(p) for p in doc["parameters"]],
        builds=dict(sorted(builds.items(), key=lambda kv: (kv[1].created_at, kv[0]))),
    )


@cache
def _catalogue(root: Path) -> dict[str, Sample]:
    index = root / "index.json"
    if not index.is_file():
        return {}
    ids = json.loads(index.read_text())["samples"]
    return {sid: _load(root / sid) for sid in ids}


def catalogue() -> dict[str, Sample]:
    """Every sample, in the order `demos/sessions/index.json` lists them."""
    return _catalogue(samples_dir())


def is_sample(session_id: str) -> bool:
    return session_id in catalogue()


def get(session_id: str) -> Sample:
    sample = catalogue().get(session_id)
    if sample is None:
        raise HTTPException(404, f"no session {session_id}")
    return sample


class SampleSessionConvertor(Convertor):
    """Matches only the IDs of the samples on disk, so a sample's routes never shadow a real
    session's."""

    @property
    def regex(self) -> str:  # type: ignore[override]
        ids = sorted(catalogue(), key=len, reverse=True)
        return "|".join(re.escape(i) for i in ids) if ids else "(?!)"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("sample_session", SampleSessionConvertor())

router = APIRouter(include_in_schema=False)


# --- Sessions, messages, events ----------------------------------------------------------------


@router.get(SESSION, response_model=Session)
def get_session(session_id: str) -> Session:
    return get(session_id).session


@router.get(SESSION + "/messages", response_model=list[Message])
def list_messages(session_id: str) -> list[Message]:
    return get(session_id).messages


@router.get(SESSION + "/events", response_class=StreamingResponse)
async def events(
    session_id: str,
    after: Annotated[int | None, Query()] = None,
    follow: Annotated[bool, Query()] = True,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """The recorded conversation, then keep-alives: a sample never has new events."""
    sample = get(session_id)
    try:
        start = after if after is not None else int(last_event_id or 0)
    except ValueError:
        start = 0

    async def stream():
        for event_id, event in sample.events:
            if event_id > start:
                yield sse_frame(event_id, event)
        while follow:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            yield ": keep-alive\n\n"

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type=EVENT_STREAM, headers=headers)


# --- Builds ------------------------------------------------------------------------------------


@router.get(SESSION + "/builds", response_model=list[Build])
def list_builds(session_id: str) -> list[Build]:
    return list(get(session_id).builds.values())


@router.get(SESSION + "/builds/{build_id}", response_model=Build)
def get_build(session_id: str, build_id: str) -> Build:
    return get(session_id).build(build_id)


@router.get(SESSION + "/builds/{build_id}/mesh", response_class=Response)
def get_mesh(session_id: str, build_id: str) -> Response:
    path = get(session_id).file(build_id, "mesh.gltf")
    return Response(path.read_text(), media_type=GLTF)


@router.get(SESSION + "/builds/{build_id}/checks")
def get_checks(session_id: str, build_id: str) -> dict:
    return get(session_id).json(build_id, "checks.json")


@router.get(SESSION + "/builds/{build_id}/source")
def get_source(session_id: str, build_id: str) -> dict:
    return get(session_id).json(build_id, "source.json")


@router.get(SESSION + "/builds/{build_id}/board", response_model=Board)
def get_board(session_id: str, build_id: str) -> Board:
    """The board revision the build was checked against, from `demos/<board>/`."""
    build = get(session_id).build(build_id)
    if build.board is None:
        raise HTTPException(404, f"build {build_id} has no board")
    try:
        return find_board(build.board.name, build.board.revision)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@router.get(SESSION + "/builds/{build_id}/maps/{map_type}")
def get_map(session_id: str, build_id: str, map_type: MapType) -> dict:
    sample = get(session_id)
    if map_type not in sample.build(build_id).maps:
        raise HTTPException(404, f"build {build_id} has no {map_type} map")
    return sample.json(build_id, f"maps/{map_type}.json")


@router.get(SESSION + "/builds/{build_id}/diff", response_model=BuildDiff)
def get_diff(session_id: str, build_id: str, against: Annotated[str, Query()]) -> dict[str, Any]:
    """What changed from build `against` to this one, as the generator measured it
    (`geometry.diff`)."""
    sample = get(session_id)
    sample.build(against)
    path = sample.file(build_id, f"diffs/{against}.json")
    if not path.is_file():
        raise HTTPException(404, f"no diff of {build_id} against {against}")
    return json.loads(path.read_text())


@router.post(SESSION + "/builds/{build_id}/find", response_model=FindResult)
def find(session_id: str, build_id: str, body: FindRequest) -> FindResult:
    """Find in model on a sample build: it reads the build and changes nothing."""
    from app.find import find_in_build

    sample = get(session_id)
    build = sample.build(build_id)
    maps = {t: sample.json(build_id, f"maps/{t}.json") for t in build.maps}
    mesh = sample.file(build_id, "mesh.gltf").read_text()
    return find_in_build(body.query, mesh, sample.parameters, maps, load_standards())


# --- Parameters, comments, changes -------------------------------------------------------------


@router.get(SESSION + "/parameters", response_model=list[Parameter])
def list_parameters(session_id: str) -> list[Parameter]:
    return measured_parameters(get(session_id))


def measured_parameters(sample: Sample) -> list[Parameter]:
    """The sample's parameters, each with where it is measured on its latest build, then the
    design's grouped values that measure there (`app.dimensions`)."""
    from app.dimensions import measured

    latest = sample.session.latest_build_id
    if latest is None or latest not in sample.builds:
        return measured(sample.parameters, None)
    mesh = sample.file(latest, "mesh.gltf").read_text()
    return measured(sample.parameters, mesh, sample.json(latest, "design.json")["params"])


@router.get(SESSION + "/comments")
def list_comments(session_id: str) -> list:
    get(session_id)
    return []


@router.get(SESSION + "/edits")
def list_edits(session_id: str) -> list:
    """A sample has no modeling edits; making one is refused as read-only, below."""
    get(session_id)
    return []


@router.get(SESSION + "/changes")
def get_changes(session_id: str) -> None:
    get(session_id)
    raise HTTPException(404, f"session {session_id} has no change set")


@router.get(SESSION + "/history", response_model=DesignHistory)
def get_history(session_id: str) -> DesignHistory:
    """A sample's design never changes: its timeline is empty."""
    get(session_id)
    return DesignHistory(steps=[], position=0, undo=None, redo=None)


# --- Exports -----------------------------------------------------------------------------------


def _export(sample: Sample, build: Build, out: Path, formats: list[str]):
    """Rebuild the build from its source and parameters and write the exports, in a geometry
    worker (`geometry.worker`): the server never loads the CAD kernel."""
    from geometry.worker import call

    source = sample.json(build.id, "source.json")
    design = sample.json(build.id, "design.json")
    board = find_board(build.board.name, build.board.revision) if build.board else None
    written = call(
        "app.exports:worker_stored_exports",
        out,
        board,
        source["code"],
        design["params"],
        source["backend"],
        sample.json(build.id, "checks.json"),
        formats,
    )
    if isinstance(written, dict):
        raise HTTPException(409, f"build {build.id} could not be rebuilt: {written}")
    return written


@router.post(SESSION + "/exports", response_model=Export, status_code=201)
async def create_export(session_id: str, body: ExportCreate) -> Export:
    """Exports write files outside the sample and leave it as it was."""
    sample = get(session_id)
    build = sample.build(body.build_id)
    eid = f"export-{len(sample.exports) + 1}"
    out = data_dir() / "exports" / session_id / eid
    written = await asyncio.to_thread(_export, sample, build, out, list(body.formats))
    base = f"{API}/sessions/{session_id}/exports/{eid}/files/"
    files = [
        ExportFile(format=f.format, name=f.name, media_type=f.media_type, url=base + f.name)
        for f in written
    ]
    export = Export(id=eid, build_id=build.id, created_at=datetime.now(UTC), files=files)
    sample.exports[eid] = (export, out)
    return export


@router.get(SESSION + "/exports/{export_id}/files/{name}", response_class=Response)
def get_export_file(session_id: str, export_id: str, name: str) -> Response:
    found = get(session_id).exports.get(export_id)
    file = next((f for f in found[0].files if f.name == name), None) if found else None
    if found is None or file is None or not (found[1] / file.name).is_file():
        raise HTTPException(404, f"no file {name} in export {export_id}")
    headers = {"Content-Disposition": f'attachment; filename="{file.name}"'}
    return Response(
        (found[1] / file.name).read_bytes(), media_type=file.media_type, headers=headers
    )


# --- Everything else is read-only -------------------------------------------------------------


@router.post(SESSION + "/telemetry", status_code=204, response_class=Response)
def post_telemetry(session_id: str) -> Response:
    """Views and choices on a sample are not recorded: there is no session log to add to."""
    get(session_id)
    return Response(status_code=204)


@router.api_route(SESSION, methods=["POST", "PUT", "PATCH", "DELETE"])
@router.api_route(SESSION + "/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
def read_only(session_id: str) -> None:
    get(session_id)
    raise HTTPException(409, READ_ONLY)


__all__ = ["READ_ONLY", "catalogue", "is_sample", "router"]
