"""Share links in the real API: a session shared like a chat on claude.ai.

The routes are `app.share_routes` (the mock serves the same ones). Two kinds of link:

- `view`: a read-only snapshot of the session as it was when shared: the chat, the builds with
  their mesh, checks, maps, source and board, comments, the change set and the parameters.
  Anyone with the link sees it, no cookie needed, and can "Continue from this": a new session
  of their own from the snapshot's latest state, chat included.
- `copy`: the latest design alone (`app.session_copy.design_only`), for anyone with the link to
  "Open a copy": a new, editable session of their own that starts from it, with its build on
  the canvas. The chat isn't included.

A snapshot is taken from the session's saved state (`app.session_copy.snapshot`), so it never
holds the owner cookie, telemetry or exports, and attached files keep their names only unless
the sharer included them. New messages aren't shared until the owner updates the link (same
URL); stopping sharing turns the link off for good and deletes the snapshot. Snapshots are kept
in the session store (`shares` table, files under `sessions/share-<token>/`), so links survive
restarts. A shared view is not a session: it never shows up in anyone's session list or counts
towards `MAX_SESSIONS`. The token is 192 random bits; it is the link's only key.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from app import modeling_api, session_api, session_copy
from app.contracts.session import (
    Session,
    SessionCreate,
    ShareCreate,
    ShareLink,
    ShareRevoke,
)
from app.session_store import session_store
from app.share_routes import TURNED_OFF, add_share_routes, share_id, share_url

_TOKEN = re.compile(r"^[0-9a-f]{48}$")
COPY_SUFFIX = " (copy)"
RUNNING = "Claude is still replying. Share once the reply has finished."
NOTHING_BUILT = "There is no design to share yet: share a copy once something is built."


def new_token() -> str:
    return secrets.token_hex(24)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Api:
    """The session API's read handlers, by name, for `add_share_routes`."""

    def __getattr__(self, name: str) -> Any:
        if name == "list_edits":
            return modeling_api.list_edits
        return getattr(session_api, name)


class _SnapshotStore:
    """A snapshot as the session store `SessionState.load` reads a saved session from."""

    def __init__(self, record: dict[str, Any]):
        self.record = record

    def load(self, _: str) -> dict[str, Any]:
        return self.record["snapshot"]["state"]

    def events(self, _: str) -> list[tuple[int, dict[str, Any]]]:
        return [(i, e) for i, e in self.record["snapshot"]["events"]]

    def read_attachment(self, _: str, attachment_id: str) -> bytes | None:
        if not self.record["attachments"]:
            return None
        return session_store().read_attachment(share_id(self.record["token"]), attachment_id)


class Shares:
    api = _Api()

    def __init__(self) -> None:
        self._views: dict[str, tuple[str, Any]] = {}  # token -> (snapshot time, state)
        self._lock = threading.Lock()

    # --- Records ---------------------------------------------------------------------------

    def record(self, token: str) -> dict[str, Any]:
        """A live link's record: 404 for no such link, 410 for one turned off."""
        found = session_store().load_share(token) if _TOKEN.match(token) else None
        if found is None:
            raise HTTPException(404, "No such link")
        if found["revoked_at"] is not None:
            raise HTTPException(410, TURNED_OFF)
        return found

    def _live(self, session_id: str, kind: str) -> dict[str, Any] | None:
        records = session_store().shares_of(session_id)
        return next((r for r in records if r["kind"] == kind and r["revoked_at"] is None), None)

    def _link(self, record: dict[str, Any], state: Any) -> ShareLink:
        snap = record.get("snapshot")
        if record["kind"] == "copy":
            shared = snap["state"]["session"].get("latest_build_id") if snap else None
            current = state.session.latest_build_id
            up_to_date = shared == current and record["design"] == _design_key(state)
        else:
            up_to_date = record["events"] == len(state.events)
        return ShareLink(
            token=record["token"],
            kind=record["kind"],
            url=share_url(record["token"]),
            active=record["revoked_at"] is None,
            attachments=record["attachments"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            up_to_date=up_to_date,
        )

    def _snapshot(self, state: Any, record: dict[str, Any]) -> None:
        """Take the session's snapshot into `record` (and its attached files, if included)."""
        token = record["token"]
        events = [(i, e.model_dump(mode="json")) for i, e in state.events]
        doc = session_copy.snapshot(
            state.saved(), events, share_id(token), attachments=record["attachments"]
        )
        if record["kind"] == "copy":
            doc = session_copy.design_only(doc)
        store = session_store()
        store.delete_attachments(share_id(token))
        if record["attachments"]:
            for aid, (_, data) in state.attachments.files.items():
                store.write_attachment(share_id(token), aid, data)
        record.update(
            snapshot=doc, events=len(state.events), design=_design_key(state), updated_at=_now()
        )
        store.save_share(token, record["session_id"], record)
        with self._lock:
            self._views.pop(token, None)

    @staticmethod
    def _shareable(state: Any, kind: str) -> None:
        if state.session.status == "running":
            raise HTTPException(409, RUNNING)
        if kind == "copy" and state.session.latest_build_id is None:
            raise HTTPException(409, NOTHING_BUILT)

    # --- Owner routes ------------------------------------------------------------------------

    def links(self, session_id: str) -> list[ShareLink]:
        state = session_api.SESSIONS.get(session_id)  # the owner's, else 404
        records = session_store().shares_of(session_id)
        return [self._link(r, state) for r in records if r["revoked_at"] is None]

    async def create(self, session_id: str, body: ShareCreate) -> ShareLink:
        state = session_api.SESSIONS.get(session_id)
        if self._live(session_id, body.kind) is not None:
            raise HTTPException(409, f"This session already has a {body.kind} link: update it")
        self._shareable(state, body.kind)
        now = _now()
        record = {
            "token": new_token(),
            "kind": body.kind,
            "session_id": session_id,
            "owner": state.owner,
            "attachments": body.attachments and body.kind == "view",
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        await asyncio.to_thread(self._snapshot, state, record)
        return self._link(record, state)

    async def update(self, session_id: str, body: ShareCreate) -> ShareLink:
        state = session_api.SESSIONS.get(session_id)
        record = self._live(session_id, body.kind)
        if record is None:
            raise HTTPException(404, f"This session has no {body.kind} link")
        self._shareable(state, body.kind)
        record["attachments"] = body.attachments and body.kind == "view"
        await asyncio.to_thread(self._snapshot, state, record)
        return self._link(record, state)

    async def revoke(self, session_id: str, body: ShareRevoke) -> ShareLink:
        state = session_api.SESSIONS.get(session_id)
        record = self._live(session_id, body.kind)
        if record is None:
            raise HTTPException(404, f"This session has no {body.kind} link")
        link = self._link(record, state)
        record.update(revoked_at=_now(), snapshot=None)  # nothing of it is kept
        store = session_store()
        store.save_share(record["token"], session_id, record)
        store.delete_attachments(share_id(record["token"]))
        with self._lock:
            self._views.pop(record["token"], None)
        return link.model_copy(update={"active": False})

    # --- Viewer routes -----------------------------------------------------------------------

    def kind(self, token: str) -> str:
        return self.record(token)["kind"]

    def view(self, token: str) -> Any:
        """The snapshot as a session state, loaded once per snapshot and never saved."""
        record = self.record(token)
        with self._lock:
            cached = self._views.get(token)
        if cached is not None and cached[0] == record["updated_at"]:
            return cached[1]
        sid = share_id(token)
        try:
            state = session_api.SessionState.load(_SnapshotStore(record), sid)  # type: ignore[arg-type]
        except Exception as error:
            session_api.log.exception("share %s: can't load its snapshot", token[:6])
            raise HTTPException(404, "No such link") from error
        assert state is not None
        state.store = None  # a view changes nothing, and saves nothing
        state.owner = None
        with self._lock:
            self._views[token] = (record["updated_at"], state)
        return state

    async def continue_from(self, body: SessionCreate) -> Session:
        """`POST /api/sessions` with `from_share`: a new session of the browser's own from the
        link's snapshot (the whole of it for a `view` link, the design for a `copy` link)."""
        assert body.from_share is not None
        if body.example is not None or body.board is not None:
            raise HTTPException(422, "from_share can't be combined with example or board")
        record = self.record(body.from_share)
        doc = record["snapshot"]
        title = body.title or doc["state"]["session"]["title"] + COPY_SUFFIX
        attachments = {}
        if record["attachments"]:
            store = session_store()
            for aid in doc["state"]["attachments"]:
                data = store.read_attachment(share_id(record["token"]), aid)
                if data is not None:
                    attachments[aid] = data
        note = session_copy.COPY_NOTE if record["kind"] == "copy" else None
        return await session_copy.copy_into_new_session(
            doc, title=title, attachments=attachments, note=note
        )


def _design_key(state: Any) -> str:
    """What a `copy` link shares, to tell whether it is out of date: the design itself."""
    import json

    design = state.ws.design
    return json.dumps([design.code, design.params], sort_keys=True, default=str)


SHARES = Shares()
router = APIRouter()
add_share_routes(router, SHARES)

__all__ = ["SHARES", "router"]
