"""The message queue of the real session API: `/sessions/{id}/queue`.

The user can keep sending while Claude works, as in Claude Code. `POST /messages` still answers
409 while a run is going (API clients keep that rule); `POST .../queue` is the way to send then.

- A queued message waits in the session (`SessionState.queue`, saved with the session, so it
  survives a reload or a restart) and is sent as a normal message when the run before it has
  ended: one at a time, in order. Until then it can be edited, moved or removed
  (`PATCH`/`DELETE .../queue/{item}`).
- A run that stops on Claude's question pauses the queue: the next message is never taken as
  the answer. Answering resumes the run, and the queue goes on when it ends. "Send anyway"
  (`POST .../queue/{item}/send`) leaves the question unanswered instead
  (`app.session_api.leave_question_open`): the paused run ends with a note saying so, and the
  message is sent as a normal one, with a note to Claude about the question it left open. A
  message posted directly (`POST .../messages`) while the question is open does the same.
- `POST .../stop` (`app.session_api.stop_run`) stops the run (going, or waiting for an
  answer): it ends with the "stopped" error and Retry, the session is idle again, and the
  queue goes on.
- Every change streams as a `queue` event with the whole queue, so every tab shows the same.
  The event that sends a message names it (`sent`, with the stored user message's ID).

Hand edits, reviews and the other actions that change the design keep their own rules while
Claude works (409). The routes are the ones the mock declares (`app.mock_api`).
"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException

from app import conversations as convs
from app.contracts.session import (
    ErrorEvent,
    Message,
    MessageCreate,
    MessageQueue,
    QueuedMessage,
    QueuedMessageUpdate,
    QueueEvent,
)
from app.session_api import (
    CONFLICT,
    NOT_FOUND,
    QUESTION_LEFT_OPEN,
    RUN_LISTENERS,
    SESSION,
    SESSIONS,
    SessionState,
    conversations_changed,
    leave_question_open,
    now,
    park_question,
    send_message,
)

router = APIRouter()

SENT_ANYWAY = QUESTION_LEFT_OPEN
# How soon the queue tries again when a review or an import holds the session.
RETRY_SECONDS = 0.5


def _paused(state: SessionState) -> bool:
    """A queued message waits on Claude's question in its conversation."""
    return any(not convs.can_send(state, q.message) for q in state.queue)


def queue_of(state: SessionState) -> MessageQueue:
    return MessageQueue(items=list(state.queue), paused=_paused(state))


def _notify(
    state: SessionState, sent: QueuedMessage | None = None, message: str | None = None
) -> None:
    state.queue_paused = _paused(state)
    conversations_changed(state)
    state.emit(
        0,
        QueueEvent(
            type="queue",
            queue=queue_of(state),
            sent=sent.id if sent is not None else None,
            message_id=message,
        ),
    )


def _new_id(state: SessionState) -> str:
    """A random ID (`docs/security.md`): queued messages can't be found by counting."""
    taken = {q.id for q in state.queue}
    while (qid := f"queued-{secrets.token_hex(8)}") in taken:
        pass
    return qid


def _item(state: SessionState, item_id: str) -> QueuedMessage:
    found = next((q for q in state.queue if q.id == item_id), None)
    if found is None:
        raise HTTPException(404, f"no queued message {item_id}")
    return found


def _check(body: MessageCreate, state: SessionState | None = None) -> None:
    # Only the answer to a question parked in its conversation waits its turn like a message.
    if body.answer is not None and (state is None or not convs.can_send(state, body)):
        raise HTTPException(409, "a queued message can't answer a question: answer it directly")
    if not body.text.strip() and not body.comments:
        raise HTTPException(409, "a message needs text or comments")


def _blocked(state: SessionState) -> bool:
    """Something outside a run holds the session for a moment (a review, an import)."""
    initial = state.initial is not None and not state.initial.done()
    return state.reviewing.locked() or state.importing.locked() or initial


def pump(state: SessionState) -> None:
    """Send the next queued message that can go, once nothing is running: runs take turns on
    the one design. A message waits while Claude's question in its own conversation is open
    (it is never taken as the answer); a question in another conversation is parked
    (`park_question`) so the message goes. The change streams so every tab shows it."""
    # No store: the server is shutting down (`Sessions.close`), so nothing more starts.
    if state.queue_hold or not state.queue or state.store is None:
        return
    status = state.session.status
    item = next((q for q in state.queue if convs.can_send(state, q.message)), None)
    if status == "running" or item is None:
        # A question paused a message, or an answer resumed the run: say so once.
        if state.queue_paused != _paused(state):
            _notify(state)
        return
    if status == "waiting_for_answer":  # another conversation's question: park it, then go
        state.queue_hold = True
        SESSIONS.spawn(_park_then_pump(state))
        return
    if _blocked(state):
        asyncio.get_running_loop().call_later(RETRY_SECONDS, pump, state)
        return
    state.queue.remove(item)
    try:
        message = send_message(state, item.message)
    except HTTPException as error:  # it can't be sent any more (a comment resolved since)
        state.emit(
            0,
            ErrorEvent(type="error", message=f"A queued message couldn't be sent: {error.detail}"),
        )
        _notify(state)
        state.save()
        asyncio.get_running_loop().call_soon(pump, state)
        return
    _notify(state, item, message.id)
    state.save()


RUN_LISTENERS.append(pump)


async def _park_then_pump(state: SessionState) -> None:
    try:
        await park_question(state)
    finally:
        state.queue_hold = False
    pump(state)


# --- Routes -----------------------------------------------------------------------------------


@router.get(SESSION + "/queue", response_model=MessageQueue, responses=NOT_FOUND, tags=["queue"])
async def get_queue(session_id: str) -> MessageQueue:
    """The messages waiting for Claude's current run to end, in the order they will be sent."""
    return queue_of(SESSIONS.get(session_id))


@router.post(
    SESSION + "/queue",
    response_model=QueuedMessage,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def queue_message(session_id: str, body: MessageCreate) -> QueuedMessage:
    """Send a message while Claude is working: it waits in the queue and is sent as a normal
    message once the runs before it have ended. On an idle session it is sent at once."""
    state = SESSIONS.get(session_id)
    _check(body, state)
    item = QueuedMessage(id=_new_id(state), created_at=now(), message=body)
    state.queue.append(item)
    state.save()
    _notify(state)
    pump(state)
    return item


@router.patch(
    SESSION + "/queue/{item_id}",
    response_model=QueuedMessage,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def update_queued(session_id: str, item_id: str, body: QueuedMessageUpdate) -> QueuedMessage:
    """Edit a queued message's text, or move it (`position`, 0: next). 404 once it was sent."""
    state = SESSIONS.get(session_id)
    item = _item(state, item_id)
    at = state.queue.index(item) if body.position is None else body.position
    if body.text is not None:
        edited = item.message.model_copy(update={"text": body.text})
        _check(edited)
        item = item.model_copy(update={"message": edited})
    rest = [q for q in state.queue if q.id != item_id]
    rest.insert(min(at, len(rest)), item)
    state.queue = rest
    state.save()
    _notify(state)
    return item


@router.delete(
    SESSION + "/queue/{item_id}",
    response_model=QueuedMessage,
    responses=NOT_FOUND,
    tags=["queue"],
)
async def remove_queued(session_id: str, item_id: str) -> QueuedMessage:
    """Take a message out of the queue before it is sent. 404 once it was sent."""
    state = SESSIONS.get(session_id)
    item = _item(state, item_id)
    state.queue.remove(item)
    state.save()
    _notify(state)
    return item


@router.post(
    SESSION + "/queue/{item_id}/send",
    response_model=Message,
    status_code=202,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["queue"],
)
async def send_queued(session_id: str, item_id: str) -> Message:
    """Send a queued message now, as a normal message ("Send anyway"). While Claude's question
    is open, the question is left unanswered: the paused run ends with a note saying so, and
    the message starts a new run; it is never taken as the answer. 409 while Claude is working
    on a run that isn't waiting for an answer: it is sent when the run ends."""
    state = SESSIONS.get(session_id)
    item = _item(state, item_id)
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: it is sent when the run ends")
    state.queue_hold = True
    try:
        note = None
        if convs.live_conversation(state) == convs.conversation_of(item.message):
            note = await leave_question_open(state, SENT_ANYWAY)
        else:  # the question open is another conversation's: it waits there
            await park_question(state)
        item = _item(state, item_id)  # still queued: nobody removed it meanwhile
        state.queue.remove(item)
        try:
            message = send_message(state, item.message, note)
        except HTTPException:
            state.queue.insert(0, item)
            raise
    finally:
        state.queue_hold = False
    _notify(state, item, message.id)
    state.save()
    return message


__all__ = ["SENT_ANYWAY", "pump", "queue_of", "router"]
