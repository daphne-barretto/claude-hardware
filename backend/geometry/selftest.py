"""Generate the enclosure for every board revision and export STEP.

Run inside the Docker image (`python -m geometry.selftest`) to prove the image carries the board
data, the standards and a working geometry kernel. Exits non-zero if any revision fails.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from app.boards import BOARDS_DIR, load_board
from app.standards import load_standards


def board_files(boards_dir: Path = BOARDS_DIR) -> list[Path]:
    """Every board revision in `demos/<board>/` (one JSON file each, see
    `backend/app/boards/SCHEMA.md`)."""
    return sorted(boards_dir.glob("*.json"))


def run(out_dir: Path, boards_dir: Path = BOARDS_DIR) -> list[Path]:
    """Build and export each revision's enclosure into `out_dir`; return the STEP paths."""
    from build123d import import_step

    from geometry import generate_enclosure

    files = board_files(boards_dir)
    if not files:
        raise RuntimeError(f"no board revisions in {boards_dir}")
    standards = load_standards()
    exported = []
    for file in files:
        result = generate_enclosure(load_board(file), standards)
        path = result.export_step(out_dir / f"{file.stem}-enclosure.step")
        if not import_step(path).solids():
            raise RuntimeError(f"{path} has no solids")
        exported.append(path)
    return exported


def main() -> int:
    print(f"boards: {BOARDS_DIR}")
    with tempfile.TemporaryDirectory() as tmp:
        for path in run(Path(tmp)):
            print(f"ok {path.name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
