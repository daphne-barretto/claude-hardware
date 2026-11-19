"""The mock session API serves the fixtures and plays the scripted run."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.boards import BOARDS_DIR, Board, load_board
from app.contracts.checks import validate_check_report
from app.contracts.common import FIXTURES_DIR
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from app.contracts.models import MODEL_CATALOG
from app.contracts.session import (
    Attachment,
    Build,
    BuildSource,
    Message,
    Session,
    validate_event,
)
from app.examples import example_standards, examples
from app.mock_api import SESSIONS, app
from app.standards import load_standards

# The board revision example: these tests walk its board and existing design.
EXAMPLE = examples()[0].id


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    SESSIONS.clear()
    with TestClient(app) as test_client:
        yield test_client


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
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        if any(e[1] == event_type for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"no {event_type} event")


def recorded() -> list[dict]:
    lines = (FIXTURES_DIR / "session-events.jsonl").read_text().splitlines()
    return [json.loads(line)["event"] for line in lines]


def start(client) -> str:
    r = client.post("/api/sessions", json={"example": EXAMPLE})
    assert r.status_code == 201
    session = Session.model_validate(r.json())
    assert (
        session.board.model_dump()
        == json.loads((FIXTURES_DIR / "session.json").read_text())["board"]
    )
    return session.id


def test_first_message_plays_the_scripted_run(client) -> None:
    sid = start(client)
    r = client.post(
        f"/api/sessions/{sid}/messages",
        json={
            "text": "Check it",
            "model": MODEL_CATALOG.default,
            "effort": "high",
            "mode": "manual",
            "selection": {"part": "lid", "faces": ["lid.outer_cap"]},
        },
    )
    assert r.status_code == 202
    user = Message.model_validate(r.json())
    assert (user.role, user.effort, user.mode, user.selection.faces) == (
        "user",
        "high",
        "manual",
        ["lid.outer_cap"],
    )

    events = wait_for(client, sid, "message_end")
    assert [e[0] for e in events] == list(range(1, len(events) + 1))
    for _, event_type, data in events:
        assert validate_event(data).type == event_type
    # The message and its constraints come first, then the recorded run.
    assert [e[1] for e in events] == ["user_message", "constraints"] + [
        e["type"] for e in recorded()
    ]

    text = "".join(d["delta"] for _, t, d in events if t == "text")
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["text"] == text

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "waiting_for_answer"
    assert session["latest_build_id"] == json.loads((FIXTURES_DIR / "build.json").read_text())["id"]


def test_resume_after_last_event_id(client) -> None:
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    all_events = wait_for(client, sid, "message_end")
    r = client.get(
        f"/api/sessions/{sid}/events", params={"follow": "false"}, headers={"Last-Event-ID": "5"}
    )
    assert [e[0] for e in parse_sse(r.text)] == [e[0] for e in all_events[5:]]


def test_build_mesh_checks_and_maps_follow_the_contracts(client) -> None:
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    builds = [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]
    # The existing enclosure on its own revision, then on the new one.
    assert [
        b.model_dump(mode="json", exclude={"mesh_url", "checks_url", "source_url", "board_url"})
        for b in builds
    ] == [
        Build.model_validate(json.loads((FIXTURES_DIR / name).read_text())).model_dump(
            mode="json", exclude={"mesh_url", "checks_url", "source_url", "board_url"}
        )
        for name in ("previous-build.json", "build.json")
    ]
    standards = example_standards()
    for build in builds:
        assert client.get(f"/api/sessions/{sid}/builds/{build.id}").json() == build.model_dump(
            mode="json"
        )
        r = client.get(build.mesh_url)
        assert r.headers["content-type"].startswith("model/gltf+json")
        mesh = validate_mesh(r.content)
        report = client.get(build.checks_url).json()
        validate_check_report(report, standards, mesh)
        assert (report["board"], report["summary"]) == (
            build.board.model_dump(),
            build.checks.model_dump(),
        )
        for map_type in build.maps:
            r = client.get(f"/api/sessions/{sid}/builds/{build.id}/maps/{map_type}")
            validate_analysis_map(r.json(), mesh, standards)
    assert client.get(f"/api/sessions/{sid}/builds/nope").status_code == 404


def test_build_source_is_served_at_the_source_url(client) -> None:
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    builds = [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]
    assert len(builds) == 2
    build = builds[-1]
    assert build.source_url == f"/api/sessions/{sid}/builds/{build.id}/source"
    r = client.get(build.source_url)
    assert r.status_code == 200
    source = BuildSource.model_validate(r.json())
    fixture_source = BuildSource.model_validate_json(
        (FIXTURES_DIR / "build-source.json").read_text()
    )
    assert source == fixture_source
    assert source.build_id == build.id
    assert source.features, "the source gives every feature its lines"
    assert source.features == fixture_source.features
    assert client.get(f"/api/sessions/{sid}/builds/nope/source").status_code == 404
    assert client.get(f"/api/sessions/nope/builds/{build.id}/source").status_code == 404


def test_board_of_a_build_is_served_at_the_board_url(client) -> None:
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    builds = [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]
    assert len({b.board.revision for b in builds}) == 2, "a build on each revision"
    for build in builds:
        assert build.board_url == f"/api/sessions/{sid}/builds/{build.id}/board"
        r = client.get(build.board_url)
        assert r.status_code == 200
        board = Board.model_validate(r.json())
        assert (board.name, board.revision) == (build.board.name, build.board.revision)
        (path,) = [p for p in BOARDS_DIR.glob("*.json") if load_board(p) == board]
        assert r.json() == json.loads(path.read_text())
    assert client.get(f"/api/sessions/{sid}/builds/nope/board").status_code == 404
    assert client.get(f"/api/sessions/nope/builds/{build.id}/board").status_code == 404


def test_board_url_is_404_when_the_board_file_is_missing(client, monkeypatch, tmp_path) -> None:
    import app.boards

    monkeypatch.setattr(app.boards, "BOARDS_DIR", tmp_path)
    sid = start(client)
    build = Build.model_validate(json.loads((FIXTURES_DIR / "build.json").read_text()))
    r = client.get(f"/api/sessions/{sid}/builds/{build.id}/board")
    assert r.status_code == 404
    assert build.board.revision in r.json()["detail"]


def test_answering_the_question(client) -> None:
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    events = wait_for(client, sid, "message_end")
    question = next(d for _, t, d in events if t == "question")
    option = question["options"][0]

    bad = {"answer": {"question_id": question["question_id"], "option_id": "nope"}}
    assert client.post(f"/api/sessions/{sid}/messages", json=bad).status_code == 409

    answer = {"answer": {"question_id": question["question_id"], "option_id": option["id"]}}
    assert client.post(f"/api/sessions/{sid}/messages", json=answer).status_code == 202
    reply = wait_for(client, sid, "message_end", after=events[-1][0])
    assert option["label"] in "".join(d["delta"] for _, t, d in reply if t == "text")
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"


def test_parameters_comments_and_exports(client) -> None:
    sid = start(client)
    params = client.get(f"/api/sessions/{sid}/parameters").json()
    assert params == json.loads((FIXTURES_DIR / "parameters.json").read_text())
    name = next(p["name"] for p in params if p["unit"] == "mm")
    r = client.patch(f"/api/sessions/{sid}/parameters/{name}", json={"value": 3.0})
    assert r.json()["value"] == 3.0 and r.json()["locked"] is True
    assert client.patch(f"/api/sessions/{sid}/parameters/nope", json={}).status_code == 404

    build_id = json.loads((FIXTURES_DIR / "build.json").read_text())["id"]
    comment = {"build_id": build_id, "face_id": "lid.outer_cap", "point": [1, 2, 3], "text": "Hi"}
    r = client.post(f"/api/sessions/{sid}/comments", json=comment)
    assert r.status_code == 201 and r.json()["author"] == "Demo user"
    assert client.get(f"/api/sessions/{sid}/comments").json() == [r.json()]
    assert (
        client.post(
            f"/api/sessions/{sid}/comments", json={**comment, "face_id": "no.face"}
        ).status_code
        == 404
    )

    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": build_id, "formats": ["step", "stl", "build_note"]},
    )
    assert r.status_code == 201
    files = {f["format"]: f for f in r.json()["files"]}
    stl = client.get(files["stl"]["url"]).content
    assert len(stl) == 84 + 50 * int.from_bytes(stl[80:84], "little") > 84
    assert client.get(files["step"]["url"]).content.startswith(b"ISO-10303-21")
    assert "## Checks failed" in client.get(files["build_note"]["url"]).text


def test_unknown_session_is_404(client) -> None:
    r = client.get("/api/sessions/nope/messages")
    assert r.status_code == 404 and "detail" in r.json()


def test_an_uploaded_attachment_is_sent_with_a_message(client) -> None:
    from app.boards import BOARDS_DIR

    board = BOARDS_DIR / "b-parasite-case.step"  # uploads are STEP files or images
    sid = start(client)
    r = client.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": (board.name, board.read_bytes(), "application/octet-stream")},
    )
    assert r.status_code == 201, r.text
    attachment = Attachment.model_validate(r.json())
    assert attachment.name == board.name
    got = client.get(attachment.url)
    assert got.status_code == 200 and got.content == board.read_bytes()
    assert board.name in got.headers["content-disposition"]

    r = client.post(
        f"/api/sessions/{sid}/messages",
        json={"text": "Here is the case", "attachments": [attachment.model_dump()]},
    )
    assert r.status_code == 202
    assert Message.model_validate(r.json()).attachments == [attachment]
    (stored,) = client.get(f"/api/sessions/{sid}/messages").json()[:1]
    assert stored["attachments"] == [attachment.model_dump()]


def test_attachment_errors(client) -> None:
    files = {"file": ("x.txt", b"x", "text/plain")}
    assert client.post("/api/sessions/nope/attachments", files=files).status_code == 404
    sid = start(client)
    assert client.get(f"/api/sessions/{sid}/attachments/nope").status_code == 404
    assert client.post(f"/api/sessions/{sid}/attachments").status_code == 422


def test_a_hand_edit_locks_rebuilds_and_rechecks(client) -> None:
    """Setting a value locks it and a rebuild (with its checks) follows on the stream;
    unlocking hands the parameter back to Claude without a rebuild."""
    sid = start(client)
    param = next(p for p in client.get(f"/api/sessions/{sid}/parameters").json() if p["unit"])
    value = param["value"] + 1

    r = client.patch(f"/api/sessions/{sid}/parameters/{param['name']}", json={"value": value})
    assert r.status_code == 200 and r.json()["locked"] is True and r.json()["value"] == value

    events = wait_for(client, sid, "build_ready")
    [ready] = [validate_event(data) for _, kind, data in events if kind == "build_ready"]
    assert ready.message_id is None
    build = ready.build
    fixture_build = json.loads((FIXTURES_DIR / "build.json").read_text())
    assert build.id != fixture_build["id"]
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == build.id
    assert [b["id"] for b in client.get(f"/api/sessions/{sid}/builds").json()] == [build.id]
    assert client.get(f"/api/sessions/{sid}/builds/{build.id}").json() == build.model_dump(
        mode="json"
    )
    checks = client.get(build.checks_url).json()
    validate_check_report(checks, example_standards())
    assert checks["build_id"] == build.id
    assert checks["summary"] == build.checks.model_dump()
    assert client.get(build.mesh_url).status_code == 200

    r = client.patch(f"/api/sessions/{sid}/parameters/{param['name']}", json={"locked": False})
    assert r.json()["locked"] is False and r.json()["value"] == value
    after = events[-1][0]
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
    assert parse_sse(r.text) == []

    # A second edit is a second build.
    client.patch(f"/api/sessions/{sid}/parameters/{param['name']}", json={"value": value + 1})
    builds = [b["id"] for b in client.get(f"/api/sessions/{sid}/builds").json()]
    assert len(set(builds)) == 2


# --- History: diffs and restores -------------------------------------------------------


def _fixture_json(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


def _builds(client, sid: str) -> list[Build]:
    return [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]


def _diff(client, sid: str, build_id: str, against: str) -> dict:
    r = client.get(f"/api/sessions/{sid}/builds/{build_id}/diff", params={"against": against})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_diff_between_revisions_lists_every_board_difference(client) -> None:
    """The compare view's diff: one change per enclosure-affecting difference in
    `demos/b-parasite/CHANGES.md`, from the build diff."""
    from app.boards.changes import parse_changes_md
    from geometry.params import opening_tags

    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    previous, latest = _builds(client, sid)
    assert previous.board.revision != latest.board.revision
    diff = _diff(client, sid, latest.id, previous.id)
    assert (diff["old"], diff["new"]) == (previous.id, latest.id)
    fixture = _fixture_json("diff.json")
    assert (diff["parameters"], diff["features"]) == (fixture["parameters"], fixture["features"])

    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    boards = {
        "old": load_board(BOARDS_DIR / doc.old_file),
        "new": load_board(BOARDS_DIR / doc.new_file),
    }
    assert doc.rows
    for row in doc.rows:
        ref = row["Component"].strip("`")
        board = boards["old" if row["Change"] == "removed" else "new"]
        tags = opening_tags(board)
        (tag,) = {tags[o.id] for o in board.openings if o.component == ref}
        (change,) = [f for f in diff["features"] if f["tag"] == tag]
        assert change["change"] == row["Change"]

    back = _diff(client, sid, previous.id, latest.id)
    assert (back["old"], back["new"]) == (latest.id, previous.id)
    assert {f["tag"] for f in back["features"]} == {f["tag"] for f in diff["features"]}
    assert _diff(client, sid, latest.id, latest.id)["features"] == []
    for path in (f"{latest.id}/diff?against=nope", f"nope/diff?against={latest.id}"):
        assert client.get(f"/api/sessions/{sid}/builds/{path}").status_code == 404


def test_restoring_a_build_brings_back_its_parameters(client) -> None:
    """Undo in the history: an edit makes a new build; restoring the build before it brings
    the parameters back and makes a new build of them, announced on the stream."""
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    params = client.get(f"/api/sessions/{sid}/parameters").json()
    param = next(p for p in params if p["unit"])
    before = _builds(client, sid)[-1]
    client.patch(
        f"/api/sessions/{sid}/parameters/{param['name']}", json={"value": param["value"] + 1}
    )
    edited = _builds(client, sid)[-1]
    (change,) = _diff(client, sid, edited.id, before.id)["parameters"]
    assert (change["name"], change["old"], change["new"]) == (
        param["name"],
        param["value"],
        param["value"] + 1,
    )
    assert _diff(client, sid, edited.id, before.id)["features"] == []

    after = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"}).text
    last = parse_sse(after)[-1][0]
    r = client.post(f"/api/sessions/{sid}/builds/{before.id}/restore")
    assert r.status_code == 201, r.text
    restored = Build.model_validate(r.json())
    assert restored.restored_from == before.id and restored.board == before.board
    assert restored.id not in {b.id for b in (before, edited)}
    found = wait_for(client, sid, "build_ready", after=last)
    (ready,) = [d for _, t, d in found if t == "build_ready"]
    assert ready["message_id"] is None and ready["build"] == r.json()
    assert client.get(f"/api/sessions/{sid}/parameters").json() == params
    assert _diff(client, sid, restored.id, before.id)["parameters"] == []
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == restored.id
    assert client.get(restored.checks_url).json()["summary"] == restored.checks.model_dump()

    # Restoring the build on the older revision makes a build on that revision.
    previous = _builds(client, sid)[0]
    r = client.post(f"/api/sessions/{sid}/builds/{previous.id}/restore")
    again = Build.model_validate(r.json())
    assert again.board == previous.board and again.checks == previous.checks
    report = client.get(again.checks_url).json()
    assert report["board"] == previous.board.model_dump()

    assert client.post(f"/api/sessions/{sid}/builds/nope/restore").status_code == 404
    SESSIONS[sid].session.status = "running"
    assert client.post(f"/api/sessions/{sid}/builds/{before.id}/restore").status_code == 409


# --- Change sets -------------------------------------------------------------------------


def answered(client) -> tuple[str, list]:
    """A session whose recorded run has finished and whose question is answered."""
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    events = wait_for(client, sid, "message_end")
    question = next(d for _, t, d in events if t == "question")
    answer = {"question_id": question["question_id"], "option_id": question["options"][0]["id"]}
    client.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
    return sid, wait_for(client, sid, "changes_ready", after=events[-1][0])


def changes_rows() -> list[dict]:
    from app.boards import BOARDS_DIR
    from app.boards.changes import parse_changes_md

    return parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text()).rows


def test_the_fixture_change_set_covers_every_board_change(client) -> None:
    import jsonschema

    from app.contracts.common import CONTRACTS_DIR

    schema = json.loads((CONTRACTS_DIR / "change-set.schema.json").read_text())
    fixture = json.loads((FIXTURES_DIR / "change-set.json").read_text())
    jsonschema.validate(fixture, schema)
    rows = changes_rows()
    components = [c["delta"]["component"] for c in fixture["changes"]]
    assert sorted(components) == sorted(r["Component"].strip("` ") for r in rows)
    for change in fixture["changes"]:
        row = next(r for r in rows if r["Component"].strip("` ") == change["delta"]["component"])
        assert row["What it breaks"] in change["reason"]
        assert change["status"] == "proposed"
        assert any(k["effect"] == "fixes" for k in change["checks"])
    build_id = json.loads((FIXTURES_DIR / "build.json").read_text())["id"]
    assert fixture["base_build"] == fixture["build"] == build_id

    sid = start(client)
    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404
    sid, reply = answered(client)
    (ready,) = [d for _, t, d in reply if t == "changes_ready"]
    assert ready["change_set"] == fixture
    assert client.get(f"/api/sessions/{sid}/changes").json() == fixture
    for _, kind, data in reply:
        assert validate_event(data).type == kind


def test_accept_and_reject_update_the_statuses_and_the_build(client) -> None:
    import jsonschema

    from app.contracts.common import CONTRACTS_DIR

    schema = json.loads((CONTRACTS_DIR / "change-set.schema.json").read_text())
    sid, reply = answered(client)
    change_set = client.get(f"/api/sessions/{sid}/changes").json()
    first, second, *_ = change_set["changes"]
    fixes = {k["id"] for k in first["checks"] if k["effect"] == "fixes"}

    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first["id"]]})
    assert r.status_code == 200, r.text
    reviewed = r.json()
    jsonschema.validate(reviewed, schema)
    status = {c["id"]: c["status"] for c in reviewed["changes"]}
    assert status[first["id"]] == "accepted" and status[second["id"]] == "proposed"
    assert reviewed["build"] != change_set["build"]
    assert reviewed["checks"]["fail"] == change_set["checks"]["fail"] - len(fixes)
    accepted = next(c for c in reviewed["changes"] if c["id"] == first["id"])
    assert all(k["after"] == "pass" for k in accepted["checks"] if k["effect"] == "fixes")

    # The build streams and serves checks that say the same.
    found = wait_for(client, sid, "changes_ready", after=reply[-1][0])
    assert [t for _, t, _ in found] == ["build_ready", "changes_ready"]
    build = Build.model_validate(found[0][2]["build"])
    assert build.id == reviewed["build"] and found[1][2]["change_set"] == reviewed
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == build.id
    mesh = validate_mesh(client.get(build.mesh_url).content)
    report = validate_check_report(client.get(build.checks_url).json(), example_standards(), mesh)
    assert report.build_id == build.id
    assert report.summary.model_dump(by_alias=True) == reviewed["checks"]
    assert all(r.status == "pass" for r in report.results if r.id in fixes)
    assert client.get(build.source_url).json()["build_id"] == build.id

    # Rejecting it puts the checks back.
    r = client.post(
        f"/api/sessions/{sid}/changes", json={"reject": [first["id"]], "accept": [second["id"]]}
    )
    again = r.json()
    status = {c["id"]: c["status"] for c in again["changes"]}
    assert status[first["id"]] == "rejected" and status[second["id"]] == "accepted"
    report = client.get(f"/api/sessions/{sid}/builds/{again['build']}/checks").json()
    assert all(x["status"] == "fail" for x in report["results"] if x["id"] in fixes)


def test_a_question_after_the_answer_keeps_the_change_set(client) -> None:
    """A question-only turn after the answer leaves the design as it was, so the change set
    stays served and reviewable, as in the real API."""
    sid, reply = answered(client)
    before = client.get(f"/api/sessions/{sid}/changes").json()
    client.post(f"/api/sessions/{sid}/messages", json={"text": "Why did this change?"})
    wait_for(client, sid, "message_end", after=reply[-1][0])
    r = client.get(f"/api/sessions/{sid}/changes")
    assert r.status_code == 200 and r.json() == before
    first = before["changes"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [first]})
    assert r.status_code == 200, r.text
    assert next(c for c in r.json()["changes"] if c["id"] == first)["status"] == "accepted"


def test_a_restore_retires_the_change_set(client) -> None:
    sid, _ = answered(client)
    some = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    first = client.get(f"/api/sessions/{sid}/builds").json()[0]["id"]
    assert client.post(f"/api/sessions/{sid}/builds/{first}/restore").status_code == 201
    assert client.get(f"/api/sessions/{sid}/changes").status_code == 404
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [some]})
    assert r.status_code == 404


def test_a_review_of_unknown_or_contradictory_changes_is_refused(client) -> None:
    sid, _ = answered(client)
    some = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": ["nope"]})
    assert r.status_code == 404 and "nope" in r.json()["detail"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [some], "reject": [some]})
    assert r.status_code == 409
    assert client.get("/api/sessions/nope/changes").status_code == 404


# --- Risk flags --------------------------------------------------------------------------


def flagged(client) -> tuple[str, list]:
    """A session whose recorded run has finished, its question answered and its risks flagged."""
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    events = wait_for(client, sid, "message_end")
    question = next(d for _, t, d in events if t == "question")
    answer = {"question_id": question["question_id"], "option_id": question["options"][0]["id"]}
    client.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
    return sid, wait_for(client, sid, "risks_ready", after=events[-1][0])


def test_the_mock_serves_the_fixture_risk_flags(client) -> None:
    import jsonschema

    from app.contracts.common import CONTRACTS_DIR

    schema = json.loads((CONTRACTS_DIR / "risk-flags.schema.json").read_text())
    fixture = _fixture_json("risk-flags.json")
    jsonschema.validate(fixture, schema)
    sid = start(client)
    assert client.get(f"/api/sessions/{sid}/risks").status_code == 404
    sid, reply = flagged(client)
    kinds = [t for _, t, _ in reply]
    assert kinds.index("changes_ready") < kinds.index("risks_ready")
    (ready,) = [d for _, t, d in reply if t == "risks_ready"]
    end = next(d for _, t, d in reply if t == "message_end")
    assert ready["risk_flags"] == fixture and ready["message_id"] == end["message_id"]
    assert client.get(f"/api/sessions/{sid}/risks").json() == fixture
    for _, kind, data in reply:
        assert validate_event(data).type == kind


def test_accepting_and_ignoring_a_fix_in_the_mock(client) -> None:
    sid, reply = flagged(client)
    flags = client.get(f"/api/sessions/{sid}/risks").json()
    first = flags["flags"][0]["id"]
    others = [f["id"] for f in flags["flags"][1:]]

    r = client.post(f"/api/sessions/{sid}/risks", json={"accept": [first]})
    assert r.status_code == 200, r.text
    reviewed = r.json()
    status = {f["id"]: f["status"] for f in reviewed["flags"]}
    assert status[first] == "accepted" and all(status[o] == "proposed" for o in others)
    found = wait_for(client, sid, "risks_ready", after=reply[-1][0])
    assert [t for _, t, _ in found] == ["build_ready", "risks_ready"]
    assert found[0][2]["message_id"] is None and found[1][2]["risk_flags"] == reviewed
    build = Build.model_validate(found[0][2]["build"])
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == build.id
    assert client.get(build.checks_url).json()["build_id"] == build.id

    r = client.post(f"/api/sessions/{sid}/risks", json={"reject": [first]})
    assert {f["id"]: f["status"] for f in r.json()["flags"]}[first] == "rejected"
    assert client.post(f"/api/sessions/{sid}/risks", json={"accept": ["nope"]}).status_code == 404
    r = client.post(f"/api/sessions/{sid}/risks", json={"accept": [first], "reject": [first]})
    assert r.status_code == 409
    first_build = client.get(f"/api/sessions/{sid}/builds").json()[0]["id"]
    assert client.post(f"/api/sessions/{sid}/builds/{first_build}/restore").status_code == 201
    assert client.get(f"/api/sessions/{sid}/risks").status_code == 404


# --- Loading the next revision -----------------------------------------------------------


def test_loading_the_next_revision_plays_the_run_with_its_proposal(client) -> None:
    sid = start(client)
    session = client.get(f"/api/sessions/{sid}").json()
    newer = _fixture_json("build.json")["board"]
    older = _fixture_json("previous-build.json")["board"]
    assert session["next_revision"] == newer

    r = client.post(f"/api/sessions/{sid}/revisions/next", json={})
    assert r.status_code == 202, r.text
    message = Message.model_validate(r.json())
    assert message.role == "user"
    assert newer["revision"] in message.text and older["revision"] in message.text

    found = wait_for(client, sid, "changes_ready")
    for _, kind, data in found:
        assert validate_event(data).type == kind
    kinds = [k for _, k, _ in found]
    assert "question" not in kinds, "nobody asked, so nothing waits for an answer"
    assert kinds[-3:] == ["changes_ready", "risks_ready", "message_end"]
    ready = [d for _, k, d in found if k == "build_ready"]
    assert ready[-1]["build"]["board"] == newer
    assert found[-2][2]["risk_flags"] == _fixture_json("risk-flags.json")
    assert found[-2][2]["message_id"] == found[-1][2]["message_id"]
    change_set = found[-3][2]
    assert change_set["change_set"] == _fixture_json("change-set.json")
    assert change_set["message_id"] == found[-1][2]["message_id"]
    assert client.get(f"/api/sessions/{sid}/changes").json() == change_set["change_set"]
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "idle" and session["next_revision"] is None
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]

    r = client.post(f"/api/sessions/{sid}/revisions/next")
    assert r.status_code == 409 and "revision" in r.json()["detail"]
    assert client.post("/api/sessions/nope/revisions/next").status_code == 404


def test_standards_are_listed_and_chosen_with_the_first_message(client) -> None:
    """#73: the sets come from standards.yaml; the first message's choice is kept."""
    standards = load_standards()
    (listed,) = client.get("/api/standards").json()
    assert listed["id"] == standards.id and listed["name"] == standards.name
    assert len(listed["standards"]) == len(standards.standards)
    sid = start(client)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "go", "standards": "nope"})
    assert r.status_code == 404
    r = client.post(
        f"/api/sessions/{sid}/messages", json={"text": "Check it", "standards": standards.id}
    )
    assert r.status_code == 202 and r.json()["standards"] == standards.id
    assert client.get(f"/api/sessions/{sid}").json()["standards"] == standards.id


# --- Code edits ---------------------------------------------------------------------------------


def _fixture_source() -> dict:
    return json.loads((FIXTURES_DIR / "build-source.json").read_text())


def test_editing_the_code_rebuilds_and_rechecks(client) -> None:
    """A code edit is a hand edit: a rebuild (with its checks) follows on the stream
    without a message, and the rebuild's source is the edited code."""
    sid = start(client)
    source = _fixture_source()
    code = source["code"] + "\n# Edited by hand\n"
    r = client.put(
        f"/api/sessions/{sid}/source", json={"build_id": source["build_id"], "code": code}
    )
    assert r.status_code == 200, r.text
    build = Build.model_validate(r.json())

    events = wait_for(client, sid, "build_ready")
    [ready] = [validate_event(data) for _, kind, data in events if kind == "build_ready"]
    assert ready.message_id is None and ready.build == build
    assert build.id != source["build_id"]
    checks = client.get(build.checks_url).json()
    validate_check_report(checks, example_standards())
    assert checks["build_id"] == build.id
    edited = BuildSource.model_validate(client.get(build.source_url).json())
    assert edited.build_id == build.id and edited.code == code
    assert edited.features == BuildSource.model_validate(source).features
    # The build it was edited from keeps its code.
    assert (
        client.get(f"/api/sessions/{sid}/builds/{source['build_id']}/source").json()["code"]
        == (source["code"])
    )


@pytest.mark.parametrize(
    ("code", "line", "detail"),
    [
        ("def build(params):\n    return {\n", 2, "Syntax error"),
        ("x = 1\n\n\ndef build(params)\n    pass\n", 4, "Syntax error"),
        ("def make(params):\n    return {}\n", None, "build(params)"),
    ],
)
def test_code_that_cannot_build_is_refused_with_its_line(client, code, line, detail) -> None:
    """A syntax error (or no build function) is a 400 with the line, and nothing is
    rebuilt."""
    sid = start(client)
    build_id = _fixture_source()["build_id"]
    r = client.put(f"/api/sessions/{sid}/source", json={"build_id": build_id, "code": code})
    assert r.status_code == 400
    body = r.json()
    assert detail in body["detail"] and body.get("line") == line
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"})
    assert parse_sse(r.text) == []
    assert client.get(f"/api/sessions/{sid}/builds").json() == []


def test_editing_the_code_of_an_unknown_build_is_404(client) -> None:
    sid = start(client)
    r = client.put(f"/api/sessions/{sid}/source", json={"build_id": "nope", "code": "x"})
    assert r.status_code == 404


def test_sessions_created_at_once_get_different_ids(client, monkeypatch) -> None:
    """Parallel e2e tests create sessions at the same time; each must get its own."""
    from concurrent.futures import ThreadPoolExecutor

    from app import mock_api

    class SlowState(mock_api.SessionState):
        def __init__(self, session) -> None:
            time.sleep(0.02)  # widen the window between picking the ID and storing the session
            super().__init__(session)

    monkeypatch.setattr(mock_api, "SessionState", SlowState)
    with ThreadPoolExecutor(8) as pool:
        posts = pool.map(lambda _: client.post("/api/sessions", json={}), range(32))
        ids = [r.json()["id"] for r in posts]
    assert len(set(ids)) == len(ids)


def test_undo_and_redo_carry_the_change_set_they_moved_to(client) -> None:
    """As on the real backend: the set `GET /changes` serves after the move, null for a 404."""
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    wait_for(client, sid, "message_end")
    param = next(p for p in client.get(f"/api/sessions/{sid}/parameters").json() if p["unit"])
    client.patch(
        f"/api/sessions/{sid}/parameters/{param['name']}", json={"value": param["value"] + 1}
    )
    for direction in ("undo", "redo"):
        r = client.post(f"/api/sessions/{sid}/history/{direction}")
        assert r.status_code == 200, r.text
        served = client.get(f"/api/sessions/{sid}/changes")
        assert r.json()["change_set"] == (served.json() if served.status_code == 200 else None)
