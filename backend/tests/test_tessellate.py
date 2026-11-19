"""Tessellation with stable face and edge IDs.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension. Changes are made to the parameters
the generator derives from the board.
"""

from __future__ import annotations

import copy
import json
import math
import random
from functools import cache
from pathlib import Path

import pytest

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.contracts.common import ContractError
from app.contracts.mesh import validate_mesh
from app.standards import load_standards
from geometry import BACKENDS, BuildResult, enclosure_parameters, generate_enclosure
from geometry.tessellate import Tessellation, tessellate

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
REVISIONS = {"old": CHANGES.old_file, "new": CHANGES.new_file}
RANDOM_CHANGES = 20
REQUIRED_KEPT = 0.95


@cache
def board(which: str) -> Board:
    return load_board(BOARDS_DIR / REVISIONS[which])


def params(which: str = "old") -> dict:
    return enclosure_parameters(board(which), STANDARDS)


def build(p: dict, which: str = "old", backend: str | None = None) -> BuildResult:
    return generate_enclosure(board(which), STANDARDS, p, backend)


@cache
def baseline(which: str = "old", backend: str | None = None) -> Tessellation:
    return tessellate(build(params(which), which, backend))


def cut_openings(p: dict) -> list[dict]:
    return [o for o in p["openings"] if o["cut"]]


def same_face(old, new) -> bool:
    """The face an ID names after a rebuild is plausibly the same face: same feature and kind of
    surface, facing the same way."""
    a, b = old.signature, new.signature
    turned = math.dist(a.direction, b.direction)
    return a.feature == b.feature and a.kind == b.kind and turned < 0.25


# --- After a rebuild that moves an opening, its faces keep their IDs ----------------------------


@pytest.mark.parametrize("dx, dy", [(0.8, 0.0), (0.0, -0.8), (-0.6, 0.5)])
def test_moved_opening_keeps_its_face_ids(dx: float, dy: float) -> None:
    before = baseline()
    p = params()
    for opening in cut_openings(p):
        moved = copy.deepcopy(p)
        target = next(o for o in moved["openings"] if o["id"] == opening["id"])
        target["x"] += dx
        target["y"] += dy
        after = tessellate(build(moved), before)

        old_faces = [f for f in before.faces.values() if f.feature == opening["tag"]]
        new_faces = [f for f in after.faces.values() if f.feature == opening["tag"]]
        assert old_faces
        assert {f.id for f in new_faces} == {f.id for f in old_faces}
        for old in old_faces:
            new = after.faces[old.id]
            # The same wall: same shape, and its centroid moved with the opening.
            assert new.signature.kind == old.signature.kind
            assert math.dist(new.signature.position, old.signature.position) < 1e-3
            assert new.signature.area == pytest.approx(old.signature.area, rel=1e-3)
            moved_by = [
                a - b
                for a, b in zip(_centroid(after, new.id), _centroid(before, old.id), strict=True)
            ]
            assert moved_by[0] == pytest.approx(dx, abs=1e-3)
            assert moved_by[1] == pytest.approx(dy, abs=1e-3)
        # Every other face keeps its ID too.
        assert set(before.faces) == set(after.faces)


def _centroid(mesh: Tessellation, face_id: str) -> tuple[float, float, float]:
    for part in mesh.parts.values():
        points = [
            p
            for p, f in zip(part.positions, part.face_index, strict=True)
            if part.faces[f].id == face_id
        ]
        if points:
            return tuple(sum(p[k] for p in points) / len(points) for k in range(3))
    raise KeyError(face_id)


# --- 20 random parameter changes: at least 95% of tagged faces keep their IDs -------------------


def random_change(p: dict, rng: random.Random) -> tuple[dict, str]:
    """One random edit of the kind Claude or a user makes to a design."""
    p = copy.deepcopy(p)
    kind = rng.choice(["move_opening", "resize_opening", "wall", "draft", "boss", "ceiling"])
    if kind == "move_opening":
        o = rng.choice(cut_openings(p))
        o["x"] += rng.uniform(-0.7, 0.7)
        o["y"] += rng.uniform(-0.7, 0.7)
    elif kind == "resize_opening":
        o = rng.choice(cut_openings(p))
        o["size"] = [s * rng.uniform(0.85, 1.15) for s in o["size"]]
    elif kind == "wall":
        p["wall"] *= rng.uniform(0.9, 1.1)
    elif kind == "draft":
        p["draft"] *= rng.uniform(0.7, 1.3)
    elif kind == "boss":
        p["boss"]["radius"] *= rng.uniform(0.9, 1.1)
    else:
        p["ceiling"] -= rng.uniform(0.0, 0.3)
    return p, kind


def test_random_parameter_changes_keep_face_ids() -> None:
    rng = random.Random(11)
    p = params()
    previous = baseline()
    kept = total = 0
    report = []
    for _ in range(RANDOM_CHANGES):
        p, kind = random_change(p, rng)
        current = tessellate(build(p), previous)
        tagged = [f for f in previous.faces.values() if f.feature != "unknown"]
        ok = sum(1 for f in tagged if f.id in current.faces and same_face(f, current.faces[f.id]))
        kept, total = kept + ok, total + len(tagged)
        report.append(f"{kind}: {ok}/{len(tagged)}")
        validate_mesh(current.to_gltf())
        previous = current
    assert total
    assert kept / total >= REQUIRED_KEPT, report


def test_ids_survive_through_stored_signatures() -> None:
    """A rebuild in another process gets the previous IDs from the stored JSON."""
    before = baseline()
    stored = json.loads(json.dumps(before.signatures()))
    p = params()
    cut_openings(p)[0]["x"] += 0.5
    after = tessellate(build(p), stored)
    assert set(after.faces) == set(before.faces)
    assert set(after.edges) == set(before.edges)


def test_unmatched_faces_get_ids_never_used_before() -> None:
    before = baseline()
    p = params()
    opening = cut_openings(p)[0]
    opening["cut"] = False  # the opening's faces disappear
    without = tessellate(build(p), before)
    assert not any(f.feature == opening["tag"] for f in without.faces.values())
    opening["cut"] = True  # and come back as new faces: fresh numbers, not the retired ones
    again = tessellate(build(p), without)
    old_ids = {f.id for f in before.faces.values() if f.feature == opening["tag"]}
    new_ids = {f.id for f in again.faces.values() if f.feature == opening["tag"]}
    assert len(new_ids) == len(old_ids)
    assert not new_ids & old_ids


def test_fresh_ids_are_deterministic() -> None:
    a = tessellate(build(params()))
    b = baseline()
    assert [f.id for p in a.parts.values() for f in p.faces] == [
        f.id for p in b.parts.values() for f in p.faces
    ]


# --- Output passes the mesh contract tests ------------------------------------------------------


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("which", sorted(REVISIONS))
def test_output_passes_the_mesh_contract(backend: str, which: str, tmp_path: Path) -> None:
    result = build(params(which), which, backend)
    mesh = baseline(which, backend)
    path = mesh.write(tmp_path / "enclosure.gltf")
    info = validate_mesh(path)
    assert set(info.parts) == set(result.parts)
    for name, refs in result.face_refs.items():
        part = info.parts[name]
        # One mesh face per B-rep face, with the feature that created it.
        assert [f.feature for f in part.faces] == [r.feature for r in refs]
        assert part.edges
    assert {f.feature for f in mesh.faces.values()} == set(result.tags)


def test_edges_bound_their_faces() -> None:
    mesh = baseline()
    for part in mesh.parts.values():
        ids = {f.id for f in part.faces}
        for edge in part.edges:
            assert 1 <= len(edge.faces) <= 2
            assert set(edge.faces) <= ids
            assert edge.feature in {part.face(f).feature for f in edge.faces}


def test_optional_vertex_colors() -> None:
    mesh = baseline()
    colors = {name: [(0.0, 0.5, 1.0)] * len(part.positions) for name, part in mesh.parts.items()}
    info = validate_mesh(mesh.to_gltf(colors))
    assert all(p.has_colors for p in info.parts.values())
    assert not any(p.has_colors for p in validate_mesh(mesh.to_gltf()).parts.values())
    name = next(iter(mesh.parts))
    with pytest.raises(ValueError):
        mesh.to_gltf({name: [(0.0, 0.0, 0.0)]})


def test_contract_validator_rejects_a_broken_mesh() -> None:
    doc = baseline().to_gltf()
    doc["nodes"][1]["extras"]["faces"].pop()
    with pytest.raises(ContractError):
        validate_mesh(doc)
