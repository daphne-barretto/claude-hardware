"""A browser's copy of a demo (`POST /api/demos/{demo}/session`, `app.sample_copies`): a session
of the browser's own that starts where the demo's original left off, in the real API and the
mock. The demos and the examples are read from `demos/sessions/` and `demos/b-parasite/`; nothing
here names a board."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.contracts.common import BoardRef
from app.examples import examples
from app.fixtures.samples import SAMPLES_DIR


def read(path) -> dict:
    return json.loads(path.read_text())


def sample_ids() -> list[str]:
    return read(SAMPLES_DIR / "index.json")["samples"]


def sample_doc(sid: str) -> dict:
    return read(SAMPLES_DIR / sid / "session.json")


def sample_build(sid: str, build_id: str, name: str) -> dict:
    return read(SAMPLES_DIR / sid / "builds" / build_id / name)


def latest_board(sid: str) -> dict | None:
    doc = sample_doc(sid)
    return sample_build(sid, doc["session"]["latest_build_id"], "build.json")["board"]


def expected_boards(sid: str) -> tuple[dict | None, dict | None]:
    """The session's board: the one its recorded session used (its latest build's), never the
    example's newer revision. Its next revision, as opening the matching example gives it; a
    recorded run that hasn't loaded its next revision still offers it."""
    board = latest_board(sid) or sample_doc(sid)["session"]["board"]
    recorded = sample_doc(sid)["session"]["next_revision"]
    if recorded is not None:
        return board, recorded
    if board is None:
        return None, None
    for example in examples():
        previous, new = example.boards()
        new_ref = BoardRef(name=new.name, revision=new.revision).model_dump()
        if {"name": previous.name, "revision": previous.revision} == board:
            return board, new_ref
        if new_ref == board:
            return board, None
    raise AssertionError(f"no example for {board}")


def board_sample_with_next_revision() -> str:
    found = [s for s in sample_ids() if expected_boards(s)[1] is not None]
    assert found, "a sample on an example's previous board revision"
    return found[0]


@pytest.fixture(params=["real", "mock"])
def api(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    if request.param == "real":
        from app.main import app
        from app.session_api import CONFIG, SESSIONS, fixture_services

        monkeypatch.setattr(CONFIG, "services", fixture_services)
        SESSIONS.by_id.clear()
    else:
        from app.mock_api import SESSIONS, app

        SESSIONS.clear()
    return request.param, app


@pytest.fixture
def client(api):
    with TestClient(api[1]) as c:
        yield c


def continue_from(client, sid: str) -> dict:
    response = client.post(f"/api/demos/{sid}/session")
    assert response.status_code == 200, response.text
    return response.json()


def snapshot(client, sid: str) -> dict:
    base = f"/api/sessions/{sid}"
    return {
        "session": client.get(base).json(),
        "messages": client.get(base + "/messages").json(),
        "builds": client.get(base + "/builds").json(),
        "parameters": client.get(base + "/parameters").json(),
    }


@pytest.mark.parametrize("sid", sample_ids())
def test_continuing_copies_the_samples_latest_state(client, sid) -> None:
    before = snapshot(client, sid)
    doc = sample_doc(sid)
    session = continue_from(client, sid)

    assert session["id"] != sid and not session.get("sample")
    assert session["demo"] == sid
    assert session["title"] == doc["session"]["title"]
    assert session["standards"] == doc["session"]["standards"]
    assert session["status"] == "idle"
    board, next_revision = expected_boards(sid)
    assert session["board"] == board
    assert session["next_revision"] == next_revision

    base = f"/api/sessions/{session['id']}"
    assert [(m["role"], m["text"]) for m in client.get(base + "/messages").json()] == [
        (m["role"], m["text"]) for m in doc["messages"]
    ]
    params = {p["name"]: p["value"] for p in client.get(base + "/parameters").json()}
    for p in doc["parameters"]:
        assert params[p["name"]] == p["value"]

    # The builds, oldest first, with the sample's files: the canvas shows the same model.
    builds = client.get(base + "/builds").json()
    sample_builds = [
        e["event"]["build"] for e in doc["events"] if e["event"]["type"] == "build_ready"
    ]
    assert len(builds) == len(sample_builds)
    assert builds[-1]["id"] == session["latest_build_id"]
    for copy, original in zip(builds, sample_builds, strict=True):
        assert copy["board"] == original["board"]
        assert copy["checks"] == original["checks"]
        assert copy["mesh_url"] == f"{base}/builds/{copy['id']}/mesh"
        one = f"{base}/builds/{copy['id']}"
        mesh = client.get(one + "/mesh")
        assert mesh.status_code == 200
        assert json.loads(mesh.content) == sample_build(sid, original["id"], "mesh.gltf")
        checks = client.get(one + "/checks").json()
        assert checks["summary"] == original["checks"] and checks["build_id"] == copy["id"]
        source = client.get(one + "/source").json()
        assert source["code"] == sample_build(sid, original["id"], "source.json")["code"]
        for map_type in copy["maps"]:
            doc_map = client.get(f"{one}/maps/{map_type}").json()
            assert doc_map["type"] == map_type and doc_map["build_id"] == copy["id"]
    if len(builds) > 1:
        diff = client.get(
            f"{base}/builds/{builds[-1]['id']}/diff", params={"against": builds[0]["id"]}
        )
        assert diff.status_code == 200, diff.text
        assert diff.json()["old"] == builds[0]["id"] and diff.json()["new"] == builds[-1]["id"]

    # The events carry the conversation with the copied builds.
    stream = client.get(base + "/events", params={"follow": "false"}).text
    frames = [f for f in stream.split("\n\n") if f.strip()]
    assert len(frames) == len(doc["events"])

    # The original is untouched; the list shows the copy in the demo's place, once.
    assert snapshot(client, sid) == before
    listed = client.get("/api/sessions").json()
    assert [s["demo"] for s in listed if s.get("demo")] == sample_ids()
    assert [s["id"] for s in listed if s.get("demo") == sid] == [session["id"]]


def test_the_copy_takes_messages_and_edits(client, api) -> None:
    """The copy is a live session: it takes the hand edits a sample refuses."""
    sid = board_sample_with_next_revision()
    session = continue_from(client, sid)
    base = f"/api/sessions/{session['id']}"
    comment = client.post(
        base + "/comments",
        json={"text": "Check this", "build_id": session["latest_build_id"], "face": "x"},
    )
    assert comment.status_code != 409, comment.text
    # The sample still refuses it.
    refused = client.post(
        f"/api/sessions/{sid}/comments",
        json={"text": "x", "build_id": sample_doc(sid)["session"]["latest_build_id"], "face": "x"},
    )
    assert refused.status_code == 409


def test_the_board_sample_offers_the_next_revision_like_an_example(client) -> None:
    sid = board_sample_with_next_revision()
    copied = continue_from(client, sid)
    [example] = [
        e
        for e in examples()
        if {"name": e.boards()[0].name, "revision": e.boards()[0].revision} == latest_board(sid)
    ]
    opened = client.post("/api/sessions", json={"example": example.id}).json()
    # On the board its recorded session used, with the example's next revision to load.
    assert copied["board"] == latest_board(sid)
    assert copied["next_revision"] == opened["next_revision"] is not None


@pytest.mark.parametrize("sid", [s for s in sample_ids() if latest_board(s) is not None])
def test_a_copy_is_checked_against_the_board_its_session_used(real, sid) -> None:
    """Regression: a copy of a demo recorded on an example's older revision opened on the newer
    one, so the copy said (and checked against) a board its conversation never used. It opens,
    and stays after a restart, on its latest build's board until the next revision is loaded."""
    from app.session_api import SESSIONS

    board = latest_board(sid)
    with TestClient(real) as client:
        session = continue_from(client, sid)
        assert session["board"] == board
        for _ in range(2):  # as made, then after a restart (loaded from the session store)
            services = SESSIONS.get(session["id"]).ws.services
            checked = services.board
            assert {"name": checked.name, "revision": checked.revision} == board
            assert client.get(f"/api/sessions/{session['id']}").json()["board"] == board
            SESSIONS.by_id.clear()


def test_mock_loads_the_next_revision_of_a_copy(api) -> None:
    if api[0] != "mock":
        pytest.skip("the real API's revision load runs Claude")
    with TestClient(api[1]) as client:
        session = continue_from(client, board_sample_with_next_revision())
        base = f"/api/sessions/{session['id']}"
        loaded = client.post(base + "/revisions/next", json={"mode": "auto"})
        assert loaded.status_code == 202, loaded.text
        assert client.get(base).json()["next_revision"] is None


def test_unknown_demos_are_refused(client) -> None:
    assert client.post("/api/demos/sample-nope/session").status_code == 404
    # An ordinary session isn't a demo: no copy of it is made.
    own = client.post("/api/sessions", json={}).json()
    assert client.post(f"/api/demos/{own['id']}/session").status_code == 404
    assert client.post("/api/sessions", json={"from_sample": sample_ids()[0]}).status_code == 422


# --- The real API --------------------------------------------------------------------------------


@pytest.fixture
def real(api):
    if api[0] != "real":
        pytest.skip("real API only")
    return api[1]


def test_the_copy_belongs_to_the_browser_that_made_it(real) -> None:
    with TestClient(real) as mine, TestClient(real) as theirs:
        session = continue_from(mine, sample_ids()[0])
        assert mine.get(f"/api/sessions/{session['id']}").status_code == 200
        assert theirs.get(f"/api/sessions/{session['id']}").status_code == 404
        assert session["id"] not in [s["id"] for s in theirs.get("/api/sessions").json()]


def test_the_copy_is_in_the_build_store_and_survives_a_restart(real) -> None:
    from app.session_api import CONFIG, SESSIONS

    with TestClient(real) as client:
        board_less = next(s for s in sample_ids() if latest_board(s) is None)
        for sid in (board_sample_with_next_revision(), board_less):
            session = continue_from(client, sid)
            base = f"/api/sessions/{session['id']}"
            builds = client.get(base + "/builds").json()
            store = CONFIG.store()
            records = [store.get(b["id"]) for b in builds]
            assert [r.parent for r in records] == [None, *[r.id for r in records[:-1]]]
            latest = sample_build(sid, sample_doc(sid)["session"]["latest_build_id"], "design.json")
            assert records[-1].parameters == latest["params"]
            state = SESSIONS.get(session["id"])
            assert state.ws.design.params == latest["params"]
            assert state.ws.has_design
            chain = getattr(state.ws.services, "chain", None)  # real services: the next build
            if chain is not None:  # is the latest one's child
                assert chain.parent == records[-1].id

            SESSIONS.by_id.clear()  # a restart: the session is loaded from the session store
            reloaded = client.get(base).json()
            assert reloaded == session
            assert client.get(base + "/builds").json() == builds
            assert client.get(f"{base}/builds/{builds[-1]['id']}/mesh").status_code == 200
            assert SESSIONS.get(session["id"]).ws.design.params == latest["params"]


@pytest.mark.parametrize("sid", sample_ids())
def test_a_copy_keeps_the_constraints_its_conversation_set(api, client, sid) -> None:
    """The chips are the ones the sample's conversation set, so editing one or sending the next
    message goes on from them, as in the session that was recorded. (The mock lists every
    standard whatever the design, so there only the user's own chips are compared.)"""
    doc = sample_doc(sid)
    streamed = [e["event"] for e in doc["events"] if e["event"]["type"] == "constraints"]
    session = continue_from(client, sid)
    chips = client.get(f"/api/sessions/{session['id']}/constraints").json()
    if not streamed:
        assert chips == []
        return
    expected = streamed[-1]["constraints"]
    if api[0] == "mock":
        chips = [c for c in chips if c["source"] != "standard"]
        expected = [c for c in expected if c["source"] != "standard"]
    assert [(c["id"], c["value"], c["source"]) for c in chips] == [
        (c["id"], c["value"], c["source"]) for c in expected
    ]


def samples_with_risks() -> list[str]:
    return [
        s
        for s in sample_ids()
        if any(e["event"]["type"] == "risks_ready" for e in sample_doc(s)["events"])
    ]


def recorded_flags(sid: str) -> dict:
    """The risk flags of the sample's last `risks_ready`: what its chat shows."""
    events = [e["event"] for e in sample_doc(sid)["events"] if e["event"]["type"] == "risks_ready"]
    return events[-1]["risk_flags"]


@pytest.mark.parametrize("sid", samples_with_risks())
def test_a_copy_can_accept_and_ignore_its_risk_flags(api, sid) -> None:
    """Regression: a copy showed its recorded risk flags in the chat, but Accept and Ignore got
    404 ("no risk flags") because the copy's session had none. The copy carries them: it lists
    them, and each fix can be ignored and accepted, also after a restart."""
    flag = recorded_flags(sid)["flags"][0]
    with TestClient(api[1]) as client:
        session = continue_from(client, sid)
        base = f"/api/sessions/{session['id']}"
        listed = client.get(base + "/risks")
        assert listed.status_code == 200, listed.text
        assert [f["id"] for f in listed.json()["flags"]] == [
            f["id"] for f in recorded_flags(sid)["flags"]
        ]
        ignored = client.post(base + "/risks", json={"reject": [flag["id"]]})
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["flags"][0]["status"] == "rejected"
        if api[0] == "real":
            from app.session_api import SESSIONS

            SESSIONS.by_id.clear()  # a restart: the flags come back from the session store
            assert client.get(base + "/risks").json()["flags"][0]["status"] == "rejected"
        accepted = client.post(base + "/risks", json={"accept": [flag["id"]]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["flags"][0]["status"] == "accepted"
