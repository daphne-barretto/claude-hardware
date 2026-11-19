"""Comments pinned to geometry, on the real session API with a scripted Claude.

- A pin stays on its face after a rebuild that moves the face (real geometry, tessellated with
  stable IDs), and goes to the nearest face of the same feature if the face ID is gone.
- A plain comment is a note: it starts nothing. Sending it starts one run whose reply goes in
  the comment's thread, not the chat; the change made for it is linked to it in the change set,
  where accept, reject and Undo (a reject of an applied change) work.
- Three comments sent together make one run with three replies and three linked changes.
- Resolve keeps the thread in the list. Open comments reach Claude as context on every run.

The scenario comes from `demos/b-parasite/`, `standards.yaml` and `fixtures/`; nothing here names a
part.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
from datetime import UTC, datetime

import jsonschema
import pytest
import test_session_api as api
from fastapi.testclient import TestClient

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.comments import context, locate, mesh_info, pin
from app.contracts.common import CONTRACTS_DIR
from app.contracts.session import Comment, CommentCreate, validate_event
from app.main import app
from app.session_api import CONFIG, SESSIONS
from app.standards import load_standards

CHANGE_SET_SCHEMA = json.loads((CONTRACTS_DIR / "change-set.schema.json").read_text())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def claude(monkeypatch) -> api.Claude:
    fake = api.Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


class PassingServices(api.OpeningServices):
    """Instant builds whose checks all pass, so a run that answers a comment can finish."""

    def checks(self, outcome):
        return [dataclasses.replace(c, status="pass") for c in super().checks(outcome)]


@pytest.fixture
def openings(monkeypatch) -> list[api.OpeningServices]:
    made: list[api.OpeningServices] = []

    def services(board, standards):
        made.append(PassingServices(board, standards))
        return made[-1]

    monkeypatch.setattr(CONFIG, "services", services)
    return made


# --- The pin follows its face through rebuilds ------------------------------------------------


def _geometry():
    from geometry import enclosure_parameters, generate_enclosure
    from geometry.tessellate import tessellate

    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    board = load_board(BOARDS_DIR / doc.old_file)
    standards = load_standards()
    params = enclosure_parameters(board, standards)

    def build(p, previous=None):
        tess = tessellate(generate_enclosure(board, standards, p), previous)
        return tess, mesh_info(json.dumps(tess.to_gltf()))

    return params, build


def _on_face(mesh, face_id: str, point) -> bool:
    """Whether `point` lies on the face (within 1 µm of one of its triangles)."""
    from app.comments import _closest, _face, _triangles

    part, index = _face(mesh, face_id)
    return math.dist(_closest(tuple(point), _triangles(part, index)), point) < 1e-3


def _comment(mesh, face_id: str, offset=(0.0, 0.0, 0.0)) -> Comment:
    from app.comments import _centre, _face, _triangles

    part, index = _face(mesh, face_id)
    centre = _centre(_triangles(part, index))
    point = tuple(c + o for c, o in zip(centre, offset, strict=True))
    body = CommentCreate(build_id="build-1", face_id=face_id, point=point, text="Check this")
    return pin(body, mesh, comment_id="comment-1", author="You", created_at=datetime.now(UTC))


def test_a_pin_stays_on_its_face_after_a_rebuild_that_moves_it() -> None:
    params, build = _geometry()
    before, mesh = build(params)
    opening = next(o for o in params["openings"] if o["cut"])
    face = next(f.id for f in before.faces.values() if f.feature == opening["tag"])
    comment = _comment(mesh, face)
    assert comment.feature == opening["tag"] and comment.part
    assert comment.location is not None and comment.location.faces == [face]
    assert _on_face(mesh, face, comment.location.point)

    dx, dy = 0.8, -0.5
    moved = copy.deepcopy(params)
    target = next(o for o in moved["openings"] if o["id"] == opening["id"])
    target["x"] += dx
    target["y"] += dy
    _, after = build(moved, before)

    where = locate(comment, after)
    assert where is not None
    assert where.faces == [face], "the same face, by its stable ID"
    assert where.part == comment.part
    assert where.point[0] - comment.point[0] == pytest.approx(dx, abs=1e-3)
    assert where.point[1] - comment.point[1] == pytest.approx(dy, abs=1e-3)
    assert where.point[2] == pytest.approx(comment.point[2], abs=1e-3)
    assert _on_face(after, face, where.point)


def test_a_pin_whose_face_id_is_gone_moves_to_the_nearest_face_of_its_feature() -> None:
    params, build = _geometry()
    _, mesh = build(params)
    part = next(p for p in mesh.parts.values() if len({f.feature for f in p.faces}) > 1)
    face = next(f for f in part.faces if f.feature != part.name)
    comment = _comment(mesh, face.id)
    renamed = comment.model_copy(update={"face_id": f"{face.id}.gone"})
    where = locate(renamed, mesh)
    assert where is not None and where.faces == [face.id], "its feature's face at the anchor"
    gone = renamed.model_copy(update={"feature": "no_such_feature"})
    assert locate(gone, mesh) is None


# --- Notes and requests over the API ----------------------------------------------------------


def _mesh_faces(client, build) -> list[tuple[str, str, str]]:
    """(part, feature, face ID) of every face of the build's mesh."""
    gltf = json.loads(client.get(build["mesh_url"]).text)
    return [
        (n["extras"]["part"], f["feature"], f["id"])
        for n in gltf["nodes"]
        for f in n.get("extras", {}).get("faces", [])
    ]


def _pin(client, sid: str, build: dict, face: str, text: str) -> dict:
    gltf = mesh_info(client.get(build["mesh_url"]).text)
    from app.comments import _centre, _face, _triangles

    part, index = _face(gltf, face)
    point = list(_centre(_triangles(part, index)))
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build["id"], "face_id": face, "point": point, "text": text},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _latest(client, sid: str) -> dict:
    return client.get(f"/api/sessions/{sid}/builds").json()[-1]


def test_a_plain_comment_is_a_note_that_starts_nothing(client) -> None:
    sid, found = api.start(client)
    build = _latest(client, sid)
    _, feature, face = _mesh_faces(client, build)[0]
    comment = _pin(client, sid, build, face, "Can we add a light pipe here?")
    Comment.model_validate(comment)
    assert comment["request"] is None and comment["replies"] == [] and not comment["resolved"]
    assert comment["feature"] == feature and comment["location"]["faces"] == [face]

    new = api.events(client, sid, found[-1][0])
    assert [t for _, t, _ in new] == ["comment"], "only the comment streams; no run starts"
    validate_event(new[0][2])
    assert new[0][2]["comment"] == comment
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"
    assert client.get(f"/api/sessions/{sid}/messages").json() == []

    # A reply in the thread is a note too.
    r = client.post(f"/api/sessions/{sid}/comments/{comment['id']}/replies", json={"text": "+1"})
    assert r.status_code == 201
    assert [(x["role"], x["text"]) for x in r.json()["replies"]] == [("user", "+1")]
    assert client.get(f"/api/sessions/{sid}/messages").json() == []
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"


def _send(client, sid: str, comment_ids: list[str], mode: str = "auto") -> dict:
    r = client.post(
        f"/api/sessions/{sid}/messages", json={"text": "", "comments": comment_ids, "mode": mode}
    )
    assert r.status_code == 202, r.text
    return r.json()


def _opening_faces(client, sid: str, build: dict) -> dict[str, tuple[str, str]]:
    """A face of each opening feature of the build's mesh, by feature: (part, face ID)."""
    out: dict[str, tuple[str, str]] = {}
    for part, feature, face in _mesh_faces(client, build):
        if feature.startswith("opening_"):
            out.setdefault(feature, (part, face))
    return out


def _new_openings(ws) -> dict[str, dict]:
    """The new board's openings as the generator would place them, by feature tag."""
    from geometry import enclosure_parameters

    params = enclosure_parameters(ws.board, ws.standards)
    return {o["tag"]: o for o in params["openings"]}


def _opening_path(ws, tag: str) -> str:
    opening = next(o for o in ws.design.params["openings"] if o["tag"] == tag)
    return f"openings.{opening['id']}"


def test_send_to_claude_answers_in_the_thread_with_a_linked_change(
    client, claude, openings
) -> None:
    sid, found = api.start(client)
    build = _latest(client, sid)
    feature, (_, face) = next(iter(_opening_faces(client, sid, build).items()))
    comment = _pin(client, sid, build, face, "Move this opening to where the board needs it.")
    reply = "Moved the opening over its component; its alignment check passes."

    def script(ws):
        path = _opening_path(ws, feature)
        new = _new_openings(ws)[feature]
        return [
            (
                "set_parameter",
                {"changes": [{"name": f"{path}.{k}", "value": new[k]} for k in "xy"]},
            ),
            ("reply_to_comment", {"comment": comment["id"], "text": reply, "changes": [path]}),
            ("finish", {"outcome": "done", "summary": "Moved it."}),
        ]

    claude.scripts.append(script)
    after = api.last_id(client, sid)
    sent = _send(client, sid, [comment["id"]])
    assert sent["comments"] == [comment["id"]] and sent["text"] == ""
    run = api.wait_until(client, sid, "idle", after)
    api.assert_contract_events(run)

    # The request: the comment and where it is reach Claude, with the rule to reply in the thread.
    prompt = claude.prompts[-1]
    assert comment["id"] in prompt and face in prompt and "reply_to_comment" in prompt
    assert "Move this opening" in prompt

    # One run; the reply is in the thread only.
    starts = [d for _, t, d in run if t == "message_start"]
    assert len(starts) == 1
    mid = starts[0]["message_id"]
    updates = [d["comment"] for _, t, d in run if t == "comment"]
    assert updates[0]["request"] == {"message_id": mid, "status": "running"}
    final = client.get(f"/api/sessions/{sid}/comments").json()[0]
    assert final["request"] == {"message_id": mid, "status": updates[-1]["request"]["status"]}
    claude_replies = [r for r in final["replies"] if r["role"] == "assistant"]
    assert [r["text"] for r in claude_replies] == [reply]
    assert claude_replies[0]["author"] == "Claude" and claude_replies[0]["message_id"] == mid
    chat = "".join(d["delta"] for _, t, d in run if t == "text")
    assert reply not in chat


def test_the_change_for_a_comment_is_linked_and_reviewable_from_the_thread(
    client, claude, openings
) -> None:
    sid, found = api.start(client)
    build = _latest(client, sid)
    feature, (_, face) = next(iter(_opening_faces(client, sid, build).items()))
    comment = _pin(client, sid, build, face, "Line this opening up with the new board.")

    def script(ws):
        path = _opening_path(ws, feature)
        new = _new_openings(ws)[feature]
        return [
            (
                "set_parameter",
                {"changes": [{"name": f"{path}.{k}", "value": new[k]} for k in "xy"]},
            ),
            ("reply_to_comment", {"comment": comment["id"], "text": "Done.", "changes": [path]}),
            ("finish", {"outcome": "done", "summary": "Moved it."}),
        ]

    claude.scripts.append(script)
    _send(client, sid, [comment["id"]], mode="manual")
    run = api.wait_until(client, sid, "idle", found[-1][0])
    (ready,) = [d["change_set"] for _, t, d in run if t == "changes_ready"]
    jsonschema.validate(ready, CHANGE_SET_SCHEMA)
    linked = [c for c in ready["changes"] if c["comment"] == comment["id"]]
    assert len(linked) == 1 and linked[0]["delta"]["feature"] == feature
    assert linked[0]["status"] == "proposed"
    change = linked[0]["id"]

    # Accept from the thread, then Undo: a reject of the applied change rebuilds without it.
    proposal = SESSIONS.get(sid).proposal
    r = client.post(f"/api/sessions/{sid}/changes", json={"accept": [change]})
    assert r.status_code == 200
    accepted = {c["id"]: c for c in r.json()["changes"]}[change]
    assert accepted["status"] == "accepted" and accepted["comment"] == comment["id"]
    r = client.post(f"/api/sessions/{sid}/changes", json={"reject": [change]})
    assert r.status_code == 200
    assert {c["id"]: c["status"] for c in r.json()["changes"]}[change] == "rejected"
    assert SESSIONS.get(sid).ws.design.snapshot() == proposal.design_for(set())


def _three_changes(ws) -> list[tuple[str, list[dict]]]:
    """Three edits Claude could make: the openings the new board moves, then small changes to
    top-level dimensions, as (change path, set_parameter changes)."""
    new = _new_openings(ws)
    out = []
    for o in ws.design.params["openings"]:
        target = new.get(o["tag"])
        if target and (target["x"], target["y"]) != (o["x"], o["y"]):
            path = f"openings.{o['id']}"
            out.append((path, [{"name": f"{path}.{k}", "value": target[k]} for k in ("x", "y")]))
    for name, value in ws.design.params.items():
        if isinstance(value, float) and not isinstance(value, bool):
            out.append((name, [{"name": name, "value": round(value * 1.05, 3)}]))
    return out[:3]


def test_three_comments_sent_together_make_one_run_with_three_replies_and_changes(
    client, claude, openings
) -> None:
    sid, found = api.start(client)
    build = _latest(client, sid)
    gltf = _mesh_faces(client, build)
    comments: list[dict] = []
    paths: list[str] = []

    def script(ws):
        steps = []
        for c, (path, changes) in zip(comments, _three_changes(ws), strict=True):
            paths.append(path)
            steps.append(("set_parameter", {"changes": changes}))
            steps.append(
                (
                    "reply_to_comment",
                    {"comment": c["id"], "text": f"Did {path}.", "changes": [path]},
                )
            )
        return [*steps, ("finish", {"outcome": "done", "summary": "Did all three."})]

    features = sorted({f for _, f, _ in gltf})[:3]
    for n, feature in enumerate(features):
        face = next(face for _, f, face in gltf if f == feature)
        comments.append(_pin(client, sid, build, face, f"Request {n + 1}"))
    claude.scripts.append(script)
    after = api.last_id(client, sid)
    _send(client, sid, [c["id"] for c in comments])
    run = api.wait_until(client, sid, "idle", after)
    api.assert_contract_events(run)

    assert [t for _, t, _ in run].count("message_start") == 1, "one run"
    assert len(claude.prompts) == 1
    assert all(c["id"] in claude.prompts[0] for c in comments)
    listed = {c["id"]: c for c in client.get(f"/api/sessions/{sid}/comments").json()}
    for c, path in zip(comments, paths, strict=True):
        replies = [r["text"] for r in listed[c["id"]]["replies"] if r["role"] == "assistant"]
        assert replies == [f"Did {path}."], "each comment gets its own reply"
        assert listed[c["id"]]["request"]["status"] == "answered"
    (ready,) = [d["change_set"] for _, t, d in run if t == "changes_ready"]
    by_comment = {c["comment"]: c["id"] for c in ready["changes"] if c["comment"]}
    assert by_comment == {c["id"]: path for c, path in zip(comments, paths, strict=True)}


def test_resolve_keeps_the_thread_in_the_list(client, claude) -> None:
    sid, _ = api.start(client)
    build = _latest(client, sid)
    _, _, face = _mesh_faces(client, build)[0]
    comment = _pin(client, sid, build, face, "Looks good.")
    r = client.patch(f"/api/sessions/{sid}/comments/{comment['id']}", json={"resolved": True})
    assert r.status_code == 200 and r.json()["resolved"] is True
    listed = client.get(f"/api/sessions/{sid}/comments").json()
    assert [c["id"] for c in listed] == [comment["id"]] and listed[0]["resolved"]
    # A resolved comment can't be sent; a reply reopens it.
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "", "comments": [comment["id"]]})
    assert r.status_code == 409
    r = client.post(f"/api/sessions/{sid}/comments/{comment['id']}/replies", json={"text": "No"})
    assert r.json()["resolved"] is False
    assert (
        client.patch(f"/api/sessions/{sid}/comments/nope", json={"resolved": True}).status_code
        == 404
    )
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "", "comments": ["nope"]})
    assert r.status_code == 404
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": ""}).status_code == 409


def test_open_comments_reach_claude_as_context(client, claude) -> None:
    sid, found = api.start(client)
    build = _latest(client, sid)
    faces = _mesh_faces(client, build)
    note = _pin(client, sid, build, faces[0][2], "Keep this wall plain.")
    done = _pin(client, sid, build, faces[1][2], "Old note.")
    client.patch(f"/api/sessions/{sid}/comments/{done['id']}", json={"resolved": True})
    claude.scripts.append([("finish", {"outcome": "blocked", "summary": "Noted."})])
    api.send(client, sid, text="Anything to change?")
    api.wait_until(client, sid, "idle", found[-1][0])
    prompt = claude.prompts[-1]
    assert note["id"] in prompt and "Keep this wall plain." in prompt and "not requests" in prompt
    assert done["id"] not in prompt, "resolved comments are closed"


def test_comments_are_listed_on_the_build_asked_for(client) -> None:
    sid, _ = api.start(client)
    build = _latest(client, sid)
    _, _, face = _mesh_faces(client, build)[0]
    comment = _pin(client, sid, build, face, "Here.")
    r = client.get(f"/api/sessions/{sid}/comments", params={"build_id": build["id"]})
    assert r.json()[0]["location"] == comment["location"]
    assert (
        client.get(f"/api/sessions/{sid}/comments", params={"build_id": "nope"}).status_code == 404
    )


def test_reply_to_comment_refuses_comments_not_sent_and_untested_claims() -> None:
    import asyncio

    from app.agent.run import scenario_workspace
    from app.agent.services import FixtureServices

    ws = scenario_workspace(services=FixtureServices())
    out = asyncio.run(ws.reply_to_comment({"comment": "comment-1", "text": "Done."}))
    assert out.get("is_error") and "No comments" in out["content"][0]["text"]
    replies: list[tuple[str, str]] = []
    ws.comments = [
        Comment(
            id="comment-1",
            build_id="build-1",
            face_id="lid.outer_cap",
            point=(0, 0, 0),
            text="Is it strong?",
            author="You",
            created_at=datetime.now(UTC),
        )
    ]
    ws.on_reply = lambda cid, text: replies.append((cid, text))
    out = asyncio.run(ws.reply_to_comment({"comment": "comment-2", "text": "Done."}))
    assert out.get("is_error") and "comment-1" in out["content"][0]["text"]
    out = asyncio.run(ws.reply_to_comment({"comment": "comment-1", "text": "It is strong enough."}))
    assert out.get("is_error"), "the reply goes through the untested-claims scan"
    out = asyncio.run(
        ws.reply_to_comment({"comment": "comment-1", "text": "Untested: needs review by hand."})
    )
    assert not out.get("is_error") and replies == [("comment-1", "Untested: needs review by hand.")]


def test_context_lists_requests_and_open_notes() -> None:
    def make(cid: str, text: str, resolved: bool = False) -> Comment:
        return Comment(
            id=cid,
            build_id="build-1",
            face_id=f"{cid}.face",
            point=(1, 2, 3),
            text=text,
            author="You",
            created_at=datetime.now(UTC),
            resolved=resolved,
        )

    comments = [make("a", "Do this"), make("b", "FYI"), make("c", "Closed", resolved=True)]
    text = context(comments, ["a"])
    assert text.index("Do this") < text.index("FYI") and "Closed" not in text
    assert context([], []) == ""


# --- The mock API serves the same contract -------------------------------------------------------


@pytest.fixture
def mock(monkeypatch):
    from app import mock_api

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    mock_api.SESSIONS.clear()
    with TestClient(mock_api.app) as test_client:
        yield test_client


def _mock_events(mock, sid: str, after: int = 0) -> list[tuple[int, str, dict]]:
    r = mock.get(f"/api/sessions/{sid}/events", params={"follow": "false", "after": after})
    return api.parse_sse(r.text)


def _mock_idle(mock, sid: str, after: int) -> list[tuple[int, str, dict]]:
    import time

    for _ in range(300):
        found = _mock_events(mock, sid, after)
        status = mock.get(f"/api/sessions/{sid}").json()["status"]
        if status == "idle" and found and found[-1][1] == "message_end":
            return found
        time.sleep(0.02)
    raise AssertionError("the mock run never ended")


def test_the_mock_api_pins_sends_batches_and_resolves(mock) -> None:
    sid = mock.post("/api/sessions", json={}).json()["id"]
    build = json.loads((api.FIXTURES_DIR / "build.json").read_text())
    gltf = json.loads((api.FIXTURES_DIR / "enclosure.gltf").read_text())
    faces = [
        (f["feature"], f["id"]) for n in gltf["nodes"] for f in n.get("extras", {}).get("faces", [])
    ]
    picks = []
    for feature, face in faces:
        if feature not in {f for f, _ in picks}:
            picks.append((feature, face))
    comments = []
    for n, (_, face) in enumerate(picks[:3]):
        r = mock.post(
            f"/api/sessions/{sid}/comments",
            json={"build_id": build["id"], "face_id": face, "point": [0, 0, 0], "text": f"Ask {n}"},
        )
        assert r.status_code == 201, r.text
        comments.append(r.json())
        assert r.json()["location"]["faces"] == [face], "located on the fixture mesh"
    # Notes start nothing.
    found = _mock_events(mock, sid)
    assert [t for _, t, _ in found] == ["comment"] * 3
    assert mock.get(f"/api/sessions/{sid}/messages").json() == []

    # Three sent together: one run, a reply and a linked change each.
    ids = [c["id"] for c in comments]
    r = mock.post(
        f"/api/sessions/{sid}/messages", json={"text": "", "comments": ids, "mode": "manual"}
    )
    assert r.status_code == 202 and r.json()["comments"] == ids
    run = _mock_idle(mock, sid, found[-1][0])
    for _, _, data in run:
        validate_event(data)
    assert [t for _, t, _ in run].count("message_start") == 1
    listed = {c["id"]: c for c in mock.get(f"/api/sessions/{sid}/comments").json()}
    for cid in ids:
        assert [x["role"] for x in listed[cid]["replies"]] == ["assistant"]
        assert listed[cid]["request"]["status"] == "answered"
    change_set = mock.get(f"/api/sessions/{sid}/changes").json()
    jsonschema.validate(change_set, CHANGE_SET_SCHEMA)
    by_comment = {c["comment"]: c for c in change_set["changes"]}
    assert set(by_comment) == set(ids)
    assert all(c["status"] == "proposed" for c in change_set["changes"])

    # Accept from a thread, then Undo (reject).
    change = by_comment[ids[0]]["id"]
    r = mock.post(f"/api/sessions/{sid}/changes", json={"accept": [change]})
    assert {c["id"]: c["status"] for c in r.json()["changes"]}[change] == "accepted"
    r = mock.post(f"/api/sessions/{sid}/changes", json={"reject": [change]})
    assert {c["id"]: c["status"] for c in r.json()["changes"]}[change] == "rejected"
    assert {c["id"]: c["comment"] for c in r.json()["changes"]}[change] == ids[0]

    # Resolve keeps the thread; a reply reopens it.
    r = mock.patch(f"/api/sessions/{sid}/comments/{ids[0]}", json={"resolved": True})
    assert r.json()["resolved"] is True
    assert [c["id"] for c in mock.get(f"/api/sessions/{sid}/comments").json()] == ids
    r = mock.post(
        f"/api/sessions/{sid}/messages", json={"text": "", "comments": [ids[0]], "mode": "auto"}
    )
    assert r.status_code == 409
    r = mock.post(f"/api/sessions/{sid}/comments/{ids[0]}/replies", json={"text": "Again"})
    assert r.status_code == 201 and r.json()["resolved"] is False
    r = mock.patch(f"/api/sessions/{sid}/comments/nope", json={"resolved": True})
    assert r.status_code == 404
