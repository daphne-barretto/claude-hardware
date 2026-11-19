"""Conflict detection: locks, standards and the board that can't all be met.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension. Expected values are worked out here
from the board and the standards, independently of the detector's linear program.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from functools import cache

import pytest
import yaml

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.mesh import validate_mesh
from app.examples import example_standards
from app.parameters import ParameterStore
from app.standards import REPO_ROOT, StandardsFile, parse_standards
from geometry import enclosure_parameters, generate_enclosure
from geometry.conflicts import conflict_locks, detect_conflicts
from geometry.tessellate import tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
REVISIONS = [CHANGES.old_file, CHANGES.new_file]
STANDARDS = example_standards()
MIN_WALL = float(STANDARDS.get("min_wall").value)
CLEARANCE = float(STANDARDS.get("min_component_clearance").value)
MAX_HEIGHT = float(STANDARDS.get("max_height").value)


@cache
def board(name: str) -> Board:
    return load_board(BOARDS_DIR / name)


def params(name: str) -> dict:
    return enclosure_parameters(board(name), STANDARDS)


def inside(c, p: dict) -> bool:
    (x0, x1), (y0, y1) = p["footprint"]["x"], p["footprint"]["y"]
    return x0 < c.position[0] < x1 and y0 < c.position[1] < y1


def tallest_top(name: str):
    p = params(name)
    parts = [c for c in board(name).components if c.side == "top" and inside(c, p)]
    return max(parts, key=lambda c: c.height)


def with_standard(standard_id: str, value: float) -> StandardsFile:
    data = yaml.safe_load((REPO_ROOT / "standards.yaml").read_text())
    data = copy.deepcopy(data)
    for s in data["standards"]:
        if s["id"] == standard_id:
            s["value"] = value
    return parse_standards(data)


def by_id(conflict) -> dict:
    return {(c.kind, c.id): c for c in conflict.constraints}


# --- A lock that clashes with the clearance standard and the height limit ----------------------


@pytest.mark.parametrize("name", REVISIONS)
def test_the_fixture_returns_the_clashing_constraints_their_locations_and_best_values(name):
    b, p = board(name), params(name)
    locks = conflict_locks(b, STANDARDS, p)
    ((path, locked),) = locks.items()
    assert path == "floor_top"

    (conflict,) = detect_conflicts(b, STANDARDS, p, locks)
    ids = by_id(conflict)
    part = tallest_top(name)

    # Which constraints clash: the lock, the clearance standard, the height limit (and the
    # minimum wall of roof and floor), squeezing the tallest top-side part.
    assert set(ids) == {
        ("locked", path),
        ("standard", "min_component_clearance"),
        ("standard", "max_height"),
        ("standard", "min_wall"),
        ("board", part.ref),
    }

    # By how much: the lowest floor the stack allows, worked out by hand.
    roof_top = b.pcb.thickness + part.height + CLEARANCE + MIN_WALL
    lowest_floor = roof_top - MAX_HEIGHT + MIN_WALL
    short = lowest_floor - locked
    assert short > 0
    assert conflict.over_by == pytest.approx(short, abs=1e-3)

    # Best achievable value of each, when only it gives way.
    assert ids[("locked", path)].best == pytest.approx(lowest_floor, abs=1e-3)
    assert ids[("standard", "min_component_clearance")].best == pytest.approx(
        CLEARANCE - short, abs=1e-3
    )
    assert ids[("standard", "max_height")].best == pytest.approx(MAX_HEIGHT + short, abs=1e-3)
    room = MAX_HEIGHT - (b.pcb.thickness + part.height + CLEARANCE) + locked
    assert ids[("standard", "min_wall")].best == pytest.approx(room / 2, abs=1e-3)
    assert ids[("board", part.ref)].best is None

    # Where on the model: the clearance acts at the part, under the lid, and so does the
    # conflict as a whole; the lock acts on the base.
    (at_part,) = ids[("standard", "min_component_clearance")].locations
    assert at_part["component"] == part.ref and at_part["part"] == "lid"
    assert at_part["point"][:2] == pytest.approx(list(part.position))
    assert conflict.location["component"] == part.ref
    assert ids[("locked", path)].locations[0]["part"] == "base"
    assert {loc["part"] for loc in ids[("standard", "min_wall")].locations} == {"lid", "base"}
    assert path in conflict.message and part.ref in conflict.message


@pytest.mark.parametrize("name", REVISIONS)
def test_each_best_value_resolves_the_conflict_on_its_own(name):
    b, p = board(name), params(name)
    locks = conflict_locks(b, STANDARDS, p)
    (conflict,) = detect_conflicts(b, STANDARDS, p, locks)
    for c in conflict.constraints:
        if c.kind == "locked":
            assert detect_conflicts(b, STANDARDS, p, {**locks, c.id: c.best}) == []
        elif c.kind == "standard":
            assert detect_conflicts(b, with_standard(c.id, c.best), p, locks) == []


def test_the_locations_name_faces_of_the_built_model():
    name = CHANGES.new_file
    b, p = board(name), params(name)
    locks = conflict_locks(b, STANDARDS, p)
    result = generate_enclosure(b, STANDARDS, {**p, **locks})
    mesh = tessellate(result)
    info = validate_mesh(mesh.to_gltf())

    (conflict,) = detect_conflicts(b, STANDARDS, p, locks, result, mesh)
    locations = [conflict.location] + [loc for c in conflict.constraints for loc in c.locations]
    for loc in locations:
        (face,) = loc["faces"]
        assert info.face_parts[face] == loc["part"]
    part = tallest_top(name)
    (at_part,) = by_id(conflict)[("standard", "min_component_clearance")].locations
    # On the lid's ceiling, right above the part.
    assert at_part["point"][:2] == pytest.approx(list(part.position), abs=1e-3)
    assert at_part["point"][2] == pytest.approx(p["ceiling"], abs=1e-3)


# --- Satisfiable constraints -------------------------------------------------------------------


@pytest.mark.parametrize("name", REVISIONS)
def test_no_conflict_without_locks(name):
    assert detect_conflicts(board(name), STANDARDS, params(name), {}) == []


@pytest.mark.parametrize("name", REVISIONS)
def test_no_conflict_when_the_locks_can_be_met(name):
    b, p = board(name), params(name)
    # Every stack parameter locked at the design's own value.
    keys = ("wall", "z_top", "z_bottom", "ceiling", "floor_top")
    assert detect_conflicts(b, STANDARDS, p, {k: p[k] for k in keys}) == []
    # The fixture's lock moved to the limit it can reach, from the parameter store.
    (conflict,) = detect_conflicts(b, STANDARDS, p, conflict_locks(b, STANDARDS, p))
    best = by_id(conflict)[("locked", "floor_top")].best
    t = datetime(2026, 1, 1, tzinfo=UTC)
    store = ParameterStore(clock=lambda: t)
    store.set("floor_top", best, by="Demo user", by_hand=True)
    store.set("wall", p["wall"] + 1.0, by="Demo user", by_hand=True)
    assert detect_conflicts(b, STANDARDS, p, store) == []


def test_unlocked_values_are_free_to_move():
    """A value the agent set (not locked) is no constraint, however far off it is."""
    name = CHANGES.new_file
    b, p = board(name), params(name)
    store = ParameterStore()
    store.set("floor_top", conflict_locks(b, STANDARDS, p)["floor_top"], by="agent")
    assert detect_conflicts(b, STANDARDS, p, store) == []
