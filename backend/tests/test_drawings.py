"""Production drawings.

Every part of the build gets a drawing sheet a molder can read: front, top, side and isometric
views, the key dimensions, a title block and a revision table generated from the build history.
The PDF is read back with pypdf and its dimensions are compared with the built solids, measured
here independently of the exporter with exact solid booleans.

The scenario comes from `demos/b-parasite/` (the revisions named in `CHANGES.md`) and
`standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import re
import tempfile
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path

import pypdf
import pytest
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.builds import BuildStore
from app.contracts.common import BoardRef
from app.exports import store_revisions
from app.standards import load_standards
from geometry import BACKENDS, DEFAULT_BACKEND, BuildResult, enclosure_parameters, get_backend, occ
from geometry.export import (
    DRAWING_VIEWS,
    FILE_NAMES,
    Revision,
    export_build,
    key_dimensions,
    revision_letter,
)
from geometry.interface import ENCLOSURE
from geometry.report import check_report

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()
TOLERANCE = 0.01  # mm: the drawing's dimensions against the model
BUILD_ID = "build-0123456789ab"


@cache
def board() -> Board:
    return load_board(BOARDS_DIR / CHANGES.new_file)


@cache
def built(backend: str = DEFAULT_BACKEND) -> tuple[BuildResult, str, dict, dict]:
    engine = get_backend(backend)
    code = engine.example("enclosure")
    params = enclosure_parameters(board(), STANDARDS)
    result = engine.build(code, params)
    return result, code, params, check_report(board(), STANDARDS, result, BUILD_ID)


HISTORY = (
    Revision("build-aaaaaaaaaaaa", date(2026, 1, 5), "first design"),
    Revision("build-bbbbbbbbbbbb", date(2026, 2, 6), "second design"),
    Revision(BUILD_ID, date(2026, 3, 7), "this build"),
    Revision("build-cccccccccccc", date(2026, 4, 8), "a later build"),
)


def drawing(
    backend: str = DEFAULT_BACKEND, parts: list[str] | None = None, history=HISTORY
) -> Path:
    result, code, params, report = built(backend)
    out = Path(tempfile.mkdtemp(prefix="drawing-"))
    files = export_build(
        board(), STANDARDS, result, code, params, out, report, None, ["pdf"], parts, history
    )
    (file,) = files
    assert file.name == FILE_NAMES["pdf"][0]
    return file.path


@cache
def sheets(backend: str = DEFAULT_BACKEND) -> list[list[str]]:
    """Each page's text lines."""
    reader = pypdf.PdfReader(drawing(backend))
    return [[line.strip() for line in p.extract_text().splitlines()] for p in reader.pages]


def sheet_of(part: str, backend: str = DEFAULT_BACKEND) -> list[str]:
    (lines,) = [s for s in sheets(backend) if f"Part {part}" in s]
    return lines


def stated(lines: list[str]) -> dict[str, float]:
    """The key dimensions a sheet states: `label value` lines with a number at the end."""
    out = {}
    for line in lines:
        label, _, value = line.rpartition(" ")
        try:
            out[label] = float(value)
        except ValueError:
            pass
    return out


# --- Each part exports a PDF with four views, key dimensions, a title block, revisions --------


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_every_part_has_a_sheet_with_four_views_dimensions_title_block_and_revisions(
    backend: str,
) -> None:
    result, *_ = built(backend)
    assert len(sheets(backend)) == len(result.parts)
    for part in result.parts:
        lines = sheet_of(part, backend)
        for title, _ in DRAWING_VIEWS.values():
            assert title in lines, (part, title)
        assert "Key dimensions (mm)" in lines
        dims = stated(lines)
        for label, _ in key_dimensions(result, part):
            assert label in dims, (part, label)
        # Title block: part name, material, revision, date and units.
        assert f"Material {result.materials[part]}" in lines
        assert f"Revision {revision_letter(2)}" in lines
        assert f"Date {HISTORY[2].date.isoformat()}" in lines
        assert "Units mm" in lines
        assert "Revisions" in lines


def test_the_key_dimensions_cover_wall_openings_and_bosses() -> None:
    result, *_ = built()
    for part in result.parts:
        labels = {label for label, _ in key_dimensions(result, part)}
        assert {"Overall length (X)", "Overall width (Y)", "Overall height (Z)"} <= labels
        assert ("Wall" in labels) == (result.kind(part) == ENCLOSURE), part
        for tag in result.tags:
            feature = result.feature(tag)
            if feature.part == part and tag.startswith(("opening_", "boss_")):
                for what in ("centre X", "centre Y", "size X", "size Y"):
                    assert f"{tag} {what}" in labels, (part, tag)
    tags = {t for p in result.parts for t, _ in key_dimensions(result, p)}
    assert any(t.startswith("opening_") for t in tags)
    assert any(t.startswith("boss_") for t in tags)


def test_each_part_exports_its_own_pdf() -> None:
    result, *_ = built()
    for part in result.parts:
        reader = pypdf.PdfReader(drawing(parts=[part]))
        assert len(reader.pages) == 1
        assert f"Part {part}" in reader.pages[0].extract_text()


def test_the_build_note_lists_the_drawing() -> None:
    result, code, params, report = built()
    out = Path(tempfile.mkdtemp(prefix="drawing-"))
    export_build(board(), STANDARDS, result, code, params, out, report, None, ["pdf", "build_note"])
    note = (out / FILE_NAMES["build_note"][0]).read_text()
    (row,) = [line for line in note.splitlines() if f"| {FILE_NAMES['pdf'][0]} |" in line]
    for part in result.parts:
        assert part in row


# --- Dimensions match the model within 0.01 mm -----------------------------------------------


def _box(lo, hi):
    return occ.prism([(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])], lo[2], hi[2])


def measured_wall(shape) -> float:
    """The thinnest of the four side walls, cut out at half height through the middle of the
    plan with a thin slab (exact boolean): the outermost piece of each slab's width."""
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(shape)
    cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
    e = 0.01
    walls = []
    for axis in (0, 1):
        if axis == 0:
            slab = _box((x0 - 1, cy - e, cz - e), (x1 + 1, cy + e, cz + e))
        else:
            slab = _box((cx - e, y0 - 1, cz - e), (cx + e, y1 + 1, cz + e))
        pieces = [occ.bbox(s) for s in occ.solids(occ.common(shape, slab))]
        first = min(pieces, key=lambda b: b[0][axis])
        last = max(pieces, key=lambda b: b[1][axis])
        walls += [first[1][axis] - first[0][axis], last[1][axis] - last[0][axis]]
    return min(walls)


def centroid(shape) -> tuple[float, float, float]:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    c = props.CentreOfMass()
    return c.X(), c.Y(), c.Z()


def model_dimensions(result: BuildResult, part: str) -> dict[str, float]:
    """The key dimensions measured on the built solids, independently of the exporter."""
    shape = result.parts[part]
    lo, hi = occ.bbox(shape)
    dims = {
        "Overall length (X)": hi[0] - lo[0],
        "Overall width (Y)": hi[1] - lo[1],
        "Overall height (Z)": hi[2] - lo[2],
    }
    if result.kind(part) == ENCLOSURE:
        dims["Wall"] = measured_wall(shape)
    void = occ.cut(_box(lo, hi), shape)  # where the part is not, inside its bounding box
    for tag in result.tags:
        feature = result.feature(tag)
        if feature.part != part:
            continue
        if tag.startswith("boss_"):
            (tool,) = feature.tools
            boss = occ.common(tool, shape)
            (bx0, by0, _), (bx1, by1, _) = occ.bbox(boss)
            cx, cy, _ = centroid(boss)  # an axisymmetric boss: its centroid is on its axis
        elif tag.startswith("opening_"):
            # The hole through the material: the tool where the part is not, within the walls
            # the cut left (the tool runs on into the cavity beyond them).
            (tool,) = feature.tools
            walls = [occ.bbox(f) for p, f in result.faces_of(tag) if p == part]
            w0 = [min(b[0][i] for b in walls) for i in range(3)]
            w1 = [max(b[1][i] for b in walls) for i in range(3)]
            hole = occ.common(occ.common(tool, void), _box(w0, w1))
            assert occ.volume(hole) > 0, tag
            (bx0, by0, _), (bx1, by1, _) = occ.bbox(hole)
            cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        else:
            continue
        dims |= {
            f"{tag} centre X": cx,
            f"{tag} centre Y": cy,
            f"{tag} size X": bx1 - bx0,
            f"{tag} size Y": by1 - by0,
        }
    return dims


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_dimensions_match_the_model_within_a_hundredth(backend: str) -> None:
    result, *_ = built(backend)
    for part in result.parts:
        model = model_dimensions(result, part)
        exported = dict(key_dimensions(result, part))
        assert set(exported) == set(model), part
        on_sheet = stated(sheet_of(part, backend))
        for label, value in model.items():
            assert abs(exported[label] - value) <= TOLERANCE, (part, label, exported[label], value)
            assert abs(on_sheet[label] - value) <= TOLERANCE, (part, label, on_sheet[label], value)


# --- The revision table lists the builds from history ----------------------------------------


REVISION_ROW = re.compile(r"^[A-Z]+ \d{4}-\d{2}-\d{2} build-\S+ ")


def revision_rows(lines: list[str]) -> list[str]:
    """The revision table's rows: `<letter> <date> <build> <description>`."""
    return [line for line in lines if REVISION_ROW.match(line)]


def test_the_revision_table_lists_the_history_up_to_this_build() -> None:
    for lines in sheets():
        rows = revision_rows(lines)
        expected = HISTORY[: [r.build_id for r in HISTORY].index(BUILD_ID) + 1]
        assert rows == [
            f"{revision_letter(i)} {r.date.isoformat()} {r.build_id} {r.description}"
            for i, r in enumerate(expected)
        ]


def test_without_history_the_build_is_the_first_revision() -> None:
    text = pypdf.PdfReader(drawing(history=None)).pages[0].extract_text()
    lines = [line.strip() for line in text.splitlines()]
    (row,) = revision_rows(lines)
    assert row.startswith(f"A {date.today().isoformat()} {BUILD_ID} ")
    assert "Revision A" in lines


def test_revision_letters() -> None:
    assert [revision_letter(i) for i in (0, 1, 25, 26, 27)] == ["A", "B", "Z", "AA", "AB"]


def test_the_build_store_history_feeds_the_revision_table(tmp_path) -> None:
    """Builds stored with their parents (`app.builds`): the drawing of the latest lists it and
    every ancestor, oldest first, dated when each was built."""
    start = datetime(2026, 5, 1, tzinfo=UTC)
    times = iter(start + timedelta(days=i) for i in range(10))
    store = BuildStore(tmp_path, clock=lambda: next(times))
    result, code, params, report = built()
    ref = BoardRef(name=board().name, revision=board().revision)
    parent = None
    chain = []
    for _ in range(3):
        rec = store.save_result(
            result, code=code, parameters=params, board=ref, checks=report, parent=parent
        )
        chain.append(rec)
        parent = rec.id
    store.save_result(result, code=code, parameters=params, board=ref)  # unrelated build
    history = store_revisions(store, chain[-1].id)
    assert [r.build_id for r in history] == [b.id for b in chain]
    assert [r.date for r in history] == [b.created_at.date() for b in chain]
    out = Path(tempfile.mkdtemp(prefix="drawing-"))
    export_build(
        board(),
        STANDARDS,
        result,
        code,
        params,
        out,
        {**report, "build_id": chain[-1].id},
        None,
        ["pdf"],
        history=history,
    )
    text = pypdf.PdfReader(out / FILE_NAMES["pdf"][0]).pages[0].extract_text()
    rows = revision_rows([line.strip() for line in text.splitlines()])
    assert [row.split(" ")[2] for row in rows] == [b.id for b in chain]
