"""Sessions start empty; the board revision is one example, the agent designs anything, and
designs with no board get real builds, checks, and exports.

Claude is the scripted stand-in of the end-to-end backend (`app.e2e_api.script`), played through
the real session API, agent loop, tools and hooks. A design without a board is built in the
sandbox and checked by the check runner, as in production. The example comes from
`demos/b-parasite/examples.yaml` and `demos/b-parasite/CHANGES.md`, the standards from
`standards.yaml`.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent.events import EventSink
from app.agent.run import run_agent
from app.agent.scripted import ScriptedDriver, box_design
from app.agent.services import GeometryServices
from app.agent.tools import MAX_ROUNDS, Workspace, failing_line
from app.builds import BuildStore
from app.contracts.checks import CheckReport, validate_check_report
from app.contracts.mesh import validate_mesh
from app.contracts.session import Build, Session, validate_event
from app.examples import examples, load_examples
from app.main import app
from app.session_api import CONFIG, SESSIONS, fixture_services
from app.standards import load_standards

BOX = "make a box 40 x 30 x 20 mm with 2 mm walls"
BRACKET = "an L bracket with two M4 holes"


@pytest.fixture(autouse=True)
def _keep_config(monkeypatch):
    # Importing app.e2e_api swaps the driver and the services; other tests need the defaults.
    monkeypatch.setattr(CONFIG, "driver", CONFIG.driver)
    monkeypatch.setattr(CONFIG, "services", CONFIG.services)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The session API with the end-to-end backend's stand-in Claude, and its services: the
    fixtures for the board example, real builds for a design without a board."""
    from app import e2e_api

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


def sse(client, sid: str) -> list[tuple[str, dict]]:
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"})
    out = []
    for frame in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def run(client, sid: str, text: str) -> list[tuple[str, dict]]:
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": text})
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        found = sse(client, sid)
        status = client.get(f"/api/sessions/{sid}").json()["status"]
        if status != "running" and found and found[-1][0] in ("message_end", "question"):
            return found
        time.sleep(0.05)
    raise AssertionError(f"the run in {sid} never ended")


def keep_if_asked(client, sid: str, found: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """A design that fails a standard asks whether to keep it: only the user accepts a
    failure. Keep it, and return the events once the run has ended."""
    if not found or found[-1][0] != "question":
        return found
    answer = {"question_id": found[-1][1]["question_id"], "option_id": "keep"}
    r = client.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
    assert r.status_code == 202, r.text
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        found = sse(client, sid)
        status = client.get(f"/api/sessions/{sid}").json()["status"]
        if status == "idle" and found and found[-1][0] == "message_end":
            return found
        time.sleep(0.05)
    raise AssertionError(f"the run in {sid} never ended")


def extents(gltf: dict) -> list[float]:
    """The mesh's overall sizes, smallest first, from its position accessors' bounds."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for mesh in gltf["meshes"]:
        for primitive in mesh["primitives"]:
            accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
            lo = [min(a, b) for a, b in zip(lo, accessor["min"], strict=True)]
            hi = [max(a, b) for a, b in zip(hi, accessor["max"], strict=True)]
    return sorted(round(h - low, 3) for low, h in zip(lo, hi, strict=True))


def new_session(client, **body) -> Session:
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    return Session.model_validate(r.json())


# --- Examples ---------------------------------------------------------------------------------


def test_the_examples_are_listed_from_boards(client) -> None:
    listed = client.get("/api/examples").json()
    assert [e["id"] for e in listed] == [e.id for e in load_examples()]
    for entry, example in zip(listed, load_examples(), strict=True):
        previous, new = example.boards()
        assert entry["title"] == f"{new.name} {previous.revision} to {new.revision}"
        assert entry["description"]


def test_opening_the_example_starts_the_board_revision(client) -> None:
    example = examples()[0]
    previous, new = example.boards()
    session = new_session(client, example=example.id)
    assert session.title == example.title
    assert (session.board.name, session.board.revision) == (new.name, new.revision)
    assert session.next_revision == session.board
    deadline = time.monotonic() + 60
    while not client.get(f"/api/sessions/{session.id}/builds").json():
        assert time.monotonic() < deadline, "the existing design was never built"
        time.sleep(0.05)
    (build,) = client.get(f"/api/sessions/{session.id}/builds").json()
    assert build["board"] == {"name": previous.name, "revision": previous.revision}


def test_an_unknown_example_is_404(client) -> None:
    assert client.post("/api/sessions", json={"example": "no-such-example"}).status_code == 404


def test_an_empty_session_has_no_revision_to_load(client) -> None:
    session = new_session(client)
    assert session.board is None and session.next_revision is None
    r = client.post(f"/api/sessions/{session.id}/revisions/next", json={})
    assert r.status_code == 409


# --- Designs from scratch, with real builds and checks ---------------------------------------


def test_make_a_box_builds_checks_and_shows_it(client) -> None:
    session = new_session(client)
    found = keep_if_asked(client, session.id, run(client, session.id, BOX))
    for _, data in found:
        validate_event(data)
    builds = [Build.model_validate(d["build"]) for kind, d in found if kind == "build_ready"]
    assert builds and builds[-1].status == "ready" and builds[-1].board is None
    build = builds[-1]
    base = f"/api/sessions/{session.id}/builds/{build.id}"

    # The mesh is the box: 40 x 30 x 20 mm.
    mesh = client.get(f"{base}/mesh")
    assert mesh.status_code == 200
    assert set(validate_mesh(mesh.json()).parts) == {"box"}
    assert extents(mesh.json()) == [20.0, 30.0, 40.0]

    # The checks: only the standards that apply, the rest not applicable with a reason.
    report = client.get(f"{base}/checks").json()
    CheckReport.model_validate(report)
    validate_check_report(report, standards=load_standards())
    assert report["board"] is None
    standards = load_standards().standards
    by_standard = {r["standard"]: r for r in report["results"] if r["standard"]}
    for standard in standards:
        result = by_standard[standard.id]
        if standard.applies_to != "any":  # a board, or an object the box does not fit
            assert result["status"] == "not_applicable" and result["reason"], result
        else:
            assert result["status"] in ("pass", "fail"), result
            assert result["measured"] is not None
    assert report["summary"]["not_applicable"] == sum(s.applies_to != "any" for s in standards)
    assert build.checks.not_applicable == report["summary"]["not_applicable"]
    assert not any(r["check"] in ("openings", "board_fit") for r in report["results"])

    # The chat says what was built; a failing standard was the user's to accept (asked
    # first), and the failing checks are listed when the run finishes.
    messages = client.get(f"/api/sessions/{session.id}/messages").json()
    said = " ".join(m["text"] for m in messages if m["role"] == "assistant")
    assert "box" in said
    failing = [r for r in report["results"] if r["status"] == "fail"]
    if any(r["standard"] for r in failing):
        assert any(kind == "question" for kind, _ in sse(client, session.id))
    for r in failing:  # named as people name them (the standard's title), not by ID
        assert r["title"] in said

    # A design without a board has no board to serve.
    assert client.get(f"{base}/board").status_code == 404


def test_an_l_bracket_is_designed_from_scratch(client) -> None:
    session = new_session(client)
    found = keep_if_asked(client, session.id, run(client, session.id, BRACKET))
    builds = [Build.model_validate(d["build"]) for kind, d in found if kind == "build_ready"]
    assert builds and builds[-1].status == "ready" and builds[-1].board is None
    source = client.get(f"/api/sessions/{session.id}/builds/{builds[-1].id}/source").json()
    assert "hole_2" in source["code"]
    messages = client.get(f"/api/sessions/{session.id}/messages").json()
    assert "bracket" in " ".join(m["text"] for m in messages if m["role"] == "assistant")
    params = {p["name"] for p in client.get(f"/api/sessions/{session.id}/parameters").json()}
    assert {"length", "thickness", "hole_diameter"} <= params


def test_a_build_with_no_board_exports_step_stl_and_the_build_note(client) -> None:
    session = new_session(client)
    found = run(client, session.id, BOX)
    build = [d["build"] for kind, d in found if kind == "build_ready"][-1]
    r = client.post(
        f"/api/sessions/{session.id}/exports",
        json={"build_id": build["id"], "formats": ["step", "stl", "build_note"]},
    )
    assert r.status_code == 201, r.text
    files = {f["format"]: f for f in r.json()["files"]}
    assert set(files) == {"step", "stl", "build_note"}
    note = client.get(files["build_note"]["url"]).text
    assert "No board." in note and "## Not applicable" in note
    step = client.get(files["step"]["url"])
    assert step.status_code == 200 and b"ISO-10303-21" in step.content[:200]
    assert client.get(files["stl"]["url"]).status_code == 200


def test_anything_else_in_an_empty_session_gets_a_question(client) -> None:
    session = new_session(client)
    found = run(client, session.id, "Something for my desk")
    assert any(kind == "question" for kind, _ in found)


# --- The agent: honest results, not a pass gate ----------------------------------------------


def test_when_the_rounds_run_out_the_last_design_is_shown_with_its_failures(tmp_path) -> None:
    standards = load_standards()
    ws = Workspace(
        standards=standards,
        services=GeometryServices(None, standards, store=BuildStore(tmp_path / "builds")),
        sink=EventSink("msg-1"),
    )
    material = str(standards.get("material").value)
    thin = float(standards.get("min_wall").value) / 2  # too thin for the wall standard
    edits = [
        (
            "write_design",
            {
                "code": box_design(40, 30, 20, thin, material)[0],
                "params": box_design(40, 30, 20, thin, material)[1],
            },
        ),
        *[("set_parameter", {"name": "length", "value": 40 + n}) for n in range(1, MAX_ROUNDS)],
        ("set_parameter", {"name": "length", "value": 50}),  # past the last round: denied
        # Only the user accepts a failing standard: Claude asks, and the user keeps the design.
        (
            "ask_user",
            {
                "question": "The walls fail min_wall. Keep the design as it is?",
                "options": [{"id": "keep", "label": "Keep it"}, {"id": "stop", "label": "Stop"}],
            },
        ),
        ("finish", {"outcome": "done", "summary": "A box; the walls are too thin."}),
    ]

    async def keep(question_id, text, options):
        return "keep"

    ws.answer = keep
    driver = ScriptedDriver(ws, edits)
    result = asyncio.run(run_agent(ws, BOX, driver=driver))
    assert driver.calls[MAX_ROUNDS].denied  # out of rounds
    assert not driver.calls[-1].is_error
    assert result.error is None and result.finished["outcome"] == "done"
    assert not result.passing
    shown = ws.sink.of_type("build_ready")[-1].build
    assert shown.status == "ready" and shown.id == ws.latest.build.id
    failing = [i for i in ws.report if i.status == "fail"]
    assert failing and failing_line(failing, ws.standards) in result.text


def test_the_stand_in_designs_from_scratch_only_without_a_board() -> None:
    from app import e2e_api

    standards = load_standards()
    options = SimpleNamespace(model=None, effort=None)
    body = SimpleNamespace(text=BOX, selection=None)
    empty = SimpleNamespace(board=None, standards=standards)
    steps = e2e_api.script(body, BOX, options, empty)
    kinds = [k for k, _ in steps]
    assert kinds.index("write_design") < kinds.index("run_checks") < kinds.index("finish")
    params = dict(steps)["write_design"]["params"]
    assert (params["length"], params["width"], params["height"], params["wall"]) == (40, 30, 20, 2)
    assert params["material"] == standards.get("material").value


# --- The mock API: the same examples and empty sessions --------------------------------------


@pytest.fixture
def mock(monkeypatch):
    from app import mock_api

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    mock_api.SESSIONS.clear()
    with TestClient(mock_api.app) as test_client:
        yield test_client


def test_the_mock_lists_the_same_examples(mock, client) -> None:
    assert mock.get("/api/examples").json() == client.get("/api/examples").json()


def test_a_mock_session_starts_empty_unless_it_opens_the_example(mock) -> None:
    empty = new_session(mock)
    assert empty.board is None and empty.next_revision is None
    assert mock.get(f"/api/sessions/{empty.id}/builds").json() == []
    example = examples()[0]
    opened = new_session(mock, example=example.id)
    assert opened.title == example.title
    assert opened.board is not None and opened.next_revision is not None
    assert mock.post("/api/sessions", json={"example": "no-such-example"}).status_code == 404


# --- The check-results contract: not applicable ----------------------------------------------


def box_report(tmp_path) -> dict:
    standards = load_standards()
    services = GeometryServices(None, standards, store=BuildStore(tmp_path / "builds"))
    code, params = box_design(40, 30, 20, 2, str(standards.get("material").value))
    outcome = services.build({"code": code, "params": params, "backend": "build123d"})
    assert outcome.ok, outcome.error
    return outcome.artifacts.report


def test_a_standard_that_applies_cannot_be_reported_not_applicable(tmp_path) -> None:
    from app.contracts.common import ContractError

    report = box_report(tmp_path)
    standards = load_standards()
    validate_check_report(report, standards=standards)
    applies = next(s for s in standards.standards if s.applies_to == "any")
    broken = json.loads(json.dumps(report))
    for r in broken["results"]:
        if r["standard"] == applies.id:
            if r["status"] in ("pass", "fail"):
                broken["summary"][r["status"]] -= 1
                broken["summary"]["not_applicable"] += 1
            r.update(status="not_applicable", reason="no", measured=None, required=None)
            r["location"] = None
    with pytest.raises(ContractError, match="applies to this design"):
        validate_check_report(broken, standards=standards)


def test_a_not_applicable_result_needs_its_reason(tmp_path) -> None:
    report = box_report(tmp_path)
    broken = json.loads(json.dumps(report))
    next(r for r in broken["results"] if r["status"] == "not_applicable")["reason"] = None
    with pytest.raises(ValueError, match="reason"):
        CheckReport.model_validate(broken)
