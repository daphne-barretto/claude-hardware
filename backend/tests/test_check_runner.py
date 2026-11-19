"""Check runner: every check on a build, as a check-results report.

The scenario comes from `demos/b-parasite/` (the revisions and differences in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import copy
import json
from functools import cache

import jsonschema
import pytest
import yaml

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import validate_check_report
from app.contracts.common import CONTRACTS_DIR, ContractError
from app.contracts.mesh import MeshInfo, validate_mesh
from app.standards import REPO_ROOT, load_standards, parse_standards
from geometry import BACKENDS, DEFAULT_BACKEND, BuildResult, enclosure_parameters, occ
from geometry import generate_enclosure as generate
from geometry.checks import CHECKS
from geometry.params import distance_to_polygon, opening_corners, point_in_polygon
from geometry.report import UNTESTED, check_names, check_report
from geometry.tessellate import Tessellation, tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
FIT = float(STANDARDS.get("fit_tolerance").value)
CLEARANCE = float(STANDARDS.get("min_component_clearance").value)
BACKEND_NAMES = sorted(BACKENDS)
OLD, NEW = "old", "new"
BUILD_ID = "build-test"
SURFACE_EPS = 1e-3  # mm: a location's point lies on its face


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / (CHANGES.old_file if which == OLD else CHANGES.new_file))


@cache
def build(backend: str, which: str, params_from: str) -> tuple[BuildResult, Tessellation]:
    """The enclosure designed for `params_from`, on the board `which`, and its mesh."""
    params = enclosure_parameters(board(params_from), STANDARDS)
    result = generate(board(which), STANDARDS, params, backend)
    return result, tessellate(result, source={"build_id": BUILD_ID})


@cache
def report(backend: str, which: str, params_from: str) -> dict:
    result, mesh = build(backend, which, params_from)
    return check_report(board(which), STANDARDS, result, BUILD_ID, mesh)


@cache
def mesh_info(backend: str, which: str, params_from: str) -> MeshInfo:
    return validate_mesh(build(backend, which, params_from)[1].to_gltf())


def failing(rep: dict) -> list[dict]:
    return [r for r in rep["results"] if r["status"] == "fail"]


# Every case the runner is run on: each revision with its own design, and the older design on
# the newer board (the demo).
CASES = [(OLD, OLD), (NEW, NEW), (NEW, OLD)]


# --- For each difference in CHANGES.md, the old enclosure on the new board fails at least one
# --- check located at that component ------------------------------------------------------------


def _changed_rows() -> list[dict[str, str]]:
    rows = [r for r in CHANGES.rows if r["Component"].strip("`") in _refs(board(NEW))]
    assert rows, "CHANGES.md lists no component on the newer board"
    return rows


def _near_area(x: float, y: float, polygon: list[tuple[float, float]], grow: float) -> bool:
    return point_in_polygon(x, y, polygon) or distance_to_polygon(x, y, polygon) <= grow


def _refs(b: Board) -> set[str]:
    return {c.ref for c in b.components}


def _at_component(location: dict, b: Board, ref: str, opening_kind: str | None) -> bool:
    """The location's point is over the opening the board needs for the component, or (with no
    opening) over the component's body grown by the clearance standard."""
    x, y, _ = location["point"]
    for o in b.openings:
        if o.component == ref and (opening_kind is None or o.kind == opening_kind):
            polygon = opening_corners(
                {"x": o.position[0], "y": o.position[1], "rotation": o.rotation, "size": o.size}
            )
            if _near_area(x, y, polygon, FIT):
                return True
    c = b.component(ref)
    body = opening_corners(
        {"x": c.position[0], "y": c.position[1], "rotation": c.rotation, "size": c.size}
    )
    return _near_area(x, y, body, CLEARANCE)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_old_enclosure_on_new_board_fails_at_each_changed_component(backend: str) -> None:
    rep = report(backend, NEW, OLD)
    b = board(NEW)
    for row in _changed_rows():
        ref = row["Component"].strip("`")
        kind = row.get("Opening", "").strip("`") or None
        located = [
            r
            for r in failing(rep)
            if r["component"] == ref and _at_component(r["location"], b, ref, kind)
        ]
        assert located, f"no failing check located at {ref} ({row['Change']})"
        # The result also says which opening the board needs there.
        if kind:
            assert any(r["opening"] for r in located), ref


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_old_enclosure_on_new_board_fails_only_at_the_changes(backend: str) -> None:
    changed = {row["Component"].strip("`") for row in CHANGES.rows}
    assert {r["component"] for r in failing(report(backend, NEW, OLD))} == changed


# --- Passes a correct enclosure for each revision -----------------------------------------------


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_passes_the_correct_enclosure_for_each_revision(backend: str, which: str) -> None:
    rep = report(backend, which, which)
    assert not failing(rep), failing(rep)
    assert rep["summary"]["fail"] == 0
    assert rep["summary"]["pass"] > 0
    assert rep["board"] == {"name": board(which).name, "revision": board(which).revision}


# --- Every standard's check: resolves to an implemented check -----------------------------------


def test_every_standards_check_is_implemented() -> None:
    for s in STANDARDS.standards:
        assert s.check in CHECKS, f"standard {s.id} names unimplemented check {s.check}"
    assert set(check_names(STANDARDS)) >= STANDARDS.checks


@pytest.mark.parametrize("which, params_from", CASES)
def test_every_standard_has_a_result_from_its_check(which: str, params_from: str) -> None:
    rep = report(DEFAULT_BACKEND, which, params_from)
    for s in STANDARDS.standards:
        mine = [r for r in rep["results"] if r["standard"] == s.id]
        assert mine, f"no result for standard {s.id}"
        assert {r["check"] for r in mine} == {s.check}
        # The enclosure is built around a board and fits no object: an object's standard
        # (object_fit) doesn't apply to it, and says why.
        if s.applies(has_board=True):
            assert all(r["status"] in ("pass", "fail") for r in mine)
        else:
            assert all(r["status"] == "not_applicable" for r in mine)
            assert all(r["reason"] == s.not_applicable_reason() for r in mine)


def test_a_standard_naming_an_unknown_check_is_refused() -> None:
    data = yaml.safe_load((REPO_ROOT / "standards.yaml").read_text())
    data["standards"][0]["check"] = "no_such_check"
    standards = parse_standards(data)
    with pytest.raises(KeyError, match="no_such_check"):
        check_names(standards)
    result, mesh = build(DEFAULT_BACKEND, OLD, OLD)
    with pytest.raises(KeyError, match="no_such_check"):
        check_report(board(OLD), standards, result, BUILD_ID, mesh)


def test_two_standards_sharing_a_check_are_refused() -> None:
    data = yaml.safe_load((REPO_ROOT / "standards.yaml").read_text())
    twin = copy.deepcopy(data["standards"][0])
    twin["id"] = twin["id"] + "_twin"
    data["standards"].append(twin)
    result, mesh = build(DEFAULT_BACKEND, OLD, OLD)
    with pytest.raises(ValueError, match="share the check"):
        check_report(board(OLD), parse_standards(data), result, BUILD_ID, mesh)


# --- Results are JSON matching the check-result contract ----------------------------------------


@pytest.mark.parametrize("which, params_from", CASES)
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_report_follows_the_check_results_contract(
    backend: str, which: str, params_from: str
) -> None:
    rep = report(backend, which, params_from)
    data = json.loads(json.dumps(rep))  # plain JSON
    assert data == rep
    validate_check_report(data, STANDARDS, mesh_info(backend, which, params_from))
    schema = json.loads((CONTRACTS_DIR / "check-results.schema.json").read_text())
    jsonschema.validate(data, schema)
    assert data["build_id"] == BUILD_ID


@pytest.mark.parametrize("which, params_from", CASES)
def test_every_result_is_measured_limited_located_and_explained(
    which: str, params_from: str
) -> None:
    rep = report(DEFAULT_BACKEND, which, params_from)
    result, mesh = build(DEFAULT_BACKEND, which, params_from)
    faces = {f.id: (name, i) for name, part in mesh.parts.items() for i, f in enumerate(part.faces)}
    for r in rep["results"]:
        assert r["title"] and r["summary"] and r["covers"], r["id"]
        if r["status"] in ("untested", "not_applicable"):
            continue
        assert r["measured"] is not None and r["required"] is not None, r["id"]
        loc = r["location"]
        assert loc["faces"], f"{r['id']} names no face"
        # The point lies on the face it names.
        name, index = faces[loc["faces"][0]]
        assert loc["part"] == name
        face = occ.faces(result.parts[name])[index]
        d, _ = occ.distance(face, occ.vertex(tuple(loc["point"])))
        assert d <= SURFACE_EPS, f"{r['id']}: point is {d} mm off its face"


@pytest.mark.parametrize("which, params_from", CASES)
def test_strength_drop_and_heat_are_always_untested(which: str, params_from: str) -> None:
    rep = report(DEFAULT_BACKEND, which, params_from)
    untested = {r["check"]: r for r in rep["results"] if r["status"] == "untested"}
    assert set(untested) == set(UNTESTED) == {"strength", "drop", "heat"}
    for r in untested.values():
        assert r["measured"] is None and r["required"] is None and r["location"] is None
    assert rep["summary"]["untested"] == len(UNTESTED)


def test_failures_point_at_the_feature_the_check_names() -> None:
    """A failing result's face belongs to the feature the check found, when it found one."""
    rep = report(DEFAULT_BACKEND, NEW, OLD)
    _, mesh = build(DEFAULT_BACKEND, NEW, OLD)
    feature_of = {f.id: f.feature for part in mesh.parts.values() for f in part.faces}
    for r in failing(rep):
        assert feature_of[r["location"]["faces"][0]], r["id"]
    # And a broken report is caught by the contract test.
    broken = copy.deepcopy(rep)
    broken["results"][0]["location"]["faces"] = ["no_such_face"]
    with pytest.raises(ContractError, match="unknown face"):
        validate_check_report(broken, STANDARDS, mesh_info(DEFAULT_BACKEND, NEW, OLD))


# --- Limits compare with the geometry's tolerance --------------------------------------------

# The limits that apply to the enclosure built here (a design around a board, with no object).
LIMITED = [
    s
    for s in STANDARDS.standards
    if s.kind in ("min", "max") and s.check in CHECKS and s.applies(has_board=True)
]
NOISE = 0.001  # a measurement this far past a limit is float noise (a 2.999 mm wall for 3.0)
REAL = 0.05  # this far past it is a real violation


def _standards_with(standard_id: str, value: float):
    return STANDARDS.model_copy(
        update={
            "standards": [
                s.model_copy(update={"value": value}) if s.id == standard_id else s
                for s in STANDARDS.standards
            ]
        }
    )


@pytest.mark.parametrize("standard", LIMITED, ids=lambda s: s.id)
def test_a_limit_met_within_float_noise_passes_and_a_real_violation_fails(standard) -> None:
    """Set the limit just past the worst measured value: by float noise it still passes, by a
    real margin it fails."""
    from geometry.checks import CheckContext

    result, _ = build(DEFAULT_BACKEND, NEW, NEW)
    measured = [
        float(r.measured)
        for r in CHECKS[standard.check](CheckContext(board(NEW), STANDARDS, result))
        if isinstance(r.measured, int | float)
    ]
    assert measured, standard.id
    worst = min(measured) if standard.kind == "min" else max(measured)
    sign = 1 if standard.kind == "min" else -1
    for past, expected in ((NOISE, "pass"), (REAL, "fail")):
        limit = worst + sign * past
        results = CHECKS[standard.check](
            CheckContext(board(NEW), _standards_with(standard.id, limit), result)
        )
        statuses = {r.status for r in results if isinstance(r.measured, int | float)}
        if expected == "pass":
            assert statuses == {"pass"}, (standard.id, limit, worst)
        else:
            assert "fail" in statuses, (standard.id, limit, worst)
