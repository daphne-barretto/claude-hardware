"""Builds, history and diff.

The scenario comes from `demos/b-parasite/` (the revisions and differences in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import pytest

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import POSITION_TOLERANCE, parse_changes_md
from app.builds import BuildNotFound, BuildStore, default_store, diff_builds
from app.contracts.common import BoardRef
from app.standards import load_standards
from geometry import BACKENDS, enclosure_parameters, generate_enclosure, get_backend, occ
from geometry.diff import diff_features, diff_parameters, feature_summary, flatten_parameters
from geometry.interface import BuildResult, Feature
from geometry.params import CUT_KINDS, opening_tags
from geometry.report import check_report
from geometry.tessellate import tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
OLD, NEW = "old", "new"
BACKEND_DIR = Path(__file__).resolve().parents[1]


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / (CHANGES.old_file if which == OLD else CHANGES.new_file))


@cache
def built(backend: str, which: str) -> BuildResult:
    return generate_enclosure(board(which), STANDARDS, backend=backend)


def save(store: BuildStore, backend: str, which: str, parent: str | None = None):
    b = board(which)
    result = built(backend, which)
    mesh = tessellate(result)
    return store.save_result(
        result,
        code=get_backend(backend).example("enclosure"),
        parameters=enclosure_parameters(b, STANDARDS),
        board=BoardRef(name=b.name, revision=b.revision),
        checks=check_report(b, STANDARDS, result, "pending", mesh),
        files={"enclosure.gltf": json.dumps(mesh.to_gltf()).encode()},
        parent=parent,
    )


def _point(text: str) -> tuple[float, float]:
    x, y = re.findall(r"-?\d+(?:\.\d+)?", text)
    return float(x), float(y)


def _angle(text: str) -> float:
    return float(text.rstrip("°"))


# --- The diff between the enclosures for each revision: one change per difference -------------


def _row_tag(row: dict[str, str]) -> str:
    """The feature the enclosure makes for a CHANGES.md row: the cut for its opening."""
    ref, kind = row["Component"].strip("`"), row["Opening"].strip("`")
    assert kind in CUT_KINDS, f"CHANGES.md row {ref}: no enclosure feature for {kind!r}"
    which = OLD if row["Change"] == "removed" else NEW
    tags = opening_tags(board(which))
    (tag,) = {tags[o.id] for o in board(which).openings if o.component == ref and o.kind == kind}
    return tag


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_diff_lists_one_change_per_board_difference(backend: str, tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    old = save(store, backend, OLD)
    new = save(store, backend, NEW, parent=old.id)
    diff = store.diff(old.id, new.id)
    by_tag: dict[str, list[dict]] = {}
    for change in diff["features"]:
        by_tag.setdefault(change["tag"], []).append(change)

    assert CHANGES.rows
    expected = {_row_tag(row): row for row in CHANGES.rows}
    assert len(expected) == len(CHANGES.rows), "two differences map to one feature"
    for tag, row in expected.items():
        assert len(by_tag.get(tag, [])) == 1, f"{tag}: {by_tag.get(tag)}"
        (change,) = by_tag[tag]
        assert change["change"] == row["Change"], tag
        if "moved" in row["Change"]:
            (ox, oy), (nx, ny) = _point(row["Old position (mm)"]), _point(row["New position (mm)"])
            assert change["distance"] == pytest.approx(math.dist((ox, oy), (nx, ny)), abs=0.02)
            assert change["offset"][:2] == pytest.approx((nx - ox, ny - oy), abs=0.02)
        if "rotated" in row["Change"]:
            turn = (_angle(row["New rotation"]) - _angle(row["Old rotation"])) % 180
            assert change["rotation"] == pytest.approx(min(turn, 180 - turn), abs=0.5)
        if row["Change"] == "added":
            x, y = _point(row["New position (mm)"])
            assert change["new"]["centroid"][:2] == pytest.approx([x, y], abs=0.02)

    # No opening changes beyond the differences: the other changed features are the ones that
    # hold the board, which follow the clear spots on it.
    openings = set(opening_tags(board(OLD)).values()) | set(opening_tags(board(NEW)).values())
    assert {c["tag"] for c in diff["features"] if c["tag"] in openings} == set(expected)
    retention = {
        f["tag"]
        for which in (OLD, NEW)
        for kind in ("pegs", "clamps", "supports")
        for f in enclosure_parameters(board(which), STANDARDS)["retention"][kind]
    }
    assert {c["tag"] for c in diff["features"]} <= set(expected) | retention


def test_diff_lists_the_parameters_changed(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    old, new = save(store, "build123d", OLD), save(store, "build123d", NEW)
    changes = {c["name"]: c for c in store.diff(old.id, new.id)["parameters"]}
    params = enclosure_parameters(board(NEW), STANDARDS)
    for row in CHANGES.rows:
        ref, kind = row["Component"].strip("`"), row["Opening"].strip("`")
        (opening,) = [o for o in params["openings"] if o["component"] == ref and o["kind"] == kind]
        name = f"openings[{opening['id']}]"
        if row["Change"] == "added":
            assert changes[f"{name}.x"]["change"] == "added"
            continue
        if "moved" in row["Change"]:
            assert {f"{name}.x", f"{name}.y"} & changes.keys()
        if "rotated" in row["Change"]:
            assert changes[f"{name}.rotation"]["new"] == _angle(row["New rotation"])
    assert changes["source.revision"]["new"] == board(NEW).revision
    # Unchanged values are not listed.
    assert "material" not in changes


def test_diff_of_a_build_with_itself_is_empty(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    build = save(store, "build123d", OLD)
    assert store.diff(build.id, build.id) == {
        "old": build.id,
        "new": build.id,
        "parameters": [],
        "features": [],
    }


def test_diff_reverses(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    old, new = save(store, "build123d", OLD), save(store, "build123d", NEW)
    forward = {c["tag"]: c["change"] for c in store.diff(old.id, new.id)["features"]}
    back = {c["tag"]: c["change"] for c in store.diff(new.id, old.id)["features"]}
    flip = {"added": "removed", "removed": "added"}
    assert back == {tag: flip.get(change, change) for tag, change in forward.items()}


# --- Feature summaries and the diff on synthetic shapes ----------------------------------------


def _box_result(tag: str, x: float, y: float, rotation: float, w: float = 4.0) -> BuildResult:
    tool = occ.rotated(
        occ.prism([(0, 0), (w, 0), (w, 1), (0, 1)], 0, 1), (0, 0, 0), (0, 0, 1), rotation
    )
    a, b = occ.bbox(tool)
    tool = occ.translated(tool, (x - (a[0] + b[0]) / 2, y - (a[1] + b[1]) / 2, 0))
    return BuildResult("build123d", {"lid": tool}, [Feature(tag, "lid", (tool,))])


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0, 135.0])
def test_feature_summary_measures_the_long_axis(rotation: float) -> None:
    (f,) = feature_summary(_box_result("cut", 1.0, 2.0, rotation))
    assert f["centroid"] == pytest.approx([1.0, 2.0, 0.5], abs=1e-6)
    assert f["volume"] == pytest.approx(4.0)
    assert f["rotation"] == pytest.approx(rotation % 180, abs=0.01)


def test_round_shapes_have_no_rotation() -> None:
    hole = occ.cylinder(0, 0, 1.0, 0, 2)
    result = BuildResult("build123d", {"lid": hole}, [Feature("hole", "lid", (hole,))])
    assert feature_summary(result)[0]["rotation"] is None


def test_feature_diff_kinds_and_distances() -> None:
    old = feature_summary(_box_result("a", 0, 0, 0)) + feature_summary(_box_result("b", 0, 0, 0))
    new = feature_summary(_box_result("a", 3, 4, 90)) + feature_summary(
        _box_result("c", 0, 0, 0, w=6)
    )
    changes = {c["tag"]: c for c in diff_features(old, new)}
    assert changes["a"]["change"] == "moved and rotated"
    assert changes["a"]["distance"] == pytest.approx(5.0)
    assert changes["a"]["rotation"] == pytest.approx(90.0)
    assert changes["b"]["change"] == "removed"
    assert changes["c"]["change"] == "added"
    grown = diff_features(old[:1], feature_summary(_box_result("a", 0, 0, 0, w=6)))
    assert [c["change"] for c in grown] == ["resized"]
    nudged = diff_features(old[:1], feature_summary(_box_result("a", POSITION_TOLERANCE / 2, 0, 0)))
    assert nudged == []


def test_parameters_are_named_by_id_not_position() -> None:
    old = {"openings": [{"id": "a", "x": 1.0}], "wall": 2.0, "z": [0.0, 1.0]}
    new = {
        "openings": [{"id": "new", "x": 5.0}, {"id": "a", "x": 1.0}],
        "wall": 2.5,
        "z": [0.0, 1.0],
    }
    assert flatten_parameters(old) == {
        "openings[a].id": "a",
        "openings[a].x": 1.0,
        "wall": 2.0,
        "z[0]": 0.0,
        "z[1]": 1.0,
    }
    assert diff_parameters(old, new) == [
        {"name": "openings[new].id", "change": "added", "old": None, "new": "new"},
        {"name": "openings[new].x", "change": "added", "old": None, "new": 5.0},
        {"name": "wall", "change": "changed", "old": 2.0, "new": 2.5},
    ]


# --- Every build is stored: code, parameters, check results, files and parent -----------------


def _plain(store: BuildStore, parent: str | None = None, **kw):
    return store.save(
        code="def build(params):\n    return {}\n",
        parameters={"wall": 2.0},
        features=[],
        backend="build123d",
        parent=parent,
        **kw,
    )


def test_stores_everything_about_a_build(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    old = save(store, "build123d", OLD)
    new = save(store, "cadquery", NEW, parent=old.id)
    got = store.get(new.id)
    assert got == new
    assert got.parent == old.id
    assert got.backend == "cadquery"
    assert got.code == get_backend("cadquery").example("enclosure")
    assert got.parameters == json.loads(json.dumps(enclosure_parameters(board(NEW), STANDARDS)))
    assert got.board == BoardRef(name=board(NEW).name, revision=board(NEW).revision)
    assert got.checks is not None and got.checks["summary"]["untested"] >= 1
    assert [f["tag"] for f in got.features] == built("cadquery", NEW).tags
    gltf = json.loads(store.file(new.id, "enclosure.gltf").read_bytes())
    assert gltf["asset"]["version"] == "2.0"
    assert [b.id for b in store.history(new.id)] == [new.id, old.id]


def test_files_are_copied_from_paths(tmp_path: Path) -> None:
    store = BuildStore(tmp_path / "store")
    step = built("build123d", OLD).export_step(tmp_path / "enclosure.step")
    record = _plain(store, files={"enclosure.step": step})
    assert store.file(record.id, "enclosure.step").read_bytes() == step.read_bytes()
    with pytest.raises(FileNotFoundError):
        store.file(record.id, "missing.stl")


def test_lists_builds_oldest_first(tmp_path: Path) -> None:
    times = iter(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=n) for n in range(3))
    store = BuildStore(tmp_path, clock=lambda: next(times))
    first = _plain(store)
    second = _plain(store, parent=first.id)
    third = _plain(store, parent=second.id)
    assert [b.id for b in store.builds()] == [first.id, second.id, third.id]
    assert [b.id for b in store.history(third.id)] == [third.id, second.id, first.id]


def test_refuses_unknown_parents_ids_and_bad_file_names(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    with pytest.raises(BuildNotFound):
        _plain(store, parent="build-000000000000")
    with pytest.raises(BuildNotFound):
        store.get("../../etc")
    for name in ("../escape", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            _plain(store, files={name: b"x"})
    assert store.builds() == []


def test_a_failed_save_leaves_nothing_behind(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        _plain(store, files={"a.step": tmp_path / "does-not-exist"})
    assert store.builds() == []
    assert list(tmp_path.iterdir()) == []


def test_diff_works_on_stored_records(tmp_path: Path) -> None:
    store = BuildStore(tmp_path)
    old, new = save(store, "build123d", OLD), save(store, "build123d", NEW)
    assert diff_builds(store.get(old.id), store.get(new.id)) == diff_builds(old, new)


# --- Builds survive a restart when a persistent data directory is configured ------------------

RESTART = """
import json, sys
from app.builds import default_store
store = default_store()
if sys.argv[1] == "save":
    first = store.save(code="a = 1\\n", parameters={"wall": 2.0}, features=[],
                       backend="build123d", files={"note.txt": b"hello"})
    second = store.save(code="a = 2\\n", parameters={"wall": 2.5}, features=[],
                        backend="build123d", parent=first.id)
    print(json.dumps([first.id, second.id]))
else:
    ids = json.loads(sys.argv[2])
    print(json.dumps({
        "builds": [b.id for b in store.builds()],
        "code": store.get(ids[1]).code,
        "parent": store.get(ids[1]).parent,
        "file": store.file(ids[0], "note.txt").read_text(),
        "diff": store.diff(ids[0], ids[1])["parameters"],
    }))
"""


def _process(data_dir: Path, *args: str) -> str:
    env = {"DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"}
    done = subprocess.run(
        [sys.executable, "-c", RESTART, *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_builds_survive_a_restart(tmp_path: Path) -> None:
    data = tmp_path / "persistent"
    ids = json.loads(_process(data, "save"))
    after = json.loads(_process(data, "load", json.dumps(ids)))
    assert set(after["builds"]) == set(ids)
    assert after["code"] == "a = 2\n"
    assert after["parent"] == ids[0]
    assert after["file"] == "hello"
    assert after["diff"] == [{"name": "wall", "change": "changed", "old": 2.0, "new": 2.5}]
    assert (data / "builds").is_dir()


def test_default_store_is_in_the_data_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert default_store().root == tmp_path / "builds"
