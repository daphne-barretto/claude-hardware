"""`python -m app.telemetry report` (see `docs/architecture.md`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.telemetry.log import telemetry_dir
from app.telemetry.report import load_sessions, metrics, render


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.telemetry")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="the product metrics over every recorded session")
    report.add_argument("--dir", type=Path, help="the logs (default: TELEMETRY_DIR)")
    report.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = parser.parse_args(argv)

    directory = args.dir or telemetry_dir()
    if directory is None:
        print("telemetry is off (TELEMETRY_DIR=off); give --dir", file=sys.stderr)
        return 2
    m = metrics(load_sessions(directory))
    print(json.dumps(m, indent=2) if args.json else render(m), end="\n" if args.json else "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
