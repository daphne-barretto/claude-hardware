"""Enclosure generator with named features.

The scenario comes from `demos/b-parasite/` (the two revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.examples import example_standards
from geometry import (
    BACKENDS,
    DEFAULT_BACKEND,
    BuildError,
    BuildResult,
    enclosure_parameters,
    generate_enclosure,
    get_backend,
    occ,
)
from geometry.checks import ASSEMBLY_CHECKS, BOARD_CHECKS, CHECKS, CheckResult, run_checks
from geometry.params import CUT_KINDS, THICK_FACTOR, case_in_board_axes, opening_tags

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = example_standards()
BACKEND_NAMES = sorted(BACKENDS)
OLD, NEW = "old", "new"


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / (CHANGES.old_file if which == OLD else CHANGES.new_file))


@cache
def build(backend: str, which: str, params_from: str | None = None) -> BuildResult:
    """Build the enclosure for a revision, with its own parameters or another revision's."""
    params = enclosure_parameters(board(params_from), STANDARDS) if params_from else None
    return generate_enclosure(board(which), STANDARDS, params, backend)


@cache
def checks(backend: str, which: str, params_from: str | None = None) -> list[CheckResult]:
    return run_checks(board(which), STANDARDS, build(backend, which, params_from))


def failing(results: list[CheckResult]) -> list[CheckResult]:
    """The results that don't pass. A standard for other designs (`object_fit`: the enclosure
    fits no object) is not applicable, which is not a failure."""
    na = [r for r in results if r.status == "not_applicable"]
    by_check = {st.check: st for st in STANDARDS.standards}
    assert all(not by_check[r.check].applies(has_board=True) for r in na), na
    return [r for r in results if r.status not in ("pass", "not_applicable")]


def changed_components() -> set[str]:
    return {row["Component"].strip("`") for row in CHANGES.rows}


# --- Generates a valid closed solid for both revisions with both backends ----------------------


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_generates_a_valid_closed_solid(backend: str, which: str) -> None:
    result = build(backend, which)
    assert result.backend == backend
    assert set(result.enclosure) == {"base", "lid"}
    for name, shape in result.parts.items():
        assert occ.is_closed_valid_solid(shape), f"{name} is not one valid closed solid"


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_step_export_round_trips(backend: str, tmp_path: Path) -> None:
    from build123d import import_step

    result = build(backend, NEW)
    path = result.export_step(tmp_path / "enclosure.step")
    imported = import_step(path)
    solids = imported.solids()
    assert len(solids) == len(result.parts)
    assert all(s.is_valid for s in solids)
    want = sorted(occ.volume(s) for s in result.parts.values())
    got = sorted(s.volume for s in solids)
    assert got == pytest.approx(want, rel=1e-6)
    labels = {c.label for c in imported.children} if imported.children else set()
    assert labels == set(result.parts)


@pytest.mark.parametrize("which", [OLD, NEW])
def test_backends_build_the_same_enclosure(which: str) -> None:
    a, b = (build(name, which) for name in BACKEND_NAMES)
    for part in a.parts:
        assert occ.volume(a.parts[part]) == pytest.approx(occ.volume(b.parts[part]), rel=1e-6)
        flat = [[v for corner in occ.bbox(r.parts[part]) for v in corner] for r in (a, b)]
        assert flat[0] == pytest.approx(flat[1], abs=1e-5)


def test_generator_starts_from_the_existing_case() -> None:
    b = board(OLD)
    params = enclosure_parameters(b, STANDARDS)
    case = case_in_board_axes(b)
    assert [params["z_bottom"], params["z_top"]] == pytest.approx(case["z"])
    assert params["footprint"]["y"] == pytest.approx(case["y"])
    # It only grows sideways, where the corner bosses need room.
    assert params["footprint"]["x"][0] <= case["x"][0]
    assert params["footprint"]["x"][1] >= case["x"][1]


# --- Each face reports the feature that created it ---------------------------------------------


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_every_face_reports_its_feature(backend: str, which: str) -> None:
    result = build(backend, which)
    for part, shape in result.parts.items():
        refs = result.face_refs[part]
        assert len(refs) == len(occ.faces(shape))
        unknown = [r.index for r in refs if r.feature == "unknown"]
        assert not unknown, f"{part} faces {unknown} have no feature"
        assert {r.feature for r in refs} <= {f.tag for f in result.features if f.part == part}
    # Every feature left at least one face behind.
    for tag in result.tags:
        assert result.faces_of(tag), f"feature {tag} has no faces"


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_feature_tags_come_from_the_board(backend: str, which: str) -> None:
    b = board(which)
    result = build(backend, which)
    tags = set(result.tags)
    expected = {f"opening_{o.component}" for o in b.openings if o.kind in CUT_KINDS}
    assert expected == set(opening_tags(b).values())
    assert expected <= tags
    bosses = sorted(t for t in tags if t.startswith("boss_"))
    assert bosses and all(re.fullmatch(r"boss_\d+", t) for t in bosses)
    for boss in bosses:
        n = boss.removeprefix("boss_")
        assert {f"insert_{n}", f"screw_{n}"} <= tags
    assert {"base", "lid", "opening_pcb"} <= tags


@pytest.mark.parametrize("which", [OLD, NEW])
def test_face_tags_agree_across_backends(which: str) -> None:
    counts = [
        Counter(r.feature for refs in build(name, which).face_refs.values() for r in refs)
        for name in BACKEND_NAMES
    ]
    assert all(c == counts[0] for c in counts)


def test_opening_faces_sit_at_their_component() -> None:
    b = board(NEW)
    result = build(DEFAULT_BACKEND, NEW)
    for o in b.openings:
        if o.kind not in CUT_KINDS:
            continue
        faces = result.faces_of(f"opening_{o.component}")
        (x0, y0, _), (x1, y1, _) = occ.bbox(faces[0][1])
        for _, face in faces[1:]:
            (a0, b0, _), (a1, b1, _) = occ.bbox(face)
            x0, y0, x1, y1 = min(x0, a0), min(y0, b0), max(x1, a1), max(y1, b1)
        assert x0 <= o.position[0] <= x1 and y0 <= o.position[1] <= y1, o.id


# --- Checks: the older revision passes; the newer one with old parameters fails exactly at the
# --- differences in demos/b-parasite/CHANGES.md ---------------------------------------------------


def test_every_standard_check_is_implemented() -> None:
    for s in STANDARDS.standards:
        assert s.check in CHECKS, f"standard {s.id} names unimplemented check {s.check}"
    assert set(BOARD_CHECKS) <= set(CHECKS)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_older_revision_passes_every_check(backend: str) -> None:
    results = checks(backend, OLD)
    assert {r.check for r in results} == STANDARDS.checks | set(BOARD_CHECKS) | set(ASSEMBLY_CHECKS)
    failed = failing(results)
    assert not failed, failed


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_newer_revision_passes_with_its_own_parameters(backend: str) -> None:
    failed = failing(checks(backend, NEW))
    assert not failed, failed


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_newer_revision_with_old_parameters_fails_exactly_at_the_changes(backend: str) -> None:
    results = checks(backend, NEW, OLD)
    failed = failing(results)
    assert failed
    assert all(r.component for r in failed), [r for r in failed if not r.component]
    assert {r.component for r in failed} == changed_components()
    # And every standard's own check still passes away from the changes.
    unaffected = {r.check for r in results} - {r.check for r in failed}
    assert {"overall_height", "material", "wall_thickness", "draft_angle"} <= unaffected


# --- The checks catch what they claim to --------------------------------------------------------


def _mutated(change) -> dict:
    params = copy.deepcopy(enclosure_parameters(board(OLD), STANDARDS))
    change(params)
    return params


def _thin_wall(p: dict) -> None:
    p["wall"] = float(STANDARDS.get("min_wall").value) / 2
    p["floor_top"] = p["z_bottom"] + p["wall"]


def _no_draft(p: dict) -> None:
    p["draft"] = 0.0


def _too_tall(p: dict) -> None:
    p["z_top"] += 1.0


def _wrong_material(p: dict) -> None:
    p["material"] = "PLA"


def _small_pocket(p: dict) -> None:
    p["boss"]["insert_diameter"] -= 1.0


def _low_ceiling(p: dict) -> None:
    p["ceiling"] = p["pcb_thickness"] + 0.1


def _no_board_slot(p: dict) -> None:
    p["slots"] = []


def _boss_over_antenna(p: dict) -> None:
    keepout = next(o for o in p["openings"] if o["kind"] == "antenna_keepout")
    p["bosses"][0].update(x=keepout["x"], y=keepout["y"])


def _no_retention(p: dict) -> None:
    p["retention"].update(pegs=[], clamps=[], supports=[])


def _peg_too_wide(p: dict) -> None:
    p["retention"]["pegs"][0]["pin_radius"] += 2 * p["retention"]["gap"]


def _oversized_opening(p: dict) -> None:
    o = next(o for o in p["openings"] if o["cut"])
    o["size"] = [s + 1.0 for s in o["size"]]


@pytest.mark.parametrize(
    "change, check",
    [
        (_thin_wall, "wall_thickness"),
        (_no_draft, "draft_angle"),
        (_too_tall, "overall_height"),
        (_wrong_material, "material"),
        (_small_pocket, "insert_pockets"),
        (_low_ceiling, "component_clearance"),
        (_no_board_slot, "board_fit"),
        (_boss_over_antenna, "antenna_keepout"),
        (_oversized_opening, "fit_tolerance"),
        (_no_retention, "board_retention"),
        (_peg_too_wide, "board_fit"),
    ],
)
def test_checks_catch_violations(change, check: str) -> None:
    b = board(OLD)
    result = generate_enclosure(b, STANDARDS, _mutated(change), DEFAULT_BACKEND)
    results = run_checks(b, STANDARDS, result, [check])
    assert any(not r.passed for r in results), results


# --- The board is held: pegs through the mounting holes, a clamp and a support ----------------


RETENTION_TAGS = ("peg_", "clamp_", "support_")


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_board_cannot_move_in_the_closed_enclosure(backend: str, which: str) -> None:
    results = [r for r in checks(backend, which) if r.check == "board_retention"]
    # Each of the four edges is pushed out, lifted and pressed down.
    assert len(results) == 12
    assert len({r.detail.split(":")[0] for r in results}) == 12
    failed = [r for r in results if not r.passed]
    assert not failed, failed
    assert all(r.feature for r in results), results
    # The new features do the holding: every edge's lift and press away from the slot is
    # stopped by one of them, not only by the shells.
    stoppers = {r.feature for r in results}
    assert any(t.startswith("peg_") for t in stoppers)
    assert any(t.startswith("clamp_") for t in stoppers)
    assert any(t.startswith("support_") for t in stoppers)


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_retention_features_are_named_from_the_board(backend: str, which: str) -> None:
    b = board(which)
    result = build(backend, which)
    tags = set(result.tags)
    # A peg through every mounting hole, named after the hole.
    assert {f"peg_{h.id}" for h in b.mounting_holes} <= tags
    new = sorted(t for t in tags if t.startswith(RETENTION_TAGS))
    assert {t.split("_")[0] for t in new} == {"peg", "clamp", "support"}
    for tag in new:
        assert re.fullmatch(r"peg_.+|clamp_\d+|support_\d+", tag)
        assert result.faces_of(tag), f"{tag} has no faces"
    # Every face of the new features reports its feature (no face is left unknown).
    for part, refs in result.face_refs.items():
        assert all(r.feature != "unknown" for r in refs), part


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_pegs_go_into_the_mounting_holes(backend: str) -> None:
    b = board(NEW)
    result = build(backend, NEW)
    t = b.pcb.thickness
    for h in b.mounting_holes:
        in_hole = occ.cylinder(*h.position, h.diameter / 2, 0.0, t)
        assert occ.overlap(result.parts["lid"], in_hole) > 0, f"no peg in {h.id}"


def test_board_fit_sees_the_mounting_holes() -> None:
    """A pin in a hole is not enclosure material inside the board."""
    results = [r for r in checks(DEFAULT_BACKEND, NEW) if r.check == "board_fit"]
    assert results and all(r.passed for r in results)


@pytest.mark.parametrize("which", [OLD, NEW])
@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_board_fits_with_the_slimmer_posts(backend: str, which: str) -> None:
    # The posts stay under the thick-section limit and the board still fits.
    results = [r for r in checks(backend, which) if r.check == "board_fit"]
    assert results
    assert all(r.passed for r in results), [r for r in results if not r.passed]


@pytest.mark.parametrize("which", [OLD, NEW])
def test_posts_press_on_the_board_and_stay_under_the_thick_limit(which: str) -> None:
    p = enclosure_parameters(board(which), STANDARDS)
    holes = {h.id: h for h in board(which).mounting_holes}
    fit = float(STANDARDS.get("fit_tolerance").value)
    limit = THICK_FACTOR * float(STANDARDS.get("min_wall").value)
    lid_top = p["ceiling"] + p["wall"] / 2
    post_bottom = p["pcb_thickness"] + p["retention"]["gap"]
    widen = (lid_top - post_bottom) * math.tan(math.radians(p["draft"]))
    assert p["retention"]["pegs"]
    for peg in p["retention"]["pegs"]:
        # Still a post wider than the hole, so it presses on the board's top face...
        assert peg["post_radius"] is not None, peg["tag"]
        assert peg["post_radius"] >= holes[peg["hole"]].diameter / 2 + fit
        # ...and no wider than the thick-section limit anywhere up to the lid.
        assert 2 * (peg["post_radius"] + widen) <= limit + 1e-9


def test_unheld_board_fails_at_the_far_end() -> None:
    """Without the new features only the slot holds the board, so its far end is loose."""
    b = board(NEW)
    params = copy.deepcopy(enclosure_parameters(b, STANDARDS))
    _no_retention(params)
    result = generate_enclosure(b, STANDARDS, params, DEFAULT_BACKEND)
    failed = [r for r in run_checks(b, STANDARDS, result, ["board_retention"]) if not r.passed]
    assert any("lifted" in r.detail for r in failed), failed
    assert any("pressed down" in r.detail for r in failed), failed
    assert any("pushed out" in r.detail for r in failed), failed


# --- Swappable backend --------------------------------------------------------------------------


def test_backend_is_chosen_per_run_or_per_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEOMETRY_BACKEND", raising=False)
    assert get_backend().name == DEFAULT_BACKEND == "build123d"
    for name in BACKEND_NAMES:
        assert get_backend(name).name == name
        monkeypatch.setenv("GEOMETRY_BACKEND", name)
        assert get_backend().name == name
    with pytest.raises(ValueError, match="unknown geometry backend"):
        get_backend("nope")


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_each_backend_has_its_own_prompt_and_examples(backend: str) -> None:
    engine = get_backend(backend)
    prompt = engine.prompt()
    assert backend.lower() in prompt.lower() and "build(params" in prompt
    examples = engine.examples()
    assert "enclosure" in examples
    assert re.search(rf"^import {backend}|^from {backend} import", examples["enclosure"], re.M)
    others = set(BACKEND_NAMES) - {backend}
    for other in others:
        assert not re.search(rf"^\s*(import|from) {other}\b", examples["enclosure"], re.M)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_build_errors_report_the_line(backend: str) -> None:
    engine = get_backend(backend)
    code = "def build(params):\n    x = 1\n    return params['missing']\n"
    with pytest.raises(BuildError) as error:
        engine.build(code, {})
    assert error.value.line == 3
    with pytest.raises(BuildError) as error:
        engine.build("def build(params)\n", {})
    assert error.value.line == 1


def test_generator_code_hardcodes_no_scenario() -> None:
    """The generator code takes every board detail from the parameters."""
    words = set()
    for which in (OLD, NEW):
        b = board(which)
        words |= {b.name, *(c.ref for c in b.components), *(h.id for h in b.mounting_holes)}
    sources = Path(__file__).resolve().parents[1] / "geometry"
    for path in sources.rglob("*"):
        if path.suffix not in (".py", ".md"):
            continue
        text = path.read_text()
        found = {w for w in words if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text)}
        assert not found, f"{path.name} names {sorted(found)}"
