"""The session API: the whole demo script over HTTP, without the frontend, and the
contract (`contracts/session-api.openapi.json`).

Claude is played by `ScriptedDriver` (the same tools and hooks, no model). The scenario comes
from `demos/b-parasite/`, `standards.yaml`, `fixtures/` and `demos/b-parasite/demo-script.md`.
"""

from __future__ import annotations

import io
import json
import re
import struct
import time

import pytest
from fastapi.testclient import TestClient

from app.agent.design import set_path
from app.agent.run import scenario_boards
from app.agent.scripted import ScriptedDriver
from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import CheckReport, validate_check_report
from app.contracts.common import FIXTURES_DIR, REPO_ROOT
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from app.contracts.session import (
    Build,
    BuildDiff,
    BuildSource,
    Comment,
    Export,
    Message,
    Parameter,
    Session,
    assert_matches_session_api,
    validate_event,
)
from app.examples import examples
from app.main import app
from app.session_api import (
    CONFIG,
    NOT_CONFIGURED,
    SESSIONS,
    fixture_services,
    geometry_services,
)
from app.standards import load_standards
from geometry.conflicts import conflict_locks

# The board revision example: these tests walk its board and existing design.
EXAMPLE = examples()[0].id


class Claude:
    """Plays one script per run, in order, and records the prompts and drivers."""

    def __init__(self) -> None:
        self.scripts: list = []
        self.prompts: list[str] = []
        self.drivers: list[ScriptedDriver] = []

    def __call__(self, state, body, prompt):
        self.prompts.append(prompt)
        steps = self.scripts.pop(0)
        driver = ScriptedDriver(state.ws, steps(state.ws) if callable(steps) else steps)
        self.drivers.append(driver)
        return driver


@pytest.fixture
def claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The API on the contract fixtures (`FixtureServices`), which are fast; `real` swaps in the
    real services."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def real(client, monkeypatch):
    """The API on the real services: sandboxed builds, the check runner, conflict detection and
    the build store."""
    monkeypatch.setattr(CONFIG, "services", geometry_services)
    return client


def parse_sse(text: str) -> list[tuple[int, str, dict]]:
    events = []
    for frame in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":")
        )
        if fields:
            events.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return events


def events(client, sid: str, after: int = 0) -> list[tuple[int, str, dict]]:
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    return parse_sse(r.text)


def wait_until(client, sid: str, status: str, after: int = 0) -> list[tuple[int, str, dict]]:
    """The events after `after` once the session reaches `status` with nothing running."""
    deadline = time.monotonic() + 120  # real builds and checks take seconds each
    while True:
        session = client.get(f"/api/sessions/{sid}").json()
        new = events(client, sid, after)
        if session["status"] == status and new and new[-1][1] in ("message_end", "question"):
            return new
        if time.monotonic() > deadline:
            break
        time.sleep(0.02)
    raise AssertionError(f"session {sid} never reached {status}: {session}")


def send(client, sid: str, **body) -> Message:
    r = client.post(f"/api/sessions/{sid}/messages", json=body)
    assert r.status_code == 202, r.text
    return Message.model_validate(r.json())


def go_on_after_edit(client, sid: str, text: str) -> None:
    """Claude goes on after a hand edit. An edit that broke a standard (on real builds) left
    the decision to the user, with the conflict: let Claude adjust the rest around it, which
    starts the run. Otherwise the user asks in words."""
    decision = SESSIONS.get(sid).edit_decision
    if decision is None:
        send(client, sid, text=text)
        return
    (question,) = [d for _, t, d in events(client, sid) if t == "question"][-1:]
    assert question["question_id"] == decision.question_id and question["location"]
    answer = {"question_id": decision.question_id, "option_id": "adjust"}
    send(client, sid, answer=answer)


def start(client) -> tuple[str, list[tuple[int, str, dict]]]:
    r = client.post("/api/sessions", json={"example": EXAMPLE})
    assert r.status_code == 201, r.text
    sid = Session.model_validate(r.json()).id
    for _ in range(5000):
        found = events(client, sid)
        if any(t == "build_ready" for _, t, _ in found):
            return sid, found
        time.sleep(0.02)
    raise AssertionError("the first build never arrived")


def last_id(client, sid: str) -> int:
    found = events(client, sid)
    return found[-1][0] if found else 0


def changes():
    return parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())


def demo_steps() -> list[str]:
    text = (BOARDS_DIR / "demo-script.md").read_text()
    return re.findall(r"^\d+\. \*\*(.+?)\*\*", text, flags=re.M)


def assert_contract_events(found) -> None:
    ids = [i for i, _, _ in found]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    for _, kind, data in found:
        assert validate_event(data).type == kind


# --- The demo script, end to end ------------------------------------------------------------


def test_the_demo_script_runs_through_the_api(real, claude) -> None:
    """On the real services: every build is built in the sandbox, checked by the check runner,
    stored with its history, and served from the store."""
    client = real
    # The demo script has the nine steps this test walks.
    assert len(demo_steps()) == 9
    standards = load_standards()
    doc = changes()
    new_revision = json.loads((BOARDS_DIR / doc.new_file).read_text())["revision"]

    # 1. Start: New opens the existing enclosure, built and checked.
    sid, found = start(client)
    assert_contract_events(found)
    (first,) = [d for _, t, d in found if t == "build_ready"]
    assert first["message_id"] is None
    build = Build.model_validate(first["build"])
    assert build.status == "ready"
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["latest_build_id"] == build.id and session["status"] == "idle"
    assert session["board"]["revision"] == new_revision
    mesh = validate_mesh(client.get(build.mesh_url).content)
    first_report = client.get(build.checks_url).json()
    validate_check_report(first_report, standards, mesh)
    # A real build: the existing design checked by the check runner against the board it was
    # designed for, stored with that report and served from the store.
    store = CONFIG.store()
    record = store.get(build.id)
    old_revision = json.loads((BOARDS_DIR / doc.old_file).read_text())["revision"]
    assert record.parent is None and record.board.revision == old_revision
    assert CheckReport.model_validate(first_report) == CheckReport.model_validate(record.checks)
    assert (
        first_report["results"] != json.loads((FIXTURES_DIR / "checks.json").read_text())["results"]
    )
    assert build.maps
    source = BuildSource.model_validate(client.get(build.source_url).json())
    assert source.code == record.code and source.features
    for map_type in build.maps:
        amap = client.get(f"/api/sessions/{sid}/builds/{build.id}/maps/{map_type}").json()
        validate_analysis_map(amap, mesh, standards)

    # 2. The new revision arrives: Claude lists what changed and which checks fail, and where.
    #    3. It proposes the fix: an edit, rechecked, with a build that carries its checks.
    change_list = "".join(f"\n- {c['Component']}: {c['Change']}" for c in doc.rows)
    claude.scripts.append(
        [
            ("read_spec", {}),
            ("read_board", {"revision": "new"}),
            ("run_checks", {}),
            ("text", f"Revision {new_revision} changes:{change_list}"),
            ("set_parameter", {"name": "clearance", "value": 0.6}),
            (
                "ask_user",
                {
                    "question": "Apply the proposed changes?",
                    "options": [
                        {"id": "accept", "label": "Accept"},
                        {"id": "reject", "label": "Reject"},
                    ],
                },
            ),
            ("finish", {"outcome": "blocked", "summary": "Waiting on review."}),
        ]
    )
    prompt = json.loads((FIXTURES_DIR / "session.json").read_text())["prompt"]
    after = last_id(client, sid)
    send(client, sid, text=prompt, model="claude-opus-5-5", effort="high")
    found = wait_until(client, sid, "waiting_for_answer", after)
    assert_contract_events(found)
    kinds = [t for _, t, _ in found]
    # The user's message, its constraints (the chips), then Claude's reply.
    assert kinds[:3] == ["user_message", "constraints", "message_start"]
    assert kinds[-1] == "question"
    # The revision updates the existing case, so it edits without a build plan.
    assert not claude.drivers[0].calls[3].denied
    assert "plan" not in kinds and "propose_plan" not in claude.prompts[0]
    progress = [d["text"] for _, t, d in found if t == "progress"]
    assert f"Reading board revision {new_revision}" in progress
    assert any("round 1 of 4" in p for p in progress)
    text = "".join(d["delta"] for _, t, d in found if t == "text")
    for row in doc.rows:
        assert row["Component"] in text
    builds = [Build.model_validate(d["build"]) for _, t, d in found if t == "build_ready"]
    assert len(builds) == 2 and len({b.id for b in builds} | {build.id}) == 3
    for b in builds:
        report = client.get(b.checks_url).json()
        b_mesh = validate_mesh(client.get(b.mesh_url).content)
        validate_check_report(report, standards, b_mesh)
        assert report["summary"] == b.checks.model_dump()
        failing = [r for r in report["results"] if r["status"] == "fail"]
        assert all(r["location"]["faces"] for r in failing)
    assert claude.drivers[0].calls[3].context, "the checks rerun after the edit"

    # 4. Review: answer the proposal, then ask about a face with it selected.
    (question,) = [d for _, t, d in found if t == "question"]
    after = found[-1][0]
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "accept"})
    found = wait_until(client, sid, "idle", after)
    assert found[-1][1] == "message_end"
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"

    gltf = json.loads(client.get(build.mesh_url).text)
    part, face = next(
        (n["extras"]["part"], f["id"])
        for n in gltf["nodes"]
        for f in n.get("extras", {}).get("faces", [])
    )
    claude.scripts.append(
        [
            ("text", "That face moved with the sensor window."),
            ("finish", {"outcome": "blocked", "summary": "Answered."}),
        ]
    )
    after = found[-1][0]
    selection = {"part": part, "faces": [face]}
    sent = send(client, sid, text="Why did this change?", selection=selection)
    assert sent.selection.faces == [face]
    wait_until(client, sid, "idle", after)
    assert face in claude.prompts[-1], "the selection is sent to Claude as context"
    messages = [
        Message.model_validate(m) for m in client.get(f"/api/sessions/{sid}/messages").json()
    ]
    assert messages[-1].role == "assistant" and "sensor window" in messages[-1].text

    # 5. Direct edit: a hand edit locks the value, and Claude reworks the rest around it.
    params = {
        p.name: p
        for p in map(Parameter.model_validate, client.get(f"/api/sessions/{sid}/parameters").json())
    }
    r = client.patch(
        f"/api/sessions/{sid}/parameters/draft", json={"value": params["draft"].value + 0.5}
    )
    assert r.status_code == 200
    edited = Parameter.model_validate(r.json())
    assert edited.locked and edited.value == params["draft"].value + 0.5
    claude.scripts.append(
        [
            ("set_parameter", {"name": "draft", "value": params["draft"].value}),
            ("set_parameter", {"name": "clearance", "value": 0.55}),
            ("finish", {"outcome": "blocked", "summary": "Reworked around the draft."}),
        ]
    )
    after = found[-1][0]
    go_on_after_edit(client, sid, "Rework the design around my draft.")
    found = wait_until(client, sid, "idle", after)
    denied, applied = claude.drivers[-1].calls[:2]
    assert denied.denied and "locked" in denied.denied and applied.denied is None
    params = {
        p.name: p
        for p in map(Parameter.model_validate, client.get(f"/api/sessions/{sid}/parameters").json())
    }
    assert params["draft"].locked and params["draft"].value == edited.value
    assert params["clearance"].value == 0.55 and not params["clearance"].locked

    # 6. Conflict: lock a value that can't satisfy the standards; Claude shows the clash and asks.
    ws = SESSIONS.get(sid).ws
    ((path, value),) = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    r = client.patch(f"/api/sessions/{sid}/parameters/{path}", json={"value": value})
    assert r.status_code == 200 and r.json()["locked"]
    claude.scripts.append(
        [
            ("run_checks", {}),
            (
                "ask_user",
                {
                    "question": "The locked value conflicts with the standards. What now?",
                    "options": [
                        {"id": "unlock", "label": "Change it", "approves": [path]},
                        {"id": "keep", "label": "Keep it"},
                    ],
                },
            ),
            ("finish", {"outcome": "blocked", "summary": "Kept the lock."}),
        ]
    )
    after = found[-1][0]
    go_on_after_edit(client, sid, "Check it again.")
    found = wait_until(client, sid, "waiting_for_answer", after)
    report = json.loads(claude.drivers[-1].calls[0].text)
    assert report["conflicts"], "run_checks reports the conflict"
    (conflict,) = report["conflicts"]  # from conflict detection, located on the mesh
    assert path in {c["id"] for c in conflict["constraints"] if c["kind"] == "locked"}
    assert conflict["location"]["faces"]
    question = [d for _, t, d in found if t == "question"][-1]  # Claude's, after any decision
    assert [o["id"] for o in question["options"]] == ["unlock", "keep"]
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "keep"})
    found = wait_until(client, sid, "idle", found[-1][0])
    assert ws.design.locked[path] == value

    # 7. Comment: pinned to a face, it stays attached after the model rebuilds.
    builds = [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": builds[0].id, "face_id": face, "point": [0, 0, 0], "text": "Check this"},
    )
    assert r.status_code == 201
    comment = Comment.model_validate(r.json())
    latest = builds[-1]
    assert latest.id != builds[0].id
    later = json.loads(client.get(latest.mesh_url).text)
    assert face in {f["id"] for n in later["nodes"] for f in n.get("extras", {}).get("faces", [])}
    listed = client.get(f"/api/sessions/{sid}/comments").json()
    assert [Comment.model_validate(c) for c in listed] == [comment]

    # 8. Compare: every build is kept, with its checks, newest last, and its history and diff.
    assert len(builds) >= 4
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == latest.id
    for b in (builds[0], latest):
        assert client.get(f"/api/sessions/{sid}/builds/{b.id}").json()["id"] == b.id
    assert builds[0].id in [r.id for r in store.history(latest.id)]
    r = client.get(f"/api/sessions/{sid}/builds/{latest.id}/diff", params={"against": build.id})
    assert r.status_code == 200, r.text
    diff = BuildDiff.model_validate(r.json())
    assert (diff.old, diff.new) == (build.id, latest.id)
    changed = {c.name: c for c in diff.parameters}
    assert changed["clearance"].new == 0.55 and changed[path].new == value
    assert diff == BuildDiff.model_validate(store.diff(build.id, latest.id))
    # The build history: the nearest ancestor the session announced (a run's intermediate
    # builds are stored too).
    ancestors = [r.id for r in store.history(latest.id)[1:]]
    parent = next(a for a in ancestors if a in {b.id for b in builds})
    r = client.get(f"/api/sessions/{sid}/builds/{latest.id}/diff", params={"against": parent})
    assert BuildDiff.model_validate(r.json()) == BuildDiff.model_validate(
        store.diff(parent, latest.id)
    )

    # 9. Export STEP, STL and the build note, and check the files open.
    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": latest.id, "formats": ["step", "stl", "build_note"]},
    )
    assert r.status_code == 201
    export = Export.model_validate(r.json())
    files = {f.format: client.get(f.url) for f in export.files}
    assert all(f.status_code == 200 for f in files.values())
    assert files["step"].content.startswith(b"ISO-10303-21")
    stl = files["stl"].content
    (count,) = struct.unpack_from("<I", stl, 80)
    assert count > 0 and len(stl) == 84 + 50 * count
    assert latest.id in files["build_note"].text
    step_url = client.get(f"/api/sessions/{sid}/builds/{latest.id}").json()["step_url"]
    assert step_url == next(f.url for f in export.files if f.format == "step")

    # The whole stream follows the contract.
    assert_contract_events(events(client, sid))


# --- Contract and docs ------------------------------------------------------------------------


def test_the_api_matches_the_session_api_contract() -> None:
    assert_matches_session_api(app.openapi())


def test_the_openapi_schema_is_published_at_docs(client) -> None:
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower()
    assert "/api/openapi.json" in r.text
    doc = client.get("/api/openapi.json").json()
    assert_matches_session_api(doc)
    assert "SessionEvent" in doc["components"]["schemas"]


# --- Messages and runs --------------------------------------------------------------------------


def test_a_message_while_running_or_waiting_is_refused(client, claude) -> None:
    sid, _ = start(client)
    claude.scripts.append(
        [
            (
                "ask_user",
                {
                    "question": "Which?",
                    "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                },
            ),
            ("finish", {"outcome": "blocked", "summary": "ok"}),
        ]
    )
    send(client, sid, text="go")
    found = wait_until(client, sid, "waiting_for_answer")
    (question,) = [d for _, t, d in found if t == "question"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "again"})
    assert r.status_code == 409 and "answer" in r.json()["detail"]
    for answer in ({"question_id": "q-9", "option_id": "a"},
                   {"question_id": question["question_id"], "option_id": "c"}):  # fmt: skip
        r = client.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
        assert r.status_code == 409
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "b"})
    wait_until(client, sid, "idle")
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "  "})
    assert r.status_code == 409


def test_without_an_api_key_the_run_reports_an_error(client, monkeypatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    sid, found = start(client)
    send(client, sid, text="go")
    found = wait_until(client, sid, "idle", found[-1][0])
    assert [t for _, t, _ in found] == [
        "user_message",
        "constraints",
        "message_start",
        "error",
        "message_end",
    ]
    # Said plainly: the server isn't set up; the details go to the log, not the chat.
    assert found[3][2]["message"] == NOT_CONFIGURED
    assert found[3][2]["reason"] == "not_configured" and not found[3][2]["retryable"]
    assert "ANTHROPIC_API_KEY" not in json.dumps(found) and "sk-" not in json.dumps(found)


def test_events_resume_after_last_event_id(client, claude) -> None:
    sid, _ = start(client)
    claude.scripts.append([("text", "Hello."), ("finish", {"outcome": "done", "summary": "x"})])
    send(client, sid, text="go")
    everything = wait_until(client, sid, "idle")
    r = client.get(
        f"/api/sessions/{sid}/events", params={"follow": "false"}, headers={"Last-Event-ID": "2"}
    )
    assert [e[0] for e in parse_sse(r.text)] == [e[0] for e in everything[2:]]


def test_the_selection_and_mode_reach_claude(client, claude) -> None:
    sid, _ = start(client)
    claude.scripts.append([("finish", {"outcome": "done", "summary": "x"})])
    send(
        client,
        sid,
        text="Thicken this",
        mode="manual",
        selection={"part": "p", "faces": ["f.1"], "point": [1, 2, 3]},
    )
    wait_until(client, sid, "idle")
    (prompt,) = claude.prompts
    assert prompt.startswith("Thicken this")
    assert '"faces": ["f.1"]' in prompt and "Manual" in prompt


def test_attachments_are_uploaded_and_reach_claude(client, claude) -> None:
    # Uploads are STEP files or images; a STEP file is text, so Claude reads it as text.
    step = b"ISO-10303-21;\nHEADER;\nFILE_NAME('part.step');\nENDSEC;\nEND-ISO-10303-21;\n"
    sid, _ = start(client)
    r = client.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("rev/part.step", step, "application/octet-stream")},
    )
    assert r.status_code == 201, r.text
    attachment = r.json()
    assert attachment["name"] == "rev/part.step"
    assert attachment["media_type"] == "application/octet-stream"
    assert client.get(attachment["url"]).content == step
    assert client.get(f"/api/sessions/{sid}/attachments/nope").status_code == 404

    # The run reads the attached file by the name the prompt gives it.
    claude.scripts.append(
        [
            ("read_attachment", {"name": attachment["name"]}),
            ("finish", {"outcome": "done", "summary": "x"}),
        ]
    )
    elsewhere = {**attachment, "url": "/api/sessions/session-99/attachments/attachment-1"}
    elsewhere["name"] = "elsewhere.step"
    message = send(client, sid, text="New part", attachments=[attachment, elsewhere])
    assert [a.model_dump() for a in message.attachments] == [attachment, elsewhere]
    wait_until(client, sid, "idle")
    (prompt,) = claude.prompts
    assert "- rev/part.step (application/octet-stream)\n" in prompt and "read_attachment" in prompt
    assert "elsewhere.step (application/octet-stream): not uploaded to this session" in prompt
    read = claude.drivers[0].calls[0]
    assert read.tool == "read_attachment" and not read.is_error
    # The file's text, marked as untrusted data (docs/security.md).
    body = read.text.split("\n\n", 1)[1]
    assert body.startswith('<untrusted_data source="attachment rev/part.step">\n')
    inner = body.split("\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0]
    assert inner == step.decode()
    assert set(SESSIONS.get(sid).ws.attachments) == {"rev/part.step"}


def test_a_multi_selection_reaches_claude_with_every_id(client, claude) -> None:
    """Parts, faces, edges and board components picked together all go to Claude."""
    gltf = json.loads((FIXTURES_DIR / "enclosure.gltf").read_text())
    nodes = [n["extras"] for n in gltf["nodes"] if n.get("extras", {}).get("part")]
    board = json.loads(next(BOARDS_DIR.glob("*.json")).read_text())
    selection = {
        "parts": [n["part"] for n in nodes],
        "faces": [n["faces"][0]["id"] for n in nodes],
        "edges": [n["edges"][0]["id"] for n in nodes],
        "components": [o["component"] for o in board["openings"]],
    }
    sid, _ = start(client)
    claude.scripts.append([("finish", {"outcome": "done", "summary": "x"})])
    sent = send(client, sid, text="Look at these", selection=selection)
    assert sent.selection.model_dump(include=set(selection)) == selection
    wait_until(client, sid, "idle")
    (prompt,) = claude.prompts
    for ids in selection.values():
        for i in ids:
            assert f'"{i}"' in prompt, f"{i} is missing from what Claude gets"


# --- Team standards ----------------------------------------------------------------------


def test_the_standards_sets_come_from_the_standards_file(client, tmp_path, monkeypatch) -> None:
    standards = load_standards()
    (listed,) = client.get("/api/standards").json()
    assert listed["id"] == standards.id and listed["name"] == standards.name
    assert [s["id"] for s in listed["standards"]] == [s.id for s in standards.standards]
    assert [s["check"] for s in listed["standards"]] == [s.check for s in standards.standards]
    # Nothing is hardcoded: another file lists another set.
    text = (REPO_ROOT / "standards.yaml").read_text()
    renamed = tmp_path / "standards.yaml"
    renamed.write_text(text.replace(f"name: {standards.name}", "name: Another team"))
    monkeypatch.setenv("STANDARDS_PATH", str(renamed))
    assert [s["name"] for s in client.get("/api/standards").json()] == ["Another team"]


def test_the_first_message_chooses_the_standards_for_the_agent_and_checks(
    client, claude, monkeypatch
) -> None:
    made: list = []
    original = CONFIG.services
    monkeypatch.setattr(
        CONFIG,
        "services",
        lambda board, standards: made.append(standards) or original(board, standards),
    )
    sid, _ = start(client)
    (chosen,) = client.get("/api/standards").json()
    claude.scripts += [[("finish", {"outcome": "done", "summary": "x"})]] * 2
    user = send(client, sid, text="Update the case", standards=chosen["id"])
    assert user.standards == chosen["id"]
    wait_until(client, sid, "idle")
    assert client.get(f"/api/sessions/{sid}").json()["standards"] == chosen["id"]
    assert chosen["name"] in claude.prompts[0]
    ws = SESSIONS.get(sid).ws
    assert ws.standards.id == chosen["id"]
    assert made[-1].id == chosen["id"]  # the checks run against the chosen set
    # Later messages may repeat the set but not change it.
    r = client.post(
        f"/api/sessions/{sid}/messages", json={"text": "again", "standards": "another_set"}
    )
    assert r.status_code == 409 and "first message" in r.json()["detail"]
    send(client, sid, text="again", standards=chosen["id"])
    wait_until(client, sid, "idle")


def test_an_unknown_standards_set_is_refused(client) -> None:
    sid, _ = start(client)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "go", "standards": "nope"})
    assert r.status_code == 404 and "nope" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}").json()["standards"] is None
    assert client.get(f"/api/sessions/{sid}/messages").json() == []


# --- Parameters, comments, exports and errors ---------------------------------------------------


def test_unlocking_a_parameter_by_hand(client) -> None:
    sid, _ = start(client)
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"locked": True})
    assert r.json()["locked"] and SESSIONS.get(sid).ws.design.locked
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"value": 3.0})
    assert r.json() == {**r.json(), "value": 3.0, "locked": True}
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"locked": False})
    assert not r.json()["locked"] and not SESSIONS.get(sid).ws.design.locked
    assert (
        client.patch(f"/api/sessions/{sid}/parameters/nope", json={"value": 1}).status_code == 404
    )


def test_parameters_written_back_as_whole_numbers_serve_without_a_warning(client) -> None:
    """Claude writes 5.0 as 5: the parameters still serve as numbers, with no serializer
    warning."""
    import warnings

    sid, _ = start(client)
    ws = SESSIONS.get(sid).ws
    whole = {
        p["name"]: int(p["value"])
        for p in client.get(f"/api/sessions/{sid}/parameters").json()
        if isinstance(p["value"], float) and p["value"].is_integer()
    }
    assert whole
    for name, value in whole.items():
        ws.design.params = set_path(ws.design.params, name, value)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        served = client.get(f"/api/sessions/{sid}/parameters").json()
    assert {p["name"]: p["value"] for p in served if p["name"] in whole} == whole


def test_a_build_serves_its_board_revision(client) -> None:
    sid, found = start(client)
    build = Build.model_validate(found[-1][2]["build"])
    assert build.board_url == f"/api/sessions/{sid}/builds/{build.id}/board"
    r = client.get(build.board_url)
    assert r.status_code == 200, r.text
    board = Board.model_validate(r.json())
    assert (board.name, board.revision) == (build.board.name, build.board.revision)
    files = [
        path
        for path in BOARDS_DIR.glob("*.json")
        if (load_board(path).name, load_board(path).revision) == (board.name, board.revision)
    ]
    assert len(files) == 1
    assert r.json() == json.loads(files[0].read_text())


def test_unknown_resources_are_404(client) -> None:
    assert client.get("/api/sessions/nope").status_code == 404
    sid, found = start(client)
    build = found[-1][2]["build"]["id"]
    for path in (
        "builds/nope",
        "builds/nope/mesh",
        "builds/nope/checks",
        "builds/nope/board",
        "exports/x/files/y",
    ):
        r = client.get(f"/api/sessions/{sid}/{path}")
        assert r.status_code == 404 and r.json()["detail"]
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build, "face_id": "nope", "point": [0, 0, 0], "text": "x"},
    )
    assert r.status_code == 404
    r = client.post("/api/sessions", json={"board": {"name": "x", "revision": "9"}})
    assert r.status_code == 422


def test_a_stl_export_opens_as_a_mesh(client) -> None:
    sid, found = start(client)
    build = found[-1][2]["build"]["id"]
    export = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build, "formats": ["stl"]}
    ).json()
    import trimesh

    mesh = trimesh.load(io.BytesIO(client.get(export["files"][0]["url"]).content), file_type="stl")
    assert len(mesh.faces) > 0


def test_exports_are_written_by_the_shared_helper(client, monkeypatch) -> None:
    """The real API writes its export files through `app.exports.write_exports`, the
    helper the mock API uses."""
    from app import exports, session_api

    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return exports.write_exports(*args, **kwargs)

    assert session_api.write_exports is exports.write_exports
    monkeypatch.setattr(session_api, "write_exports", spy)
    sid, found = start(client)
    build = found[-1][2]["build"]["id"]
    r = client.post(f"/api/sessions/{sid}/exports", json={"build_id": build, "formats": ["stl"]})
    assert r.status_code == 201, r.text
    assert len(calls) == 1
    assert [f["format"] for f in r.json()["files"]] == ["stl"]


def test_an_export_takes_the_chosen_parts(client, monkeypatch) -> None:
    """The real API passes `parts` through to the exporters; an unknown part is a
    422."""
    from app import session_api

    calls = []
    real = session_api.write_exports

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_api, "write_exports", spy)
    sid, found = start(client)
    build = found[-1][2]["build"]["id"]
    part = next(iter(validate_mesh(FIXTURES_DIR / "enclosure.gltf").parts))
    url = f"/api/sessions/{sid}/exports"
    r = client.post(url, json={"build_id": build, "formats": ["glb"], "parts": [part]})
    assert r.status_code == 201, r.text
    assert calls[-1][3] == [part]
    glb = client.get(r.json()["files"][0]["url"]).content
    assert list(validate_mesh(glb).parts) == [part]
    r = client.post(url, json={"build_id": build, "formats": ["stl"]})
    assert r.status_code == 201 and calls[-1][3] is None
    r = client.post(url, json={"build_id": build, "formats": ["stl"], "parts": [part + "-nope"]})
    assert r.status_code == 422 and part + "-nope" in r.json()["detail"]
    assert len(calls) == 2


def test_every_export_format_is_served_with_its_media_type(client) -> None:
    """The real export endpoint writes every format of `geometry.export.FORMATS`
    with the exporters, and serves each file with the exporters' media type."""
    import zipfile

    import ezdxf
    import pypdf

    from geometry.export import FILE_NAMES, FORMATS

    sid, found = start(client)
    build = found[-1][2]["build"]["id"]
    r = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build, "formats": list(FORMATS)}
    )
    assert r.status_code == 201, r.text
    export = Export.model_validate(r.json())
    assert {f.format for f in export.files} == set(FORMATS)
    got: dict[str, bytes] = {}
    for f in export.files:
        assert f.name in FILE_NAMES[f.format]
        expected = "application/json" if f.name.endswith(".json") else FORMATS[f.format].media_type
        assert f.media_type == expected, f.name
        res = client.get(f.url)
        assert res.status_code == 200, f.name
        assert res.headers["content-type"].split(";")[0] == expected, f.name
        assert f'filename="{f.name}"' in res.headers["content-disposition"]
        got[f.name] = res.content
    assert sorted(got) == sorted(n for names in FILE_NAMES.values() for n in names)

    def one(fmt: str) -> bytes:
        (name,) = FILE_NAMES[fmt]
        return got[name]

    assert one("step").startswith(b"ISO-10303-21")
    stl = one("stl")
    (count,) = struct.unpack_from("<I", stl, 80)
    assert count > 0 and len(stl) == 84 + 50 * count
    with zipfile.ZipFile(io.BytesIO(one("3mf"))) as z:
        assert "<triangle " in z.read("3D/3dmodel.model").decode()
    assert one("glb")[:4] == b"glTF"
    assert re.search(rb"^o \S+", one("obj"), re.M) and b"\nf " in one("obj")
    dxf = ezdxf.read(io.StringIO(one("dxf").decode()))
    assert len(dxf.modelspace()) > 0
    assert len(pypdf.PdfReader(io.BytesIO(one("pdf"))).pages) >= 1
    code, params = (got[n] for n in FILE_NAMES["source"])
    compile(code, FILE_NAMES["source"][0], "exec")
    assert isinstance(json.loads(params), dict)
    assert build in one("build_note").decode()


# --- History: diffs and restores -------------------------------------------------------


def _fixture_json(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


def _builds(client, sid: str) -> list[Build]:
    return [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]


def _clearance(client, sid: str) -> Parameter:
    params = map(Parameter.model_validate, client.get(f"/api/sessions/{sid}/parameters").json())
    return next(p for p in params if p.name == "clearance")


def _session_on_both_revisions(client, claude) -> tuple[str, Build, Build]:
    """A session with the existing enclosure's first build (on its own revision) and Claude's
    build on the new revision, after an edit."""
    sid, found = start(client)
    first = Build.model_validate(found[-1][2]["build"])
    claude.scripts.append(
        [
            ("set_parameter", {"name": "clearance", "value": 0.6}),
            ("run_checks", {}),
            ("finish", {"outcome": "blocked", "summary": "Done."}),
        ]
    )
    after = last_id(client, sid)
    send(client, sid, text="Update it")
    wait_until(client, sid, "idle", after)
    return sid, first, _builds(client, sid)[-1]


def test_the_drawing_revision_table_lists_the_session_builds(client, claude) -> None:
    """Each sheet's revision table lists the session's builds up to the exported
    one, oldest first."""
    import pypdf

    from geometry.export import revision_letter

    sid, first, latest = _session_on_both_revisions(client, claude)
    builds = _builds(client, sid)
    assert len(builds) >= 2

    def rows(build: Build) -> list[str]:
        r = client.post(
            f"/api/sessions/{sid}/exports", json={"build_id": build.id, "formats": ["pdf"]}
        )
        assert r.status_code == 201, r.text
        (f,) = r.json()["files"]
        reader = pypdf.PdfReader(io.BytesIO(client.get(f["url"]).content))
        tables = []
        for page in reader.pages:
            lines = [line.strip() for line in page.extract_text().splitlines()]
            tables.append([line for line in lines if re.match(r"^[A-Z]+ \d{4}-", line)])
        assert all(t == tables[0] for t in tables)
        return tables[0]

    for upto in (first, latest):
        chain = builds[: [b.id for b in builds].index(upto.id) + 1]
        got = rows(upto)
        assert [r.split(" ")[:3] for r in got] == [
            [revision_letter(i), b.created_at.date().isoformat(), b.id] for i, b in enumerate(chain)
        ]
        for row, b in zip(got, chain, strict=True):
            assert f"{b.board.name} {b.board.revision}" in row


def test_the_first_build_is_on_the_revision_the_design_was_made_for(client) -> None:
    doc = changes()
    old = load_board(BOARDS_DIR / doc.old_file)
    sid, found = start(client)
    build = Build.model_validate(found[-1][2]["build"])
    assert (build.board.name, build.board.revision) == (old.name, old.revision)
    report = client.get(build.checks_url).json()
    assert report["board"] == build.board.model_dump()
    assert report["summary"] == build.checks.model_dump()


def test_the_diff_between_revisions_is_the_build_diff(client, claude) -> None:
    sid, first, latest = _session_on_both_revisions(client, claude)
    assert first.board != latest.board
    r = client.get(f"/api/sessions/{sid}/builds/{latest.id}/diff", params={"against": first.id})
    assert r.status_code == 200, r.text
    diff = r.json()
    assert (diff["old"], diff["new"]) == (first.id, latest.id)
    assert diff["features"] == _fixture_json("diff.json")["features"]
    # The parameters are the builds' own designs: Claude's edit.
    (change,) = [p for p in diff["parameters"] if p["name"] == "clearance"]
    assert change["change"] == "changed" and change["new"] == 0.6

    # The other way round, what was added is removed and moves go back.
    back = client.get(
        f"/api/sessions/{sid}/builds/{first.id}/diff", params={"against": latest.id}
    ).json()
    assert [f["tag"] for f in back["features"]] == [f["tag"] for f in diff["features"]]
    for a, b in zip(diff["features"], back["features"], strict=True):
        assert {a["change"], b["change"]} in ({"added", "removed"}, {a["change"]})
        if a["offset"] is not None:
            assert b["offset"] == [-v for v in a["offset"]]

    same = client.get(
        f"/api/sessions/{sid}/builds/{latest.id}/diff", params={"against": latest.id}
    ).json()
    assert same["parameters"] == [] and same["features"] == []
    for path in (f"{latest.id}/diff?against=nope", f"nope/diff?against={first.id}"):
        assert client.get(f"/api/sessions/{sid}/builds/{path}").status_code == 404


def test_restoring_a_build_brings_back_its_design(client, claude) -> None:
    sid, first, latest = _session_on_both_revisions(client, claude)
    assert _clearance(client, sid).value == 0.6
    # A hand edit locks a value; the restore puts the build's value back and keeps the lock.
    client.patch(f"/api/sessions/{sid}/parameters/clearance", json={"value": 0.7})
    after = last_id(client, sid)

    r = client.post(f"/api/sessions/{sid}/builds/{first.id}/restore")
    assert r.status_code == 201, r.text
    restored = Build.model_validate(r.json())
    assert restored.restored_from == first.id
    assert restored.id not in (first.id, latest.id)
    assert restored.board == first.board
    assert restored.checks == first.checks
    (ready,) = [d for _, t, d in events(client, sid, after) if t == "build_ready"]
    assert ready["message_id"] is None and ready["build"] == r.json()
    assert _builds(client, sid)[-1] == restored
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == restored.id

    clearance = _clearance(client, sid)
    first_design = SESSIONS.get(sid).designs[first.id]
    assert clearance.locked and clearance.value not in (0.6, 0.7)
    assert clearance.value == first_design["params"]["clearance"]
    assert SESSIONS.get(sid).ws.design.snapshot() == first_design
    diff = client.get(
        f"/api/sessions/{sid}/builds/{restored.id}/diff", params={"against": first.id}
    ).json()
    assert diff["parameters"] == [] and diff["features"] == []
    for kind in ("mesh_url", "checks_url", "source_url", "board_url"):
        assert client.get(getattr(restored, kind)).status_code == 200

    # The next run starts from the restored design: it is rebuilt before it is trusted.
    assert SESSIONS.get(sid).ws.latest is None


def test_restore_and_diff_on_real_builds(real, claude) -> None:
    """On the real services, a restore is a new stored build, a child in the build history, and
    diffs between stored builds are the build store's own."""
    client, store = real, CONFIG.store()
    sid, first, latest = _session_on_both_revisions(client, claude)
    base = f"/api/sessions/{sid}/builds"
    diff = BuildDiff.model_validate(
        client.get(f"{base}/{latest.id}/diff", params={"against": first.id}).json()
    )
    assert diff == BuildDiff.model_validate(store.diff(first.id, latest.id))
    assert [(c.name, c.new) for c in diff.parameters] == [("clearance", 0.6)]

    r = client.post(f"{base}/{first.id}/restore")
    assert r.status_code == 201, r.text
    restored = Build.model_validate(r.json())
    record = store.get(restored.id)
    assert restored.restored_from == first.id and record.board == first.board
    assert latest.id in [b.id for b in store.history(restored.id)]
    assert record.parameters == store.get(first.id).parameters
    back = client.get(f"{base}/{restored.id}/diff", params={"against": first.id}).json()
    assert back["parameters"] == [] and back["features"] == []
    assert client.get(restored.checks_url).json()["summary"] == restored.checks.model_dump()


def test_a_restore_while_claude_works_is_refused(client) -> None:
    sid, found = start(client)
    first = Build.model_validate(found[-1][2]["build"])
    SESSIONS.get(sid).session.status = "running"
    r = client.post(f"/api/sessions/{sid}/builds/{first.id}/restore")
    assert r.status_code == 409
    SESSIONS.get(sid).session.status = "idle"
    assert client.post(f"/api/sessions/{sid}/builds/nope/restore").status_code == 404
    assert client.post(f"/api/sessions/{sid}/builds/{first.id}/restore").status_code == 201


# --- Change sets -------------------------------------------------------------------------


CHANGE_SET_SCHEMA = json.loads((REPO_ROOT / "contracts" / "change-set.schema.json").read_text())


class OpeningServices:
    """Instant builds; `openings:<component>` passes when the design's opening for it sits where
    the new board needs it, so each change's checks follow whether it is applied."""

    def __init__(self, board, standards) -> None:
        from app.agent.services import FixtureServices

        self.board = board
        self.built: list[dict] = []
        self._ids = FixtureServices()._ids  # shared with the first build, as in the session

    def build(self, design):
        from app.agent.services import BuildOutcome, make_build

        self.built.append(design)
        return BuildOutcome(make_build(f"build-{next(self._ids)}", self.board), shapes=design)

    def checks(self, outcome):
        from app.agent.services import CheckItem

        have = {o["id"]: o for o in outcome.shapes["params"]["openings"]}
        items = [CheckItem("wall", "wall_thickness", "pass", standard="min_wall")]
        for o in self.board.openings:
            got = have.get(o.id)
            ok = got is not None and (got["x"], got["y"], got["rotation"]) == (
                o.position[0],
                o.position[1],
                o.rotation,
            )
            status = "pass" if ok else "fail"
            items.append(
                CheckItem(f"openings:{o.component}", "openings", status, component=o.component)
            )
        return items

    def analyze(self, outcome):
        return []


@pytest.fixture
def openings(monkeypatch) -> list[OpeningServices]:
    made: list[OpeningServices] = []

    def services(board, standards):
        made.append(OpeningServices(board, standards))
        return made[-1]

    monkeypatch.setattr(CONFIG, "services", services)
    return made


def component_of(row: dict) -> str:
    return row["Component"].strip("` ")


def run_with_changes(client, claude, mode: str) -> tuple[str, list]:
    """A session whose run writes the parameters for the new board and finishes."""
    from geometry import enclosure_parameters

    sid, found = start(client)
    claude.scripts.append(
        lambda ws: [
            ("write_design", {"params": enclosure_parameters(ws.board, ws.standards)}),
            ("finish", {"outcome": "done", "summary": "Updated the openings."}),
        ]
    )
    send(client, sid, text="Update the enclosure for the new revision.", mode=mode)
    return sid, wait_until(client, sid, "idle", found[-1][0])


def change_ids(change_set: dict) -> dict[str, str]:
    """Change IDs by the board component they serve."""
    return {
        c["delta"]["component"]: c["id"] for c in change_set["changes"] if c["delta"]["component"]
    }


def test_a_run_that_changes_the_design_serves_its_change_set(client, claude, openings) -> None:
    import jsonschema

    sid, _ = start(client)
    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404
    sid, found = run_with_changes(client, claude, "auto")
    (ready,) = [d for _, t, d in found if t == "changes_ready"]
    (begin,) = [d for _, t, d in found if t == "message_start"]
    assert ready["message_id"] == begin["message_id"], "inside the run's message"
    assert [t for _, t, _ in found][-1] == "message_end"

    r = client.get(f"/api/sessions/{sid}/changes")
    assert r.status_code == 200
    change_set = r.json()
    jsonschema.validate(change_set, CHANGE_SET_SCHEMA)
    assert change_set == ready["change_set"]
    assert change_set["mode"] == "auto"
    assert {component_of(row) for row in changes().rows} <= set(change_ids(change_set))
    assert all(c["status"] == "accepted" for c in change_set["changes"])
    # Both builds it names are served.
    for build_id in (change_set["base_build"], change_set["build"]):
        assert client.get(f"/api/sessions/{sid}/builds/{build_id}").status_code == 200


def test_accepting_and_rejecting_rebuilds_with_only_the_accepted_changes(
    client, claude, openings
) -> None:
    import jsonschema

    sid, found = run_with_changes(client, claude, "manual")
    services = SESSIONS.get(sid).ws.services
    change_set = client.get(f"/api/sessions/{sid}/changes").json()
    assert all(c["status"] == "proposed" for c in change_set["changes"])
    ids = change_ids(change_set)
    first, second, *rest = [component_of(row) for row in changes().rows]

    after = found[-1][0]
    messages = len(client.get(f"/api/sessions/{sid}/messages").json())
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [ids[first], ids[second]]})
    assert r.status_code == 200, r.text
    reviewed = r.json()
    jsonschema.validate(reviewed, CHANGE_SET_SCHEMA)
    status = {c["id"]: c["status"] for c in reviewed["changes"]}
    assert status[ids[first]] == status[ids[second]] == "accepted"
    assert all(status[ids[n]] == "proposed" for n in rest)

    # Rebuilt and rechecked with only the accepted changes.
    proposal = SESSIONS.get(sid).proposal
    assert services.built[-1] == proposal.design_for({ids[first], ids[second]})
    new = events(client, sid, after)
    assert_contract_events(new)
    assert [t for _, t, _ in new] == ["build_ready", "changes_ready"]
    build = new[0][2]
    assert build["message_id"] is None
    assert build["build"]["id"] == reviewed["build"] != change_set["build"]
    assert build["build"]["checks"] == reviewed["checks"]
    assert new[1][2]["change_set"] == reviewed
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == reviewed["build"]
    assert client.get(f"/api/sessions/{sid}/changes").json() == reviewed
    proof = {c["id"]: c["checks"] for c in reviewed["changes"]}
    for name in (first, second):
        assert {k["id"]: k["after"] for k in proof[ids[name]]}[f"openings:{name}"] == "pass"
    for name in rest:
        assert {k["id"]: k["after"] for k in proof[ids[name]]}[f"openings:{name}"] == "fail"

    # Rejecting one of them rebuilds with the other alone.
    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [ids[first]]})
    assert r.status_code == 200
    assert services.built[-1] == proposal.design_for({ids[second]})
    assert {c["id"]: c["status"] for c in r.json()["changes"]}[ids[first]] == "rejected"
    # No message was opened for the reviews.
    assert len(client.get(f"/api/sessions/{sid}/messages").json()) == messages


def test_a_review_refused_by_a_lock_names_the_lock(client, claude, openings) -> None:
    sid, _ = run_with_changes(client, claude, "auto")
    change_set = client.get(f"/api/sessions/{sid}/changes").json()
    added = next(c for c in change_set["changes"] if c["delta"]["kind"] == "added")
    ws = SESSIONS.get(sid).ws
    path = f"{added['id']}.x"
    ws.design.lock(path)  # a hand edit on the added opening since the run

    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [added["id"]]})
    assert r.status_code == 409
    assert path in r.json()["detail"] and "lock" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/changes").json() == change_set  # nothing changed


def test_a_later_run_retires_the_earlier_change_set(client, claude, openings) -> None:
    """A run that changes the design and ends without a change set of its own (here, without
    `finish`) must not leave the earlier run's set reviewable: reviewing it would put back the
    design that set was proposed on and drop the later run's work."""
    sid, found = run_with_changes(client, claude, "manual")
    first = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    ws = SESSIONS.get(sid).ws
    name, value = next(
        (k, v) for k, v in ws.design.params.items() if isinstance(v, int | float) and v
    )
    later = {**ws.design.params, name: value * 3}
    claude.scripts.append([("write_design", {"params": later})])
    send(client, sid, text="Make it sturdier.")
    wait_until(client, sid, "idle", found[-1][0])
    assert ws.design.params[name] == value * 3, "the later run's design stands"

    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first]})
    assert r.status_code == 404
    assert ws.design.params[name] == value * 3, "the review did not undo the later run"


def test_a_failed_run_that_changed_the_design_retires_the_change_set(
    client, claude, openings
) -> None:
    """A run that changes the design and then fails leaves a design the set wasn't proposed
    on, so the set is retired like after any run that moves the design."""
    sid, found = run_with_changes(client, claude, "manual")
    ws = SESSIONS.get(sid).ws
    name, value = next(
        (k, v) for k, v in ws.design.params.items() if isinstance(v, int | float) and v
    )
    later = {**ws.design.params, name: value * 3}
    claude.scripts.append([("write_design", {"params": later}), ("no_such_tool", {})])
    send(client, sid, text="Make it sturdier.")
    after = wait_until(client, sid, "idle", found[-1][0])
    assert any(t == "error" for _, t, _ in after), "the run failed"
    assert ws.design.params[name] == value * 3
    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404


@pytest.mark.parametrize("finish", [True, False])
def test_a_question_keeps_the_open_change_set(client, claude, openings, finish) -> None:
    """A run that only answers a question (demo step 4: "Why did this change?" about a
    selected face), with or without `finish`, leaves the design as it was, so the change set
    proposed on that design stays served and reviewable."""
    sid, found = run_with_changes(client, claude, "manual")
    before = client.get(f"/api/sessions/{sid}/changes").json()
    ws = SESSIONS.get(sid).ws
    design = ws.design.snapshot()
    steps: list = [("text", "Because the board changed under it.")]
    if finish:
        steps.append(("finish", {"outcome": "done", "summary": "Answered the question."}))
    claude.scripts.append(steps)
    send(client, sid, text="Why did this change?")
    answer = wait_until(client, sid, "idle", found[-1][0])
    assert ws.design.snapshot() == design, "the run did not change the design"
    assert not [d for _, t, d in answer if t == "changes_ready"]

    r = client.get(f"/api/sessions/{sid}/changes")
    assert r.status_code == 200, r.text
    assert r.json() == before
    first, *rest = [c["id"] for c in before["changes"]]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first], "reject": rest})
    assert r.status_code == 200, r.text
    status = {c["id"]: c["status"] for c in r.json()["changes"]}
    assert status[first] == "accepted"
    assert all(status[c] == "rejected" for c in rest)


def test_a_restore_retires_the_change_set(client, claude, openings) -> None:
    """Restoring an earlier build makes a design the change set was not proposed on: reviewing
    the set afterwards would put the run's design back over the restore."""
    sid, _ = run_with_changes(client, claude, "auto")
    first = _builds(client, sid)[0]
    some = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/builds/{first.id}/restore")
    assert r.status_code == 201, r.text
    restored = SESSIONS.get(sid).ws.design.snapshot()
    assert restored == SESSIONS.get(sid).designs[first.id]

    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404
    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [some]})
    assert r.status_code == 404
    assert SESSIONS.get(sid).ws.design.snapshot() == restored, "the restore stands"


def test_a_review_of_unknown_or_contradictory_changes_is_refused(client, claude, openings) -> None:
    sid, _ = run_with_changes(client, claude, "manual")
    some = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": ["nope"]})
    assert r.status_code == 404 and "nope" in r.json()["detail"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [some], "reject": [some]})
    assert r.status_code == 409 and some in r.json()["detail"]
    assert client.post("/api/sessions/nope/changes", json={}).status_code == 404


# --- Loading the next revision -----------------------------------------------------------


def write_new_parameters(ws):
    from geometry import enclosure_parameters

    return [
        ("write_design", {"params": enclosure_parameters(ws.board, ws.standards)}),
        ("finish", {"outcome": "done", "summary": "Updated the openings."}),
    ]


def test_loading_the_next_revision_proposes_changes_without_typing(
    client, claude, openings
) -> None:
    """The demo's "load next revision": Claude starts on the new revision with a fixed prompt,
    and the run's proposal (its builds and change set) arrives on the stream."""
    previous, new = scenario_boards()
    sid, found = start(client)
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["next_revision"] == {"name": new.name, "revision": new.revision}
    claude.scripts.append(write_new_parameters)

    r = client.post(f"/api/sessions/{sid}/revisions/next", json={"mode": "auto"})
    assert r.status_code == 202, r.text
    message = Message.model_validate(r.json())
    assert message.role == "user" and message.mode == "auto"
    assert new.revision in message.text and previous.revision in message.text
    (prompt,) = claude.prompts
    assert f"{new.name} {new.revision}" in prompt and previous.revision in prompt

    run = wait_until(client, sid, "idle", found[-1][0])
    assert_contract_events(run)
    kinds = [t for _, t, _ in run]
    # The load as the user's message, then the run.
    assert kinds[:2] == ["user_message", "message_start"] and kinds[-1] == "message_end"
    assert run[0][2]["message"]["id"] == message.id
    assert "build_ready" in kinds and "changes_ready" in kinds
    mid = run[1][2]["message_id"]
    builds = [d["build"] for _, t, d in run if t == "build_ready"]
    assert all(b["board"] == {"name": new.name, "revision": new.revision} for b in builds)
    (ready,) = [d for _, t, d in run if t == "changes_ready"]
    assert ready["message_id"] == mid, "the proposal is in the run's chat message"
    change_set = client.get(f"/api/sessions/{sid}/changes").json()
    assert change_set == ready["change_set"]
    assert {component_of(row) for row in changes().rows} <= set(change_ids(change_set))
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["latest_build_id"] == change_set["build"]
    assert session["next_revision"] is None
    roles = [m["role"] for m in client.get(f"/api/sessions/{sid}/messages").json()]
    assert roles == ["user", "assistant"]

    r = client.post(f"/api/sessions/{sid}/revisions/next")
    assert r.status_code == 409 and "revision" in r.json()["detail"]
    assert len(claude.prompts) == 1


def test_asking_about_a_selected_face_gets_an_answer_without_an_error(
    client, claude, openings
) -> None:
    """Regression: Claude answers a question in text and changes nothing, without calling
    `finish`. That is an answer, not a run that failed to finish."""
    sid, found = start(client)
    claude.scripts.append(write_new_parameters)
    r = client.post(f"/api/sessions/{sid}/revisions/next", json={"mode": "auto"})
    assert r.status_code == 202, r.text
    run = wait_until(client, sid, "idle", found[-1][0])
    assert run[-1][1] == "message_end"

    latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    gltf = json.loads(client.get(f"/api/sessions/{sid}/builds/{latest}/mesh").text)
    part, face = next(
        (n["extras"]["part"], f["id"])
        for n in gltf["nodes"]
        for f in n.get("extras", {}).get("faces", [])
    )
    claude.scripts.append([("text", "That face moved with the new opening.")])
    after = run[-1][0]
    send(client, sid, text="Why did this change?", selection={"part": part, "faces": [face]})
    answer = wait_until(client, sid, "idle", after)
    assert_contract_events(answer)
    kinds = [t for _, t, _ in answer]
    assert "error" not in kinds, [d for _, t, d in answer if t == "error"]
    # The user's side first (the message, and the chips its words change), then the reply.
    start_at = kinds.index("message_start")
    assert set(kinds[:start_at]) <= {"user_message", "constraints"} and kinds[-1] == "message_end"
    assert "user_message" in kinds[:start_at]
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert messages[-1]["role"] == "assistant" and "new opening" in messages[-1]["text"]


def test_loading_a_revision_while_claude_works_is_refused(client, claude) -> None:
    sid, found = start(client)
    options = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    claude.scripts.append(
        [
            ("ask_user", {"question": "Which?", "options": options}),
            ("finish", {"outcome": "blocked", "summary": "ok"}),
        ]
    )
    send(client, sid, text="Look at the board.")
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    r = client.post(f"/api/sessions/{sid}/revisions/next")
    assert r.status_code == 409 and "answer" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}").json()["next_revision"] is not None
    (question,) = [d for _, t, d in asked if t == "question"]
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "a"})
    wait_until(client, sid, "idle")
    assert len(claude.prompts) == 1, "no run was started for the refused load"
    assert client.post("/api/sessions/nope/revisions/next").status_code == 404


def test_both_apis_serve_the_revision_route() -> None:
    from app.mock_api import contract_openapi_document

    for doc in (contract_openapi_document(), app.openapi()):
        ops = doc["paths"]["/api/sessions/{session_id}/revisions/next"]
        assert "post" in ops
        assert "next_revision" in doc["components"]["schemas"]["Session"]["properties"]


def test_both_apis_serve_the_change_routes() -> None:
    from app.mock_api import app as mock_app
    from app.mock_api import contract_openapi_document

    for doc in (contract_openapi_document(), app.openapi()):
        ops = doc["paths"]["/api/sessions/{session_id}/changes"]
        assert {"get", "post"} <= set(ops)
        ref = ops["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ChangeReview")
        assert "changes_ready" in json.dumps(doc["components"]["schemas"]["SessionEvent"])
    assert mock_app.openapi() == contract_openapi_document()


# --- Code edits ---------------------------------------------------------------------------------


def edit_code(client, sid: str, build_id: str, code: str):
    return client.put(f"/api/sessions/{sid}/source", json={"build_id": build_id, "code": code})


def test_a_code_edit_rebuilds_rechecks_and_reaches_claude(client, claude) -> None:
    """Editing the code rebuilds and rechecks it, the design takes the code, and the
    next agent turn gets the change (once)."""
    sid, found = start(client)
    first = found[-1][2]["build"]["id"]
    state = SESSIONS.get(sid)
    before = state.ws.design.code
    code = before + "\n# Edited by hand: thicker walls next time\n"
    after = last_id(client, sid)

    r = edit_code(client, sid, first, code)
    assert r.status_code == 200, r.text
    build = Build.model_validate(r.json())
    assert build.id != first and build.status == "ready"
    [(_, kind, data)] = events(client, sid, after)
    assert kind == "build_ready" and data["message_id"] is None
    assert data["build"]["id"] == build.id
    assert state.ws.design.code == code
    checks = client.get(build.checks_url).json()
    validate_check_report(checks, load_standards())
    assert checks["build_id"] == build.id
    assert checks["summary"] == build.checks.model_dump(by_alias=True)
    source = BuildSource.model_validate(client.get(build.source_url).json())
    assert source.code == code and source.build_id == build.id

    claude.scripts += [[("finish", {"outcome": "done", "summary": "x"})]] * 2
    send(client, sid, text="Carry on")
    wait_until(client, sid, "idle")
    send(client, sid, text="And again")
    wait_until(client, sid, "idle")
    first_prompt, second_prompt = claude.prompts
    assert first_prompt.startswith("Carry on")
    assert "edited the generator code by hand" in first_prompt
    assert "+# Edited by hand: thicker walls next time" in first_prompt
    assert "edited the generator code" not in second_prompt


def test_a_code_edit_is_allowed_while_parameters_are_locked(client) -> None:
    """The user's own code edit needs no approval, unlike Claude's."""
    sid, found = start(client)
    client.patch(f"/api/sessions/{sid}/parameters/wall", json={"locked": True})
    state = SESSIONS.get(sid)
    code = state.ws.design.code + "\n# hand edit\n"
    r = edit_code(client, sid, found[-1][2]["build"]["id"], code)
    assert r.status_code == 200, r.text
    assert state.ws.design.code == code


def test_refused_code_changes_nothing(client, claude) -> None:
    """A syntax error is a 400 with its line; the design keeps its code, nothing is
    rebuilt and Claude isn't told about an edit."""
    sid, found = start(client)
    state = SESSIONS.get(sid)
    before = state.ws.design.code
    after = last_id(client, sid)
    r = edit_code(client, sid, found[-1][2]["build"]["id"], "def build(params):\n  return (\n")
    assert r.status_code == 400
    assert r.json()["line"] == 2 and r.json()["detail"].startswith("Syntax error")
    assert events(client, sid, after) == []
    assert state.ws.design.code == before

    claude.scripts.append([("finish", {"outcome": "done", "summary": "x"})])
    send(client, sid, text="Go")
    wait_until(client, sid, "idle")
    assert "edited the generator code" not in claude.prompts[0]


def test_code_that_fails_to_build_is_refused_with_its_line(client, monkeypatch) -> None:
    """A build error comes back as a 400 with the failing line."""
    from app.agent.services import BuildOutcome, make_build

    sid, found = start(client)
    state = SESSIONS.get(sid)
    before = state.ws.design.code

    def failing(design):
        return BuildOutcome(
            make_build("build-x", state.ws.board, status="failed"),
            error={
                "kind": "exception",
                "message": "NameError: name 'oops' is not defined",
                "line": 3,
            },
        )

    monkeypatch.setattr(state.ws.services, "build", failing)
    code = "def build(params):\n    x = 1\n    return oops\n"
    r = edit_code(client, sid, found[-1][2]["build"]["id"], code)
    assert r.status_code == 400
    assert r.json() == {
        "detail": "The build failed: NameError: name 'oops' is not defined",
        "line": 3,
    }
    assert state.ws.design.code == before


def test_a_code_edit_while_running_is_refused(client, claude) -> None:
    sid, found = start(client)
    SESSIONS.get(sid).session.status = "running"
    r = edit_code(client, sid, found[-1][2]["build"]["id"], "def build(params):\n    pass\n")
    assert r.status_code == 409


# --- Risk flags --------------------------------------------------------------------------


RISK_FLAGS_SCHEMA = json.loads((REPO_ROOT / "contracts" / "risk-flags.schema.json").read_text())
# The risk the fixtures flag (a thick section or a check near its limit). None when the fixtures
# flag none; the risk tests then fail on it, but the module (which other test files import)
# still loads.
_FIXTURE_FLAGS = _fixture_json("risk-flags.json")["flags"]
THICK = _FIXTURE_FLAGS[0] if _FIXTURE_FLAGS else None


class ThickServices:
    """Instant builds where every check passes; the fixture's risk is there until the path its
    fix edits moves off the value it had at the first build."""

    def __init__(self, board, standards) -> None:
        from app.agent.services import FixtureServices

        self.board = board
        self.standards = standards
        self.built: list[dict] = []
        self.path = next(iter(THICK["fix"]["edits"]))
        self.original = None
        self._ids = FixtureServices()._ids

    def build(self, design):
        from app.agent.design import get_path
        from app.agent.services import BuildOutcome, make_build

        self.built.append(design)
        if self.original is None:
            self.original = get_path(design["params"], self.path)
        return BuildOutcome(make_build(f"build-{next(self._ids)}", self.board), shapes=design)

    def checks(self, outcome):
        from app.agent.services import CheckItem

        wall = float(self.standards.get("min_wall").value)
        items = [
            CheckItem(
                "wall_thickness",
                "wall_thickness",
                "pass",
                standard="min_wall",
                measured=wall * 2,
                required=wall,
                unit="mm",
            )
        ]
        if THICK["kind"] == "near_limit":  # the check at its limit, well inside it once fixed
            check_id = THICK["id"].split(":", 1)[1]
            limit = THICK["limit"]
            inside = (
                0.5
                * abs(limit)
                * (1 if self.standards.get(THICK["standard"]).kind == "min" else -1)
            )
            items.append(
                CheckItem(
                    check_id,
                    check_id.split(":")[0],
                    "pass",
                    standard=THICK["standard"],
                    measured=limit + inside if self._fixed(outcome) else THICK["value"],
                    required=limit,
                    unit=THICK["unit"],
                    faces=THICK["location"]["faces"],
                    point=THICK["location"]["point"],
                )
            )
        return items

    def _fixed(self, outcome) -> bool:
        from app.agent.design import get_path

        return get_path(outcome.shapes["params"], self.path) != self.original

    def analyze(self, outcome):
        if THICK["kind"] != "thick" or self._fixed(outcome):
            return []
        location = THICK["location"]
        return [
            {
                "map": "wall_thickness",
                "kind": "thick",
                "value": THICK["value"],
                "unit": THICK["unit"],
                "part": location["part"],
                "feature": location["feature"],
                "faces": location["faces"],
                "point": location["point"],
            }
        ]


@pytest.fixture
def thick(monkeypatch) -> None:
    monkeypatch.setattr(CONFIG, "services", ThickServices)


def run_with_risk(client, claude) -> tuple[str, list]:
    """A session whose run flags the thick section with the fixture's fix and finishes."""
    sid, found = start(client)
    ((path, value),) = THICK["fix"]["edits"].items()
    claude.scripts.append(
        [
            ("run_checks", {}),
            (
                "flag_risk",
                {"risk": THICK["id"], "fix": THICK["fix"]["title"], "name": path, "value": value},
            ),
            ("finish", {"outcome": "done", "summary": "Flagged a risk."}),
        ]
    )
    send(client, sid, text="Look for risks.", mode="manual")
    return sid, wait_until(client, sid, "idle", found[-1][0])


def test_a_run_that_flags_risks_serves_them(client, claude, thick) -> None:
    import jsonschema

    sid, _ = start(client)
    assert client.get(f"/api/sessions/{sid}/risks").status_code == 404
    sid, found = run_with_risk(client, claude)
    assert not claude.drivers[-1].calls[1].is_error, claude.drivers[-1].calls[1].text
    (ready,) = [d for _, t, d in found if t == "risks_ready"]
    (start_,) = [d for _, t, d in found if t == "message_start"]
    assert ready["message_id"] == start_["message_id"], "inside the run's message"
    assert [t for _, t, _ in found][-1] == "message_end"
    assert_contract_events(found)

    r = client.get(f"/api/sessions/{sid}/risks")
    assert r.status_code == 200
    flags = r.json()
    jsonschema.validate(flags, RISK_FLAGS_SCHEMA)
    assert flags == ready["risk_flags"]
    (flag,) = flags["flags"]
    assert flag["id"] == THICK["id"] and flag["status"] == "proposed"
    assert flag["location"]["faces"] == THICK["location"]["faces"]
    # The message line said in chat is the flag's.
    said = "".join(d["delta"] for _, t, d in found if t == "text")
    assert flag["message"] in said


def test_accepting_and_ignoring_a_fix_reviews_the_flags_and_rebuilds(client, claude, thick) -> None:
    from app.agent.design import get_path

    sid, found = run_with_risk(client, claude)
    state = SESSIONS.get(sid)
    services = state.ws.services
    ((path, value),) = THICK["fix"]["edits"].items()
    original = get_path(state.ws.design.params, path)
    assert original != value, "the fix is not applied until accepted"
    messages = len(client.get(f"/api/sessions/{sid}/messages").json())

    after = found[-1][0]
    built = len(services.built)
    r = client.post(f"/api/sessions/{sid}/risks", json={"accept": [THICK["id"]]})
    assert r.status_code == 200, r.text
    reviewed = r.json()
    assert reviewed["flags"][0]["status"] == "accepted"
    assert get_path(state.ws.design.params, path) == value  # Flags.review applied it
    assert len(services.built) == built + 1
    new = events(client, sid, after)
    assert_contract_events(new)
    assert [t for _, t, _ in new] == ["build_ready", "risks_ready"]
    assert new[0][2]["message_id"] is None and new[1][2]["message_id"] is None
    assert new[1][2]["risk_flags"] == reviewed
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == new[0][2]["build"]["id"]
    assert client.get(f"/api/sessions/{sid}/risks").json() == reviewed

    # Ignoring it takes the fix back, and rebuilds.
    after = new[-1][0]
    r = client.post(f"/api/sessions/{sid}/risks", json={"reject": [THICK["id"]]})
    assert r.status_code == 200
    assert r.json()["flags"][0]["status"] == "rejected"
    assert get_path(state.ws.design.params, path) == original
    assert [t for _, t, _ in events(client, sid, after)] == ["build_ready", "risks_ready"]
    assert len(client.get(f"/api/sessions/{sid}/messages").json()) == messages


def test_a_risk_review_refused_by_a_lock_or_unknown_flag(client, claude, thick) -> None:
    sid, _ = run_with_risk(client, claude)
    flags = client.get(f"/api/sessions/{sid}/risks").json()
    r = client.post(f"/api/sessions/{sid}/risks", json={"accept": ["nope"]})
    assert r.status_code == 404 and "nope" in r.json()["detail"]
    r = client.post(
        f"/api/sessions/{sid}/risks", json={"accept": [THICK["id"]], "reject": [THICK["id"]]}
    )
    assert r.status_code == 409
    path = next(iter(THICK["fix"]["edits"]))
    SESSIONS.get(sid).ws.design.lock(path)  # a hand edit since the run
    r = client.post(f"/api/sessions/{sid}/risks", json={"accept": [THICK["id"]]})
    assert r.status_code == 409 and path in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/risks").json() == flags  # nothing changed
    assert client.post("/api/sessions/nope/risks", json={}).status_code == 404


def test_a_restore_retires_the_risk_flags(client, claude, thick) -> None:
    sid, _ = run_with_risk(client, claude)
    first = _builds(client, sid)[0]
    assert client.post(f"/api/sessions/{sid}/builds/{first.id}/restore").status_code == 201
    assert client.get(f"/api/sessions/{sid}/risks").status_code == 404


def test_both_apis_serve_the_risk_routes() -> None:
    from app.mock_api import contract_openapi_document

    for doc in (contract_openapi_document(), app.openapi()):
        ops = doc["paths"]["/api/sessions/{session_id}/risks"]
        assert {"get", "post"} <= set(ops)
        ref = ops["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/RiskReview")
        assert "risks_ready" in json.dumps(doc["components"]["schemas"]["SessionEvent"])


# --- Fixes from walking the demo script against the real backend ---------------------------------


def test_a_hand_edit_rebuilds_with_the_locked_value(client) -> None:
    """Demo step 5: setting a value locks it and rebuilds, like the mock API (`docs/architecture.md`
    item 9): a `build_ready` with no `message_id` follows, built from the edited design."""
    sid, found = start(client)
    before = {b.id for b in _builds(client, sid)}
    after = found[-1][0]
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"value": 3.0})
    assert r.status_code == 200, r.text
    assert r.json()["locked"] and r.json()["value"] == 3.0
    new = events(client, sid, after)
    assert_contract_events(new)
    (ready,) = [d for _, t, d in new if t == "build_ready"]
    assert ready["message_id"] is None
    build = Build.model_validate(ready["build"])
    assert build.id not in before and build.status == "ready" and build.checks is not None
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == build.id
    state = SESSIONS.get(sid)
    assert state.designs[build.id]["params"]["wall"] == 3.0
    assert client.get(f"/api/sessions/{sid}/builds/{build.id}").status_code == 200
    assert not any(t == "message_start" for _, t, _ in new), "no chat message for a hand edit"

    # Unlocking hands the value back to Claude without changing the design: no rebuild.
    after = last_id(client, sid)
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"locked": False})
    assert r.status_code == 200 and not r.json()["locked"]
    assert not [t for _, t, _ in events(client, sid, after) if t == "build_ready"]


def test_a_hand_edit_while_claude_is_working_is_refused(client) -> None:
    sid, _ = start(client)
    state = SESSIONS.get(sid)
    value = state.ws.design.params["wall"]
    state.session.status = "running"
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"value": value + 1})
    assert r.status_code == 409
    assert state.ws.design.params["wall"] == value and not state.ws.design.locked


def test_rejected_changes_reach_claude_in_later_runs(client, claude, openings) -> None:
    """A change the user rejected is in Claude's context for every later run, with the rule
    not to propose it again unless the user asks."""
    sid, found = run_with_changes(client, claude, "manual")
    change = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]
    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [change["id"]]})
    assert r.status_code == 200, r.text

    for text in ("What next?", "And now?"):
        claude.scripts.append([("text", "Nothing to change.")])
        after = last_id(client, sid)
        send(client, sid, text=text)
        wait_until(client, sid, "idle", after)
        prompt = claude.prompts[-1]
        assert change["id"] in prompt and change["title"] in prompt
        assert "rejected" in prompt.lower() and "unless" in prompt.lower()


class WallSpotServices:
    """Instant builds where every check passes, with a thick section on the wall until `wall`
    gets thinner than it was at the first build: a risk with a fix, on any board."""

    def __init__(self, board, standards) -> None:
        from app.agent.services import FixtureServices

        self.board = board
        self.standards = standards
        self.original = None
        self._ids = FixtureServices()._ids

    def build(self, design):
        from app.agent.services import BuildOutcome, make_build

        if self.original is None:
            self.original = design["params"]["wall"]
        return BuildOutcome(make_build(f"build-{next(self._ids)}", self.board), shapes=design)

    def checks(self, outcome):
        from app.agent.services import CheckItem

        wall = float(self.standards.get("min_wall").value)
        return [
            CheckItem(
                "wall_thickness",
                "wall_thickness",
                "pass",
                standard="min_wall",
                measured=wall * 2,
                required=wall,
                unit="mm",
            )
        ]

    def analyze(self, outcome):
        if outcome.shapes["params"]["wall"] < self.original:
            return []
        spot = {"map": "wall_thickness", "kind": "thick", "value": 9.0, "unit": "mm"}
        return [{**spot, "part": "case", "feature": "case_wall", "faces": [], "point": None}]


def test_a_change_review_retires_the_risk_flags_of_the_earlier_build(
    client, claude, monkeypatch
) -> None:
    """Flags are checked on one build. After a change review rebuilds the design, a later run
    must not stream them again as if they were on the new build."""
    monkeypatch.setattr(CONFIG, "services", WallSpotServices)
    sid, found = start(client)
    params = SESSIONS.get(sid).ws.design.params
    wall, clearance = params["wall"], params["clearance"]
    claude.scripts.append(
        [
            ("set_parameter", {"name": "clearance", "value": clearance + 0.05}),
            ("run_checks", {}),
            (
                "flag_risk",
                {
                    "risk": "thick:case_wall",
                    "fix": "Thinner wall",
                    "name": "wall",
                    "value": wall - 0.2,
                },
            ),
            ("finish", {"outcome": "done", "summary": "Changed it and flagged a risk."}),
        ]
    )
    send(client, sid, text="Change it.", mode="auto")
    found = wait_until(client, sid, "idle", found[-1][0])
    assert not any(c.is_error for c in claude.drivers[-1].calls), [
        c.text for c in claude.drivers[-1].calls
    ]
    (flagged,) = [d["risk_flags"] for _, t, d in found if t == "risks_ready"]
    change = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]

    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [change["id"]]})
    assert r.status_code == 200, r.text
    reviewed = r.json()["build"]
    assert reviewed and reviewed != flagged["build"]

    claude.scripts.append([("text", "Nothing to change.")])
    after = last_id(client, sid)
    send(client, sid, text="Anything else?")
    found = wait_until(client, sid, "idle", after)
    for _, t, d in found:
        if t == "risks_ready":
            assert d["risk_flags"]["build"] == reviewed, "flags of the pre-review build"
