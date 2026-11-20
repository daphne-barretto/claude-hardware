"""Each comment thread sent to Claude is its own conversation, beside the main chat
(`app.conversations`): its own history and its own question, which waits on that thread only.
All of them edit the one design, so their runs take turns, and a thread's run reports back to
the main chat.

Claude is a gated `ScriptedDriver` (as in the message queue tests): each run waits for the
test to let it go. What it does comes from the comment or message it was sent.
"""

from __future__ import annotations

import json
import threading

import test_message_queue as mq
from test_message_queue import Gated, events, replies, running, send, start, wait_for, wait_until

from app.conversations import MAIN
from app.session_api import CONFIG, SESSIONS

# The message queue tests' fixtures: the API on the contract fixtures.
data, client = mq.data, mq.client

OPTIONS = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]


def _number(ws) -> str:
    """A top-level number of the design, for a run to change."""
    return next(k for k, v in ws.design.params.items() if isinstance(v, float))


class Claude(mq.Claude):
    """One gated run per message. A comment or message starting with "Ask" asks a question;
    one starting with "Grow" makes a number of the design 10% bigger. Each run notes the value
    of that number as it starts (`seen`)."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[float] = []

    def __call__(self, state, body, prompt):
        text = body.text or " ".join(state.comment(c).text for c in body.comments)
        self.texts.append(text)
        self.prompts.append(prompt)
        name = _number(state.ws)
        self.seen.append(state.ws.design.params[name])
        gate = threading.Event()
        self.gates.append(gate)
        if text.startswith("Ask") and "The user answered" not in prompt:
            steps = [
                ("ask_user", {"question": f"Which for: {text}?", "options": OPTIONS}),
                ("text", "Answered."),
                ("finish", {"outcome": "done", "summary": "Answered."}),
            ]
        elif text.startswith("Grow"):
            value = round(state.ws.design.params[name] * 1.1, 3)
            steps = [
                ("set_parameter", {"changes": [{"name": name, "value": value}]}),
                ("text", f"Made {name} bigger."),
                ("finish", {"outcome": "done", "summary": f"Made {name} {value}."}),
            ]
        else:
            steps = [
                ("text", f"Reply to: {text}"),
                ("finish", {"outcome": "done", "summary": f"Replied to {text}."}),
            ]
        return Gated(state.ws, steps, gate)


def _claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


def _comment(client, sid: str, text: str) -> str:
    build = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    mesh_url = client.get(f"/api/sessions/{sid}/builds/{build}").json()["mesh_url"]
    mesh = json.loads(client.get(mesh_url).text)
    face = next(f["id"] for n in mesh["nodes"] for f in n.get("extras", {}).get("faces", []))
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build, "face_id": face, "point": [0, 0, 0], "text": text},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _conversations(client, sid: str) -> dict[str, dict]:
    r = client.get(f"/api/sessions/{sid}/conversations")
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()}


def _settle(client, sid: str, check, seconds: float = 15) -> dict[str, dict]:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = _conversations(client, sid)
        if check(found):
            return found
        time.sleep(0.02)
    raise AssertionError(f"never happened: {_conversations(client, sid)}")


def test_a_thread_question_waits_on_that_thread_only(client, monkeypatch) -> None:
    claude = _claude(monkeypatch)
    sid, found = start(client)
    lid, corners = _comment(client, sid, "Ask about the lid"), _comment(client, sid, "Corners")
    send(client, sid, comments=[lid])
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]
    convs = _conversations(client, sid)
    assert convs[lid]["status"] == "waiting_for_answer"
    assert convs[lid]["question"]["question_id"] == question["question_id"]
    assert convs[MAIN]["status"] == "idle"

    # Another thread goes on: the question is parked in its thread, not left unanswered.
    send(client, sid, comments=[corners])
    running(client, sid)
    assert "left open" not in claude.prompts[1]
    # It knows the other thread waits on its question.
    assert 'Claude asked: "Which for: Ask about the lid?"' in claude.prompts[1]
    convs = _conversations(client, sid)
    assert convs[corners]["status"] == "working"
    assert convs[lid]["status"] == "waiting_for_answer"
    assert convs[lid]["question"]["question_id"] == question["question_id"]
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Corners" in replies(f))
    wait_until(client, sid, "idle", found[-1][0])

    # So does the main chat.
    send(client, sid, text="Hello")
    claude.release(2)
    wait_for(client, sid, lambda f: "Reply to: Hello" in replies(f))
    wait_until(client, sid, "idle", found[-1][0])
    convs = _conversations(client, sid)
    assert convs[lid]["status"] == "waiting_for_answer"
    # The pointer in the main chat and the thread answer it the same way: the answer carries on
    # in the thread, in a new run on the same request.
    answer = {"question_id": question["question_id"], "option_id": "b"}
    send(client, sid, answer=answer)
    claude.release(3)
    wait_for(client, sid, lambda f: "Reply to: Ask about the lid" in replies(f))
    assert "The user answered: B" in claude.prompts[3]
    assert "Which for: Ask about the lid?" in claude.prompts[3]
    wait_until(client, sid, "idle", found[-1][0])
    convs = _settle(client, sid, lambda c: c[lid]["status"] == "idle")
    assert convs[lid]["question"] is None
    comment = next(c for c in client.get(f"/api/sessions/{sid}/comments").json() if c["id"] == lid)
    assert comment["request"]["status"] == "answered"
    # Answered once: it can't be answered again.
    assert client.post(f"/api/sessions/{sid}/messages", json={"answer": answer}).status_code == 409


def test_runs_take_turns_and_the_second_builds_on_the_first(client, monkeypatch) -> None:
    claude = _claude(monkeypatch)
    sid, found = start(client)
    first, second = _comment(client, sid, "Grow it"), _comment(client, sid, "Grow it more")
    send(client, sid, comments=[first])
    running(client, sid)
    # Sent while the first works: it queues behind it, and nothing is refused.
    r = client.post(f"/api/sessions/{sid}/queue", json={"text": "", "comments": [second]})
    assert r.status_code == 201, r.text
    convs = _conversations(client, sid)
    assert convs[first]["status"] == "working"
    assert convs[second]["status"] == "queued"
    assert convs[second]["queued_behind"] == first
    assert convs[second]["queued_behind_title"] == "Grow it"
    streamed = [d for _, t, d in events(client, sid) if t == "conversations"]
    assert streamed and {c["id"]: c["status"] for c in streamed[-1]["conversations"]} == {
        MAIN: "idle",
        first: "working",
        second: "queued",
    }

    claude.release(0)
    wait_for(client, sid, lambda f: len(claude.seen) == 2)
    # The second run starts from the design the first left.
    assert claude.seen[1] == round(claude.seen[0] * 1.1, 3)
    name = _number(SESSIONS.get(sid).ws)
    assert f"Made {name}" in claude.prompts[1]  # and is told what the first did
    claude.release(1)
    wait_for(client, sid, lambda f: sum(t == "thread_report" for _, t, _ in f) == 2)
    assert SESSIONS.get(sid).ws.design.params[name] == round(claude.seen[1] * 1.1, 3)


def test_a_threads_run_reports_back_to_the_main_chat(client, monkeypatch) -> None:
    claude = _claude(monkeypatch)
    sid, found = start(client)
    grow = _comment(client, sid, "Grow it")
    send(client, sid, comments=[grow])
    claude.release(0)
    after = wait_for(client, sid, lambda f: any(t == "thread_report" for _, t, _ in f))
    (report,) = [d for _, t, d in after if t == "thread_report"]
    name = _number(SESSIONS.get(sid).ws)
    assert report["comment_id"] == grow and report["thread_title"] == "Grow it"
    assert report["outcome"] == "changed" and f"Made {name}" in report["summary"]
    # The build it ended on, numbered as the chat shows the session's builds.
    streamed = [d["build"] for _, t, d in after if t == "build_ready"]
    assert report["build_id"] == streamed[-1]["id"] and report["build_number"] == len(streamed)
    assert report["checks"] == streamed[-1]["checks"]
    # The entry is a message of the main chat, linked to its thread.
    stored = client.get(f"/api/sessions/{sid}/messages").json()
    entry = next(m for m in stored if m["id"] == report["message_id"])
    assert entry["role"] == "assistant" and entry["comments"] == [grow]


def test_each_conversation_is_told_about_the_others(client, monkeypatch) -> None:
    claude = _claude(monkeypatch)
    sid, found = start(client)
    send(client, sid, text="Add a star on the lid")
    claude.release(0)
    wait_until(client, sid, "idle", found[-1][0])

    # A thread's run gets the main timeline, next to its own thread.
    grow = _comment(client, sid, "Grow it")
    send(client, sid, comments=[grow])
    claude.release(1)
    wait_for(client, sid, lambda f: any(t == "thread_report" for _, t, _ in f))
    wait_until(client, sid, "idle", found[-1][0])
    thread_prompt = claude.prompts[1]
    assert "main chat's timeline" in thread_prompt
    assert "Add a star on the lid" in thread_prompt and "Replied to Add a star" in thread_prompt
    assert "Grow it" in thread_prompt

    # The main chat's run gets the thread's summary: what it changed is part of the design.
    send(client, sid, text="Make it bigger still")
    claude.release(2)
    wait_for(client, sid, lambda f: "Reply to: Make it bigger still" in replies(f))
    name = _number(SESSIONS.get(sid).ws)
    main_prompt = claude.prompts[2]
    assert "Comment threads sent to you" in main_prompt
    assert f'"Grow it" ({grow}): Latest: Made {name}' in main_prompt
