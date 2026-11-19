"""Find in model from chat: Claude's `find_in_model` tool and the `found` event.

The tool resolves a query with the find-in-model resolver (`app.find`) on the latest
build, exactly as `POST .../builds/{build}/find` does, and streams the result as a `found`
event: the canvas highlights the matched faces and the chat lists them, ready to send back as
the selection. An unclear query comes back with what was understood and a question to ask.

Claude is played by `ScriptedDriver` (no model). Queries name feature kinds and analysis
values, never a part of the scenario.
"""

from __future__ import annotations

import asyncio
import json

import test_session_api
from test_session_api import Claude, events, send, start, wait_until

from app.agent.run import run_agent, scenario_workspace
from app.agent.scripted import ScriptedDriver
from app.agent.services import FIXTURES_DIR, FixtureServices
from app.agent.tools import TOOLS
from app.contracts.session import FindResult, validate_event
from app.find import find_in_build

claude = test_session_api.claude
client = test_session_api.client

# Every drafted face of the fixture enclosure is under 2° (its draft is 1.5°).
QUERY = "faces under 2° of draft"
UNCLEAR = "purple widgets"


def ask(client, claude: Claude, query: str, reply: str) -> tuple[str, dict, list]:
    """A session where the user asks for `query` and Claude looks it up, then replies. Returns
    the session ID, the tool's result and the events of the reply."""
    sid, _ = start(client)
    after = events(client, sid)[-1][0]
    claude.scripts.append([("find_in_model", {"query": query}), ("text", reply)])
    send(client, sid, text=f"Select all {query}.")
    new = wait_until(client, sid, "idle", after)
    (call,) = claude.drivers[-1].calls
    assert not call.is_error, call.text
    return sid, json.loads(call.text), new


def test_the_tool_returns_what_the_find_route_returns_for_the_latest_build(client, claude):
    sid, out, new = ask(client, claude, QUERY, "These faces have under 2° of draft.")
    latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    assert out["build"] == latest
    r = client.post(f"/api/sessions/{sid}/builds/{latest}/find", json={"query": QUERY})
    assert r.status_code == 200, r.text
    route = FindResult.model_validate(r.json())
    assert FindResult.model_validate(out["result"]) == route
    assert route.status == "found" and route.matches
    assert "highlighted" in out["next"]


def test_the_matches_reach_the_canvas_as_a_found_event(client, claude):
    sid, out, new = ask(client, claude, QUERY, "These faces have under 2° of draft.")
    (found,) = [data for _, kind, data in new if kind == "found"]
    event = validate_event(found)
    assert event.build_id == out["build"]
    assert event.result == FindResult.model_validate(out["result"])
    # The faces are the build's mesh faces, so the canvas can highlight and select them.
    mesh = client.get(f"/api/sessions/{sid}/builds/{event.build_id}/mesh")
    from app.contracts.mesh import validate_mesh

    faces = validate_mesh(mesh.content).face_parts
    assert all(faces[m.face] == m.part for m in event.result.matches)
    # Inside the reply, before Claude's text about it.
    kinds = [kind for _, kind, _ in new]
    assert kinds.index("found") < kinds.index("text")
    assert found["message_id"] == next(d for _, k, d in new if k == "message_start")["message_id"]


def test_an_unclear_query_gets_claudes_question_with_what_it_understood(client, claude):
    _, out, new = ask(client, claude, UNCLEAR, "I'm not sure what you mean. Did you mean...?")
    result = FindResult.model_validate(out["result"])
    assert result.status == "unclear" and not result.matches
    assert result.understood and result.question
    # The tool tells Claude to ask with what it understood rather than guess.
    assert "understood" in out["next"] and "ask" in out["next"]
    # And the chat gets both, whatever Claude writes.
    (found,) = [validate_event(d) for _, k, d in new if k == "found"]
    assert found.result.question == result.question
    assert found.result.understood == result.understood


def test_finding_changes_nothing_and_needs_no_finish(client, claude):
    sid, _, new = ask(client, claude, QUERY, "Here they are.")
    assert not [d for _, k, d in new if k == "error"]
    assert not [d for _, k, d in new if k == "changes_ready"]


def test_without_a_session_the_tool_reads_the_builds_own_documents():
    """Outside the session API (an evaluation run), the tool reads the fixture build's mesh and
    maps, as the session API serves a fixture build."""
    ws = scenario_workspace(services=FixtureServices())
    driver = ScriptedDriver(ws, [("find_in_model", {"query": QUERY})])
    asyncio.run(run_agent(ws, "Find them.", driver=driver))
    out = json.loads(driver.calls[0].text)
    maps = {
        t: json.loads((FIXTURES_DIR / "maps" / f"{t}.json").read_text())
        for t in ws.latest.build.maps
    }
    expected = find_in_build(
        QUERY, (FIXTURES_DIR / "enclosure.gltf").read_text(), [], maps, ws.standards
    )
    assert FindResult.model_validate(out["result"]) == expected
    assert ws.sink.of_type("found")[0].result == expected


def test_an_empty_query_is_refused():
    ws = scenario_workspace(services=FixtureServices())
    out = asyncio.run(ws.find_in_model({"query": "  "}))
    assert out.get("is_error") and not ws.sink.of_type("found")
    assert TOOLS["find_in_model"]["schema"]["required"] == ["query"]


def test_the_mock_api_answers_a_find_message_with_a_found_event(monkeypatch):
    """The mock session API (frontend work) plays Claude's find for a message that starts with
    "Find ": the result is what its find route returns for the latest build."""
    import time

    from fastapi.testclient import TestClient

    from app.mock_api import SESSIONS, app

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    SESSIONS.clear()

    def wait(client, sid, kind, after=0):
        for _ in range(500):
            new = events(client, sid, after)
            if any(k == kind for _, k, _ in new):
                return new
            time.sleep(0.02)
        raise AssertionError(f"no {kind}")

    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/messages", json={"text": "Check it."})
        (question,) = [d for _, k, d in wait(client, sid, "question") if k == "question"]
        answer = {"question_id": question["question_id"], "option_id": question["options"][0]["id"]}
        client.post(f"/api/sessions/{sid}/messages", json={"answer": answer})
        after = wait(client, sid, "changes_ready")[-1][0]
        r = client.post(f"/api/sessions/{sid}/messages", json={"text": f"Find {QUERY}."})
        assert r.status_code == 202, r.text
        new = wait(client, sid, "message_end", after)
        (found,) = [validate_event(d) for _, k, d in new if k == "found"]
        latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
        route = client.post(f"/api/sessions/{sid}/builds/{latest}/find", json={"query": QUERY})
        assert found.build_id == latest
        assert found.result == FindResult.model_validate(route.json())
        assert found.result.status == "found"
