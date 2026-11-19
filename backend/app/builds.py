"""Build store: every build with its code, parameters, check results, files and parent.

Each build is a directory under `<data dir>/builds/<build id>/`:

- `build.json`: the record (`StoredBuild`): ID, parent, time, backend, board, parameters,
  check results, the feature summary (`geometry.diff.feature_summary`) the diff works on and
  the lines that created each feature (`sources`, for the build's source view);
- `code.py`: the generator code that produced it;
- `files/`: the build's files (STEP, glTF, ...), by name.

A build is written to a temporary directory and renamed into place, so a crash never leaves a
half-written build. Nothing is cached in memory: a new store on the same directory (after a
restart) sees every build. `DATA_DIR` sets the data directory (`app.runtime`); point it at a
persistent disk for builds to survive redeploys.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app import runtime
from app.contracts.common import BoardRef

RECORD = "build.json"
CODE = "code.py"
FILES = "files"

_ID = re.compile(r"^build-[0-9a-f]{12}$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


class StoredBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    parent: str | None = Field(description="The build this one was made from, if any")
    created_at: datetime
    backend: str
    board: BoardRef | None = None
    code: str
    parameters: dict[str, Any]
    checks: dict[str, Any] | None = Field(
        default=None, description="The check-results report (`contracts/README.md`)"
    )
    features: list[dict[str, Any]] = Field(description="`geometry.diff.feature_summary`")
    sources: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Where in `code` each feature was created (`BuildResult.feature_sources`): "
        "`{tag, part, lines}` per feature in the order applied; empty when the build didn't "
        "record it (builds stored before feature sources were recorded)",
    )
    files: list[str] = Field(default_factory=list, description="Names of the stored files")


class BuildNotFound(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def new_build_id() -> str:
    """A fresh build ID in the store's format, for a build that is saved later (or never, if it
    fails)."""
    return f"build-{uuid.uuid4().hex[:12]}"


class BuildStore:
    def __init__(self, root: Path, clock: Callable[[], datetime] = _now) -> None:
        self.root = Path(root)
        self._clock = clock

    def _dir(self, build_id: str) -> Path:
        if not _ID.match(build_id):
            raise BuildNotFound(build_id)
        return self.root / build_id

    def save(
        self,
        *,
        code: str,
        parameters: dict[str, Any],
        features: list[dict[str, Any]],
        backend: str,
        board: BoardRef | None = None,
        checks: dict[str, Any] | None = None,
        files: Mapping[str, bytes | Path] | None = None,
        parent: str | None = None,
        build_id: str | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> StoredBuild:
        """Store a build and return its record. `files` are bytes or paths to copy. `build_id`
        (from `new_build_id()`) names it; by default it gets a fresh ID. `sources` are the
        lines that created each feature, when the build recorded them."""
        if parent is not None and not self.exists(parent):
            raise BuildNotFound(parent)
        build_id = build_id or new_build_id()
        if not _ID.match(build_id):
            raise ValueError(f"bad build ID {build_id!r}")
        if self.exists(build_id):
            raise ValueError(f"build {build_id} is already stored")
        files = dict(files or {})
        for name in files:
            if not _FILE_NAME.match(name) or name in (".", ".."):
                raise ValueError(f"bad file name {name!r}")
        record = StoredBuild(
            id=build_id,
            parent=parent,
            created_at=self._clock(),
            backend=backend,
            board=board,
            code=code,
            parameters=parameters,
            checks=checks,
            features=features,
            sources=list(sources or []),
            files=sorted(files),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.root))
        try:
            (tmp / FILES).mkdir()
            for name, content in files.items():
                target = tmp / FILES / name
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    shutil.copyfile(content, target)
            (tmp / CODE).write_text(code)
            body = record.model_dump(mode="json", exclude={"code"})
            (tmp / RECORD).write_text(json.dumps(body, indent=1))
            tmp.rename(self._dir(record.id))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return record

    def save_result(
        self,
        result: Any,
        *,
        code: str,
        parameters: dict[str, Any],
        board: BoardRef | None = None,
        checks: dict[str, Any] | None = None,
        files: Mapping[str, bytes | Path] | None = None,
        parent: str | None = None,
        build_id: str | None = None,
    ) -> StoredBuild:
        """Store a `geometry.BuildResult` with the code and parameters that made it, and the
        lines that created each feature when every one was recorded (`Feature.lines`)."""
        from geometry.diff import feature_summary

        recorded = all(f.lines is not None for f in result.features)
        return self.save(
            sources=result.feature_sources() if recorded and result.features else [],
            code=code,
            parameters=parameters,
            features=feature_summary(result),
            backend=result.backend,
            board=board,
            checks=checks,
            files=files,
            parent=parent,
            build_id=build_id,
        )

    def exists(self, build_id: str) -> bool:
        try:
            return (self._dir(build_id) / RECORD).is_file()
        except BuildNotFound:
            return False

    def get(self, build_id: str) -> StoredBuild:
        path = self._dir(build_id)
        if not (path / RECORD).is_file():
            raise BuildNotFound(build_id)
        body = json.loads((path / RECORD).read_text())
        return StoredBuild.model_validate({**body, "code": (path / CODE).read_text()})

    def file(self, build_id: str, name: str) -> Path:
        """Path to one of a build's stored files."""
        record = self.get(build_id)
        if name not in record.files:
            raise FileNotFoundError(f"{build_id} has no file {name!r}")
        return self._dir(build_id) / FILES / name

    def builds(self) -> list[StoredBuild]:
        """Every build, oldest first."""
        if not self.root.is_dir():
            return []
        builds = [self.get(p.name) for p in self.root.iterdir() if self.exists(p.name)]
        return sorted(builds, key=lambda b: (b.created_at, b.id))

    def history(self, build_id: str) -> list[StoredBuild]:
        """The build and its ancestors, newest first."""
        chain = []
        seen = set()
        current: str | None = build_id
        while current is not None and current not in seen:
            seen.add(current)
            record = self.get(current)
            chain.append(record)
            current = record.parent
        return chain

    def diff(self, old_id: str, new_id: str) -> dict[str, Any]:
        """What changed from one build to another (any two, related or not)."""
        return diff_builds(self.get(old_id), self.get(new_id))


def diff_builds(old: StoredBuild, new: StoredBuild) -> dict[str, Any]:
    """`{old, new, parameters, features}`: the parameters changed (`geometry.diff.
    diff_parameters`) and the features added, removed, moved, rotated or resized, with
    distances (`geometry.diff.diff_features`)."""
    from geometry.diff import diff_features, diff_parameters

    return {
        "old": old.id,
        "new": new.id,
        "parameters": diff_parameters(old.parameters, new.parameters),
        "features": diff_features(old.features, new.features),
    }


def default_store() -> BuildStore:
    """The store in the configured data directory (`DATA_DIR`)."""
    return BuildStore(runtime.data_dir() / "builds")
