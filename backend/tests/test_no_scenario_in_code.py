"""No application code names the example's board: the board revision is data in `demos/b-parasite/`,
`standards.yaml`, `fixtures/` and the examples list, never a code path. Swapping the example
means changing only those files."""

from __future__ import annotations

from app.boards import BOARDS_DIR, load_board
from app.contracts.common import REPO_ROOT

# Application code: everything that runs, outside the data and the tests.
CODE_DIRS = ["backend/app", "backend/geometry", "frontend/src", "scripts"]
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".md", ".json", ".yaml", ".sh"}


def board_names() -> set[str]:
    return {load_board(path).name for path in BOARDS_DIR.glob("*.json")}


def code_files() -> list:
    files = []
    for directory in CODE_DIRS:
        root = REPO_ROOT / directory
        if root.exists():
            files += [p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES]
    return files


def test_there_is_code_and_a_board_to_look_for() -> None:
    assert board_names() and len(code_files()) > 50


def test_no_application_code_names_the_example_board() -> None:
    names = {n.lower() for n in board_names()}
    found = [
        f"{path.relative_to(REPO_ROOT)}: {name}"
        for path in code_files()
        for name in names
        if name in path.read_text(errors="ignore").lower()
    ]
    assert found == []
