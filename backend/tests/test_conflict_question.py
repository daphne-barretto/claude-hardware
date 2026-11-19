"""Questions and the can't-satisfy state: the conflict fixture from conflict detection as a recorded
run whose question carries the clashing constraints, their locations and the options, and the
mock API continuing the run when an option is chosen.

Nothing here names a board, part or dimension: the conflict is recomputed from `demos/b-parasite/`
and `standards.yaml` with the conflict detector and compared with the fixture.
"""

from __future__ import annotations

import json
import math
import time
from functools import cache

import pytest
from fastapi.testclient import TestClient

from app.contracts.common import FIXTURES_DIR, ContractError, check_location
from app.contracts.mesh import validate_mesh
from app.contracts.session import QuestionEvent, validate_event, validate_recording
from app.examples import example_standards
from app.fixtures import conflict as cf
from app.fixtures.generate import boards, design_for
from app.fixtures.geometry import Enclosure
from app.mock_api import SESSIONS, app


@cache
def recording() -> list:
    lines = (FIXTURES_DIR / "conflict-events.jsonl").read_text().splitlines()
    return [r.event for r in validate_recording(lines)]


def question() -> QuestionEvent:
    return next(e for e in recording() if e.type == "question")


@cache
def detected():
    _, new, _ = boards()
    return cf.detect(new, example_standards())


@cache
def mesh():
    return validate_mesh(FIXTURES_DIR / "enclosure.gltf")


@cache
def parts():
    old, _, _ = boards()
    return {p.name: p for p in Enclosure(design_for(old, example_standards())).build()}


def distance_to_face(location) -> float:
    (face_id,) = location.faces
    face = next(f for f in parts()[location.part].faces if f.id == face_id)
    p = location.point
    return min(math.dist(p, cf.closest_on_triangle(p, *tri)) for tri in face.triangles)


def parse_sse(text: str) -> list[tuple[int, str, dict]]:
    events = []
    for frame in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":")
        )
        if fields:
            events.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return events


def wait_for(client, sid: str, event_type: str, after: int = 0) -> list[tuple[int, str, dict]]:
    for _ in range(200):
        r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
        events = parse_sse(r.text)
        if any(e[1] == event_type for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"no {event_type} event")


def session_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "session.json").read_text())


# --- The fixture ---------------------------------------------------------------------------------


def test_the_conflict_run_ends_with_a_build_and_a_conflict_question() -> None:
    types = [e.type for e in recording()]
    assert types[-2:] == ["question", "message_end"]
    assert "build_ready" in types and "progress" in types
    assert question().conflict is not None


def test_the_question_carries_the_clashing_constraints_from_conflict_detection() -> None:
    conflict, expected = question().conflict, detected()
    assert conflict.id == expected.id
    assert conflict.over_by == expected.over_by > 0
    got = [(c.kind, c.id, c.value, c.best) for c in conflict.constraints]
    assert got == [(c.kind, c.id, c.value, c.best) for c in expected.constraints]
    # Conflict detection's clash: a lock, the clearance standard, and at least the height limit.
    _, new, _ = boards()
    locks = cf.conflict_locks_for(new, example_standards())
    kinds = {(c.kind, c.id) for c in conflict.constraints}
    assert {("locked", path) for path in locks} <= kinds
    assert {("standard", "min_component_clearance"), ("standard", "max_height")} <= kinds


def test_each_constraint_says_how_it_binds_and_what_to_call_it() -> None:
    standards = example_standards()
    _, new, _ = boards()
    for c in question().conflict.constraints:
        if c.kind == "standard":
            s = standards.get(c.id)
            assert c.label == s.title
            assert c.relation == {"min": ">=", "max": "<="}[s.kind]
            assert c.value == float(s.value)
        else:
            assert c.relation == "="
        if c.kind == "board":
            part = next(p for p in new.components if p.ref == c.id)
            assert c.value == part.height and c.best is None
            assert c.id in c.label


def test_every_location_is_a_face_of_the_fixture_mesh_on_its_part() -> None:
    q = question()
    locations = [q.location, q.conflict.location]
    locations += [loc for c in q.conflict.constraints for loc in c.locations]
    for loc in locations:
        check_location(loc, mesh(), "conflict")
        assert loc.part and len(loc.faces) == 1
        assert distance_to_face(loc) < 1e-3, "the point is on its face"


def test_the_question_is_pinned_where_the_conflict_is() -> None:
    q = question()
    assert q.location == q.conflict.location
    # At the board part the clash squeezes, on the same part conflict detection reports.
    assert q.conflict.location.part == detected().location["part"]
    squeezed = [c for c in q.conflict.constraints if c.kind == "board"]
    assert squeezed and any(loc == q.location for c in squeezed for loc in c.locations)


def test_the_options_give_way_one_constraint_at_a_time() -> None:
    q = question()
    conflict = q.conflict
    assert 2 <= len(q.options) <= cf.MAX_OPTIONS
    ids = [o.id for o in q.options]
    assert len(set(ids)) == len(ids)
    for c in conflict.constraints:
        if c.kind == "locked":
            option = next(o for o in q.options if o.id == f"unlock-{c.id}")
            assert f"{c.best:g} mm" in option.description
    for o in q.options:
        if o.id.startswith("relax-"):
            c = next(c for c in conflict.constraints if f"relax-{c.id}" == o.id)
            assert c.kind == "standard" and f"{c.best:g} mm" in o.label
    board_parts = [c.id for c in conflict.constraints if c.kind == "board"]
    board_option = next(o for o in q.options if o.id == "ask-board-team")
    assert all(ref in board_option.description for ref in board_parts)


def test_the_prompt_locks_the_fixtures_values() -> None:
    _, new, _ = boards()
    locks = cf.conflict_locks_for(new, example_standards())
    prompt = session_fixture()["conflict_prompt"]
    for path, value in locks.items():
        assert f"{path} at {value:g} mm" in prompt


def test_closest_point_on_a_triangle() -> None:
    tri = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0))
    assert cf.closest_on_triangle((0.5, 0.5, 3.0), *tri) == (0.5, 0.5, 0.0)
    assert cf.closest_on_triangle((-1.0, -1.0, 0.0), *tri) == (0.0, 0.0, 0.0)
    assert cf.closest_on_triangle((3.0, -1.0, 0.0), *tri) == (2.0, 0.0, 0.0)
    assert cf.closest_on_triangle((1.0, -1.0, 0.0), *tri) == (1.0, 0.0, 0.0)
    assert cf.closest_on_triangle((2.0, 2.0, 0.0), *tri) == (1.0, 1.0, 0.0)


# --- The contract --------------------------------------------------------------------------------


def test_a_plain_question_has_no_conflict_or_location() -> None:
    lines = (FIXTURES_DIR / "session-events.jsonl").read_text().splitlines()
    plain = next(r.event for r in validate_recording(lines) if r.event.type == "question")
    assert plain.conflict is None and plain.location is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda c: c["constraints"].__delitem__(slice(1, None)), "a single constraint"),
        (lambda c: c["constraints"][0].update(kind="wish"), "an unknown kind"),
        (lambda c: c["constraints"][0].update(relation="<"), "an unknown relation"),
        (lambda c: c["constraints"][0].update(locations=[]), "no location"),
        (lambda c: c.update(over_by=-1), "a negative distance"),
        (lambda c: c.pop("location"), "no location for the conflict"),
    ],
)
def test_the_conflict_contract_rejects(change, problem) -> None:
    data = question().model_dump(mode="json")
    change(data["conflict"])
    with pytest.raises(ContractError):
        validate_event(data)


# --- The mock API --------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    SESSIONS.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_the_conflict_prompt_plays_the_conflict_run(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(
        f"/api/sessions/{sid}/messages", json={"text": session_fixture()["conflict_prompt"]}
    )
    assert r.status_code == 202
    events = wait_for(client, sid, "message_end")
    asked = next(data for _, kind, data in events if kind == "question")
    expected = question().model_dump(mode="json")
    assert {k: v for k, v in asked.items() if k != "message_id"} == {
        k: v for k, v in expected.items() if k != "message_id"
    }
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "waiting_for_answer"
    assert client.get(f"/api/sessions/{sid}/builds").json(), "the locked design was built"


@pytest.mark.parametrize("option", range(len(question().options)))
def test_choosing_an_option_continues_the_run(client, option) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(
        f"/api/sessions/{sid}/messages", json={"text": session_fixture()["conflict_prompt"]}
    )
    events = wait_for(client, sid, "message_end")
    last = events[-1][0]
    q = question()
    chosen = q.options[option]
    r = client.post(
        f"/api/sessions/{sid}/messages",
        json={"answer": {"question_id": q.question_id, "option_id": chosen.id}},
    )
    assert r.status_code == 202
    reply = wait_for(client, sid, "message_end", after=last)
    kinds = [kind for _, kind, _ in reply]
    # The answer as the user's message, then the run going on.
    assert kinds[:2] == ["user_message", "message_start"] and kinds[-1] == "message_end"
    for _, _, data in reply:
        validate_event(data)
    text = "".join(data["delta"] for _, kind, data in reply if kind == "text")
    assert chosen.label in text
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"
    # The question is answered: answering again is refused.
    again = client.post(
        f"/api/sessions/{sid}/messages",
        json={"answer": {"question_id": q.question_id, "option_id": chosen.id}},
    )
    assert again.status_code == 409


def test_the_first_message_still_plays_the_main_run(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": session_fixture()["prompt"]})
    events = wait_for(client, sid, "message_end")
    asked = next(data for _, kind, data in events if kind == "question")
    assert asked.get("conflict") is None
