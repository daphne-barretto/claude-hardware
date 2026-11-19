"""Undo and redo over the session's design timeline (`app.design_history`):
`/sessions/{id}/history`.

`GET` serves the timeline. `POST .../undo` moves back one step and `POST .../redo` forward: the
state there (the design with its locks, the change set as it was reviewed, the standards set)
becomes the current one and is rebuilt and rechecked; the rebuild streams as a `build_ready`
with no message. If the rebuild fails, the design stays as it was and the answer says so in
plain words (409); a state whose own build had failed comes back as the failed build it was.
Both are refused while Claude is working or a review or import is rebuilding; like a hand edit,
they are allowed while Claude's question waits. Undo never
deletes builds.

Kept apart from `app.session_api` so the timeline doesn't crowd it; the routes are the ones the
mock declares (`app.mock_api`).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from app.boards import Board, find_board
from app.contracts.session import Build, DesignHistory, HistoryMove
from app.design_history import History, capture
from app.session_api import (
    CONFLICT,
    NOT_FOUND,
    SESSION,
    SESSIONS,
    SessionState,
    _build_design,
    _emit_import,
)

router = APIRouter()
log = logging.getLogger(__name__)

BUSY = "Claude is working: undo and redo wait until the run has finished."
REBUILDING = "The design is still rebuilding: try again in a moment."
NOTHING = {"undo": "There's nothing to undo.", "redo": "There's nothing to redo."}
UNDO_FAILED = "Couldn't rebuild the design as it was before that step, so nothing changed."
REDO_FAILED = "Couldn't rebuild the design with that step again, so nothing changed."


def _ready(state: SessionState) -> None:
    status = state.session.status
    if status == "running":
        raise HTTPException(409, BUSY)
    if state.reviewing.locked() or state.importing.locked():
        raise HTTPException(409, REBUILDING)


def _board(state: SessionState, build_id: str | None) -> Board | None:
    """The board the state's build was checked against (None: a design without one)."""
    build = state.builds.get(build_id or "") or state.latest_build()
    if build is None or build.board is None:
        return None
    try:
        return find_board(build.board.name, build.board.revision)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


async def apply_state(state: SessionState, target: dict[str, Any], failed: str) -> Build:
    """Make a timeline state the current one and rebuild it. If the rebuild fails, put the
    current state back and refuse with `failed` (plain words), unless the state's own build had
    failed: then the failed build is what that state is."""
    before = capture(state)
    risks = state.risks
    board = _board(state, target.get("build_id"))
    imported = state.imports.get(target.get("import") or "")
    state.put_state(target)
    if imported is not None:  # the canvas showed an imported file: the same file again
        if board is None:
            raise HTTPException(409, failed)
        return _emit_import(state, imported, board, state.new_build_id())
    was = state.builds.get(target.get("build_id") or "")
    try:
        return await _build_design(
            state, board, failed_ok=was is not None and was.status != "ready"
        )
    except Exception as error:
        state.put_state(before)
        state.risks = risks
        log.warning("session %s: rebuilding a timeline state failed: %s", state.session.id, error)
        raise HTTPException(409, failed) from error


async def move(state: SessionState, direction: Literal["undo", "redo"]) -> HistoryMove:
    _ready(state)
    if state.initial is not None:
        await state.initial
    history: History = state.history
    if direction == "undo" and not history.can_undo():
        raise HTTPException(409, NOTHING["undo"])
    if direction == "redo" and not history.can_redo():
        raise HTTPException(409, NOTHING["redo"])
    step = history.steps[history.position - 1 if direction == "undo" else history.position]
    index = history.position - 1 if direction == "undo" else history.position + 1
    target = history.state_at(index)
    assert target is not None
    # The rebuild holds the review lock, so no message, review or other undo starts meanwhile.
    async with state.reviewing:
        build = await apply_state(
            state, target, UNDO_FAILED if direction == "undo" else REDO_FAILED
        )
    history.moved(direction, step)
    state.save()
    return HistoryMove(
        direction=direction,
        step=History.step_model(step),
        build=build,
        history=history.model(),
        change_set=state.proposal.change_set() if state.proposal else None,
    )


@router.get(
    SESSION + "/history", response_model=DesignHistory, responses=NOT_FOUND, tags=["history"]
)
async def get_history(session_id: str) -> DesignHistory:
    return SESSIONS.get(session_id).history.model()


@router.post(
    SESSION + "/history/undo",
    response_model=HistoryMove,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["history"],
)
async def undo(session_id: str) -> HistoryMove:
    """Undo the last change to the design, whoever made it (`DesignHistory.undo`): the design
    goes back to the state before it and is rebuilt; the rebuild streams as a `build_ready`
    with no message. 409 while Claude is working, with nothing to undo, or when the earlier
    design can't be rebuilt (the design stays as it is)."""
    return await move(SESSIONS.get(session_id), "undo")


@router.post(
    SESSION + "/history/redo",
    response_model=HistoryMove,
    responses={**NOT_FOUND, **CONFLICT},
    tags=["history"],
)
async def redo(session_id: str) -> HistoryMove:
    """Redo the step the last undo took back (`DesignHistory.redo`), rebuilt like an undo. A new
    change after an undo drops the steps that could be redone."""
    return await move(SESSIONS.get(session_id), "redo")
