"""Demos: hardcoded saved sessions, one per sample in `demos/sessions/` (`app.samples`), that
every browser sees in its session list, tagged Demo, and works on like any session of its own.

Each browser (`app.owners`) gets its own copy of a demo the first time it opens it:
`POST /api/demos/{demo}/session` returns the browser's copy, making it from the demo's
hardcoded original if it has none yet (`app.sample_copies`). The copy is an ordinary saved
session of that browser, marked with `Session.demo`; nothing else about it differs. Making it is
idempotent and race-safe (two tabs opening the demo at once get the same copy), counts against
the session-creation limits (`app.security.limits`), and is only possible for a known demo.
The original never changes, and no browser sees another's copy.

`GET /api/sessions` lists each demo in the order `demos/sessions/index.json` gives, after the
browser's other sessions: the browser's copy where it has one, else the original
(`"sample": true`, which a page opens with the endpoint above). `DELETE
/api/demos/{demo}/session` resets the demo: the browser's copy is deleted, and opening the demo
again makes a fresh one.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Response

from app import sample_copies, samples
from app.contracts.session import Session

API = "/api"
DEMO = API + "/demos/{demo_id}/session"

router = APIRouter(tags=["sessions"])

# One lock per browser and demo, so concurrent first opens make one copy. The server runs in one
# process (`docs/architecture.md`), so an asyncio lock is enough.
_locks: dict[tuple[str | None, str], asyncio.Lock] = {}


def _lock(owner: str | None, demo_id: str) -> asyncio.Lock:
    key = (owner, demo_id)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def original(demo_id: str) -> Session:
    """A demo's hardcoded original as the session list shows it."""
    return samples.get(demo_id).session.model_copy(update={"demo": demo_id})


def listed(own: list[Session]) -> list[Session]:
    """The session list: the browser's sessions that aren't demos, then each demo, as the
    browser's copy if it has one, else the original."""
    demos = samples.catalogue()
    copies: dict[str, Session] = {}
    rest = []
    for session in own:
        if session.demo in demos and session.demo not in copies:
            copies[session.demo] = session
        else:
            rest.append(session)
    return rest + [copies.get(d) or original(d) for d in demos]


def _known(demo_id: str) -> None:
    if not samples.is_sample(demo_id):
        raise HTTPException(404, f"no demo {demo_id}")


def _copy_of(demo_id: str):
    """The browser's copy of the demo (a `SessionState`), if it has one."""
    from app.session_api import SESSIONS

    for sid in SESSIONS.owned_ids():
        try:
            state = SESSIONS.get(sid)
        except HTTPException:  # saved in a format this version can't read
            continue
        if state.session.demo == demo_id:
            return state
    return None


@router.post(DEMO, response_model=Session, responses={404: {}, 429: {}})
async def open_demo(demo_id: str) -> Session:
    """The browser's own copy of the demo, made from its original the first time."""
    from app.owners import current_owner
    from app.security import limits as rate_limits

    _known(demo_id)
    async with _lock(current_owner.get(), demo_id):
        found = _copy_of(demo_id)
        if found is not None:
            return found.session
        rate_limits.check(rate_limits.SESSIONS)
        made = await sample_copies.create_session(demo_id)
        rate_limits.record(rate_limits.SESSIONS)
        return made


@router.delete(DEMO, status_code=204, response_class=Response, responses={404: {}, 409: {}})
async def reset_demo(demo_id: str) -> Response:
    """Reset the demo: delete the browser's copy, so the next open makes a fresh one. 409 while
    Claude is working in it."""
    from app.owners import current_owner
    from app.session_api import SESSIONS
    from app.session_store import session_store

    _known(demo_id)
    async with _lock(current_owner.get(), demo_id):
        found = _copy_of(demo_id)
        if found is None:
            return Response(status_code=204)
        if found.session.status != "idle" or (found.run is not None and not found.run.done()):
            raise HTTPException(409, "Claude is working in this demo. Stop it first.")
        sid = found.session.id
        found.store = None  # nothing saves it again
        found.end_streams()
        SESSIONS.by_id.pop(sid, None)
        await asyncio.to_thread(session_store().delete, sid)
    return Response(status_code=204)


__all__ = ["listed", "original", "router"]
