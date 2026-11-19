"""Comments pinned to geometry: where a pin is on any build, and what Claude is told.

A comment is pinned to a face of a build by its stable face ID and a point on it. The
tessellator keeps a face's ID through rebuilds (matched within the feature that made it), so the
pin finds its face again on a later build. Where on the face: the comment keeps its offset from
the face's centre (`anchor`), so a face that moves takes the pin with it, and the point is put
back on the face's surface. If a rebuild drops the face ID, the pin goes to the nearest face of
the same feature (`feature`); with neither, the comment has no location on that build.

Both session APIs (the mock and the real one) use these functions, so they place pins the same
way. `context()` is how comments reach Claude: notes as context on every run, and the comments
sent to Claude as the requests of the run that handles them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime
from functools import lru_cache

from app.contracts.common import Location, Point3
from app.contracts.mesh import MeshInfo, PartInfo, validate_mesh
from app.contracts.session import Comment, CommentCreate, CommentReply
from app.security.scrub import untrusted

CLAUDE = "Claude"

Vec = tuple[float, float, float]


@lru_cache(maxsize=32)
def mesh_info(mesh: str) -> MeshInfo:
    """The mesh contract's view of a build's glTF (cached: builds share meshes)."""
    return validate_mesh(mesh.encode())


# --- Geometry -----------------------------------------------------------------------------------


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _triangles(part: PartInfo, face: int) -> list[tuple[Vec, Vec, Vec]]:
    pos, fi = part.positions, part.face_index
    idx = part.indices or list(range(len(pos)))
    return [
        (pos[idx[t]], pos[idx[t + 1]], pos[idx[t + 2]])
        for t in range(0, len(idx) - 2, 3)
        if fi[idx[t]] == face
    ]


def _centre(tris: list[tuple[Vec, Vec, Vec]]) -> Vec:
    """Area-weighted centre of a face's triangles."""
    total, acc = 0.0, (0.0, 0.0, 0.0)
    for a, b, c in tris:
        area = math.sqrt(sum(x * x for x in _cross(_sub(b, a), _sub(c, a)))) / 2
        acc = _add(acc, _scale(_add(_add(a, b), c), area / 3))
        total += area
    if total <= 0:
        pts = [p for t in tris for p in t]
        return _scale(tuple(map(sum, zip(*pts, strict=True))), 1 / len(pts))  # type: ignore[arg-type]
    return _scale(acc, 1 / total)


def _closest_on_triangle(p: Vec, a: Vec, b: Vec, c: Vec) -> Vec:
    """The point of triangle abc nearest `p` (Ericson, Real-Time Collision Detection 5.1.5)."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return _add(a, _scale(ab, d1 / (d1 - d3)))
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return _add(a, _scale(ac, d2 / (d2 - d6)))
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        return _add(b, _scale(_sub(c, b), (d4 - d3) / ((d4 - d3) + (d5 - d6))))
    denom = 1 / (va + vb + vc)
    return _add(a, _add(_scale(ab, vb * denom), _scale(ac, vc * denom)))


def _closest(p: Vec, tris: list[tuple[Vec, Vec, Vec]]) -> Vec:
    best, dist = p, math.inf
    for a, b, c in tris:
        q = _closest_on_triangle(p, a, b, c)
        d = math.dist(p, q)
        if d < dist:
            best, dist = q, d
    return best


def _face(mesh: MeshInfo, face_id: str) -> tuple[PartInfo, int] | None:
    part_name = mesh.face_parts.get(face_id)
    if part_name is None:
        return None
    part = mesh.parts[part_name]
    return part, next(i for i, f in enumerate(part.faces) if f.id == face_id)


def _round(p: Vec) -> Point3:
    return (round(p[0], 4) + 0.0, round(p[1], 4) + 0.0, round(p[2], 4) + 0.0)


# --- Pinning and locating -----------------------------------------------------------------------


def pin(
    body: CommentCreate, mesh: MeshInfo, *, comment_id: str, author: str, created_at: datetime
) -> Comment:
    """A new comment on a face of `mesh` (the build's). Raises KeyError for a face it lacks."""
    found = _face(mesh, body.face_id)
    if found is None:
        raise KeyError(body.face_id)
    part, index = found
    tris = _triangles(part, index)
    comment = Comment(
        id=comment_id,
        author=author,
        created_at=created_at,
        part=part.name,
        feature=part.faces[index].feature,
        anchor=_round(_centre(tris)),
        **body.model_dump(),
    )
    return located(comment, mesh)


def locate(comment: Comment, mesh: MeshInfo) -> Location | None:
    """Where the comment's pin is on `mesh`: its face (or, if the face ID is gone, the nearest
    face of the same feature on the same part) and the point at the same offset from the face's
    centre as when it was pinned, put back on the face."""
    found = _face(mesh, comment.face_id)
    anchor = comment.anchor or comment.point
    if found is None and comment.feature and comment.part in mesh.parts:
        part = mesh.parts[comment.part]
        candidates = [
            (math.dist(_centre(_triangles(part, i)), anchor), i)
            for i, f in enumerate(part.faces)
            if f.feature == comment.feature
        ]
        if candidates:
            found = (part, min(candidates)[1])
    if found is None:
        return None
    part, index = found
    tris = _triangles(part, index)
    target = _add(_centre(tris), _sub(comment.point, anchor))
    return Location(
        point=_round(_closest(target, tris)), part=part.name, faces=[part.faces[index].id]
    )


def located(comment: Comment, mesh: MeshInfo | None) -> Comment:
    """The comment with its location on `mesh` (none without a mesh)."""
    return comment.model_copy(update={"location": locate(comment, mesh) if mesh else None})


def reply(
    comment: Comment,
    *,
    reply_id: str,
    role: str,
    author: str,
    text: str,
    created_at: datetime,
    message_id: str | None = None,
) -> Comment:
    new = CommentReply(
        id=reply_id,
        role=role,  # type: ignore[arg-type]
        author=author,
        text=text,
        created_at=created_at,
        message_id=message_id,
    )
    return comment.model_copy(update={"replies": [*comment.replies, new]})


# --- What Claude is told ------------------------------------------------------------------------


def _where(c: Comment) -> str:
    loc = c.location
    where = {
        "face": loc.faces[0] if loc and loc.faces else c.face_id,
        "feature": c.feature,
        "part": (loc.part if loc else None) or c.part,
        "point": list(loc.point if loc else c.point),
    }
    return json.dumps({k: v for k, v in where.items() if v is not None})


def _thread(c: Comment) -> str:
    lines = [f'- {c.id} on {_where(c)}: {c.author}: "{c.text}"']
    lines += [f'  - reply from {r.author}: "{r.text}"' for r in c.replies]
    return "\n".join(lines)


def context(comments: Iterable[Comment], sent: Iterable[str] = ()) -> str:
    """The comments as Claude gets them with a message: the open notes as context, and the
    comments `sent` to Claude as requests to answer in their threads."""
    sent = list(sent)
    requests = [c for c in comments if c.id in sent]
    notes = [c for c in comments if c.id not in sent and not c.resolved]
    parts = []
    if requests:
        parts.append(
            "Comments sent to you as requests, pinned to the model (location in the current "
            "build). Handle them all in this run. Answer each in its own thread with "
            "reply_to_comment, once, after its edits, and list in `changes` the parameter "
            "paths you changed for it; don't answer them in chat.\n"
            + untrusted("comments sent as requests", "\n".join(_thread(c) for c in requests))
        )
    if notes:
        parts.append(
            "Comments pinned to the model (notes for context, not requests; don't act on "
            "them unless asked):\n"
            + untrusted("comment notes", "\n".join(_thread(c) for c in notes))
        )
    return "\n\n".join(parts)


__all__ = ["CLAUDE", "context", "locate", "located", "mesh_info", "pin", "reply"]
