"""The message queue (`app.message_queue`): the user keeps sending while Claude works.

Messages queued during a run are sent one at a time, in order, when it ends; they can be edited,
moved and removed before that. A question pauses the queue (a queued message is never taken
as the answer) until it is answered or a message is sent anyway. Stop ends the run and the queue
goes on. The queue is saved with the session and belongs to its browser. The mock API serves
the same routes (`app.mock_api`).

Claude is a `ScriptedDriver` that waits for the test to open its gate, so a run is going for as
long as a test needs.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from test_session_api import events, send, start

from app.agent.scripted import ScriptedDriver
from app.contracts.session import Message, MessageQueue, QueuedMessage
from app.main import app
from app.message_queue import SENT_ANYWAY
from app.owners import OWNER_COOKIE, new_owner
from app.session_api import CONFIG, SESSIONS, STOPPED, fixture_services


class Gated(ScriptedDriver):
    """Plays its steps once `gate` is open (a thread event: the app runs on another thread)."""

    def __init__(self, ws, steps, gate: threading.Event):
        super().__init__(ws, steps)
        self.gate = gate

    async def _play(self):
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        async for message in super()._play():
            yield message


class Claude:
    """One run per message: a gated reply that says which message it got, or a question when
    the text asks for one."""

    def __init__(self) -> None:
        self.gates: list[threading.Event] = []
        self.texts: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, state, body, prompt):
        self.texts.append(body.text)
        self.prompts.append(prompt)
        gate = threading.Event()
        self.gates.append(gate)
        if body.text.startswith("Ask"):
            options = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
            steps = [
                ("ask_user", {"question": "Which?", "options": options}),
                ("text", "Answered."),
                ("finish", {"outcome": "blocked", "summary": "Answered."}),
            ]
        else:
            steps = [
                ("text", f"Reply to: {body.text}"),
                ("finish", {"outcome": "blocked", "summary": "Replied."}),
            ]
        return Gated(state.ws, steps, gate)

    def release(self, n: int) -> None:
        """Open the gate of run `n` (0: the first), once it has started."""
        for _ in range(500):
            if len(self.gates) > n:
                self.gates[n].set()
                return
            time.sleep(0.01)
        raise AssertionError(f"run {n} never started: {self.texts}")


@pytest.fixture
def claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


@pytest.fixture
def data(tmp_path, monkeypatch):
    path = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(path))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    yield path
    SESSIONS.by_id.clear()


BROWSER = {OWNER_COOKIE: new_owner()}


@contextmanager
def backend(cookies=BROWSER):
    """One app instance; leaving it is the shutdown of a restart."""
    with TestClient(app, cookies=cookies) as client:
        yield client
    SESSIONS.by_id.clear()


@pytest.fixture
def client(data):
    with backend() as test_client:
        yield test_client


def queue(client, sid: str, text: str, **body) -> QueuedMessage:
    r = client.post(f"/api/sessions/{sid}/queue", json={"text": text, **body})
    assert r.status_code == 201, r.text
    return QueuedMessage.model_validate(r.json())


def get_queue(client, sid: str) -> MessageQueue:
    r = client.get(f"/api/sessions/{sid}/queue")
    assert r.status_code == 200, r.text
    return MessageQueue.model_validate(r.json())


def texts(q: MessageQueue) -> list[str]:
    return [i.message.text for i in q.items]


def user_texts(client, sid: str) -> list[str]:
    messages = [
        Message.model_validate(m) for m in client.get(f"/api/sessions/{sid}/messages").json()
    ]
    return [m.text for m in messages if m.role == "user"]


def running(client, sid: str) -> None:
    for _ in range(500):
        if client.get(f"/api/sessions/{sid}").json()["status"] == "running":
            return
        time.sleep(0.01)
    raise AssertionError("the run never started")


def wait_for(client, sid: str, check, seconds: float = 10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = events(client, sid)
        if check(found):
            return found
        time.sleep(0.02)
    raise AssertionError(f"never happened: {[(t, d) for _, t, d in events(client, sid)][-8:]}")


def wait_until(client, sid: str, status: str, after: int = 0) -> list[tuple[int, str, dict]]:
    """The events after `after` once the session reaches `status` and its run has stopped (a
    `queue` event may follow the question that paused it)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        session = client.get(f"/api/sessions/{sid}").json()
        new = events(client, sid, after)
        kinds = [t for _, t, _ in new if t != "queue"]
        if session["status"] == status and kinds and kinds[-1] in ("message_end", "question"):
            return new
        time.sleep(0.02)
    raise AssertionError(f"session {sid} never reached {status}: {session}")


def replies(found) -> str:
    return "".join(d["delta"] for _, t, d in found if t == "text")


def test_messages_sent_while_claude_works_are_queued_then_sent_in_order(client, claude) -> None:
    sid, found = start(client)
    send(client, sid, text="First")
    running(client, sid)
    # API clients keep the rule: no message while a run is going.
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "x"}).status_code == 409

    selection = {"faces": ["f1"], "edges": []}
    second = queue(client, sid, "Second", mode="manual", selection=selection)
    third = queue(client, sid, "Third")
    assert second.message.mode == "manual" and second.message.selection.faces == ["f1"]
    q = get_queue(client, sid)
    assert texts(q) == ["Second", "Third"] and not q.paused
    queued = [d for _, t, d in events(client, sid) if t == "queue"]
    assert [i["message"]["text"] for i in queued[-1]["queue"]["items"]] == ["Second", "Third"]

    claude.release(0)
    # "Second" starts once "First" ends, and only then: "Third" waits for it.
    wait_for(client, sid, lambda f: "Reply to: First" in replies(f) and len(claude.texts) == 2)
    assert claude.texts == ["First", "Second"]
    assert texts(get_queue(client, sid)) == ["Third"]
    assert "Mode: Manual" in claude.prompts[1] and "f1" in claude.prompts[1]
    claude.release(1)
    claude.release(2)
    final = wait_for(client, sid, lambda f: "Reply to: Third" in replies(f))
    wait_until(client, sid, "idle", found[-1][0])
    assert claude.texts == ["First", "Second", "Third"]
    assert user_texts(client, sid) == ["First", "Second", "Third"]
    assert get_queue(client, sid).items == []

    # Each send names the queued message and the user message it became, before its reply.
    order = [(t, d) for _, t, d in final if t in ("queue", "message_start")]
    sent = [d for t, d in order if t == "queue" and d.get("sent")]
    assert [s["sent"] for s in sent] == [second.id, third.id]
    stored = {m["id"]: m["text"] for m in client.get(f"/api/sessions/{sid}/messages").json()}
    assert [stored[s["message_id"]] for s in sent] == ["Second", "Third"]
    i = next(i for i, (t, d) in enumerate(order) if t == "queue" and d.get("sent") == second.id)
    assert order[i + 1][0] == "message_start"


def test_a_message_queued_on_an_idle_session_is_sent_at_once(client, claude) -> None:
    sid, found = start(client)
    queue(client, sid, "Now")
    claude.release(0)
    wait_until(client, sid, "idle", found[-1][0])
    assert claude.texts == ["Now"] and get_queue(client, sid).items == []


def test_queued_messages_can_be_edited_moved_and_removed(client, claude) -> None:
    sid, _ = start(client)
    send(client, sid, text="First")
    running(client, sid)
    a, b, c = (queue(client, sid, t) for t in ("A", "B", "C"))

    r = client.patch(f"/api/sessions/{sid}/queue/{b.id}", json={"text": "B edited"})
    assert r.status_code == 200 and r.json()["message"]["text"] == "B edited"
    r = client.patch(f"/api/sessions/{sid}/queue/{c.id}", json={"position": 0})
    assert r.status_code == 200
    assert texts(get_queue(client, sid)) == ["C", "A", "B edited"]
    r = client.patch(f"/api/sessions/{sid}/queue/{c.id}", json={"position": 99})
    assert texts(get_queue(client, sid)) == ["A", "B edited", "C"]
    assert client.patch(f"/api/sessions/{sid}/queue/{a.id}", json={"text": " "}).status_code == 409
    r = client.delete(f"/api/sessions/{sid}/queue/{a.id}")
    assert r.status_code == 200 and r.json()["id"] == a.id
    assert texts(get_queue(client, sid)) == ["B edited", "C"]
    assert client.delete(f"/api/sessions/{sid}/queue/{a.id}").status_code == 404
    assert client.patch(f"/api/sessions/{sid}/queue/nope", json={"text": "x"}).status_code == 404
    # Every change streamed, so other tabs follow.
    last = [d for _, t, d in events(client, sid) if t == "queue"][-1]
    assert [i["message"]["text"] for i in last["queue"]["items"]] == ["B edited", "C"]

    for n in range(3):
        claude.release(n)
    wait_for(client, sid, lambda f: "Reply to: C" in replies(f))
    assert claude.texts == ["First", "B edited", "C"]
    # Sent: too late to edit or remove it.
    assert client.delete(f"/api/sessions/{sid}/queue/{b.id}").status_code == 404


def test_a_queued_message_needs_text_and_is_never_an_answer(client, claude) -> None:
    sid, _ = start(client)
    r = client.post(f"/api/sessions/{sid}/queue", json={"text": "  "})
    assert r.status_code == 409
    answer = {"text": "", "answer": {"question_id": "q", "option_id": "a"}}
    assert client.post(f"/api/sessions/{sid}/queue", json=answer).status_code == 409


def test_a_question_pauses_the_queue_until_it_is_answered(client, claude) -> None:
    sid, found = start(client)
    send(client, sid, text="Ask me")
    running(client, sid)
    queue(client, sid, "After the answer")
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]
    time.sleep(0.2)  # the queue had its chance: it must not send
    assert claude.texts == ["Ask me"]
    q = get_queue(client, sid)
    assert q.paused and texts(q) == ["After the answer"]
    paused = [d for _, t, d in events(client, sid) if t == "queue"][-1]
    assert paused["queue"]["paused"] is True

    send(client, sid, answer={"question_id": question["question_id"], "option_id": "a"})
    assert [d for _, t, d in events(client, sid) if t == "queue"][-1]["queue"]["paused"] is False
    wait_for(client, sid, lambda f: len(claude.texts) == 2)
    assert claude.texts == ["Ask me", "After the answer"]
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: After the answer" in replies(f))
    # The answer went to the question, not the queued text.
    assert "Answered." in replies(events(client, sid))


def test_send_anyway_leaves_the_question_unanswered_and_sends_a_normal_message(
    client, claude
) -> None:
    sid, found = start(client)
    send(client, sid, text="Ask me")
    running(client, sid)
    item = queue(client, sid, "Something else")
    later = queue(client, sid, "Later")
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]

    r = client.post(f"/api/sessions/{sid}/queue/{item.id}/send")
    assert r.status_code == 202, r.text
    assert Message.model_validate(r.json()).text == "Something else"
    assert Message.model_validate(r.json()).answer is None
    after = events(client, sid, asked[-1][0])
    mid = question["message_id"]
    # The paused run ends with a note in its reply, then the message goes as a normal one.
    notes = [d["text"] for _, t, d in after if t == "progress" and d["message_id"] == mid]
    assert notes == [SENT_ANYWAY]
    assert ("message_end", mid) in [(t, d.get("message_id")) for _, t, d in after]
    assert "Answered." not in replies(events(client, sid))
    assert claude.texts == ["Ask me", "Something else"]
    assert texts(get_queue(client, sid)) == ["Later"]
    sent = [d for _, t, d in after if t == "queue" and d.get("sent")]
    assert [s["sent"] for s in sent] == [item.id]
    # The old question can't be answered any more.
    stale = {"answer": {"question_id": question["question_id"], "option_id": "a"}}
    assert client.post(f"/api/sessions/{sid}/messages", json=stale).status_code == 409

    # Not while a run is going that isn't waiting for an answer: it is sent when the run ends.
    assert client.post(f"/api/sessions/{sid}/queue/{later.id}/send").status_code == 409
    claude.release(1)
    claude.release(2)
    wait_for(client, sid, lambda f: "Reply to: Later" in replies(f))
    assert client.post(f"/api/sessions/{sid}/queue/{later.id}/send").status_code == 404


def test_stop_ends_the_run_and_the_queue_goes_on(client, claude) -> None:
    sid, found = start(client)
    # Nothing to stop: the session as it is (one Stop route with the limits' Stop).
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 202
    message = send(client, sid, text="Long job")
    running(client, sid)
    queue(client, sid, "Next")
    r = client.post(f"/api/sessions/{sid}/stop")
    assert r.status_code == 202, r.text
    after = wait_for(client, sid, lambda f: len(claude.texts) == 2)
    # It ends with the "stopped" error, with Retry, and the design is kept.
    stopped = [d for _, t, d in after if t == "error" and d["message"] == STOPPED]
    assert len(stopped) == 1
    assert stopped[0]["reason"] == "stopped" and stopped[0]["retryable"]
    mid = stopped[0]["message_id"]
    assert mid != message.id
    kinds = [t for _, t, d in after if d.get("message_id") == mid]
    assert kinds[-1] == "message_end"
    assert "Reply to: Long job" not in replies(after)
    assert claude.texts == ["Long job", "Next"]
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Next" in replies(f))
    wait_until(client, sid, "idle", found[-1][0])


def test_stop_while_a_question_waits(client, claude) -> None:
    sid, found = start(client)
    send(client, sid, text="Ask me")
    claude.release(0)
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 202
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "idle"
    after = events(client, sid, asked[-1][0])
    assert [t for _, t, _ in after][-2:] == ["error", "message_end"]
    assert after[-2][2]["reason"] == "stopped"


def test_the_queue_survives_a_restart(data, claude) -> None:
    with backend() as client:
        sid, _ = start(client)
        send(client, sid, text="Cut off")
        running(client, sid)
        item = queue(client, sid, "Queued before the restart", mode="manual")
        assert texts(get_queue(client, sid)) == ["Queued before the restart"]
        saved = SESSIONS.by_id[sid].saved()
        assert [q["id"] for q in saved["queue"]] == [item.id]

    # The restart cut the run off: it comes back interrupted, and the queue goes on from there.
    with backend() as client:
        assert client.get(f"/api/sessions/{sid}").status_code == 200  # loaded on demand
        claude.release(1)
        found = wait_for(client, sid, lambda f: "Reply to: Queued before the restart" in replies(f))
        assert claude.texts == ["Cut off", "Queued before the restart"]
        assert "Mode: Manual" in claude.prompts[1]
        assert get_queue(client, sid).items == []
        # The cut-off reply ends with the "interrupted" error, before the queued one starts.
        assert [d["reason"] for _, t, d in found if t == "error"][0] == "interrupted"
        assert user_texts(client, sid) == ["Cut off", "Queued before the restart"]


def test_a_paused_queue_is_kept_across_a_restart(data, claude) -> None:
    with backend() as client:
        sid, found = start(client)
        send(client, sid, text="Ask me")
        running(client, sid)
        queue(client, sid, "Waiting")
        claude.release(0)
        wait_until(client, sid, "waiting_for_answer", found[-1][0])
        assert get_queue(client, sid).paused

    # A restart keeps Claude's open question (nothing was running), so the queue stays paused
    # with its message, until the question is answered or the message is sent anyway.
    with backend() as client:
        r = client.get(f"/api/sessions/{sid}")  # loaded on demand
        assert r.status_code == 200 and r.json()["status"] == "waiting_for_answer"
        q = get_queue(client, sid)
        assert q.paused and texts(q) == ["Waiting"]
        r = client.post(f"/api/sessions/{sid}/queue/{q.items[0].id}/send")
        assert r.status_code == 202, r.text
        claude.release(1)
        wait_for(client, sid, lambda f: "Reply to: Waiting" in replies(f))
        assert claude.texts == ["Ask me", "Waiting"]
        # The question's reply ends with the note, as when the run that asked is still there.
        after = events(client, sid)
        (mid,) = [d["message_id"] for _, t, d in after if t == "question"]
        notes = [d["text"] for _, t, d in after if t == "progress" and d["message_id"] == mid]
        assert notes[-1] == SENT_ANYWAY
        assert ("message_end", mid) in [(t, d.get("message_id")) for _, t, d in after]


def test_another_browser_cant_see_or_change_the_queue(data, claude) -> None:
    with backend() as client:
        sid, _ = start(client)
        send(client, sid, text="Mine")
        running(client, sid)
        item = queue(client, sid, "Mine too")
        mine = dict(client.cookies)
        client.cookies.clear()
        client.cookies.set(OWNER_COOKIE, new_owner())
        base = f"/api/sessions/{sid}"
        assert client.get(f"{base}/queue").status_code == 404
        assert client.post(f"{base}/queue", json={"text": "theirs"}).status_code == 404
        assert client.patch(f"{base}/queue/{item.id}", json={"text": "x"}).status_code == 404
        assert client.delete(f"{base}/queue/{item.id}").status_code == 404
        assert client.post(f"{base}/queue/{item.id}/send").status_code == 404
        assert client.post(f"{base}/stop").status_code == 404
        client.cookies.clear()
        for name, value in mine.items():
            client.cookies.set(name, value)
        assert texts(get_queue(client, sid)) == ["Mine too"]
        assert client.get(f"{base}").json()["status"] == "running"
        claude.release(0)
        claude.release(1)
        wait_for(client, sid, lambda f: "Reply to: Mine too" in replies(f))


def test_hand_edits_and_restores_keep_their_rule_while_claude_works(client, claude) -> None:
    sid, _ = start(client)
    build = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    send(client, sid, text="Working")
    running(client, sid)
    queue(client, sid, "Queued")
    # Queueing changes nothing for the actions that change the design: still 409 mid-run.
    assert client.post(f"/api/sessions/{sid}/builds/{build}/restore").status_code == 409
    edit = {"build_id": build, "code": "def build(params):\n    pass\n"}
    assert client.put(f"/api/sessions/{sid}/source", json=edit).status_code == 409
    claude.release(0)
    claude.release(1)
    wait_for(client, sid, lambda f: "Reply to: Queued" in replies(f))


# --- The mock API -----------------------------------------------------------------------------


@pytest.fixture
def mock(monkeypatch):
    from app import mock_api

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    mock_api.SESSIONS.clear()
    with TestClient(mock_api.app) as test_client:
        yield test_client


def test_the_mock_pauses_on_its_question_and_sends_anyway(mock) -> None:
    from app.examples import examples

    sid = mock.post("/api/sessions", json={"example": examples()[0].id}).json()["id"]
    assert mock.post(f"/api/sessions/{sid}/messages", json={"text": "go"}).status_code == 202
    found = wait_for(mock, sid, lambda f: any(t == "message_end" for _, t, _ in f))
    item = queue(mock, sid, "Queued in the mock")
    q = get_queue(mock, sid)
    assert q.paused and texts(q) == ["Queued in the mock"]  # the recorded run ends on a question
    other = queue(mock, sid, "Removed")
    r = mock.patch(f"/api/sessions/{sid}/queue/{other.id}", json={"position": 0, "text": "Moved"})
    assert r.status_code == 200 and texts(get_queue(mock, sid)) == ["Moved", "Queued in the mock"]
    assert mock.delete(f"/api/sessions/{sid}/queue/{other.id}").status_code == 200

    r = mock.post(f"/api/sessions/{sid}/queue/{item.id}/send")
    assert r.status_code == 202, r.text
    after = wait_for(mock, sid, lambda f: "this message was received" in replies(f))
    new = after[len(found) :]
    assert SENT_ANYWAY in [d["text"] for _, t, d in new if t == "progress"]
    assert [d["sent"] for _, t, d in new if t == "queue" and d.get("sent")] == [item.id]
    assert get_queue(mock, sid).items == [] and not get_queue(mock, sid).paused


def test_the_mock_queues_while_a_reply_streams_and_stops(mock, monkeypatch) -> None:
    from app.examples import examples

    monkeypatch.setenv("MOCK_API_SPEED", "0.05")  # the recorded run takes a while
    sid = mock.post("/api/sessions", json={"example": examples()[0].id}).json()["id"]
    mock.post(f"/api/sessions/{sid}/messages", json={"text": "go"})
    queue(mock, sid, "After the stop")
    assert texts(get_queue(mock, sid)) == ["After the stop"]
    assert mock.post(f"/api/sessions/{sid}/stop").status_code == 202
    found = wait_for(mock, sid, lambda f: "this message was received" in replies(f))
    assert STOPPED in [d["message"] for _, t, d in found if t == "error"]
    assert not any(t == "question" for _, t, _ in found)
    assert get_queue(mock, sid).items == []
    assert mock.post(f"/api/sessions/{sid}/stop").status_code == 202  # nothing to stop
