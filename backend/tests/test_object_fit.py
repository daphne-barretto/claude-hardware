"""The object_fit standard: a part made to fit an object (the design's `object`
parameters) takes up none of the object's volume and comes within the standard's gap of it,
by exact solid booleans. It applies only to designs that fit an object (`applies_to: object`);
the rest report it not applicable, with the reason.
"""

from __future__ import annotations

import pytest

from app.contracts.checks import validate_check_report
from app.standards import load_standards
from geometry import occ
from geometry.checks import run_checks
from geometry.fit_object import FitObject
from geometry.interface import BuildResult
from geometry.report import check_report

STANDARDS = load_standards()
GAP = float(STANDARDS.get("object_fit").value)
OBJECT = {"shape": "box", "width": 40, "depth": 30, "height": 20}


def cup(clearance: float, wall: float = 2.0, floor: float = 2.0) -> BuildResult:
    """A tray around the object (a box at the origin, its bottom on z = 0) with `clearance` all
    round: a block with a pocket the object's size plus the clearance."""
    w, d = OBJECT["width"] / 2 + clearance, OBJECT["depth"] / 2 + clearance
    ow, od = w + wall, d + wall
    outer = [(-ow, -od), (ow, -od), (ow, od), (-ow, od)]
    pocket = [(-w, -d), (w, -d), (w, d), (-w, d)]
    block = occ.prism(outer, -floor - clearance, 10)
    tray = occ.cut(block, occ.prism(pocket, -clearance, 11))
    return BuildResult("build123d", {"tray": tray}, [])


def fit(params: dict | None = None) -> FitObject:
    found = FitObject.from_params({"object": {**OBJECT, **(params or {})}})
    assert found is not None
    return found


def object_fit(result: BuildResult, fit_object: FitObject | None):
    return run_checks(None, STANDARDS, result, ["object_fit"], fit_object)


def test_the_standard_exists_with_its_check_and_applies_only_to_object_designs():
    standard = STANDARDS.get("object_fit")
    assert standard.check == "object_fit" and standard.applies_to == "object"
    assert standard.applies(has_board=False, has_object=True)
    assert not standard.applies(has_board=True, has_object=False)
    assert "fit an object" in standard.not_applicable_reason()


def test_the_object_comes_from_the_design_parameters():
    assert FitObject.from_params({}) is None
    assert FitObject.from_params({"object": {"width": 1, "depth": 1}}) is None
    assert FitObject.from_params({"object": {"width": 1, "depth": 1, "height": -2}}) is None
    found = FitObject.from_params(
        {"object": {"shape": "cylinder", "width": 80, "depth": 80, "height": 95, "at": [1, 2, 3]}}
    )
    assert found == FitObject("cylinder", 80.0, 80.0, 95.0, (1.0, 2.0, 3.0))
    odd = FitObject.from_params({"object": {"shape": "other", **OBJECT, "at": "here"}})
    assert odd is not None and odd.shape == "box" and odd.at == (0.0, 0.0, 0.0)


def test_a_snug_part_passes_with_its_gap():
    (result,) = object_fit(cup(0.3), fit())
    assert result.status == "pass", result
    assert result.measured == pytest.approx(0.3, abs=1e-3) and result.limit == GAP
    assert result.part == "tray"


def test_a_part_too_far_from_the_object_fails():
    (result,) = object_fit(cup(GAP + 2), fit())
    assert result.status == "fail" and result.measured == pytest.approx(GAP + 2, abs=1e-3)


def test_a_part_that_cuts_into_the_object_fails_where_it_does():
    (result,) = object_fit(cup(0.3), fit({"width": 46}))  # wider than the pocket
    assert result.status == "fail" and result.measured == "cuts into the object"
    assert result.part == "tray" and "mm^3" in result.detail
    assert 0 <= result.point[2] <= 10  # where the walls meet the object, below their top


def test_a_cylinder_object_sitting_in_its_pocket():
    radius = 10
    block = occ.prism([(-15, -15), (15, -15), (15, 15), (-15, 15)], -2, 8)
    tray = occ.cut(block, occ.cylinder(0, 0, radius + 0.5, 0, 9))
    result = BuildResult("build123d", {"tray": tray}, [])
    obj = fit({"shape": "cylinder", "width": 2 * radius, "depth": 2 * radius, "height": 30})
    (check,) = object_fit(result, obj)
    assert check.status == "pass" and check.measured == pytest.approx(0.0, abs=1e-3)


def test_a_design_without_an_object_reports_it_not_applicable():
    (result,) = object_fit(cup(0.3), None)
    assert result.status == "not_applicable" and "fit an object" in result.detail


def test_the_report_carries_object_fit_in_the_contract():
    with_object = check_report(None, STANDARDS, cup(0.3), "build-1", fit_object=fit())
    validate_check_report(with_object, standards=STANDARDS)
    (row,) = [r for r in with_object["results"] if r["standard"] == "object_fit"]
    assert row["status"] == "pass"
    assert row["required"] == {"kind": "max", "value": GAP, "unit": "mm"}
    assert row["location"] is not None and row["location"]["part"] == "tray"

    without = check_report(None, STANDARDS, cup(0.3), "build-2")
    validate_check_report(without, standards=STANDARDS)
    (row,) = [r for r in without["results"] if r["standard"] == "object_fit"]
    assert row["status"] == "not_applicable" and "fit an object" in row["reason"]


def test_a_build_with_object_parameters_is_checked_against_the_object():
    """The build path (`make_artifacts`, as the geometry worker runs it) fits the part to the
    object in the design's parameters."""
    from app.agent.services import make_artifacts

    params = {"object": OBJECT, "wall": 2}
    report = make_artifacts(None, STANDARDS, cup(0.3), "build-3", None, params).report
    (row,) = [r for r in report["results"] if r["standard"] == "object_fit"]
    assert row["status"] == "pass"
    report = make_artifacts(None, STANDARDS, cup(0.3), "build-4", None, {"wall": 2}).report
    (row,) = [r for r in report["results"] if r["standard"] == "object_fit"]
    assert row["status"] == "not_applicable"
