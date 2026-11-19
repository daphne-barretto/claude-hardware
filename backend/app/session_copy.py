"""Copies of a session's saved state (`SessionState.saved()`): the snapshot a share link keeps
(`app.shares`), and a new session started from one ("Open a copy").

A copy is made from the saved state, the document the session store keeps, so it holds
exactly what a session needs after a restart, and no more:

- `snapshot()` keeps an allowlist of that document: the session, its messages and events,
  builds and their designs, comments, change set, parameters, modeling edits and the design.
  Never the owner (`app.owners`), the telemetry log, exports, or anything a later
  version adds to the saved state (a message queue, say) until it is added here on purpose.
  Attached files keep their names only unless the sharer opted in.
- `design_only()` narrows a snapshot to the artifact: the latest design (its code and
  parameters, locks, hand edits, sketches and modeling edits, board and revision, standards
  set) and the latest build, with no chat, comments, change set, question or history.
- `copy_into_new_session()` saves a snapshot as a new session of the browser making the request
  and loads it like any saved session. The source is never changed.

Builds are shared, not copied: a build in the build store (`app.builds`) is immutable and named
by a random ID, so the copy serves, diffs and exports the same build, and its own new builds are
its own. A browser's copy of a demo (`app.sample_copies`, `app.demos`) starts from files
rather than a saved state; turning the demos into saved-state documents would let them use
`copy_into_new_session` too.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

# The parts of the saved state a snapshot keeps; everything else is left out.
KEPT = (
    "session",
    "messages",
    "builds",
    "designs",
    "comments",
    "question",
    "next_message",
    "parameters",
    "edits",
    "proposal",
    "design",
    "next_build",
    "latest",
    "report",
)
# What `SessionState.load` needs for the parts left out: nothing of the source's.
EMPTY = {"owner": None, "exports": [], "export_dirs": {}, "attachments": {}}
COPY_NOTE = "Started from a shared design."


def session_base(session_id: str) -> str:
    """The prefix of a session's URLs (builds, attachments, exports)."""
    return f"/api/sessions/{session_id}/"


def _rebase(doc: Any, old: str, new: str) -> Any:
    """`doc` with every URL under session `old` moved to session `new`."""
    return json.loads(json.dumps(doc).replace(session_base(old), session_base(new)))


def _no_step(build: dict[str, Any]) -> None:
    build["step_url"] = None  # the source's exports stay the source's


def snapshot(
    saved: dict[str, Any],
    events: list[tuple[int, dict[str, Any]]],
    snapshot_id: str,
    *,
    attachments: bool = False,
) -> dict[str, Any]:
    """A snapshot of a session (its `saved()` state and events, as JSON) under the ID
    `snapshot_id`: the allowlisted parts, with the session's URLs moved to that ID."""
    source_id = saved["session"]["id"]
    state = {k: copy.deepcopy(saved[k]) for k in KEPT if k in saved} | copy.deepcopy(EMPTY)
    state["events"] = len(events)
    if attachments:
        state["attachments"] = copy.deepcopy(saved.get("attachments", {}))
    else:
        for message in state["messages"]:
            for attachment in message.get("attachments", []):
                attachment["url"] = ""  # the name only
    doc = _rebase({"state": state, "events": [list(e) for e in events]}, source_id, snapshot_id)
    doc["state"]["session"]["id"] = snapshot_id
    for build in doc["state"]["builds"]:
        _no_step(build)
    for _, event in doc["events"]:
        if event.get("type") == "build_ready":
            _no_step(event["build"])
    return doc


def design_only(doc: dict[str, Any]) -> dict[str, Any]:
    """A snapshot narrowed to its latest design and build: no chat, comments, change set,
    question or history."""
    state = copy.deepcopy(doc["state"])
    session = state["session"]
    latest = session.get("latest_build_id")
    builds = [b for b in state["builds"] if b["id"] == latest]
    state.update(
        messages=[],
        comments=[],
        question=None,
        proposal=None,
        next_message=1,
        builds=builds,
        designs={latest: state["designs"][latest]} if latest in state["designs"] else {},
        attachments={},
    )
    session["status"] = "idle"
    events = [[1, {"type": "build_ready", "message_id": None, "build": b}] for b in builds]
    state["events"] = len(events)
    return {"state": state, "events": events}


def copied(doc: dict[str, Any], session_id: str, *, title: str) -> dict[str, Any]:
    """A snapshot as a new session `session_id`'s saved state and events."""
    out = _rebase(doc, doc["state"]["session"]["id"], session_id)
    state = out["state"]
    session = state["session"]
    session.update(id=session_id, title=title, created_at=datetime.now(UTC).isoformat())
    session["shared"] = None
    # A question the source was waiting on belongs to its run, which the copy doesn't have.
    session["status"] = "idle"
    state["question"] = None
    return out


async def copy_into_new_session(
    doc: dict[str, Any],
    *,
    title: str,
    attachments: dict[str, bytes] | None = None,
    note: str | None = None,
) -> Any:
    """Save the snapshot `doc` as a new session of the browser making the request
    (`app.owners`) and return it (`Session`). `note` is a short line in the chat, after the
    copied conversation."""
    import asyncio

    from app.agent.events import EventSink
    from app.owners import current_owner
    from app.session_api import SESSIONS
    from app.session_store import session_store

    store = session_store()
    sid = SESSIONS.next_id()
    out = copied(doc, sid, title=title)
    out["state"]["owner"] = current_owner.get()

    def save() -> None:
        for aid, data in (attachments or {}).items():
            store.write_attachment(sid, aid, data)
        for event_id, event in out["events"]:
            store.append_event(sid, event_id, event)
        store.save(sid, out["state"])

    await asyncio.to_thread(save)
    state = SESSIONS.get(sid)
    if note:
        sink = EventSink(state.message_id(), [state.emit])
        sink.text(note)
        sink.end()
    state.save()
    return state.session


__all__ = ["COPY_NOTE", "copied", "copy_into_new_session", "design_only", "snapshot"]
