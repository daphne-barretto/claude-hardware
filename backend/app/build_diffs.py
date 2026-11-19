"""Diffs between two builds of a session, as `GET .../builds/{id}/diff?against={other}` serves
them (`BuildDiff` in the session API contract), for the mock and the real session API.

Contract-first, a session's builds serve the fixture geometry, so the features changed
come from `fixtures/diff.json`: the diff (`geometry.diff`) between the enclosure
generator's designs for the two revisions in `demos/<board>/CHANGES.md`. Between builds of the
two revisions it is that diff, turned around when the newer build is the `old` one; between builds
of one revision, or of one design, no feature moved. The parameters changed are the builds' own when
the caller knows their designs, else the fixture's. The session API diffs two builds in the build
store with the store's own diff (`app.builds.diff_builds`) instead.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from app.contracts.common import BoardRef
from app.contracts.session import Build, BuildDiff
from app.exports import fixtures_dir

_REVERSED = {"added": "removed", "removed": "added"}


def _fixture(name: str) -> Any:
    return json.loads(_fixture_text(fixtures_dir() / name))


@cache
def _fixture_text(path: Path) -> str:
    return path.read_text()


def fixture_revisions() -> tuple[BoardRef, BoardRef]:
    """The older and newer board of the fixture diff."""
    old = BoardRef.model_validate(_fixture("previous-build.json")["board"])
    new = BoardRef.model_validate(_fixture("build.json")["board"])
    return old, new


def reverse(diff: dict[str, Any]) -> dict[str, Any]:
    """The same diff from `new` back to `old`: added and removed swap, moves go the other way."""
    params = [
        {
            **p,
            "change": _REVERSED.get(p["change"], p["change"]),
            "old": p["new"],
            "new": p["old"],
        }
        for p in diff["parameters"]
    ]
    features = []
    for f in diff["features"]:
        offset = f["offset"]
        features.append(
            {
                **f,
                "change": _REVERSED.get(f["change"], f["change"]),
                "offset": None if offset is None else [-v + 0.0 for v in offset],
                "old": f["new"],
                "new": f["old"],
            }
        )
    return {"old": diff["new"], "new": diff["old"], "parameters": params, "features": features}


def build_diff(
    old: Build,
    new: Build,
    designs: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> BuildDiff:
    """What changed from build `old` to build `new`. `designs` are the two builds' designs
    (`{"params": ..., "code": ...}`, as `Design.snapshot()` gives them), when known: their
    parameters are diffed, and two builds of the same design have no feature changes."""
    from geometry.diff import diff_parameters

    before, after = fixture_revisions()
    fixture: dict[str, Any] | None = None
    if (old.board, new.board) == (before, after):
        fixture = _fixture("diff.json")
    elif (old.board, new.board) == (after, before):
        fixture = reverse(_fixture("diff.json"))
    if designs is not None:
        a, b = designs
        params = diff_parameters(a["params"], b["params"])
        if not params and a.get("code") == b.get("code"):
            fixture = None  # the same design: nothing in it moved
    else:
        params = fixture["parameters"] if fixture else []
    features = fixture["features"] if fixture and old.id != new.id else []
    return BuildDiff.model_validate(
        {"old": old.id, "new": new.id, "parameters": params, "features": features}
    )
