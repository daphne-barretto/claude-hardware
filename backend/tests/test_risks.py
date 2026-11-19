"""Claude flags problems: risks the checks don't block, named in chat with a location and
a fix that can be accepted on its own.

The risks come from the contract fixtures (`fixtures/maps/`, `fixtures/checks.json`) and the
standards (`standards.yaml`); nothing about the scenario is written here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from app.agent.design import get_path, set_path
from app.agent.events import EventSink
from app.agent.risks import NEAR, Flags, find_risks
from app.agent.run import RunConfig, run_agent, scenario_workspace
from app.agent.scripted import ScriptedDriver
from app.agent.services import (
    FIXTURES_DIR,
    BuildOutcome,
    CheckItem,
    FixtureServices,
    items_from_report,
    make_build,
)
from app.contracts.changes import RiskFlags
from app.contracts.maps import MAP_TYPES
from app.examples import example_standards
from app.parameters import LockedParameterError

STANDARDS = example_standards()
MAPS = [json.loads(p.read_text()) for p in sorted((FIXTURES_DIR / "maps").glob("*.json"))]
FIX_PATH = "boss.radius"  # the fixture's thick spots go away when this changes


def thick_spots() -> list[dict]:
    return [s for m in MAPS for s in m["flagged"] if s["kind"] == "thick"]


def feature_of(spot: dict) -> str:
    return spot["location"]["faces"][0].split(".")[0]


@dataclass
class RiskFixtureServices(FixtureServices):
    """The contract fixtures, with the design kept on the build: the analysis fixture's thick
    spots are there until `boss.radius` changes."""

    built: list = field(default_factory=list)
    original: float | None = None

    def build(self, design):
        outcome = super().build(design)
        outcome.shapes = design
        self.built.append(design)
        return outcome

    def analyze(self, outcome):
        spots = super().analyze(outcome)
        if get_path(outcome.shapes["params"], FIX_PATH) != self.original:
            spots = [s for s in spots if s["kind"] != "thick"]
        return spots


def fixture_workspace():
    sink = EventSink("msg-2")
    ws = scenario_workspace(sink=sink, services=FixtureServices())
    ws.services = RiskFixtureServices(original=get_path(ws.design.params, FIX_PATH))
    return ws, sink


def call(ws, tool: str, args: dict | None = None):
    result = asyncio.run(getattr(ws, tool)(args or {}))
    text = "".join(c["text"] for c in result["content"])
    return text, bool(result.get("is_error"))


def chat(sink: EventSink) -> str:
    return "".join(e.delta for e in sink.of_type("text"))


def thick_fix(ws, risk_id: str) -> dict:
    radius = get_path(ws.design.params, FIX_PATH)
    return {
        "risk": risk_id,
        "fix": "Slim the bosses",
        "changes": [{"name": FIX_PATH, "value": radius - 0.5}],
    }


# --- Finding and naming risks -----------------------------------------------------------------


def test_a_known_risk_in_the_analysis_fixture_is_named_in_chat_with_a_location():
    ws, sink = fixture_workspace()
    report = json.loads(call(ws, "run_checks")[0])
    ids = {r["id"] for r in report["risks"]}
    features = {feature_of(s) for s in thick_spots()}
    assert features and {f"thick:{f}" for f in features} <= ids

    spot = thick_spots()[0]
    risk_id = f"thick:{feature_of(spot)}"
    text, error = call(ws, "flag_risk", thick_fix(ws, risk_id))
    assert not error, text

    said = chat(sink)
    lines = [line for line in said.splitlines() if line.startswith("Risk:")]
    assert len(lines) == 1  # one line per risk
    line = lines[0]
    location = spot["location"]
    assert feature_of(spot) in line and location["part"] in line
    assert "(" + ", ".join(f"{c:.1f}" for c in location["point"]) + ")" in line
    assert "Slim the bosses" in line
    assert json.loads(text)["flagged"] == risk_id


def test_analysis_spots_a_failing_check_covers_are_not_risks():
    """Low draft that the draft check already fails on is blocked by the checks, not a risk."""
    report = items_from_report(json.loads((FIXTURES_DIR / "checks.json").read_text()))
    analysis = FixtureServices().analyze(BuildOutcome(None))
    risks = {r.id for r in find_risks(report, analysis, STANDARDS)}
    for spot in analysis:
        check = STANDARDS.get(MAP_TYPES[spot["map"]][0]).check
        failing = any(i.check == check and i.status == "fail" for i in report)
        if spot["kind"] != "thick" and failing:
            assert f"{spot['kind']}:{spot['faces'][0].split('.')[0]}" not in risks


def test_a_check_passing_within_the_measuring_tolerance_is_right_at_its_limit():
    """A wall measured 0.001 under its minimum passes (`geometry.checks` tolerance): it is a
    risk right at the limit, not dropped."""
    for standard in (s for s in STANDARDS.standards if s.kind in ("min", "max")):
        limit = float(standard.value)
        measured = limit - 0.001 if standard.kind == "min" else limit + 0.001
        item = CheckItem(
            f"{standard.check}:x",
            standard.check,
            "pass",
            standard=standard.id,
            measured=measured,
            required=limit,
            unit=standard.unit,
        )
        (risk,) = find_risks([item], [], STANDARDS)
        assert (risk.kind, risk.margin) == ("near_limit", 0.0) and "right at" in risk.what


def test_a_passing_check_close_to_its_limit_is_a_risk():
    report = items_from_report(json.loads((FIXTURES_DIR / "checks.json").read_text()))
    risks = {r.id: r for r in find_risks(report, [], STANDARDS)}
    expected = set()
    for item in report:
        if item.status != "pass" or not item.standard:
            continue
        standard = STANDARDS.get(item.standard)
        if standard.kind not in ("min", "max") or not isinstance(item.measured, float | int):
            continue
        margin = item.measured - item.required
        margin = margin if standard.kind == "min" else -margin
        if margin <= NEAR * abs(item.required):
            expected.add(f"near_limit:{item.id}")
    assert expected and set(risks) == expected
    for risk_id in expected:
        assert risks[risk_id].standard and risks[risk_id].message.endswith(".")


# --- The fix, on its own ----------------------------------------------------------------------


def test_each_flagged_risk_comes_with_a_fix_that_can_be_accepted_on_its_own():
    ws, sink = fixture_workspace()
    call(ws, "run_checks")
    before = get_path(ws.design.params, FIX_PATH)
    thick = [r.id for r in ws.risks if r.kind == "thick"]
    near = next(r for r in ws.risks if r.kind == "near_limit" and r.standard == "max_height")
    for risk_id in thick:
        call(ws, "flag_risk", {**thick_fix(ws, risk_id), "fix": f"Slim {risk_id}"})
    z_top = ws.design.params["z_top"]
    text, error = call(
        ws,
        "flag_risk",
        {"risk": near.id, "fix": "Lower the top", "name": "z_top", "value": z_top - 1},
    )
    # The fixture's height check doesn't change with the design, so the risk stays as bad.
    assert error and "still there" in text

    assert {f.id for f in ws.flags} == set(thick)
    assert ws.design.params[FIX_PATH.split(".")[0]]["radius"] == before  # not applied
    flags = Flags(ws, list(ws.flags))
    contract = RiskFlags.model_validate(flags.risk_flags().model_dump(mode="json"))
    assert all(f.status == "proposed" and f.fix.changes for f in contract.flags)
    assert all(f.fix.changes[0].path == FIX_PATH.split(".")[0] for f in contract.flags)

    built = len(ws.services.built)
    first = thick[0]
    out = asyncio.run(flags.review(accept=[first]))
    assert get_path(ws.design.params, FIX_PATH) == before - 0.5
    assert len(ws.services.built) == built + 1  # rebuilt and rechecked
    assert ws.report is not None
    status = {f.id: f.status for f in out.flags}
    assert status[first] == "accepted"
    assert all(s == "proposed" for i, s in status.items() if i != first)

    asyncio.run(flags.review(reject=[first]))
    assert get_path(ws.design.params, FIX_PATH) == before


def test_accepting_a_fix_keeps_the_runs_change_set():
    """A fix laid over the run's accepted changes; rejecting a change keeps the fix."""
    ws, sink = fixture_workspace()
    services = PassingServices(ws.board, thick_part=True)
    ws.services = services
    steps = [
        ("run_checks", {}),
        ("set_parameter", {"name": "wall", "value": ws.design.params["wall"] + 0.1}),
        (
            "flag_risk",
            {
                "risk": "thick:boss_1",
                "fix": "Core the boss",
                "name": "boss.radius",
                "value": ws.design.params["boss"]["radius"] - 0.5,
            },
        ),
        ("finish", {"outcome": "done", "summary": "Done."}),
    ]
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, "New revision.", RunConfig(mode="auto"), driver))
    assert result.error is None and result.proposal and result.risks
    assert not driver.calls[2].is_error, driver.calls[2].text
    radius = ws.design.params["boss"]["radius"]
    asyncio.run(result.risks.review(accept=["thick:boss_1"]))
    assert ws.design.params["boss"]["radius"] == radius - 0.5
    wall = ws.design.params["wall"]
    asyncio.run(result.proposal.review(reject=["wall"]))
    assert ws.design.params["wall"] == wall - 0.1
    assert ws.design.params["boss"]["radius"] == radius - 0.5  # the fix stays


@dataclass
class PassingServices:
    """Instant builds where every check passes far from its limit. With `thick_part`, the
    lid's boss_1 is a thick section until its radius shrinks; `fails_when` makes a check fail."""

    board: object
    thick_part: bool = False
    fails_when: object = lambda params: False
    built: list = field(default_factory=list)
    radius: float | None = None

    def build(self, design):
        self.built.append(design)
        if self.radius is None:
            self.radius = design["params"]["boss"]["radius"]
        return BuildOutcome(make_build(f"build-{len(self.built)}", self.board), shapes=design)

    def checks(self, outcome):
        params = outcome.shapes["params"]
        wall = float(STANDARDS.get("min_wall").value)
        status = "fail" if self.fails_when(params) else "pass"
        return [
            CheckItem(
                "wall_thickness",
                "wall_thickness",
                "pass",
                standard="min_wall",
                measured=wall * 2,
                required=wall,
                unit="mm",
            ),
            CheckItem("openings:X", "openings", status, component="X"),
        ]

    def analyze(self, outcome):
        params = outcome.shapes["params"]
        if not self.thick_part or params["boss"]["radius"] < self.radius:
            return []
        limit = float(STANDARDS.get("min_wall").value)
        return [
            {
                "map": "wall_thickness",
                "kind": "thick",
                "value": limit * 4,
                "unit": "mm",
                "part": "lid",
                "feature": "boss_1",
                "point": [1.0, 2.0, 3.0],
            }
        ]


def passing_workspace(**kw):
    sink = EventSink("msg-2")
    ws = scenario_workspace(sink=sink, services=FixtureServices())
    ws.services = PassingServices(ws.board, **kw)
    return ws, sink


def test_a_fix_that_breaks_a_passing_check_or_a_lock_is_refused():
    ws, sink = passing_workspace(thick_part=True, fails_when=lambda p: p["boss"]["radius"] < 1)
    call(ws, "run_checks")
    text, error = call(
        ws,
        "flag_risk",
        {"risk": "thick:boss_1", "fix": "Tiny bosses", "name": "boss.radius", "value": 0.5},
    )
    assert error and "openings:X" in text
    ws.design.lock("boss.radius")
    text, error = call(
        ws,
        "flag_risk",
        {
            "risk": "thick:boss_1",
            "fix": "Slim",
            "name": "boss.radius",
            "value": ws.design.params["boss"]["radius"] - 0.5,
        },
    )
    assert error and "locked" in text
    assert ws.flags == [] and "Risk:" not in chat(sink)


def test_a_lock_set_after_the_flag_refuses_accepting_its_fix():
    ws, sink = passing_workspace(thick_part=True)
    call(ws, "run_checks")
    radius = ws.design.params["boss"]["radius"]
    call(
        ws,
        "flag_risk",
        {"risk": "thick:boss_1", "fix": "Slim", "name": "boss.radius", "value": radius - 0.5},
    )
    flags = Flags(ws, list(ws.flags))
    ws.design.lock("boss.radius")
    with pytest.raises(LockedParameterError):
        asyncio.run(flags.review(accept=["thick:boss_1"]))
    assert ws.design.params["boss"]["radius"] == radius
    assert flags.flags[0].status == "proposed"


def test_an_edit_drops_flags_made_on_the_design_before_it():
    ws, sink = passing_workspace(thick_part=True)
    call(ws, "run_checks")
    radius = ws.design.params["boss"]["radius"]
    call(
        ws,
        "flag_risk",
        {"risk": "thick:boss_1", "fix": "Slim", "name": "boss.radius", "value": radius - 0.5},
    )
    text, _ = call(ws, "set_parameter", {"name": "wall", "value": ws.design.params["wall"]})
    assert ws.flags == [] and "thick:boss_1" in text


# --- No risks, no flags -----------------------------------------------------------------------


def test_no_flag_when_the_analysis_shows_no_risks():
    ws, sink = passing_workspace(thick_part=False)
    steps = [
        ("run_checks", {}),
        ("flag_risk", {"risk": "thick:boss_1", "fix": "Slim", "name": "boss.radius", "value": 1.0}),
        ("finish", {"outcome": "done", "summary": "Nothing to change."}),
    ]
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, "New revision.", RunConfig(), driver))
    assert result.error is None
    report = json.loads(driver.calls[0].text)
    assert report["risks"] == [] and ws.risks == []
    assert driver.calls[1].is_error and "No risk" in driver.calls[1].text
    assert "aren't flagged" not in driver.calls[2].text
    assert result.risks is None and ws.flags == []
    assert "Risk:" not in chat(sink)


def test_finish_reminds_claude_of_unflagged_risks():
    ws, sink = passing_workspace(thick_part=True)
    call(ws, "run_checks")
    text, error = call(ws, "finish", {"outcome": "done", "summary": "Done."})
    assert not error and "thick:boss_1" in text and "flag_risk" in text


def test_the_set_is_valid_contract_json():
    ws, sink = passing_workspace(thick_part=True)
    call(ws, "run_checks")
    call(
        ws,
        "flag_risk",
        {
            "risk": "thick:boss_1",
            "fix": "Slim",
            "name": "boss.radius",
            "value": ws.design.params["boss"]["radius"] - 0.5,
        },
    )
    data = Flags(ws, list(ws.flags)).risk_flags().model_dump(mode="json")
    flag = RiskFlags.model_validate(data).flags[0]
    assert flag.location.part == "lid" and flag.location.point == (1.0, 2.0, 3.0)
    assert flag.fix.resolves and flag.fix.edits == {
        "boss.radius": set_path(
            ws.design.params, "boss.radius", ws.design.params["boss"]["radius"] - 0.5
        )["boss"]["radius"]
    }


def test_the_fixtures_flag_a_real_risk_with_a_checked_fix():
    """The fixture enclosure passes every check; the risk it carries is one the checks don't
    block, flagged with the fix Claude would give and that fix's own checked result."""
    flags = RiskFlags.model_validate(json.loads((FIXTURES_DIR / "risk-flags.json").read_text()))
    assert flags.flags, "the fixtures flag no risk"
    report = items_from_report(json.loads((FIXTURES_DIR / "checks.json").read_text()))
    found = {r.id: r for r in find_risks(report, FixtureServices().analyze(None), STANDARDS)}
    for flag in flags.flags:
        assert flag.id in found and flag.location.faces
        assert flag.fix.edits and flag.fix.changes
