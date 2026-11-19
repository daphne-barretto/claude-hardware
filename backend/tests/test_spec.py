"""Spec, constraints and the build plan.

- Chip extraction: a description names standards by their `aliases` in `standards.yaml`; a
  value in the user's words becomes a chip traced to those words, the rest are traced to the
  standard.
- Editing or removing a chip rebuilds and rechecks the design against the new spec.
- A new design starts with Claude's build plan, which asks only the questions that change
  the geometry; answering continues the run.

Every value comes from `standards.yaml`, so the tests follow the file.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.agent.events import EventSink
from app.agent.run import scenario_workspace
from app.agent.scripted import ScriptedDriver, box_design
from app.agent.services import FixtureServices
from app.agent.tools import MAX_PLAN_QUESTIONS
from app.contracts.session import Constraint, validate_event
from app.examples import examples
from app.main import app
from app.mock_api import app as mock_app
from app.revisions import revision_message
from app.session_api import CONFIG, NEW_DESIGN_NOTE, SESSIONS
from app.spec import Spec, SpecError, extract, label
from app.standards import load_standards

STANDARDS = load_standards()
EXAMPLE = examples()[0].id
# The standards a description can give a value for: numeric, with words that name them
# (`test_every_standard_can_be_named_in_a_description` checks that every standard has some).
NUMERIC = [s for s in STANDARDS.standards if s.kind != "spec" and s.aliases]
SPECS = [s for s in STANDARDS.standards if s.kind == "spec"]
UNIT_WORDS = {"mm": "mm", "deg": "°"}


def phrase(standard, value: float) -> str:
    """A plain-language phrase giving `standard` a value: "2.5 mm wall"."""
    sep = "" if standard.unit == "deg" else " "
    return f"{value:g}{sep}{UNIT_WORDS[standard.unit]} {standard.aliases[0]}"


def other(standard) -> float:
    """A value different from the standard's own."""
    return round(float(standard.value) + 0.5, 3)


# --- Chip extraction ----------------------------------------------------------------------------


def test_every_standard_can_be_named_in_a_description():
    assert all(s.aliases for s in STANDARDS.standards)


@pytest.mark.parametrize("standard", NUMERIC, ids=lambda s: s.id)
def test_a_value_in_the_users_words_becomes_that_standards_constraint(standard):
    text = f"A small case, {phrase(standard, other(standard))}."
    (mention,) = extract(text, STANDARDS)
    assert mention.standard == standard.id
    assert mention.value == other(standard)
    assert mention.quote == phrase(standard, other(standard))


def test_several_constraints_in_one_description_keep_their_own_values():
    text = " and ".join(phrase(s, other(s)) for s in NUMERIC)
    found = {m.standard: m.value for m in extract(text, STANDARDS)}
    assert found == {s.id: other(s) for s in NUMERIC}


@pytest.mark.parametrize("standard", SPECS, ids=lambda s: s.id)
def test_naming_a_spec_standard_traces_it_to_the_standard(standard):
    (mention,) = extract(f"Use {standard.aliases[0]}", STANDARDS)
    assert (mention.standard, mention.value) == (standard.id, None)


def test_a_description_that_names_no_standard_has_no_mentions():
    assert extract("Check the enclosure against the new revision", STANDARDS) == []


def test_a_number_in_the_wrong_unit_is_not_taken():
    mm = next(s for s in NUMERIC if s.unit == "mm")
    deg = next(s for s in NUMERIC if s.unit == "deg")
    (mention,) = extract(f"{deg.aliases[0]} 3 mm", STANDARDS)
    assert (mention.standard, mention.value) == (deg.id, None)
    assert mm.id not in {m.standard for m in extract(f"{deg.aliases[0]} 3 mm", STANDARDS)}


# --- A locked value is a design value, not a rule -----------------------------------------------


def _values(spec: Spec) -> dict:
    return {c.id: c.value for c in spec.constraints()}


@pytest.mark.parametrize("standard", NUMERIC, ids=lambda s: s.id)
@pytest.mark.parametrize(
    "words",
    [
        "Lock the {alias} at {value}",
        "I locked the {alias} at {value}, rework the rest around it",
        "Keep the {alias} at {value}",
        "Set the {alias} to {value}",
    ],
)
def test_locking_a_value_in_words_leaves_the_standard_as_it_is(standard, words):
    """Telling Claude a design value (to lock, keep or set) is not a new rule: the checks keep
    measuring against the standard."""
    value = phrase(standard, other(standard)).rsplit(" ", 1)[0]
    spec = Spec(STANDARDS)
    spec.describe(words.format(alias=standard.aliases[0], value=value), "msg-1")
    assert _values(spec)[standard.id] == standard.value


@pytest.mark.parametrize("standard", NUMERIC, ids=lambda s: s.id)
def test_naming_a_hand_locked_value_leaves_the_standard_as_it_is(standard):
    """A message that names a value the user locked by hand (any parameter) describes the
    lock, not a rule."""
    value = other(standard)
    spec = Spec(STANDARDS)
    name = standard.aliases[0]
    text = f"Rework the design around the {phrase(standard, value)}"
    spec.describe(text, "msg-1", locked={f"x.{name}": value})
    assert _values(spec)[standard.id] == standard.value


@pytest.mark.parametrize("standard", NUMERIC, ids=lambda s: s.id)
def test_a_rule_in_words_still_changes_the_standard_with_a_value_locked(standard):
    op = {"min": "at least", "max": "at most", "tolerance": "within"}[standard.kind]
    value = other(standard)
    text = f"Every {standard.aliases[0]} {op} {value:g} {UNIT_WORDS[standard.unit]}"
    spec = Spec(STANDARDS)
    spec.describe(text, "msg-1", locked={standard.aliases[0]: value})
    assert _values(spec)[standard.id] == value


def test_chips_show_every_standard_and_where_each_came_from():
    spec = Spec(STANDARDS)
    assert spec.constraints() == []  # no chips before anything is described
    target = NUMERIC[0]
    assert spec.describe(f"Make it with {phrase(target, other(target))}", "msg-1")
    chips = {c.id: c for c in spec.constraints()}
    assert set(chips) == {s.id for s in STANDARDS.standards}
    for s in STANDARDS.standards:
        chip = chips[s.id]
        assert chip.check == s.check and chip.standard_value == s.value
        if s.id == target.id:
            assert (chip.source, chip.value, chip.message_id) == ("user", other(s), "msg-1")
            assert phrase(s, other(s)) in chip.quote
        else:
            assert (chip.source, chip.value, chip.quote) == ("standard", s.value, None)
        assert chip.label == label(s, chip.value)
    # The same words again change nothing.
    assert not spec.describe(f"Make it with {phrase(target, other(target))}", "msg-1")


def test_the_spec_values_are_what_the_checks_use():
    spec = Spec(STANDARDS)
    target = NUMERIC[-1]
    spec.describe(phrase(target, other(target)), "msg-1")
    effective = spec.effective()
    assert effective.get(target.id).value == other(target)
    for s in STANDARDS.standards:
        if s.id != target.id:
            assert effective.get(s.id) == s
    assert STANDARDS.get(target.id).value == target.value  # the team file is untouched


def test_editing_and_removing_chips():
    spec = Spec(STANDARDS)
    spec.describe("anything", "msg-1")
    target = NUMERIC[0]
    edited = spec.edit(target.id, str(other(target)))
    assert (edited.source, edited.value) == ("edit", other(target))
    assert spec.effective().get(target.id).value == other(target)
    removed = spec.remove(target.id)
    assert (removed.source, removed.value) == ("standard", target.value)
    with pytest.raises(SpecError, match="constraint in the set"):
        spec.remove(target.id)
    with pytest.raises(SpecError):
        spec.edit(target.id, "not a number")
    with pytest.raises(SpecError):
        spec.edit(target.id, -1)
    with pytest.raises(KeyError):
        spec.edit("no_such_standard", 1)
    text = spec.edit(SPECS[0].id, "  something else ")
    assert (text.value, text.source) == ("something else", "edit")
    with pytest.raises(SpecError):
        spec.edit(SPECS[0].id, " ")
    # Editing back to the standard's value is the standard again.
    assert spec.edit(SPECS[0].id, SPECS[0].value).source == "standard"


# --- The session API: chips, and the rebuild and recheck --------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


class Recorder:
    """Services that record the standards the checks are made with."""

    def __init__(self):
        self.made: list = []

    def __call__(self, board, standards):
        self.made.append(standards)
        return FixtureServices()


@pytest.fixture
def services(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(CONFIG, "services", rec)
    return rec


def sse(client, sid, after=0):
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
    out = []
    for frame in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "id" in fields:
            out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return out


def settle(client, sid, status="idle", after=0):
    for _ in range(500):
        found = sse(client, sid, after)
        state = client.get(f"/api/sessions/{sid}").json()["status"]
        if state == status and found and found[-1][1] in ("message_end", "question"):
            return found
        time.sleep(0.02)
    raise AssertionError(f"{sid} never reached {status}")


def new_session(client) -> str:
    """A session on the example case: a new session starts empty, with no build."""
    sid = client.post("/api/sessions", json={"example": EXAMPLE}).json()["id"]
    for _ in range(500):
        if any(t == "build_ready" for _, t, _ in sse(client, sid)):
            return sid
        time.sleep(0.02)
    raise AssertionError("no first build")


def say(client, monkeypatch, sid, text, steps):
    monkeypatch.setattr(
        CONFIG, "driver", lambda state, body, prompt: ScriptedDriver(state.ws, steps)
    )
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    assert r.status_code == 202, r.text
    return r.json()


FINISH = [("finish", {"outcome": "done", "summary": "ok"})]


def test_typing_a_description_produces_traceable_chips(client, services, monkeypatch):
    sid = new_session(client)
    assert client.get(f"/api/sessions/{sid}/constraints").json() == []
    target = NUMERIC[0]
    text = f"A sensor case with {phrase(target, other(target))}"
    message = say(client, monkeypatch, sid, text, FINISH)
    found = settle(client, sid)
    (event,) = [d for _, t, d in found if t == "constraints"]
    validate_event(event)
    chips = {c["id"]: Constraint.model_validate(c) for c in event["constraints"]}
    assert chips[target.id].source == "user" and chips[target.id].message_id == message["id"]
    assert chips[target.id].quote in text
    listed = [
        Constraint.model_validate(c) for c in client.get(f"/api/sessions/{sid}/constraints").json()
    ]
    assert listed == list(chips.values())
    # The checks and Claude go by the user's value.
    ws = SESSIONS.get(sid).ws
    assert ws.standards.get(target.id).value == other(target)
    assert services.made[-1].get(target.id).value == other(target)
    assert any(c["id"] == target.id and c["source"] == "user" for c in ws.constraints)


@pytest.mark.parametrize("action", ["edit", "remove"])
def test_editing_or_removing_a_chip_rebuilds_and_rechecks(client, services, monkeypatch, action):
    sid = new_session(client)
    target = NUMERIC[-1]
    say(client, monkeypatch, sid, phrase(target, other(target)), FINISH)
    before = settle(client, sid)
    builds = len(client.get(f"/api/sessions/{sid}/builds").json())
    url = f"/api/sessions/{sid}/constraints/{target.id}"
    if action == "edit":
        new_value = round(other(target) + 0.25, 3)
        r = client.patch(url, json={"value": new_value})
    else:
        new_value = target.value
        r = client.delete(url)
    assert r.status_code == 200, r.text
    assert Constraint.model_validate(r.json()).value == new_value
    found = settle(client, sid, after=before[-1][0])
    kinds = [t for _, t, _ in found]
    assert kinds[0] == "message_start" and kinds[-1] == "message_end"
    for _, kind, data in found:
        assert validate_event(data).type == kind
    (chips,) = [d["constraints"] for _, t, d in found if t == "constraints"]
    assert next(c for c in chips if c["id"] == target.id)["value"] == new_value
    progress = " ".join(d["text"] for _, t, d in found if t == "progress")
    assert "Rebuilding and rechecking" in progress
    # A new build, checked with the new value.
    (build,) = [d["build"] for _, t, d in found if t == "build_ready"]
    assert len(client.get(f"/api/sessions/{sid}/builds").json()) == builds + 1
    assert build["id"] == client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    assert services.made[-1].get(target.id).value == new_value
    assert SESSIONS.get(sid).ws.report is not None


def test_a_team_standard_chip_cant_be_removed_and_bad_values_are_refused(
    client, services, monkeypatch
):
    sid = new_session(client)
    say(client, monkeypatch, sid, "a case", FINISH)
    settle(client, sid)
    base = f"/api/sessions/{sid}/constraints"
    assert client.delete(f"{base}/{NUMERIC[0].id}").status_code == 409
    assert client.patch(f"{base}/{NUMERIC[0].id}", json={"value": "wide"}).status_code == 409
    assert client.patch(f"{base}/nope", json={"value": 1}).status_code == 404


def test_chips_cant_change_while_claude_waits_for_an_answer(client, services, monkeypatch):
    sid = new_session(client)
    ask = {
        "question": "Which?",
        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    }
    say(client, monkeypatch, sid, "a case", [("ask_user", ask), *FINISH])
    settle(client, sid, "waiting_for_answer")
    r = client.patch(f"/api/sessions/{sid}/constraints/{NUMERIC[0].id}", json={"value": 9})
    assert r.status_code == 409


def test_a_new_value_in_a_later_message_rechecks_with_it(client, services, monkeypatch):
    sid = new_session(client)
    target = NUMERIC[0]
    say(client, monkeypatch, sid, "a case", FINISH)
    settle(client, sid)
    ws = SESSIONS.get(sid).ws
    before = sse(client, sid)[-1][0]
    say(client, monkeypatch, sid, phrase(target, other(target)), [("run_checks", {}), *FINISH])
    found = settle(client, sid, after=before)
    assert "constraints" in [t for _, t, _ in found]
    assert ws.standards.get(target.id).value == other(target)
    assert services.made[-1].get(target.id).value == other(target)


# --- The build plan ------------------------------------------------------------------------------


def plan_workspace(**kw):
    ws = scenario_workspace(services=FixtureServices(), **kw)
    ws.plan_required = True
    return ws


def geometry_question(ws, n=1):
    path = next(iter(ws.design.params))
    return {
        "question": f"Question {n}?",
        "changes": [path],
        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    }


def test_a_new_design_needs_the_plan_before_any_edit():
    ws = plan_workspace()
    path = next(k for k, v in ws.design.params.items() if isinstance(v, int | float))
    value = ws.design.params[path]
    steps = [
        ("set_parameter", {"name": path, "value": value + 1}),
        ("propose_plan", {"steps": ["Fit the board", "Recheck"]}),
        ("set_parameter", {"name": path, "value": value + 1}),
        *FINISH,
    ]
    driver = ScriptedDriver(ws, steps)
    from app.agent.run import run_agent

    asyncio.run(run_agent(ws, "Make a case", driver=driver))
    denied, plan, edit = driver.calls[:3]
    assert denied.denied and "propose_plan" in denied.denied
    assert not plan.is_error and not edit.is_error
    (event,) = ws.sink.of_type("plan")
    assert event.steps == ["Fit the board", "Recheck"]
    # The tool refuses it too, without the hook.
    ws2 = plan_workspace()
    out = asyncio.run(ws2.set_parameter({"name": path, "value": value + 1}))
    assert out.get("is_error") and "propose_plan" in out["content"][0]["text"]


def test_the_plan_asks_only_questions_that_change_the_geometry():
    ws = plan_workspace()
    no_changes = {**geometry_question(ws), "changes": []}
    out = asyncio.run(ws.propose_plan({"steps": ["x"], "questions": [no_changes]}))
    assert out.get("is_error") and "changes" in out["content"][0]["text"]
    unknown = {**geometry_question(ws), "changes": ["not_a_parameter"]}
    out = asyncio.run(ws.propose_plan({"steps": ["x"], "questions": [unknown]}))
    assert out.get("is_error") and "not_a_parameter" in out["content"][0]["text"]
    many = [geometry_question(ws, n) for n in range(MAX_PLAN_QUESTIONS + 1)]
    out = asyncio.run(ws.propose_plan({"steps": ["x"], "questions": many}))
    assert out.get("is_error") and f"at most {MAX_PLAN_QUESTIONS}" in out["content"][0]["text"]
    assert ws.plan is None and not ws.sink.of_type("plan")
    # A constraint ID counts as geometry too.
    by_constraint = {**geometry_question(ws), "changes": [STANDARDS.standards[0].id]}
    ws.answer = _answer_with("b")
    out = asyncio.run(ws.propose_plan({"steps": ["x"], "questions": [by_constraint]}))
    assert not out.get("is_error")


def _answer_with(option):
    async def answer(question_id, text, options):
        return option

    return answer


def new_design_session(client) -> str:
    r = client.post("/api/sessions", json={"new_design": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_new_design_session_has_no_starting_case_and_needs_the_plan(
    client, services, monkeypatch
):
    sid = new_design_session(client)
    assert SESSIONS.get(sid).needs_plan()
    time.sleep(0.1)
    assert "build_ready" not in [t for _, t, _ in sse(client, sid)], "no starting case"
    # Nothing to set a value on yet: Claude's first edit writes the design.
    code, params = box_design(40, 30, 20, 2, "ABS")
    edit = ("write_design", {"code": code, "params": params})
    prompts, drivers = [], []
    steps = [edit, ("propose_plan", {"steps": ["Make the box"]}), edit, *FINISH]

    def driver(state, body, prompt):
        prompts.append(prompt)
        drivers.append(ScriptedDriver(state.ws, steps))
        return drivers[-1]

    monkeypatch.setattr(CONFIG, "driver", driver)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "Make a case"})
    found = settle(client, sid)
    assert NEW_DESIGN_NOTE in prompts[0]
    denied, _, edited = drivers[0].calls[:3]
    assert denied.denied and "propose_plan" in denied.denied and not edited.is_error
    kinds = [t for _, t, _ in found]
    assert "plan" in kinds and "build_ready" in kinds
    assert kinds.index("plan") < kinds.index("build_ready")
    assert not SESSIONS.get(sid).needs_plan()


def test_a_revision_update_on_the_existing_case_edits_without_a_plan(client, services, monkeypatch):
    sid = new_session(client)  # starts on the existing case, built on the previous revision
    state = SESSIONS.get(sid)
    assert not state.needs_plan()
    ws = state.ws
    path = next(k for k, v in ws.design.params.items() if isinstance(v, int | float))
    prompts, drivers = [], []
    steps = [("set_parameter", {"name": path, "value": ws.design.params[path] + 1}), *FINISH]

    def driver(state, body, prompt):
        prompts.append(prompt)
        drivers.append(ScriptedDriver(state.ws, steps))
        return drivers[-1]

    monkeypatch.setattr(CONFIG, "driver", driver)
    before = sse(client, sid)[-1][0]
    text = revision_message(ws.previous, ws.board)
    client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    found = settle(client, sid, after=before)
    (call,) = [c for c in drivers[0].calls if c.tool.endswith("set_parameter")]
    assert not call.denied and not call.is_error
    assert "plan" not in [t for _, t, _ in found] and "build_ready" in [t for _, t, _ in found]
    assert NEW_DESIGN_NOTE not in prompts[0] and not ws.plan_required


def test_answering_the_plans_question_continues_the_run(client, services, monkeypatch):
    sid = new_design_session(client)
    ws = SESSIONS.get(sid).ws
    # A new design has no parameters yet: the question changes a constraint.
    question = {**geometry_question(plan_workspace()), "changes": [NUMERIC[0].id]}
    say(
        client,
        monkeypatch,
        sid,
        "Make a case",
        [
            ("propose_plan", {"steps": ["Fit the board"], "questions": [question]}),
            ("text", "Building to your answer."),
            ("finish", {"outcome": "blocked", "summary": "Answered."}),
        ],
    )
    found = settle(client, sid, "waiting_for_answer")
    kinds = [t for _, t, _ in found]
    assert kinds.index("plan") < kinds.index("question")
    (asked,) = [d for _, t, d in found if t == "question"]
    assert asked["text"] == question["question"]
    r = client.post(
        f"/api/sessions/{sid}/messages",
        json={"answer": {"question_id": asked["question_id"], "option_id": "b"}},
    )
    assert r.status_code == 202
    found = settle(client, sid, after=found[-1][0])
    text = "".join(d["delta"] for _, t, d in found if t == "text")
    assert "Building to your answer." in text
    assert ws.plan["answers"][0]["answer"]["id"] == "b"
    assert ws.finished["outcome"] == "blocked"
    # The next run isn't a new design any more.
    assert not ws.needs_plan


def test_sink_events_carry_the_message():
    sink = EventSink("msg-9")
    sink.constraints([])
    sink.plan(["a"])
    sink.constraints([])
    kinds = [(e.type, getattr(e, "message_id", None)) for _, e in sink.events]
    assert kinds == [
        ("constraints", None),
        ("message_start", "msg-9"),
        ("plan", "msg-9"),
        ("constraints", "msg-9"),
    ]


# --- The mock API serves the same -----------------------------------------------------------


def test_the_mock_api_serves_chips_and_rechecks(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    target = NUMERIC[0]
    with TestClient(mock_app) as mock:
        sid = mock.post("/api/sessions", json={}).json()["id"]
        mock.post(f"/api/sessions/{sid}/messages", json={"text": phrase(target, other(target))})
        chips = {c["id"]: c for c in mock.get(f"/api/sessions/{sid}/constraints").json()}
        assert chips[target.id]["source"] == "user"
        for _ in range(500):
            if mock.get(f"/api/sessions/{sid}").json()["status"] != "running":
                break
            time.sleep(0.02)
        # The recorded run ends on a question: chips wait for the answer.
        url = f"/api/sessions/{sid}/constraints/{target.id}"
        assert mock.delete(url).status_code == 409
        events = sse(mock, sid)
        (q,) = [d for _, t, d in events if t == "question"]
        answer = {"question_id": q["question_id"], "option_id": q["options"][0]["id"]}
        mock.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
        for _ in range(500):
            if mock.get(f"/api/sessions/{sid}").json()["status"] == "idle":
                break
            time.sleep(0.02)
        after = sse(mock, sid)[-1][0]
        r = mock.delete(url)
        assert r.status_code == 200 and r.json()["source"] == "standard"
        for _ in range(500):
            found = sse(mock, sid, after)
            if found and found[-1][1] == "message_end":
                break
            time.sleep(0.02)
        assert [t for _, t, _ in found] == [
            "message_start",
            "constraints",
            "progress",
            "build_ready",
            "text",
            "message_end",
        ]
        assert "plan" in [t for _, t, _ in events]
