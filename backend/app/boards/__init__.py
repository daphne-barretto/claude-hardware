"""Board revisions as data: schema, KiCad converter and enclosure-affecting differences.

The file format is in `SCHEMA.md`, next to this loader. The boards themselves are data: by default
the example board's folder in `demos/` (the one with an `examples.yaml`; `DEMOS_DIR` overrides
where the demos are). `BOARDS_DIR` overrides the folder itself.
"""

import os
from pathlib import Path

from app.boards.schema import Board, load_board

REPO_DEMOS_DIR = Path(__file__).resolve().parents[3] / "demos"


def example_board_dir(demos_dir: Path) -> Path:
    """The folder of `demos_dir` that holds the example board (its `examples.yaml`); the first
    one by name if there are several, `demos_dir` itself if there is none."""
    found = sorted(p.parent for p in demos_dir.glob("*/examples.yaml"))
    return found[0] if found else demos_dir


REPO_BOARDS_DIR = example_board_dir(REPO_DEMOS_DIR)
BOARDS_DIR = Path(
    os.environ.get("BOARDS_DIR")
    or example_board_dir(Path(os.environ.get("DEMOS_DIR") or REPO_DEMOS_DIR))
)


def find_board(name: str, revision: str, boards_dir: Path | None = None) -> Board:
    """The board revision with this `name` and `revision`, from `boards_dir` (`BOARDS_DIR`).
    Raises `LookupError` if there is none."""
    for path in sorted((boards_dir or BOARDS_DIR).glob("*.json")):
        board = load_board(path)
        if board.name == name and board.revision == revision:
            return board
    raise LookupError(f"no board {name} {revision}")


__all__ = ["BOARDS_DIR", "Board", "example_board_dir", "find_board", "load_board"]
