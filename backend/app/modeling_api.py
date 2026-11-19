"""The modeling tools' routes of the real session API: `/sessions/{id}/edits`.

A modeling edit (`app.modeling`) is a hand edit, like a sketch: it goes into the design's
parameters (`params["edits"]`) and code as a feature, locked (`edits.<tag>`), and the design
rebuilds and rechecks: a `build_ready` with no message follows on `/events`. Claude keeps it on
every run. Changing an edit's values, suppressing it or deleting it (with the edits that copy
it) are hand edits too, and rebuild the same way. Each is a step of the design timeline
(`app.design_history`), which undo and redo move along (`app.history_api`).

Kept apart from `app.session_api` so the modeling tools don't crowd it; the routes are the ones
the mock declares (`app.mock_api`).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from app.boards import Board, find_board
from app.contracts.session import EDIT_VALUES, Edit, EditCreate, EditRemoval, EditUpdate
from app.design_history import capture, edit_label
from app.modeling import (
    EditError,
    edit_params,
    new_edit,
    removal,
    source_parts,
    updated_edit,
)
from app.session_api import (
    CONFLICT,
    HAND_EDIT,
    NOT_FOUND,
    SESSION,
    SESSIONS,
    SessionState,
    _build_design,
)

router = APIRouter()
log = logging.getLogger(__name__)


def edit_list(state: SessionState) -> list[Edit]:
    """The modeled features in the current design, in the order they apply (an undo can take
    some out), at the values the design has."""
    out = []
    for e in state.ws.design.params.get("edits", []):
        known = state.edits.get(e["tag"])
        if known is not None:  # at the values the design has (an undo can bring back old ones)
            values = {k: e.get(k) for k in EDIT_VALUES[e["kind"]]}
            values.update(suppressed=bool(e.get("suppressed")))
            out.append(Edit.model_validate({**known.model_dump(), **values}))
    return out


def _taken(state: SessionState) -> set[str]:
    """Every tag a new edit can't have: the design's features and every edit ever made (an undo
    takes an edit out, but its lock is still held)."""
    design = state.ws.design
    taken = {e["tag"] for e in design.params.get("edits", [])} | set(state.edits)
    taken |= {p.name.split(".", 1)[1] for p in design.store if p.name.startswith("edits.")}
    return taken


def _idle(state: SessionState) -> None:
    if state.session.status == "running":
        raise HTTPException(409, "Claude is working: edit the model when the run ends")


def _current(state: SessionState, tag: str) -> Edit:
    edit = next((e for e in edit_list(state) if e.tag == tag), None)
    if edit is None:
        raise HTTPException(404, f"no edit {tag} in the design")
    return edit


REBUILD_FAILED = "Couldn't rebuild the design with that edit, so nothing was changed. Try again."


def _board_of(state: SessionState) -> Board | None:
    """The board the latest build was checked against, or None for a design without one."""
    latest = state.builds[state.session.latest_build_id] if state.session.latest_build_id else None
    if latest is None:
        raise HTTPException(409, "There's no model to edit yet.")
    if latest.board is None:
        return None
    try:
        return find_board(latest.board.name, latest.board.revision)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


async def _rebuild(state: SessionState, undo: Callable[[], None]) -> None:
    """Rebuild and recheck after an edit. If the rebuild itself breaks (not a build that fails
    its checks, which is a normal result), `undo` puts the design back and the user is told
    nothing changed, so no edit is ever listed without being built."""
    board = _board_of(state)
    state.ws._changed()  # the design changed: rebuild and recheck before trusting a report
    try:
        await _build_design(state, board)
    except Exception as error:
        undo()
        state.ws._changed()
        log.exception("rebuild after a modeling edit failed: %s", error)
        raise HTTPException(500, REBUILD_FAILED) from error
    state.save()


def _keep(state: SessionState) -> tuple[str, dict[str, Any], dict[str, Edit]]:
    design = state.ws.design
    return design.code, copy.deepcopy(design.params), dict(state.edits)


def _restore(state: SessionState, kept: tuple[str, dict[str, Any], dict[str, Edit]]) -> None:
    design = state.ws.design
    design.code, design.params = kept[0], copy.deepcopy(kept[1])
    state.edits.clear()
    state.edits.update(kept[2])
    # The lock store follows the restored values, so Claude keeps exactly what was there.
    for e in design.params.get("edits", []):
        design.store.set(
            f"edits.{e['tag']}", json.dumps(e), by="user", by_hand=True, approval=HAND_EDIT
        )


@router.get(SESSION + "/edits", response_model=list[Edit], responses=NOT_FOUND, tags=["edits"])
async def list_edits(session_id: str) -> list[Edit]:
    return edit_list(SESSIONS.get(session_id))


@router.post(
    SESSION + "/edits",
    response_model=Edit,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def create_edit(session_id: str, body: EditCreate) -> Edit:
    """A modeling tool's edit on a build: move a part, push or pull a flat face, add a hole
    (optionally for the standard insert), boss, rib, fillet, chamfer or shell, or mirror or
    pattern a feature. It becomes a named, locked feature (`edits.<tag>`) that Claude keeps, in
    the code, and the design rebuilds and rechecks: a `build_ready` without a `message_id`
    follows on `/events`."""
    state = SESSIONS.get(session_id)
    if state.initial is not None:
        await state.initial
    _idle(state)
    build = state.build(body.build_id, ready=True)
    features = source_parts(await asyncio.to_thread(state.source, build))  # may start a worker
    try:
        edit = new_edit(body, state.mesh(build), features, _taken(state), state.ws.standards)
    except EditError as error:
        raise HTTPException(409, str(error)) from error
    if edit is None:
        what = f"edge {body.edge_id}" if body.edge_id else f"face {body.face_id}"
        raise HTTPException(404, f"no {what}")
    kept, before = _keep(state), capture(state)
    state.ws.design.add_edit(edit_params(edit))
    state.edits[edit.tag] = edit
    await _rebuild(state, lambda: _restore(state, kept))
    state.record(before, "edit", edit_label(edit))
    return edit


@router.patch(
    SESSION + "/edits/{tag}",
    response_model=Edit,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def update_edit(session_id: str, tag: str, body: EditUpdate) -> Edit:
    """Change a modeled feature's values, or suppress it or bring it back: still locked, and the
    design rebuilds and rechecks. A suppressed edit stays in the design but not in the build."""
    state = SESSIONS.get(session_id)
    _idle(state)
    try:
        edit = updated_edit(_current(state, tag), body)
    except EditError as error:
        raise HTTPException(409, str(error)) from error
    kept, before, old = _keep(state), capture(state), _current(state, tag)
    state.ws.design.replace_edit(edit_params(edit), HAND_EDIT)
    state.edits[tag] = edit
    await _rebuild(state, lambda: _restore(state, kept))
    if edit.suppressed != old.suppressed:
        label = f"{'suppressed' if edit.suppressed else 'brought back'} {edit_label(edit)}"
    else:
        label = f"changed {edit_label(old)} to {edit_label(edit)}"
    state.record(before, "edit", label)
    return edit


@router.delete(
    SESSION + "/edits/{tag}",
    response_model=EditRemoval,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["edits"],
)
async def delete_edit(session_id: str, tag: str) -> EditRemoval:
    """Delete a modeled feature, and the mirrors and patterns that copy it (`removed`). The
    design rebuilds and rechecks without them."""
    state = SESSIONS.get(session_id)
    _idle(state)
    edit = _current(state, tag)
    removed = removal(state.ws.design.params.get("edits", []), tag)
    kept, before = _keep(state), capture(state)
    state.ws.design.remove_edits(removed, HAND_EDIT)
    await _rebuild(state, lambda: _restore(state, kept))
    state.record(before, "edit", f"deleted {edit_label(edit)}")
    return EditRemoval(tag=tag, removed=removed)
