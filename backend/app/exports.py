"""Export code shared by the mock API (`app.mock_api`) and the real session API
(`app.session_api`): the fixture artifacts' location, the fixture mesh's face ids, the build the
exports are made from, and the writing of the export files.

Both APIs call these public helpers, so a change here reaches both and neither imports the
other's private names.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import CheckReport
from app.contracts.common import FIXTURES_DIR
from app.standards import load_standards

if TYPE_CHECKING:
    from app.builds import BuildStore
    from app.contracts.session import Build
    from geometry.files import ExportedFile, Revision
    from geometry.interface import BuildResult


def fixtures_dir() -> Path:
    """Where the fixture artifacts are read from: `FIXTURES_DIR` if set, else `fixtures/`."""
    return Path(os.environ.get("FIXTURES_DIR", FIXTURES_DIR))


@cache
def fixture_face_ids() -> frozenset[str]:
    """The face ids of the fixture enclosure mesh (`enclosure.gltf`)."""
    doc = json.loads((fixtures_dir() / "enclosure.gltf").read_text())
    return frozenset(f["id"] for n in doc["nodes"] for f in n.get("extras", {}).get("faces", []))


@cache
def fixture_mesh_parts() -> tuple[str, ...]:
    """The part names of the fixture enclosure mesh (`enclosure.gltf`)."""
    doc = json.loads((fixtures_dir() / "enclosure.gltf").read_text())
    names = (n.get("extras", {}).get("part") for n in doc["nodes"] if "mesh" in n)
    return tuple(dict.fromkeys(name for name in names if name))


class ExportPartsError(ValueError):
    """The parts asked for can't be exported (the API answers 422)."""


def export_parts(parts: list[str] | None, mesh_parts: Iterable[str]) -> list[str] | None:
    """The `parts` of an export request, checked against the build's mesh parts and the board.
    `None` (the default) is the whole assembly."""
    from geometry.parts import BOARD  # not geometry.interface, which loads the CAD kernel

    if parts is None:
        return None
    known = [*mesh_parts, BOARD]
    unknown = [p for p in parts if p not in known]
    if unknown:
        raise ExportPartsError(f"unknown parts {unknown}; choose from {known}")
    return list(dict.fromkeys(parts))


@dataclass
class ExportBuild:
    """The real build the exports are made from: the default backend's enclosure generator run
    with the parameters of the board the fixture enclosure was designed for (the code
    `build-source.json` shows), checked against the board the fixture build is for."""

    board: Board
    result: BuildResult
    code: str
    params: dict[str, Any]


_export_lock = threading.Lock()


def export_build_geometry() -> ExportBuild:
    """The build exports are made from. Built once per process (a geometry worker, for the
    server's exports); concurrent callers wait for it."""
    with _export_lock:
        return _make_export_build()


@cache
def _make_export_build() -> ExportBuild:
    from geometry import DEFAULT_BACKEND, enclosure_parameters, get_backend

    changes = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    old = load_board(BOARDS_DIR / changes.old_file)
    new = load_board(BOARDS_DIR / changes.new_file)
    from app.examples import example_standards

    standards = example_standards()  # the board example's set, with what the example brings
    engine = get_backend(DEFAULT_BACKEND)
    code = engine.example("enclosure")
    params = enclosure_parameters(old, standards)
    return ExportBuild(new, engine.build(code, params), code, params)


def session_revisions(builds: Iterable[Build], build: Build) -> list[Revision]:
    """The revision table of `build`'s drawing: the session's builds (its history, oldest
    first) up to and including `build`, which is added last if the history lacks it."""
    from geometry.files import Revision

    chain = []
    for b in builds:
        if b.id == build.id:
            break
        chain.append(b)
    chain.append(build)
    return [Revision(b.id, b.created_at.date(), _describe(b)) for b in chain]


def _describe(build: Build) -> str:
    s = build.checks
    text = f"{build.board.name} {build.board.revision}: {s.passed} pass, {s.failed} fail"
    return text + (f", restores {build.restored_from}" if build.restored_from else "")


def store_revisions(store: BuildStore, build_id: str) -> list[Revision]:
    """The revision table of a stored build's drawing: the build and its ancestors in the build
    store (`app.builds`), oldest first."""
    from geometry.files import Revision

    out = []
    for b in reversed(store.history(build_id)):
        text = f"{b.board.name} {b.board.revision}" if b.board else b.backend
        if b.checks and "summary" in b.checks:
            s = b.checks["summary"]
            text += f": {s['pass']} pass, {s['fail']} fail"
        out.append(Revision(b.id, b.created_at.date(), text))
    return out


# Exports already written, by request: the export build is the same for every request and the
# exporters are deterministic, so an export asked for again is copied from here instead of
# starting a geometry worker (which takes seconds to load the CAD kernel). Per process, so a
# cache never outlives the code that wrote it.
_EXPORT_CACHE = Path(tempfile.gettempdir()) / f"claude-hardware-export-cache-{os.getpid()}"
_MANIFEST = ".files.json"
_cache_locks: dict[str, threading.Lock] = {}


def _cached_exports(
    report: dict[str, Any],
    formats: list[str],
    parts: list[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
) -> Path:
    """The cache directory holding this request's files, written by a geometry worker if new."""
    from geometry.worker import call

    revisions = [[r.build_id, str(r.date), r.description] for r in history or []]
    request = json.dumps([report, formats, parts, revisions, units], sort_keys=True)
    key = hashlib.sha256(request.encode()).hexdigest()
    cached = _EXPORT_CACHE / key[:32]
    with _export_lock:
        lock = _cache_locks.setdefault(cached.name, threading.Lock())
    with lock:
        if not (cached / _MANIFEST).exists():
            tmp = cached.with_name(f"{cached.name}.tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            written = call(
                f"{__name__}:worker_exports", tmp, report, formats, parts, history, units
            )
            manifest = [
                {
                    "format": f.format,
                    "name": f.name,
                    "media_type": f.media_type,
                    "path": str(Path(f.path).relative_to(tmp)),
                }
                for f in written
            ]
            (tmp / _MANIFEST).write_text(json.dumps(manifest))
            shutil.rmtree(cached, ignore_errors=True)
            tmp.rename(cached)
    return cached


def write_exports(
    out: Path,
    report: dict[str, Any],
    formats: list[str],
    parts: list[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
) -> list[ExportedFile]:
    """Write `formats` of the export build to `out` (emptied first) with the geometry exporters
    (`geometry.export`); the build note is written from the check `report`. `parts` (checked
    with `export_parts`) exports only those parts; default: the whole assembly. A choice the
    exporters can't write (the board alone in a mesh format) raises `ExportPartsError`. The
    drawing's revision table lists `history` (oldest first, ending with the exported build),
    and the build note's lengths are in `units`. Runs in a geometry worker
    (`geometry.worker`), so the server never loads the CAD kernel; a request made before is
    copied from the cache instead."""
    from geometry.files import ExportedFile

    cached = _cached_exports(report, formats, parts, history, units)
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(cached, out, ignore=shutil.ignore_patterns(_MANIFEST))
    return [
        ExportedFile(f["format"], f["name"], f["media_type"], out / f["path"])
        for f in json.loads((cached / _MANIFEST).read_text())
    ]


def prewarm_exports(
    report: dict[str, Any], formats: list[str], history: Sequence[Revision] | None = None
) -> None:
    """Write the export a first request will most likely ask for, so it doesn't wait for it."""
    _cached_exports(report, formats, history=history)


def worker_exports(
    out: Path,
    report: dict[str, Any],
    formats: list[str],
    parts: list[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
) -> list[ExportedFile]:
    """In a geometry worker: what `write_exports` does."""
    build = export_build_geometry()
    b = build
    return write_build_exports(
        out, b.board, b.result, b.code, b.params, report, formats, parts, history, units
    )


def write_build_exports(
    out: Path,
    board: Board | None,
    result: BuildResult,
    code: str,
    params: dict[str, Any],
    report: dict[str, Any],
    formats: list[str],
    parts: list[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
    face_ids: dict[str, list[str]] | None = None,
) -> list[ExportedFile]:
    """Write `formats` of a build's `parts` (default: the whole assembly) to `out` (emptied
    first) with the geometry exporters; the build note is written from its check `report`.
    `face_ids` are the face IDs of the build's mesh (`mesh_face_ids`), which the files keep.
    Raises `ExportPartsError` for a choice the exporters can't write. Loads the CAD kernel: only
    a geometry worker calls it."""
    from geometry.export import export_build

    shutil.rmtree(out, ignore_errors=True)
    # Every optional field present, as the exporters read them.
    full_report = CheckReport.model_validate(report).model_dump(mode="json")
    try:
        return export_build(
            board,
            load_standards(),
            result,
            code,
            params,
            out,
            full_report,
            formats=formats,
            parts=parts,
            history=history,
            units=units,
            face_ids=face_ids,
        )
    except ValueError as error:
        raise ExportPartsError(str(error)) from error


def worker_stored_exports(
    out: Path,
    board: Board,
    code: str,
    params: dict[str, Any],
    backend: str,
    report: dict[str, Any],
    formats: list[str],
    parts: list[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
    face_ids: dict[str, list[str]] | None = None,
) -> list[ExportedFile] | dict:
    """In a geometry worker: rebuild a stored build in the sandbox from its code and parameters
    and write its exports, with the face IDs of its stored mesh (`mesh_face_ids`). Returns the
    build's error if it no longer builds."""
    from app.agent.services import sandbox_build

    result, error = sandbox_build(code, params, backend)
    if result is None:
        return error
    return write_build_exports(
        out, board, result, code, params, report, formats, parts, history, units, face_ids
    )


def mesh_face_ids(gltf: str | dict[str, Any]) -> dict[str, list[str]]:
    """The face IDs of each part of a mesh (glTF, the mesh contract), in face order."""
    doc = json.loads(gltf) if isinstance(gltf, str) else gltf
    out = {}
    for node in doc.get("nodes", []):
        extras = node.get("extras") or {}
        if "mesh" in node and extras.get("part"):
            out[extras["part"]] = [f["id"] for f in extras.get("faces", [])]
    return out
