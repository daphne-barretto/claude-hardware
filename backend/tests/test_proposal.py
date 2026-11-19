"""Proposed changes: a finished run as a change set, proved by the checks, reviewed by
accepting and rejecting changes.

The scenario comes from `demos/b-parasite/` and `standards.yaml`: the run starts from the enclosure
for the previous revision and ends with the parameters derived for the new one.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field

import pytest

from app.agent.design import get_path, set_path
from app.agent.events import EventSink
from app.agent.proposal import check_name, combine, delta, diff, title
from app.agent.run import RunConfig, run_agent, scenario_workspace
from app.agent.scripted import ScriptedDriver
from app.agent.services import BuildOutcome, CheckItem, make_build
from app.boards import BOARDS_DIR
from app.boards.changes import parse_changes_md
from app.contracts.changes import ChangeSet
from app.contracts.session import validate_recording
from app.parameters import LockedParameterError
from geometry.conflicts import conflict_locks

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())


def component(row: dict) -> str:
    return row["Component"].strip("` ")


ROWS = {component(r): r for r in CHANGES.rows}


@dataclass
class OpeningServices:
    """Instant builds; `openings:<component>` passes when the design's opening for it sits
    where the new board needs it. A `wall` check always passes."""

    board: object
    built: list = field(default_factory=list)

    def build(self, design):
        self.built.append(design)
        return BuildOutcome(make_build(f"build-{len(self.built)}", self.board), shapes=design)

    def checks(self, outcome):
        params = outcome.shapes["params"]
        have = {o["id"]: o for o in params["openings"]}
        items = [CheckItem("wall", "wall_thickness", "pass", standard="min_wall")]
        for o in self.board.openings:
            got = have.get(o.id)
            ok = got is not None and (got["x"], got["y"], got["rotation"]) == (
                o.position[0],
                o.position[1],
                o.rotation,
            )
            items.append(
                CheckItem(
                    f"openings:{o.component}",
                    "openings",
                    "pass" if ok else "fail",
                    component=o.component,
                )
            )
        return items

    def analyze(self, outcome):
        return []


def fixed_params(ws):
    from geometry import enclosure_parameters

    return enclosure_parameters(ws.board, ws.standards)


def finished_run(mode="auto", ws=None, extra=(), keep=None, outcome="done"):
    """A scripted run that writes the parameters for the new board (with `keep`, paths and
    values set by the `extra` steps) and finishes."""
    if ws is None:
        ws = scenario_workspace()
        ws.services = OpeningServices(ws.board)
    params = fixed_params(ws)
    for path, value in (keep or {}).items():
        params = set_path(params, path, value)
    steps = [
        ("run_checks", {}),
        *extra,
        ("write_design", {"params": params}),
        ("finish", {"outcome": outcome, "summary": "Updated the openings."}),
    ]
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, "Here is the new revision.", RunConfig(mode=mode), driver))
    assert result.error is None, result.error
    assert result.proposal is not None
    return ws, result.proposal


def by_component(change_set: ChangeSet) -> dict[str, list]:
    out: dict[str, list] = {}
    for change in change_set.changes:
        out.setdefault(change.delta.component, []).append(change)
    return out


def checks_of(ws) -> dict[str, str]:
    return {i.id: i.status for i in ws.report}


# --- The change set ---------------------------------------------------------------------------


def test_a_finished_run_becomes_a_change_set_covering_every_board_change():
    ws, proposal = finished_run()
    change_set = proposal.change_set()
    ChangeSet.model_validate_json(change_set.model_dump_json(by_alias=True))
    changes = by_component(change_set)
    for name, row in ROWS.items():
        (change,) = changes[name]
        assert change.title and change.reason
        assert name in change.reason and row["What it breaks"] in change.reason
        expected = "added" if row["Change"] == "added" else "modified"
        assert change.delta.kind == expected
        assert change.rounds == [1]
        if "moved" in row["Change"]:
            assert change.delta.move is not None
        if "rotated" in row["Change"]:
            assert change.delta.rotate is not None
    assert all(c.status == "accepted" for c in change_set.changes)
    validate_recording(ws.sink.recording())


def test_each_change_links_to_the_checks_that_prove_it():
    ws, proposal = finished_run()
    change_set = proposal.change_set()
    for name in ROWS:
        (change,) = by_component(change_set)[name]
        proof = {link.id: link for link in change.checks}
        link = proof[f"openings:{name}"]
        assert (link.effect, link.before, link.alone, link.after) == (
            "fixes",
            "fail",
            "pass",
            "pass",
        )
        # The reason names the check as people read it (its title), never its ID.
        assert check_name(link) in change.reason and link.id not in change.reason
        # Another component's fix isn't this change's proof.
        others = {f"openings:{n}" for n in ROWS if n != name}
        assert not others & {k for k, v in proof.items() if v.effect == "fixes"}


# --- Review -----------------------------------------------------------------------------------


def test_manual_mode_applies_nothing_until_accepted():
    ws, proposal = finished_run(mode="manual")
    assert ws.design.params == ws.base["params"]
    change_set = proposal.change_set()
    assert all(c.status == "proposed" for c in change_set.changes)
    assert all(checks_of(ws)[f"openings:{n}"] == "fail" for n in ROWS)


def test_accepting_a_subset_rebuilds_and_rechecks_with_only_that_subset():
    ws, proposal = finished_run(mode="manual")
    first, second, *rest = list(ROWS)
    ids = {n: by_component(proposal.change_set())[n][0].id for n in ROWS}
    built = len(ws.services.built)

    change_set = asyncio.run(proposal.review(accept=[ids[first], ids[second]]))
    assert len(ws.services.built) == built + 1  # rebuilt once
    assert ws.services.built[-1] == proposal.design_for({ids[first], ids[second]})
    status = checks_of(ws)
    assert status[f"openings:{first}"] == status[f"openings:{second}"] == "pass"
    assert all(status[f"openings:{n}"] == "fail" for n in rest)
    assert change_set.build == ws.latest.build.id == ws.sink.of_type("build_ready")[-1].build.id
    assert change_set.checks.failed == sum(v == "fail" for v in status.values())
    accepted = {c.id for c in change_set.changes if c.status == "accepted"}
    assert accepted == {ids[first], ids[second]}
    link = next(c for c in change_set.changes if c.id == ids[first]).checks[0]
    assert link.after == "pass"
    for name in rest:
        change = next(c for c in change_set.changes if c.id == ids[name])
        assert change.status == "proposed"
        assert next(k for k in change.checks if k.effect == "fixes").after == "fail"

    # Rejecting one of them rebuilds with the other alone.
    asyncio.run(proposal.review(reject=[ids[first]]))
    status = checks_of(ws)
    assert status[f"openings:{first}"] == "fail" and status[f"openings:{second}"] == "pass"


def test_auto_mode_applies_everything_and_keeps_it_reviewable():
    ws, proposal = finished_run(mode="auto")
    assert ws.design.snapshot() == proposal.final and ws.passing
    name = next(iter(ROWS))
    change = by_component(proposal.change_set())[name][0]
    change_set = asyncio.run(proposal.review(reject=[change.id]))
    assert checks_of(ws)[f"openings:{name}"] == "fail"
    assert all(checks_of(ws)[f"openings:{n}"] == "pass" for n in ROWS if n != name)
    assert next(c for c in change_set.changes if c.id == change.id).status == "rejected"
    asyncio.run(proposal.review(accept=[change.id]))
    assert ws.passing and ws.design.snapshot() == proposal.final


def test_review_refuses_unknown_and_contradictory_ids():
    _, proposal = finished_run(mode="manual")
    some = proposal.units[0].path
    with pytest.raises(KeyError):
        asyncio.run(proposal.review(accept=["nope"]))
    with pytest.raises(ValueError):
        asyncio.run(proposal.review(accept=[some], reject=[some]))


def locked_value_run(mode):
    """A run that changes a locked value with the user's approval. Returns the workspace, the
    proposal, the path and the store records of the lock before and after the run."""
    ws = scenario_workspace()
    ws.services = OpeningServices(ws.board)
    (path, value), *_ = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    ws.design.lock(path, value)
    hand = ws.design.store.get(path)
    pending = ["thin"]

    async def answer(question_id, text, options):
        return pending.pop(0)

    ws.answer = answer
    options = [
        {"id": "thin", "label": "Change the locked value", "approves": [path]},
        {"id": "keep", "label": "Keep it"},
    ]
    extra = [
        ("ask_user", {"question": "Change the locked value?", "options": options}),
        ("set_parameter", {"name": path, "value": value - 0.5}),
    ]
    # The new value still clashes with the stack-up, so the run stops after asking; its
    # changes are proposed all the same.
    ws, proposal = finished_run(
        ws=ws, mode=mode, extra=extra, keep={path: value - 0.5}, outcome="blocked"
    )
    return ws, proposal, path, hand


def test_rejecting_an_approved_change_to_a_locked_value_puts_back_the_hand_edit():
    ws, proposal, path, hand = locked_value_run("auto")
    ran = ws.design.store.get(path)
    assert ran.approval is not None and ran.approval.by == "user"
    change = next(c for c in proposal.change_set().changes if c.id == path)
    assert change.rounds == [1]

    asyncio.run(proposal.review(reject=[path]))
    assert get_path(ws.design.params, path) == json.loads(str(hand.value))
    # The store holds the person's own hand edit again, not a made-up approval.
    assert ws.design.store.get(path) == hand

    asyncio.run(proposal.review(accept=[path]))
    assert get_path(ws.design.params, path) == json.loads(str(ran.value))
    assert ws.design.store.get(path) == ran


def test_manual_mode_starts_a_locked_value_from_its_hand_edit_without_an_approval():
    ws, proposal, path, hand = locked_value_run("manual")
    assert get_path(ws.design.params, path) == json.loads(str(hand.value))
    assert ws.design.store.get(path) == hand  # no review happened, so no approval recorded
    asyncio.run(proposal.review(accept=[path]))
    stored = ws.design.store.get(path)
    assert stored.locked and stored.approval is not None and stored.approval.by == "user"
    assert get_path(ws.design.params, path) == json.loads(str(stored.value))


def test_a_review_leaves_a_run_approved_lock_alone_unless_it_decides_its_change():
    ws, proposal, path, _ = locked_value_run("auto")
    ran = ws.design.store.get(path)
    other = next(u.path for u in proposal.units if u.path != path and u.list_key)
    asyncio.run(proposal.review(reject=[other]))
    assert ws.design.store.get(path) == ran
    assert get_path(ws.design.params, path) == json.loads(str(ran.value))


def opening_field(proposal, kind):
    """A changed opening (of `kind`) and the path of its x."""
    unit = next(u for u in proposal.units if u.list_key and u.kind == kind)
    return unit.path, f"{unit.path}.x"


def test_a_hand_lock_after_the_proposal_survives_a_review_of_another_change():
    ws, proposal = finished_run(mode="auto")
    unit, path = opening_field(proposal, "modified")
    hand_value = get_path(ws.design.params, path) + 1.0
    ws.design.lock(path, hand_value)
    hand = ws.design.store.get(path)
    other = next(u.path for u in proposal.units if u.path != unit)

    change_set = asyncio.run(proposal.review(reject=[other]))
    assert get_path(ws.design.params, path) == hand_value
    assert ws.design.store.get(path) == hand
    assert next(c for c in change_set.changes if c.id == other).status == "rejected"
    assert ws.services.built[-1]["params"] == ws.design.params

    # Rejecting the change that holds it puts back the rest of it; the lock still stands.
    asyncio.run(proposal.review(reject=[unit]))
    assert get_path(ws.design.params, path) == hand_value
    assert ws.design.store.get(path) == hand
    before = get_path(proposal.base["params"], unit)
    after = get_path(ws.design.params, unit)
    assert {k: v for k, v in after.items() if k != "x"} == {
        k: v for k, v in before.items() if k != "x"
    }


def test_a_hand_lock_outside_the_runs_changes_does_not_block_a_review():
    ws, proposal = finished_run(mode="auto")
    changed = {u.path for u in proposal.units}
    name, value = next(
        (k, v)
        for k, v in ws.design.params.items()
        if k not in changed and isinstance(v, int | float) and not isinstance(v, bool)
    )
    ws.design.lock(name, value + 1)
    hand = ws.design.store.get(name)

    asyncio.run(proposal.review(reject=[proposal.units[0].path]))
    assert ws.design.params[name] == value + 1
    assert ws.design.store.get(name) == hand
    assert ws.services.built[-1]["params"][name] == value + 1


def test_a_review_that_leaves_no_place_for_a_lock_is_refused_and_changes_nothing():
    ws, proposal = finished_run(mode="auto")
    unit, path = opening_field(proposal, "added")
    ws.design.lock(path)
    snapshot, status = ws.design.snapshot(), dict(proposal.status)
    with pytest.raises(LockedParameterError) as error:
        asyncio.run(proposal.review(reject=[unit]))
    assert path in str(error.value)
    assert ws.design.snapshot() == snapshot and proposal.status == status


def test_combine_keeps_list_order_and_handles_removals():
    base = {"code": "a", "backend": "x", "params": {"l": [{"id": "1"}, {"id": "2"}], "k": 1}}
    final = {
        "code": "b",
        "backend": "x",
        "params": {"l": [{"id": "0"}, {"id": "2", "v": 1}], "k": 2, "n": 3},
    }
    units = diff(base, final)
    assert {(u.path, u.kind) for u in units} == {
        ("l.0", "added"),
        ("l.2", "modified"),
        ("l.1", "removed"),
        ("k", "modified"),
        ("n", "added"),
        ("code", "modified"),
    }
    assert combine(base, final, units, {"l.0", "l.1"})["params"]["l"] == [
        {"id": "0"},
        {"id": "2"},
    ]
    assert combine(base, final, units, {"l.2", "code"}) == {
        "code": "b",
        "backend": "x",
        "params": {"l": [{"id": "1"}, {"id": "2", "v": 1}], "k": 1},
    }
    assert combine(base, final, units, {u.path for u in units}) == final
    assert combine(base, final, units, set()) == base


# --- Real geometry ----------------------------------------------------------------------------


def test_with_real_geometry_accepting_one_change_fixes_only_its_checks():
    """Built in the sandbox and checked exactly: accepting the change for one board difference
    makes that component's checks pass and leaves the others failing."""
    ws = scenario_workspace()
    steps = [
        ("write_design", {"params": fixed_params(ws)}),
        ("finish", {"outcome": "done", "summary": "Updated the openings."}),
    ]
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, "go", RunConfig(mode="manual"), driver))
    proposal = result.proposal
    assert result.error is None and proposal is not None
    first, *rest = list(ROWS)
    change = by_component(proposal.change_set())[first][0]
    fixes = [link.id for link in change.checks if link.effect == "fixes"]
    assert fixes and all(first in f for f in fixes)

    change_set = asyncio.run(proposal.review(accept=[change.id]))
    status = checks_of(ws)
    assert all(status[f] == "pass" for f in fixes)
    for name in rest:
        assert any(i.status == "fail" for i in ws.report if i.component == name)
    accepted = next(c for c in change_set.changes if c.id == change.id)
    assert all(link.after == "pass" for link in accepted.checks if link.effect == "fixes")
    assert json.loads(change_set.model_dump_json(by_alias=True))["contract"] == "change-set/1"


def test_progress_and_build_events_stay_inside_the_runs_message():
    ws = scenario_workspace(sink=EventSink("msg-7"))
    ws.services = OpeningServices(ws.board)
    finished_run(ws=ws, mode="manual")
    types = ws.sink.types
    assert types[-1] == "message_end"
    texts = [e.text for e in ws.sink.of_type("progress")]
    assert any("Proposing the changes" in t for t in texts)
    validate_recording(ws.sink.recording())


# --- Only real differences, each design built once (demo walk with the real model) ------------


def _as_written_back(value):
    """A value as a model writes it back: whole floats as ints, others with float noise."""
    if isinstance(value, float):
        return int(value) if value.is_integer() else value + 1e-12
    if isinstance(value, dict):
        return {k: _as_written_back(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_written_back(v) for v in value]
    return value


def test_a_value_written_back_as_int_or_with_float_noise_is_no_change():
    base = {
        "code": "c",
        "backend": "x",
        "params": {
            "corner_radius": 5.0,
            "parting_z": 0.0,
            "footprint": {"corner_radius": 5.0, "w": 30.1},
            "boss": {"radius": 2.6, "insert_diameter": 3.2},
            "openings": [{"id": "a", "x": 1.9, "y": 123.71, "rotation": 0.0}],
        },
    }
    final = {**base, "params": _as_written_back(base["params"])}
    assert diff(base, final) == []
    moved = copy.deepcopy(final)
    moved["params"]["openings"][0]["x"] = 1.08
    moved["params"]["footprint"]["corner_radius"] = 4
    units = diff(base, moved)
    assert sorted(u.path for u in units) == ["footprint", "openings.a"]
    (footprint,) = [u for u in units if u.path == "footprint"]
    assert [f.field for f in delta(footprint).fields] == ["corner_radius"]
    assert title(footprint, delta(footprint)) == "Change footprint.corner_radius from 5 to 4"


def test_the_change_set_lists_only_what_the_run_really_changed():
    """The run writes every parameter back as JSON (ints for whole floats, float noise): only
    the board changes become changes."""
    ws = scenario_workspace()
    ws.services = OpeningServices(ws.board)
    params = _as_written_back(fixed_params(ws))
    steps = [
        ("run_checks", {}),
        ("write_design", {"params": params}),
        ("finish", {"outcome": "done", "summary": "Updated the openings."}),
    ]
    result = asyncio.run(run_agent(ws, "go", RunConfig(mode="auto"), ScriptedDriver(ws, steps)))
    assert result.error is None and result.proposal is not None
    expected = {u.path for u in diff(ws.base, {**ws.base, "params": fixed_params(ws)})}
    assert {c.id for c in result.proposal.change_set().changes} == expected


def test_proposing_builds_no_design_twice():
    """The design the run started from was checked by the run, the final design too, and a
    design with one change alone that is the final design is not built again."""
    ws, proposal = finished_run()
    keys = [json.dumps(d, sort_keys=True) for d in ws.services.built]
    assert len(keys) == len(set(keys)), "a design was built more than once"
    # one build per change alone (the final design is one of them only with a single change)
    assert len(keys) == 2 + len(proposal.units)
