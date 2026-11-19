"""The agent's questions carry the conflict behind them.

When `run_checks` reports a conflict and Claude then asks (`ask_user`), the `question`
event carries that conflict in the contract's `ConstraintConflict` form and its location, so
the chat shows the can't-satisfy card and the canvas pins the question there. On a real
build every location names a face of the build's mesh.

The runs are scripted (`ScriptedDriver`): no model. Nothing here names a board, part or
dimension: the clashing lock comes from `geometry.conflicts.conflict_locks()`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from app.agent.run import RunConfig, run_agent, scenario_boards, scenario_workspace
from app.agent.scripted import ScriptedDriver
from app.agent.services import MESH_FILE, BuildOutcome, CheckItem, GeometryServices, make_build
from app.contracts.common import check_location
from app.contracts.mesh import validate_mesh
from app.contracts.session import validate_event, validate_recording
from app.standards import load_standards
from geometry.conflicts import conflict_locks, detect_conflicts


@dataclass
class FakeServices:
    """Instant builds whose checks all pass: only conflict detection can stop the design."""

    board: object
    built: list = field(default_factory=list)

    def build(self, design):
        self.built.append(design)
        return BuildOutcome(make_build(f"build-{len(self.built)}", self.board), shapes=design)

    def checks(self, outcome):
        return [CheckItem("wall_thickness", "wall_thickness", "pass", standard="min_wall")]

    def analyze(self, outcome):
        return []


def workspace(answers: list[str], services=None):
    pending = list(answers)

    async def answer(question_id, text, options):
        return pending.pop(0)

    ws = scenario_workspace(answer=answer, services=services)
    if services is None:
        ws.services = FakeServices(ws.board)
    return ws


def lock_conflict(ws) -> tuple[str, float]:
    ((path, value),) = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    ws.design.lock(path, value)
    return path, value


def run(ws, steps, config=None):
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, "Here is the new revision.", config, driver=driver))
    return result, driver


def options(path: str) -> list[dict]:
    return [
        {"id": "unlock", "label": "Change the locked value", "approves": [path]},
        {"id": "keep", "label": "Keep it and stop"},
    ]


def as_contract(event):
    """The event as the session API sends it, read back through the contract."""
    return validate_event(json.loads(event.model_dump_json()))


def test_a_question_after_a_conflict_carries_it_with_faces_of_the_builds_mesh():
    """The acceptance run: real builds and checks, the fixture's lock, then Claude asks."""
    _, board = scenario_boards()
    ws = workspace(["keep"], services=GeometryServices(board, load_standards()))
    path, _ = lock_conflict(ws)
    steps = [
        ("run_checks", {}),
        ("ask_user", {"question": "The locked value leaves no room.", "options": options(path)}),
        ("finish", {"outcome": "blocked", "summary": "Waiting on the lock."}),
    ]
    result, driver = run(ws, steps, RunConfig(propose=False))
    assert result.error is None and result.finished["outcome"] == "blocked"
    validate_recording(ws.sink.recording())
    (question,) = ws.sink.of_type("question")
    question = as_contract(question)

    # The conflict is the one run_checks reported, with each constraint's binding and label.
    (reported,) = json.loads(driver.calls[0].text)["conflicts"]
    conflict = question.conflict
    assert conflict is not None and conflict.id == reported["id"] == "stackup"
    assert conflict.over_by == reported["over_by"] > 0
    got = [(c.kind, c.id, c.value, c.best) for c in conflict.constraints]
    assert got == [(c["kind"], c["id"], c["value"], c["best"]) for c in reported["constraints"]]
    for c in conflict.constraints:
        assert c.label and c.locations
        if c.kind == "standard":
            standard = ws.standards.get(c.id)
            assert c.label == standard.title
            assert c.relation == {"min": ">=", "max": "<="}[standard.kind]
        else:
            assert c.relation == "="
    assert ("locked", path) in {(c.kind, c.id) for c in conflict.constraints}

    # Pinned where the conflict is, and every location is a face of the build's mesh.
    assert question.location == conflict.location
    build_id = ws.latest.build.id
    mesh = validate_mesh(ws.services.store.file(build_id, MESH_FILE).read_bytes())
    locations = [question.location, *(loc for c in conflict.constraints for loc in c.locations)]
    for loc in locations:
        assert loc.faces, "each location names the face it is on"
        check_location(loc, mesh, "question")


def test_questions_without_a_conflict_carry_none():
    ws = workspace(["a"])
    plain = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    run(ws, [("run_checks", {}), ("ask_user", {"question": "Which finish?", "options": plain})])
    (question,) = ws.sink.of_type("question")
    assert question.conflict is None and question.location is None
    assert as_contract(question).conflict is None


def test_once_the_conflict_is_resolved_the_next_question_carries_none():
    ws = workspace(["unlock", "a"])
    path, _ = lock_conflict(ws)
    plain = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    report = asyncio.run(ws.check())
    (conflict,) = report["conflicts"]
    best = next(c["best"] for c in conflict["constraints"] if c["id"] == path)
    steps = [
        ("run_checks", {}),
        ("ask_user", {"question": "The locked value leaves no room.", "options": options(path)}),
        ("set_parameter", {"name": path, "value": best}),
        ("ask_user", {"question": "Anything else?", "options": plain}),
    ]
    _, driver = run(ws, steps)
    assert not [c for c in driver.calls if c.is_error]
    first, second = ws.sink.of_type("question")
    # Without a mesh (no real build) the locations have no faces, but still a point and part.
    assert first.conflict is not None and first.conflict.id == conflict["id"]
    assert first.location == first.conflict.location and first.location.part
    assert ws.conflicts == [] and second.conflict is None and second.location is None


def test_claude_can_name_which_conflict_a_question_is_about():
    ws = workspace(["keep"])
    path, _ = lock_conflict(ws)
    asyncio.run(ws.check())
    expected = detect_conflicts(ws.board, ws.standards, ws.design.params, ws.design.locked)
    ask = {"question": "The locked value leaves no room.", "options": options(path)}
    wrong = asyncio.run(ws.ask_user({**ask, "conflict": "no-such-conflict"}))
    assert wrong.get("is_error") and "stackup" in wrong["content"][0]["text"]
    assert not ws.sink.of_type("question")
    asyncio.run(ws.ask_user({**ask, "conflict": expected[0].id}))
    (question,) = ws.sink.of_type("question")
    assert question.conflict.id == expected[0].id
