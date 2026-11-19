"""Share links in the mock session API (`app.mock_api`): the same routes as the real API
(`app.share_routes`), on the mock's in-memory sessions.

A link's snapshot is a copy of the mock session as it was when shared (for a `copy` link, its
latest build and parameters alone), served through the mock's own read handlers. The mock has
no owners, so any browser may manage a session's links. `POST /api/sessions` with
`from_share` copies a snapshot into a new session (`continue_from`), as the real API does.
"""

from __future__ import annotations

import copy
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from app.contracts.session import (
    BuildReadyEvent,
    MessageEndEvent,
    MessageStartEvent,
    Session,
    SessionCreate,
    ShareCreate,
    ShareLink,
    ShareRevoke,
    TextEvent,
)
from app.session_copy import COPY_NOTE
from app.share_routes import TURNED_OFF, add_share_routes, share_id, share_url

COPY_SUFFIX = " (copy)"


def _mock() -> Any:
    from app import mock_api

    return mock_api


class _Api:
    def __getattr__(self, name: str) -> Any:
        return getattr(_mock(), name)


def _moved(model: Any, old: str, new: str) -> Any:
    """A copy of a contract model with session `old`'s URLs moved to session `new`."""
    text = model.model_dump_json().replace(f"/api/sessions/{old}/", f"/api/sessions/{new}/")
    return type(model).model_validate_json(text)


def copy_state(
    source: Any, session_id: str, *, design_only: bool = False, attachments: bool = True
) -> Any:
    """A mock session state copied under `session_id`: all of it, or its latest design."""
    m = _mock()
    old = source.session.id

    def mv(model: Any) -> Any:
        return _moved(model, old, session_id)

    out = m.SessionState(mv(source.session).model_copy(update={"id": session_id}))
    out.builds = [mv(b).model_copy(update={"step_url": None}) for b in source.builds]
    out.parameters = [p.model_copy(deep=True) for p in source.parameters]
    out.designs = copy.deepcopy(source.designs)
    out.sources = dict(source.sources)
    out.spec = copy.deepcopy(source.spec)
    out.sketches = list(source.sketches)
    out.build_sketches = copy.deepcopy(source.build_sketches)
    out.sketches_made = source.sketches_made
    out.edits = list(source.edits)
    out.build_edits = copy.deepcopy(source.build_edits)
    out.edits_made = set(source.edits_made)
    out.next_edit = source.next_edit
    out.played = True  # the copy's first message isn't the scripted run again
    if design_only:
        latest = out.session.latest_build_id
        out.builds = [b for b in out.builds if b.id == latest]
        out.events = [
            (1, BuildReadyEvent(type="build_ready", message_id=None, build=b)) for b in out.builds
        ]
        out.session.status = "idle"
        return out
    out.messages = [mv(msg) for msg in source.messages]
    if not attachments:
        for msg in out.messages:
            for a in msg.attachments:
                a.url = ""
    out.events = [(i, mv(e)) for i, e in source.events]
    for i, (_, e) in enumerate(out.events):
        if e.type == "build_ready":
            out.events[i] = (
                out.events[i][0],
                e.model_copy(update={"build": e.build.model_copy(update={"step_url": None})}),
            )
    out.comments = [mv(c) for c in source.comments]
    out.change_set = source.change_set.model_copy(deep=True) if source.change_set else None
    out.risk_flags = source.risk_flags.model_copy(deep=True) if source.risk_flags else None
    out.review_builds = {k: (mv(b), copy.deepcopy(r)) for k, (b, r) in source.review_builds.items()}
    out.question = source.question
    out.next_message = source.next_message
    if attachments:
        out.attachments = source.attachments
    return out


class MockShares:
    api = _Api()

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def _source(self, session_id: str) -> Any:
        return _mock()._state(session_id)

    def _live(self, session_id: str, kind: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in self.records.values()
                if r["session_id"] == session_id and r["kind"] == kind and not r["revoked"]
            ),
            None,
        )

    def _link(self, record: dict[str, Any]) -> ShareLink:
        state = self._source(record["session_id"])
        if record["kind"] == "copy":
            up_to_date = record["latest"] == state.session.latest_build_id
        else:
            up_to_date = record["events"] == len(state.events)
        return ShareLink(
            token=record["token"],
            kind=record["kind"],
            url=share_url(record["token"]),
            active=not record["revoked"],
            attachments=record["attachments"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            up_to_date=up_to_date,
        )

    def _snapshot(self, record: dict[str, Any]) -> None:
        state = self._source(record["session_id"])
        if state.session.status == "running":
            raise HTTPException(409, "Claude is still replying. Share once the reply has finished.")
        if record["kind"] == "copy" and state.session.latest_build_id is None:
            raise HTTPException(409, "There is no design to share yet.")
        record["view"] = copy_state(
            state,
            share_id(record["token"]),
            design_only=record["kind"] == "copy",
            attachments=record["attachments"],
        )
        record.update(
            events=len(state.events),
            latest=state.session.latest_build_id,
            updated_at=datetime.now(UTC),
        )

    def links(self, session_id: str) -> list[ShareLink]:
        self._source(session_id)
        return [
            self._link(r)
            for r in self.records.values()
            if r["session_id"] == session_id and not r["revoked"]
        ]

    async def create(self, session_id: str, body: ShareCreate) -> ShareLink:
        self._source(session_id)
        if self._live(session_id, body.kind) is not None:
            raise HTTPException(409, f"This session already has a {body.kind} link: update it")
        token = secrets.token_hex(24)
        record = {
            "token": token,
            "kind": body.kind,
            "session_id": session_id,
            "attachments": body.attachments and body.kind == "view",
            "created_at": datetime.now(UTC),
            "revoked": False,
        }
        self._snapshot(record)
        self.records[token] = record
        return self._link(record)

    async def update(self, session_id: str, body: ShareCreate) -> ShareLink:
        record = self._live(session_id, body.kind)
        if record is None:
            raise HTTPException(404, f"This session has no {body.kind} link")
        record["attachments"] = body.attachments and body.kind == "view"
        self._snapshot(record)
        return self._link(record)

    async def revoke(self, session_id: str, body: ShareRevoke) -> ShareLink:
        record = self._live(session_id, body.kind)
        if record is None:
            raise HTTPException(404, f"This session has no {body.kind} link")
        link = self._link(record)
        record.update(revoked=True, view=None)
        return link.model_copy(update={"active": False})

    def record(self, token: str) -> dict[str, Any]:
        record = self.records.get(token)
        if record is None:
            raise HTTPException(404, "No such link")
        if record["revoked"]:
            raise HTTPException(410, TURNED_OFF)
        return record

    def view(self, token: str) -> Any:
        return self.record(token)["view"]

    def kind(self, token: str) -> str:
        return self.record(token)["kind"]

    async def continue_from(self, body: SessionCreate) -> Session:
        assert body.from_share is not None
        if body.example is not None or body.board is not None:
            raise HTTPException(422, "from_share can't be combined with example or board")
        record = self.record(body.from_share)
        m = _mock()
        sid = f"session-{len(m.SESSIONS) + 1}"
        state = copy_state(record["view"], sid)
        state.session = state.session.model_copy(
            update={
                "title": body.title or state.session.title + COPY_SUFFIX,
                "created_at": datetime.now(UTC),
                "status": "idle",
            }
        )
        state.question = None
        m.SESSIONS[sid] = state
        if record["kind"] == "copy":
            mid = state.message_id()
            await state.emit(
                MessageStartEvent(type="message_start", message_id=mid, role="assistant")
            )
            await state.emit(TextEvent(type="text", message_id=mid, delta=COPY_NOTE))
            await state.emit(MessageEndEvent(type="message_end", message_id=mid))
        return state.session


SHARES = MockShares()
router = APIRouter()
add_share_routes(router, SHARES)

__all__ = ["SHARES", "copy_state", "router"]
