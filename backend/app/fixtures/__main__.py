"""Write `fixtures/` (or with `--check`, fail if it is out of date)."""

import sys

from app.contracts.common import FIXTURES_DIR
from app.fixtures.compare import stale_files
from app.fixtures.generate import generate, write


def main(argv: list[str]) -> int:
    files = generate()
    if "--check" in argv:
        stale = stale_files(files, FIXTURES_DIR)
        for name in stale:
            print(f"out of date: fixtures/{name}", file=sys.stderr)
        if stale:
            print("run: cd backend && .venv/bin/python -m app.fixtures", file=sys.stderr)
        return 1 if stale else 0
    write(files)
    print(f"wrote {len(files)} files to {FIXTURES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
