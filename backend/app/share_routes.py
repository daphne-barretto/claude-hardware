"""The share link routes, declared once for the real API (`app.shares`) and the mock
(`app.mock_api`), so both serve the same contract.

Owner routes, on the session (only its owner reaches them: another browser gets 404, as for
every session route; a sample gets 409 from `app.samples`). Every change is a POST with a JSON
body:

- `GET  /api/sessions/{id}/shares`: the session's live links, one per kind at most.
- `POST /api/sessions/{id}/shares`: share it (`ShareCreate`): a snapshot of the session as it
  is now, at a new unguessable `/share/<token>`. 409 if a link of that kind is already live
  (update it instead) or Claude is still replying.
- `POST /api/sessions/{id}/shares/update`: take the snapshot again, at the same link.
- `POST /api/sessions/{id}/shares/revoke`: stop sharing; the link answers 410 from then on.

Viewer routes, `/api/shares/{token}/...`, need no cookie: they mirror the session's read routes
(the session, messages, events, builds with mesh, checks, source, board, maps and diffs, faces,
find in model, change set, risks, sketches, modeling edits, parameters, constraints, comments,
attachments the sharer included, and exports, which write files outside the snapshot). They
serve the snapshot through the session API's own handlers (`backend.api`), with the snapshot as
the session (`SHARE_VIEW`), so a shared view reads exactly as the session did. Any other method
on a share is 409: a shared view changes nothing and starts no run. An unknown token is 404; a
turned-off link is 410.

"Continue from this" and "Open a copy" are `POST /api/sessions` with `from_share`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextvars import ContextVar
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse

from app.attachments import FILE_RESPONSES
from app.boards import Board
from app.contracts.changes import ChangeSet, RiskFlags
from app.contracts.checks import CheckReport
from app.contracts.maps import AnalysisMap, MapType
from app.contracts.session import (
    Build,
    BuildDiff,
    BuildSource,
    Comment,
    Constraint,
    Edit,
    ErrorBody,
    Export,
    ExportCreate,
    FacePlane,
    FindRequest,
    FindResult,
    Message,
    Parameter,
    Session,
    ShareCreate,
    ShareLink,
    ShareRevoke,
    Sketch,
    sse_frame,
)
from app.contracts.telemetry import ClientTelemetry

API = "/api"
SESSION = API + "/sessions/{session_id}"
SHARE = API + "/shares/{token}"
GLTF = "model/gltf+json"
EVENT_STREAM = "text/event-stream"
KEEPALIVE_SECONDS = 15.0
READ_ONLY = "This link is read-only. Use Continue from this to make your own copy and work on it."
TURNED_OFF = "This link was turned off"
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorBody}}
SHARE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "No such link"},
    410: {"model": ErrorBody, "description": "The link was turned off"},
}

# The snapshot a share route is serving: the session API's handlers take it as the session.
SHARE_VIEW: ContextVar[Any] = ContextVar("share_view", default=None)


def share_id(token: str) -> str:
    """The ID a snapshot has as a session: its URLs are `/api/sessions/share-<token>/...`
    inside, `/api/shares/<token>/...` outside."""
    return f"share-{token}"


def share_url(token: str) -> str:
    return f"/share/{token}"


class ShareBackend(Protocol):
    """What the real API and the mock each provide."""

    api: Any  # the read handlers, by the session API's names

    def links(self, session_id: str) -> list[ShareLink]: ...

    async def create(self, session_id: str, body: ShareCreate) -> ShareLink: ...

    async def update(self, session_id: str, body: ShareCreate) -> ShareLink: ...

    async def revoke(self, session_id: str, body: ShareRevoke) -> ShareLink: ...

    def view(self, token: str) -> Any:
        """The snapshot as a session state (with `.session`, `.events`), or 404 / 410."""
        ...

    def kind(self, token: str) -> str: ...


def rewrite(token: str, data: Any) -> Any:
    """JSON with the snapshot's inside URLs as the share's outside ones."""
    text = json.dumps(jsonable_encoder(data))
    return json.loads(text.replace(f"/api/sessions/{share_id(token)}/", f"/api/shares/{token}/"))


def add_share_routes(router: APIRouter, backend: ShareBackend) -> None:  # noqa: C901
    async def view_of(token: str) -> Any:
        # Loading a snapshot builds its workspace: off the event loop.
        return await asyncio.to_thread(backend.view, token)

    async def call(token: str, name: str, *args: Any, **kwargs: Any) -> Any:
        view = await view_of(token)
        reset = SHARE_VIEW.set(view)
        try:
            result = getattr(backend.api, name)(share_id(token), *args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        finally:
            SHARE_VIEW.reset(reset)
        return result if isinstance(result, Response) else rewrite(token, result)

    # --- Owner routes --------------------------------------------------------------------------

    @router.get(
        SESSION + "/shares", response_model=list[ShareLink], responses=NOT_FOUND, tags=["shares"]
    )
    async def list_share_links(session_id: str) -> list[ShareLink]:
        """The session's live share links (owner only)."""
        return backend.links(session_id)

    @router.post(
        SESSION + "/shares",
        response_model=ShareLink,
        status_code=201,
        responses={**NOT_FOUND, **CONFLICT},
        tags=["shares"],
    )
    async def create_share_link(session_id: str, body: ShareCreate) -> ShareLink:
        """Share the session as it is now at a new, unguessable link (owner only)."""
        return await backend.create(session_id, body)

    @router.post(
        SESSION + "/shares/update",
        response_model=ShareLink,
        responses={**NOT_FOUND, **CONFLICT},
        tags=["shares"],
    )
    async def update_share_link(session_id: str, body: ShareCreate) -> ShareLink:
        """Take the snapshot again: the link shows the session as it is now (owner only)."""
        return await backend.update(session_id, body)

    @router.post(
        SESSION + "/shares/revoke", response_model=ShareLink, responses=NOT_FOUND, tags=["shares"]
    )
    async def revoke_share_link(session_id: str, body: ShareRevoke) -> ShareLink:
        """Stop sharing: the link is turned off for good (owner only)."""
        return await backend.revoke(session_id, body)

    # --- Viewer routes: read-only ----------------------------------------------------------

    @router.get(SHARE, response_model=Session, responses=SHARE_ERRORS, tags=["shares"])
    async def get_shared_session(token: str) -> Any:
        """The shared session (`shared` says which kind of link it is)."""
        session = await call(token, "get_session")
        return {**session, "shared": backend.kind(token), "next_revision": None}

    @router.get(
        SHARE + "/messages", response_model=list[Message], responses=SHARE_ERRORS, tags=["shares"]
    )
    async def list_shared_messages(token: str) -> Any:
        return await call(token, "list_messages")

    @router.get(
        SHARE + "/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "The snapshot's events, then keep-alives: it never changes",
                "content": {
                    EVENT_STREAM: {"schema": {"$ref": "#/components/schemas/SessionEvent"}}
                },
            },
            **SHARE_ERRORS,
        },
        tags=["shares"],
    )
    async def shared_events(
        token: str,
        after: Annotated[int | None, Query(description="Send events after this id")] = None,
        follow: Annotated[bool, Query(description="Keep the stream open")] = True,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        view = await view_of(token)
        try:
            start = after if after is not None else int(last_event_id or 0)
        except ValueError:
            start = 0
        inside, outside = f"/api/sessions/{share_id(token)}/", f"/api/shares/{token}/"
        frames = [
            sse_frame(i, e).replace(inside, outside) for i, e in list(view.events) if i > start
        ]

        async def stream():
            for frame in frames:
                yield frame
            while follow:
                await asyncio.sleep(KEEPALIVE_SECONDS)
                yield ": keep-alive\n\n"

        headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type=EVENT_STREAM, headers=headers)

    @router.get(
        SHARE + "/attachments/{attachment_id}",
        response_class=Response,
        responses={**FILE_RESPONSES, **SHARE_ERRORS},
        tags=["shares"],
    )
    async def get_shared_attachment(token: str, attachment_id: str) -> Response:
        """An attached file, if the sharer included attachments (else 404)."""
        return await call(token, "get_attachment", attachment_id)

    @router.get(
        SHARE + "/builds", response_model=list[Build], responses=SHARE_ERRORS, tags=["shares"]
    )
    async def list_shared_builds(token: str) -> Any:
        return await call(token, "list_builds")

    @router.get(
        SHARE + "/builds/{build_id}", response_model=Build, responses=SHARE_ERRORS, tags=["shares"]
    )
    async def get_shared_build(token: str, build_id: str) -> Any:
        return await call(token, "get_build", build_id)

    @router.get(
        SHARE + "/builds/{build_id}/mesh",
        response_class=Response,
        responses={
            200: {"description": "glTF following the mesh contract", "content": {GLTF: {}}},
            **SHARE_ERRORS,
        },
        tags=["shares"],
    )
    async def get_shared_mesh(token: str, build_id: str) -> Response:
        return await call(token, "get_mesh", build_id)

    @router.get(
        SHARE + "/builds/{build_id}/checks",
        response_model=CheckReport,
        response_model_by_alias=True,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_checks(token: str, build_id: str) -> Any:
        return await call(token, "get_checks", build_id)

    @router.get(
        SHARE + "/builds/{build_id}/source",
        response_model=BuildSource,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_source(token: str, build_id: str) -> Any:
        return await call(token, "get_source", build_id)

    @router.get(
        SHARE + "/builds/{build_id}/board",
        response_model=Board,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_board(token: str, build_id: str) -> Any:
        return await call(token, "get_board", build_id)

    @router.get(
        SHARE + "/builds/{build_id}/maps/{map_type}",
        response_model=AnalysisMap,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_map(token: str, build_id: str, map_type: MapType) -> Any:
        return await call(token, "get_map", build_id, map_type)

    @router.get(
        SHARE + "/builds/{build_id}/diff",
        response_model=BuildDiff,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_diff(
        token: str,
        build_id: str,
        against: Annotated[str, Query(description="The build to compare with: the diff's `old`")],
    ) -> Any:
        return await call(token, "get_diff", build_id, against=against)

    @router.get(
        SHARE + "/builds/{build_id}/faces/{face_id}",
        response_model=FacePlane,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def get_shared_face(token: str, build_id: str, face_id: str) -> Any:
        return await call(token, "get_face", build_id, face_id)

    @router.post(
        SHARE + "/builds/{build_id}/find",
        response_model=FindResult,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def find_in_shared_build(token: str, build_id: str, body: FindRequest) -> Any:
        """Find in model: it reads the build and changes nothing."""
        return await call(token, "find", build_id, body)

    def read(path: str, name: str, model: Any, alias: bool = False) -> None:
        async def handler(token: str) -> Any:
            return await call(token, name)

        handler.__name__ = f"shared_{name}"
        router.add_api_route(
            SHARE + path,
            handler,
            methods=["GET"],
            response_model=model,
            response_model_by_alias=alias,
            responses=SHARE_ERRORS,
            tags=["shares"],
        )

    read("/changes", "get_changes", ChangeSet, alias=True)
    read("/risks", "get_risks", RiskFlags, alias=True)
    read("/sketches", "list_sketches", list[Sketch])
    read("/edits", "list_edits", list[Edit])
    read("/parameters", "list_parameters", list[Parameter])
    read("/constraints", "list_constraints", list[Constraint])

    @router.get(
        SHARE + "/comments", response_model=list[Comment], responses=SHARE_ERRORS, tags=["shares"]
    )
    async def list_shared_comments(
        token: str,
        build_id: Annotated[
            str | None, Query(description="Locate the pins on this build (default: the latest)")
        ] = None,
    ) -> Any:
        return await call(token, "list_comments", build_id=build_id)

    @router.post(
        SHARE + "/exports",
        response_model=Export,
        status_code=201,
        responses={**SHARE_ERRORS, 422: {"model": ErrorBody}},
        tags=["shares"],
    )
    async def create_shared_export(token: str, body: ExportCreate) -> Any:
        """Exports write files outside the snapshot and leave it as it was."""
        return await call(token, "create_export", body)

    @router.get(
        SHARE + "/exports/{export_id}/files/{name}",
        response_class=Response,
        responses={
            200: {"description": "The exported file", "content": {"*/*": {}}},
            **SHARE_ERRORS,
        },
        tags=["shares"],
    )
    async def get_shared_export_file(token: str, export_id: str, name: str) -> Response:
        return await call(token, "get_export_file", export_id, name)

    @router.post(
        SHARE + "/telemetry",
        status_code=204,
        response_class=Response,
        responses=SHARE_ERRORS,
        tags=["shares"],
    )
    async def post_shared_telemetry(token: str, body: ClientTelemetry) -> Response:
        """Views on a shared link are not recorded: it has no telemetry log."""
        backend.kind(token)  # 404 or 410 first
        return Response(status_code=204)

    # Everything else would change the snapshot or start a run.
    async def read_only(token: str) -> None:
        backend.kind(token)  # 404 or 410 first
        raise HTTPException(409, READ_ONLY)

    for path in (SHARE, SHARE + "/{rest:path}"):
        router.add_api_route(
            path,
            read_only,
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )


__all__ = [
    "READ_ONLY",
    "SHARE_VIEW",
    "TURNED_OFF",
    "ShareBackend",
    "add_share_routes",
    "rewrite",
    "share_id",
    "share_url",
]
