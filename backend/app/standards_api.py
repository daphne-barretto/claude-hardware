"""The standards routes (`/api/standards`), shared by the session API and the mock API.

`GET /api/standards` lists the sets the browser can choose: the built-in team set
(`standards.yaml`, read-only) and the sets this browser made. `GET /api/standards/kinds` is the
catalogue of rule kinds the app can enforce (`app.standards_catalog`): rules are made of these
only, so every rule has a check. The other routes create, read, change and delete the
browser's own sets (`app.standards_sets`); another browser's set answers 404, and the built-in
set answers 403 to a change (duplicate it instead).

These routes are for the user. No agent tool calls them: Claude never changes standards.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse

from app import standards_sets as sets
from app.contracts.session import (
    CheckKindInfo,
    ErrorBody,
    StandardsErrorBody,
    StandardsSet,
    StandardsSetCreate,
    StandardsSetUpdate,
)
from app.standards_catalog import kinds

API = "/api"
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
READ_ONLY: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}}
INVALID: dict[int | str, dict[str, Any]] = {422: {"model": StandardsErrorBody}}
BUILT_IN_READ_ONLY = "The built-in constraint set is read-only. Duplicate the set to change it."


def _invalid(error: sets.SetProblems) -> JSONResponse:
    body = StandardsErrorBody(detail=str(error), problems=error.problems)
    return JSONResponse(status_code=422, content=body.model_dump(mode="json", exclude_none=True))


def standards_router(store: Callable[[], sets.SetsStore]) -> APIRouter:
    """The routes, keeping each browser's sets in `store()`."""
    router = APIRouter()

    def describe(standards) -> StandardsSet:
        return sets.describe(store(), sets.owner_key(), standards)

    @router.get(API + "/standards", response_model=list[StandardsSet], tags=["standards"])
    def list_standards() -> list[StandardsSet]:
        """The standards sets a session can be designed and checked against: the built-in team
        set (`standards.yaml`, read-only), then the sets this browser made. Choose one with the
        session's first message (`MessageCreate.standards`) or later with
        `PUT /api/sessions/{id}/standards`."""
        return [describe(s) for s in sets.list_sets(store(), sets.owner_key())]

    @router.get(API + "/standards/kinds", response_model=list[CheckKindInfo], tags=["standards"])
    def list_kinds() -> list[CheckKindInfo]:
        """Every kind of rule the app can enforce: its check, value, unit, valid range and what
        it measures. Rules can only be made of these kinds; a new kind needs a new check in
        code."""
        return kinds(sets.built_in())

    @router.post(
        API + "/standards",
        response_model=StandardsSet,
        status_code=201,
        responses={**NOT_FOUND, **INVALID},
        tags=["standards"],
    )
    def create_standards(body: StandardsSetCreate) -> Any:
        """A new set for this browser: empty, or a copy of `duplicate_of`."""
        try:
            created = sets.create(store(), sets.owner_key(), body.name, body.duplicate_of)
        except KeyError as error:
            raise HTTPException(404, f"no constraint set {body.duplicate_of}") from error
        except sets.SetProblems as error:
            return _invalid(error)
        return describe(created)

    @router.get(
        API + "/standards/{set_id}",
        response_model=StandardsSet,
        responses=NOT_FOUND,
        tags=["standards"],
    )
    def get_standards(set_id: str) -> StandardsSet:
        try:
            return describe(sets.find_set(store(), sets.owner_key(), set_id))
        except KeyError as error:
            raise HTTPException(404, f"no constraint set {set_id}") from error

    @router.patch(
        API + "/standards/{set_id}",
        response_model=StandardsSet,
        responses={**NOT_FOUND, **READ_ONLY, **INVALID},
        tags=["standards"],
    )
    def update_standards(set_id: str, body: StandardsSetUpdate) -> Any:
        """Rename one of this browser's sets and/or replace its rules (each from the
        catalogue). Everything is validated first; nothing is saved if anything is wrong.
        Sessions using the set pick the change up when it is chosen for them again
        (`PUT /api/sessions/{id}/standards`), which rechecks."""
        try:
            changed = sets.update(store(), sets.owner_key(), set_id, body.name, body.standards)
        except sets.ReadOnlySet as error:
            raise HTTPException(403, BUILT_IN_READ_ONLY) from error
        except KeyError as error:
            raise HTTPException(404, f"no constraint set {set_id}") from error
        except sets.SetProblems as error:
            return _invalid(error)
        return describe(changed)

    @router.delete(
        API + "/standards/{set_id}",
        status_code=204,
        responses={**NOT_FOUND, **READ_ONLY},
        tags=["standards"],
    )
    def delete_standards(set_id: str) -> Response:
        """Delete one of this browser's sets. Sessions already using it keep their copy."""
        try:
            sets.delete(store(), sets.owner_key(), set_id)
        except sets.ReadOnlySet as error:
            raise HTTPException(403, BUILT_IN_READ_ONLY) from error
        except KeyError as error:
            raise HTTPException(404, f"no constraint set {set_id}") from error
        return Response(status_code=204)

    return router


__all__ = ["standards_router"]
