"""Board revisions as data. Everything is loaded from `demos/b-parasite/`."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

import pytest

import app.boards
from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import (
    COLUMNS,
    Change,
    enclosure_changes,
    parse_changes_md,
    row_key,
)
from app.boards.kicad import convert, convert_all
from app.boards.schema import Component, MountingHole, Opening, Pcb, Source

ROOT = BOARDS_DIR.parent
SOURCE = BOARDS_DIR / "source"
# The board file format, documented next to its loader (product code, not demo data).
SCHEMA_MD = Path(app.boards.__file__).parent / "SCHEMA.md"
REVISIONS = json.loads((SOURCE / "revisions.json").read_text())["revisions"]
CHANGES_DOC = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())


@cache
def board(output: str) -> Board:
    return load_board(BOARDS_DIR / output)


# --- SCHEMA.md documents the schema with an example -------------------------------------------


def test_schema_doc_example_is_a_valid_board() -> None:
    text = SCHEMA_MD.read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert blocks, "SCHEMA.md needs a JSON example"
    example = Board.model_validate(json.loads(blocks[0]))
    assert example.openings and example.components and example.mounting_holes


def test_schema_doc_covers_every_field() -> None:
    text = SCHEMA_MD.read_text()
    fields = {
        name
        for model in (Board, Source, Pcb, MountingHole, Component, Opening)
        for name in model.model_fields
    }
    spans = re.findall(r"`([^`\n]+)`", text)
    documented = {word for span in spans for word in re.findall(r"[a-z_]+", span)}
    missing = sorted(fields - documented)
    assert not missing, f"SCHEMA.md does not document {missing}"


# --- Both revisions committed, each with its source commit -------------------------------------


@pytest.mark.parametrize("rev", REVISIONS, ids=lambda r: r["output"])
def test_board_file_is_valid_and_names_its_source_commit(rev: dict) -> None:
    b = board(rev["output"])
    assert b.revision == rev["revision"]
    assert b.source.commit == rev["commit"]
    assert b.source.repo == rev["repo"]
    assert b.source.license == "CC BY-SA 4.0"
    assert b.components, "a board without components"
    refs = [c.ref for c in b.components]
    assert len(refs) == len(set(refs)), "duplicate reference designators"
    for o in b.openings:
        b.component(o.component)


@pytest.mark.parametrize("rev", REVISIONS, ids=lambda r: r["output"])
def test_board_geometry_is_on_the_board(rev: dict) -> None:
    b = board(rev["output"])
    xs = [p[0] for p in b.pcb.outline]
    ys = [p[1] for p in b.pcb.outline]
    assert min(xs) == pytest.approx(0) and min(ys) == pytest.approx(0)
    area = sum(
        a[0] * c[1] - c[0] * a[1]
        for a, c in zip(b.pcb.outline, b.pcb.outline[1:] + b.pcb.outline[:1], strict=True)
    )
    assert area > 0, "outline must be counter-clockwise"
    for item in [*b.components, *b.openings, *b.mounting_holes]:
        x, y = item.position
        assert min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys), item


def test_the_revisions_named_in_changes_md_exist() -> None:
    outputs = {r["output"] for r in REVISIONS}
    assert {CHANGES_DOC.old_file, CHANGES_DOC.new_file} <= outputs


# --- Converter: committed files are exactly what the converter produces ------------------------


def test_board_files_match_the_converter_output(tmp_path: Path) -> None:
    assert convert_all(BOARDS_DIR, check=True) == [], (
        "board files are out of date: run `python -m app.boards.kicad` in backend/"
    )


@pytest.mark.parametrize("rev", REVISIONS, ids=lambda r: r["output"])
def test_kicad_copies_are_complete_board_files(rev: dict) -> None:
    text = (SOURCE / rev["kicad_file"]).read_text()
    assert text.lstrip().startswith("(kicad_pcb")


def test_every_footprint_needs_an_entry_in_footprints_json() -> None:
    rev = REVISIONS[0]
    footprints = json.loads((SOURCE / "footprints.json").read_text())["footprints"]
    dropped = dict(footprints)
    victim = board(rev["output"]).components[0].footprint
    del dropped[victim]
    with pytest.raises(KeyError, match=re.escape(victim)):
        convert((SOURCE / rev["kicad_file"]).read_text(), rev, dropped)


def test_rotation_offset_makes_orientation_library_independent() -> None:
    """Rotating a footprint's frame by the offset must not move its body."""
    rev = REVISIONS[0]
    footprints = json.loads((SOURCE / "footprints.json").read_text())["footprints"]
    pcb = (SOURCE / rev["kicad_file"]).read_text()
    base = convert(pcb, rev, footprints)
    target = next(c for c in base["components"] if c["size"][0] != c["size"][1])
    shifted = json.loads(json.dumps(footprints))
    meta = shifted[target["footprint"]]
    meta["rotation_offset"] = meta.get("rotation_offset", 0) + 90
    other = next(c for c in convert(pcb, rev, shifted)["components"] if c["ref"] == target["ref"])
    assert other["position"] == target["position"]
    assert other["rotation"] == (target["rotation"] + 90) % 360
    assert other["size"] == target["size"][::-1]


def test_converter_is_documented() -> None:
    schema = SCHEMA_MD.read_text()
    assert "python -m app.boards.kicad" in schema
    for name in ("revisions.json", "footprints.json", "rotation_offset", "height"):
        assert name in schema


# --- CHANGES.md matches the recomputed differences ---------------------------------------------


def test_changes_md_matches_the_recomputed_differences() -> None:
    old = board(CHANGES_DOC.old_file)
    new = board(CHANGES_DOC.new_file)
    computed = sorted(c.key() for c in enclosure_changes(old, new))
    documented = sorted(row_key(r) for r in CHANGES_DOC.rows)
    assert documented == computed, (
        "demos/b-parasite/CHANGES.md is out of date: run `python -m app.boards.changes` in backend/"
    )


def test_changes_md_says_what_each_difference_breaks() -> None:
    assert CHANGES_DOC.rows
    for row in CHANGES_DOC.rows:
        breaks = row[COLUMNS[-1]]
        assert len(breaks) > 20 and "TODO" not in breaks, row


def test_claude_reads_the_board_changes_without_maintainer_notes() -> None:
    """The agent gets the differences (the table and what was checked and found unaffected),
    never the maintainers' notes on how the file is kept."""
    import asyncio

    from app.agent.run import scenario_workspace

    ws = scenario_workspace()
    spec = json.loads(asyncio.run(ws.read_spec({}))["content"][0]["text"])
    text = spec["board_changes"]
    assert parse_changes_md(text).rows == CHANGES_DOC.rows  # every difference, row by row
    assert "not enclosure-affecting" in text  # the facts checked and found unaffected
    full = (BOARDS_DIR / "CHANGES.md").read_text()
    assert "## Maintainer notes" in full, "the file keeps its maintainer notes"
    for note in ("Maintainer", "make test", "Issue", "expected"):
        assert note not in text, note


def _moved(b: Board, opening_id: str, dx: float, dy: float, rotate: float = 0) -> Board:
    openings = [
        o.model_copy(
            update={
                "position": (o.position[0] + dx, o.position[1] + dy),
                "rotation": (o.rotation + rotate) % 360,
            }
        )
        if o.id == opening_id
        else o
        for o in b.openings
    ]
    return b.model_copy(update={"openings": openings})


def test_diff_rules() -> None:
    b = board(CHANGES_DOC.old_file)
    o = b.openings[0]
    assert enclosure_changes(b, b) == []
    assert enclosure_changes(b, _moved(b, o.id, 0.1, 0)) == [], "within tolerance"
    assert enclosure_changes(b, _moved(b, o.id, 0, 0, 180)) == [], "rectangles are symmetric"
    [moved] = enclosure_changes(b, _moved(b, o.id, 1, 0))
    assert moved.change == "moved" and moved.component == o.component
    [turned] = enclosure_changes(b, _moved(b, o.id, 1, 0, 90))
    assert turned.change == "moved and rotated"
    [removed] = enclosure_changes(b, b.model_copy(update={"openings": b.openings[1:]}))
    assert removed.change == "removed"
    [added] = enclosure_changes(b.model_copy(update={"openings": b.openings[1:]}), b)
    assert added.change == "added"


def test_diff_catches_outline_holes_and_height() -> None:
    b = board(CHANGES_DOC.old_file)
    outline = [(x * 1.1, y) for x, y in b.pcb.outline]
    reshaped = b.model_copy(update={"pcb": b.pcb.model_copy(update={"outline": outline})})
    assert [c.item for c in enclosure_changes(b, reshaped)] == ["outline"]

    holes = b.model_copy(update={"mounting_holes": []})
    assert {c.change for c in enclosure_changes(b, holes)} == {"removed"}

    tall = max(b.components, key=lambda c: c.height)
    taller = b.model_copy(
        update={
            "components": [
                c.model_copy(update={"height": c.height + 1}) if c is tall else c
                for c in b.components
            ]
        }
    )
    [change] = enclosure_changes(b, taller)
    assert isinstance(change, Change) and change.item == "clearance" and change.change == "taller"


# --- License and attribution --------------------------------------------------------------------


def test_boards_license_credits_b_parasite() -> None:
    text = (BOARDS_DIR / "LICENSE").read_text()
    assert "b-parasite" in text and "CC BY-SA 4.0" in text
    assert "Attribution-ShareAlike 4.0 International" in text
    for rev in REVISIONS:
        assert rev["commit"] in text


def test_readme_credits_b_parasite() -> None:
    text = (ROOT / "README.md").read_text()
    assert "b-parasite" in text and "CC BY-SA 4.0" in text


# --- The existing enclosure ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def case_solids():
    from build123d import import_step

    shape = import_step(BOARDS_DIR / "b-parasite-case.step")
    case = next(c for c in shape.children if c.label == "Case")
    solids = case.solids()
    assert len(solids) == 3 and all(s.is_valid for s in solids)
    bottom = min(solids, key=lambda s: s.bounding_box().min.Z)
    tops = sorted((s for s in solids if s is not bottom), key=lambda s: s.volume)
    return bottom, tops[0], tops[1]


def _clear_at(
    solid, b: Board, point: tuple[float, float], z: tuple[float, float] = (-20, 20)
) -> bool:
    """True if a thin vertical column at a board point passes through the solid untouched."""
    from build123d import Box, Pos

    length = max(p[1] for p in b.pcb.outline)
    width = max(p[0] for p in b.pcb.outline)
    # board to case axes, see demos/b-parasite/README.md
    x, y = length - point[1], point[0] - width / 2
    column = Pos(x, y, (z[0] + z[1]) / 2) * Box(0.3, 0.3, z[1] - z[0])
    common = solid & column
    return common is None or common.volume < 1e-6


def test_case_bottom_pegs_go_through_the_old_board_holes(case_solids) -> None:
    bottom, *_ = case_solids
    old = board(CHANGES_DOC.old_file)
    inside_board = (0.1, old.pcb.thickness - 0.1)
    assert old.mounting_holes
    for h in old.mounting_holes:
        assert not _clear_at(bottom, old, h.position, inside_board), f"no peg in {h.id}"
    # Away from the holes, nothing of the bottom intrudes into the board.
    for c in old.components:
        if c.side == "top":
            assert _clear_at(bottom, old, c.position, inside_board), c.ref


def test_case_openings_fit_the_old_board_and_miss_the_new_one(case_solids) -> None:
    _, top_with_openings, plain_top = case_solids
    old, new = board(CHANGES_DOC.old_file), board(CHANGES_DOC.new_file)
    moved = [
        r["Component"].strip("`")
        for r in CHANGES_DOC.rows
        if r["Opening"].strip("`") in ("sensor_window", "vent") and "moved" in r["Change"]
    ]
    assert moved
    for o in old.openings:
        if o.kind in ("sensor_window", "vent"):
            assert _clear_at(top_with_openings, old, o.position), o.id
            assert not _clear_at(plain_top, old, o.position), o.id
    for o in new.openings:
        if o.component in moved:
            assert not _clear_at(top_with_openings, new, o.position), o.id
