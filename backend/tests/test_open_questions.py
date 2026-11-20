"""An open question never blocks a send: a message or a comment sent while Claude's question is
open goes to Claude. The question is closed as left unanswered (its reply ends with the note
`QUESTION_LEFT_OPEN`), and the new run is told what was asked and where, so Claude decides
whether the new message answers it, replaces it, or is about something else.

Claude is the gated `ScriptedDriver` of the message queue tests.
"""

from __future__ import annotations

import json

import test_message_queue as mq
from test_message_queue import (
    Claude,
    events,
    replies,
    running,
    send,
    start,
    wait_for,
    wait_until,
)

from app.session_api import QUESTION_LEFT_OPEN

# The message queue tests' fixtures: the API on the contract fixtures, and the gated Claude.
data, client, claude = mq.data, mq.client, mq.claude


def _ask(client, sid: str, claude, found) -> dict:
    send(client, sid, text="Ask me")
    running(client, sid)
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]
    return question


def test_a_message_sent_while_a_question_is_open_goes_to_claude(client, claude) -> None:
    sid, found = start(client)
    question = _ask(client, sid, claude, found)
    mark = events(client, sid)[-1][0]

    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "Make it red"})
    assert r.status_code == 202, r.text
    after = events(client, sid, mark)
    mid = question["message_id"]
    # The question's reply ends with the note, so the card shows what happened.
    notes = [d["text"] for _, t, d in after if t == "progress" and d["message_id"] == mid]
    assert notes == [QUESTION_LEFT_OPEN]
    assert ("message_end", mid) in [(t, d.get("message_id")) for _, t, d in after]
    # The new message reaches Claude, with the question it left open.
    assert claude.texts == ["Ask me", "Make it red"]
    prompt = claude.prompts[1]
    assert prompt.startswith("Make it red")
    assert "Which?" in prompt and "left open" in prompt
    # The question can't be answered any more.
    stale = {"answer": {"question_id": question["question_id"], "option_id": "a"}}
    assert client.post(f"/api/sessions/{sid}/messages", json=stale).status_code == 409
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Make it red" in replies(f))


def test_an_empty_message_leaves_the_question_open(client, claude) -> None:
    sid, found = start(client)
    question = _ask(client, sid, claude, found)
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": " "}).status_code == 409
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "waiting_for_answer"
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "a"})
    wait_for(client, sid, lambda f: "Answered." in replies(f))


class ThreadClaude(Claude):
    """Asks when a comment sent to it starts with "Ask"."""

    def __call__(self, state, body, prompt):
        if any(state.comment(cid).text.startswith("Ask") for cid in body.comments):
            body = body.model_copy(update={"text": "Ask in the thread"})
        return super().__call__(state, body, prompt)


def _comment(client, sid: str, build: dict, text: str) -> str:
    mesh = json.loads(client.get(build["mesh_url"]).text)
    face = next(f["id"] for n in mesh["nodes"] for f in n.get("extras", {}).get("faces", []))
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build["id"], "face_id": face, "point": [0, 0, 0], "text": text},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_comment_sent_in_the_thread_that_asked_leaves_its_question_open(
    client, monkeypatch
) -> None:
    from app.session_api import CONFIG

    fake = ThreadClaude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    sid, found = start(client)
    build = next(d["build"] for _, t, d in found if t == "build_ready")
    asking = _comment(client, sid, build, "Ask me about the lid")
    send(client, sid, comments=[asking])
    running(client, sid)
    fake.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]

    # The same thread's "Send again" goes through; Claude is told its question and the thread.
    r = client.post(f"/api/sessions/{sid}/messages", json={"comments": [asking]})
    assert r.status_code == 202, r.text
    assert len(fake.prompts) == 2
    prompt = fake.prompts[1]
    assert "Which?" in prompt and "left open" in prompt
    assert asking in prompt and "Ask me about the lid" in prompt
    after = events(client, sid, asked[-1][0])
    notes = [
        d["text"]
        for _, t, d in after
        if t == "progress" and d["message_id"] == question["message_id"]
    ]
    assert notes == [QUESTION_LEFT_OPEN]
    fake.release(1)


mock = mq.mock


def test_the_mock_sends_a_message_while_its_question_is_open(mock) -> None:
    from app.examples import examples

    sid = mock.post("/api/sessions", json={"example": examples()[0].id}).json()["id"]
    assert mock.post(f"/api/sessions/{sid}/messages", json={"text": "go"}).status_code == 202
    found = wait_for(mock, sid, lambda f: any(t == "message_end" for _, t, _ in f))
    assert any(t == "question" for _, t, _ in found)  # the recorded run ends on a question
    r = mock.post(f"/api/sessions/{sid}/messages", json={"text": "Something else"})
    assert r.status_code == 202, r.text
    after = wait_for(mock, sid, lambda f: "this message was received" in replies(f))
    assert QUESTION_LEFT_OPEN in [d["text"] for _, t, d in after[len(found) :] if t == "progress"]
