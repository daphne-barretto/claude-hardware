"""Differences between two board revisions that affect the enclosure.

A difference affects the enclosure when it changes something the enclosure is built around:

- the board outline or thickness,
- a mounting hole (added, removed, moved or resized),
- an opening the enclosure must provide (sensor window, vent, button, antenna keep-out):
  added, removed, moved, rotated or resized,
- the tallest component on either side of the board (the enclosure's inner clearance).

Positions closer than `POSITION_TOLERANCE` and angles closer than `ROTATION_TOLERANCE` count
as unchanged. Openings are rectangles, so their rotation is compared modulo 180 degrees.

`demos/<board>/CHANGES.md` must list exactly these differences; print the table rows with:

    cd backend && .venv/bin/python -m app.boards.changes ../boards/OLD.json ../boards/NEW.json
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from app.boards.schema import Board, Opening, Point, load_board

POSITION_TOLERANCE = 0.25  # mm, below FDM print tolerance
ROTATION_TOLERANCE = 1.0  # degrees

NONE = "-"


@dataclass(frozen=True)
class Change:
    component: str
    role: str
    item: str
    change: str
    old_position: str = NONE
    old_rotation: str = NONE
    new_position: str = NONE
    new_rotation: str = NONE

    def key(self) -> tuple[str, ...]:
        return (
            self.component,
            self.role,
            self.item,
            self.change,
            self.old_position,
            self.old_rotation,
            self.new_position,
            self.new_rotation,
        )


def fmt_point(p: Point) -> str:
    return f"({p[0]:.2f}, {p[1]:.2f})"


def fmt_angle(a: float) -> str:
    return f"{a:g}°"


def _moved(a: Point, b: Point) -> bool:
    return math.dist(a, b) > POSITION_TOLERANCE


def _angle_delta(a: float, b: float, period: float) -> float:
    d = (b - a) % period
    return min(d, period - d)


def _resized(a: Point, b: Point) -> bool:
    return any(abs(x - y) > POSITION_TOLERANCE for x, y in zip(a, b, strict=True))


def _join(parts: list[str]) -> str:
    return " and ".join(parts)


def _opening_changes(old: Board, new: Board) -> list[Change]:
    olds = {o.id: o for o in old.openings}
    news = {o.id: o for o in new.openings}
    changes = []

    def role(board: Board, o: Opening) -> str:
        return board.component(o.component).role or ""

    for oid in sorted(olds.keys() | news.keys()):
        a, b = olds.get(oid), news.get(oid)
        if a is None and b is not None:
            changes.append(
                Change(
                    b.component,
                    role(new, b),
                    b.kind,
                    "added",
                    new_position=fmt_point(b.position),
                    new_rotation=fmt_angle(b.rotation),
                )
            )
        elif b is None and a is not None:
            changes.append(
                Change(
                    a.component,
                    role(old, a),
                    a.kind,
                    "removed",
                    old_position=fmt_point(a.position),
                    old_rotation=fmt_angle(a.rotation),
                )
            )
        elif a is not None and b is not None:
            parts = []
            if _moved(a.position, b.position):
                parts.append("moved")
            if _angle_delta(a.rotation, b.rotation, 180) > ROTATION_TOLERANCE:
                parts.append("rotated")
            if _resized(a.size, b.size) or a.side != b.side:
                parts.append("resized")
            if parts:
                changes.append(
                    Change(
                        b.component,
                        role(new, b),
                        b.kind,
                        _join(parts),
                        fmt_point(a.position),
                        fmt_angle(a.rotation),
                        fmt_point(b.position),
                        fmt_angle(b.rotation),
                    )
                )
    return changes


def _outline_changes(old: Board, new: Board) -> list[Change]:
    changes = []
    a, b = old.pcb.outline, new.pcb.outline
    # Same vertices, wherever each loop starts.
    if any(min(math.dist(p, q) for q in b) > POSITION_TOLERANCE for p in a) or any(
        min(math.dist(p, q) for q in a) > POSITION_TOLERANCE for p in b
    ):
        changes.append(Change("board", "outline", "outline", "reshaped"))
    if abs(old.pcb.thickness - new.pcb.thickness) > 1e-6:
        changes.append(
            Change(
                "board",
                "thickness",
                "thickness",
                "changed",
                f"{old.pcb.thickness:g} mm",
                NONE,
                f"{new.pcb.thickness:g} mm",
                NONE,
            )
        )
    return changes


def _hole_changes(old: Board, new: Board) -> list[Change]:
    changes = []
    unmatched = list(new.mounting_holes)
    for h in old.mounting_holes:
        match = next((n for n in unmatched if not _moved(h.position, n.position)), None)
        if match is None:
            changes.append(
                Change(h.id, "mounting hole", "hole", "removed", old_position=fmt_point(h.position))
            )
            continue
        unmatched.remove(match)
        if abs(h.diameter - match.diameter) > POSITION_TOLERANCE:
            changes.append(
                Change(
                    h.id,
                    "mounting hole",
                    "hole",
                    "resized",
                    fmt_point(h.position),
                    NONE,
                    fmt_point(match.position),
                    NONE,
                )
            )
    for n in unmatched:
        changes.append(
            Change(n.id, "mounting hole", "hole", "added", new_position=fmt_point(n.position))
        )
    return changes


def _envelope_changes(old: Board, new: Board) -> list[Change]:
    changes = []
    for side in ("top", "bottom"):
        a = max((c.height for c in old.components if c.side == side), default=0.0)
        b = max((c.height for c in new.components if c.side == side), default=0.0)
        if abs(a - b) > 1e-6:
            changes.append(
                Change(
                    f"{side} side",
                    "tallest component",
                    "clearance",
                    "taller" if b > a else "shorter",
                    f"{a:g} mm",
                    NONE,
                    f"{b:g} mm",
                    NONE,
                )
            )
    return changes


def enclosure_changes(old: Board, new: Board) -> list[Change]:
    return (
        _outline_changes(old, new)
        + _hole_changes(old, new)
        + _envelope_changes(old, new)
        + _opening_changes(old, new)
    )


# --- demos/<board>/CHANGES.md ------------------------------------------------------------------

COLUMNS = (
    "Component",
    "Role",
    "Opening",
    "Change",
    "Old position (mm)",
    "Old rotation",
    "New position (mm)",
    "New rotation",
    "What it breaks",
)


@dataclass(frozen=True)
class ChangesDoc:
    old_file: str
    new_file: str
    rows: list[dict[str, str]]


def parse_changes_md(text: str) -> ChangesDoc:
    """Read the revisions compared and the differences table from `demos/<board>/CHANGES.md`."""
    m = re.search(r"From `([^`]+\.json)` to `([^`]+\.json)`", text)
    if m is None:
        raise ValueError("CHANGES.md must name the files compared: From `a.json` to `b.json`")
    rows = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if tuple(cells) == COLUMNS:
                header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(dict(zip(header, cells, strict=True)))
    if header is None:
        raise ValueError(f"CHANGES.md has no table with columns {COLUMNS}")
    return ChangesDoc(m.group(1), m.group(2), rows)


# The section of a changes file for its maintainers (how the file is kept and its history). It
# runs to the end of the file, and the agent never sees it (`agent_text`).
MAINTAINER_NOTES = "## Maintainer notes"


def agent_text(text: str) -> str:
    """A changes file as Claude reads it: the differences and the facts checked, without the
    maintainer notes section."""
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines) if line.strip() == MAINTAINER_NOTES), len(lines))
    return "\n".join(lines[:end]).rstrip() + "\n"


def row_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row[c].strip("`") for c in COLUMNS[:-1])


def table_rows(changes: list[Change]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for c in changes:
        cells = list(c.key())
        if c.item not in ("outline", "thickness", "clearance"):
            cells[0] = f"`{cells[0]}`"  # a reference designator or hole id
        cells[2] = f"`{cells[2]}`"
        lines.append("| " + " | ".join(cells) + " | TODO |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    old, new = (load_board(Path(p)) for p in argv)
    print(table_rows(enclosure_changes(old, new)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
