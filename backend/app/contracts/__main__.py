"""Write the generated contract files in `contracts/` and their frontend copies
(or with `--check`, fail if any is stale)."""

import json
import sys

from app.contracts.common import CONTRACTS_DIR, REPO_ROOT
from app.contracts.schemas import frontend_copies, generated


def main(argv: list[str]) -> int:
    files = {CONTRACTS_DIR / n: t for n, t in generated().items()} | frontend_copies()
    if "--check" in argv:
        stale = [
            p
            for p, t in files.items()
            if not p.exists() or json.loads(p.read_text()) != json.loads(t)
        ]
        for path in stale:
            print(f"out of date: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        if stale:
            print("run: cd backend && .venv/bin/python -m app.contracts", file=sys.stderr)
        return 1 if stale else 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"wrote {len(files)} contract files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
