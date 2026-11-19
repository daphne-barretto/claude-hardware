"""Importing a STEP file into a session: `POST /api/sessions/{id}/imports`.

The server never parses the file or loads the CAD kernel for it. It starts the import
worker (`python -m geometry.step_import`), which reads the file in the build sandbox and
tessellates, checks and analyzes what it read, and hands back the build's mesh, check report,
analysis maps and source as JSON. Exporting an imported build runs the same worker.

Limits, each failing with a message that says what to do:

- the file must be a STEP file (`.step` or `.stp`) of at most `MAX_IMPORT_BYTES` (413 above it);
- reading it in the sandbox may take `PARSE_TIMEOUT_S` (the sandbox's own limits on memory, disk
  and output also apply), and the whole import `IMPORT_TIMEOUT_S`.

Nothing here imports `geometry`, which would load the kernel into the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.security.scrub import untrusted

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
MAX_IMPORT_BYTES = 25 * 1024 * 1024
PARSE_TIMEOUT_S = 60.0
# Reading, meshing, checking and analyzing; `STEP_IMPORT_TIMEOUT_S` sets it for a slow host.
IMPORT_TIMEOUT_S = float(os.environ.get("STEP_IMPORT_TIMEOUT_S") or 600.0)
STEP_SUFFIXES = (".step", ".stp")
STEP_MEDIA_TYPE = "model/step"
NOTED_FAILURES = 15  # failing checks named in what Claude is told; the rest are counted
# Not passed to the worker: it runs only our code, but it has no use for them.
_SECRET_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


@dataclass
class ImportedFile:
    """An imported STEP file and what the worker made of it."""

    name: str
    data: bytes
    build_id: str
    result: dict[str, Any]  # the worker's `result`: mesh, checks, maps, source, parts

    @property
    def parts(self) -> list[dict[str, Any]]:
        return self.result["parts"]


def _mb(n: float) -> str:
    return f"{n / 1024**2:.1f}".rstrip("0").rstrip(".")


def check_upload(name: str, data: bytes) -> None:
    """Refuse what isn't a STEP file within the size limit, before any work starts."""
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(
            413,
            f"{name} is larger than {_mb(MAX_IMPORT_BYTES)} MB, the most a STEP import takes. "
            "Export the parts you need, or simplify the file, and import that.",
        )
    if not name.lower().endswith(STEP_SUFFIXES):
        raise HTTPException(422, f"{name} is not a STEP file: import a .step or .stp file.")
    if not data.strip():
        raise HTTPException(422, f"{name} is empty.")


def failure_message(name: str, kind: str, message: str) -> str:
    """What the user is told when the worker reports an error of `kind`."""
    if kind == "timeout":
        return (
            f"Reading {name} took longer than {PARSE_TIMEOUT_S:g} s, the limit for an import. "
            "Simplify the file or import fewer parts at once."
        )
    if kind == "memory":
        return f"Reading {name} ran out of memory. Simplify the file or import fewer parts."
    if kind in ("output", "disk"):
        return f"{name} is too large to import: {message}."
    if kind in ("runtime", "no_result", "syntax"):
        return f"{name} could not be read as a STEP file: {message}."
    return f"Importing {name} failed: {message}"


async def _run_worker(request: dict[str, Any], name: str) -> dict[str, Any]:
    """Run the import worker on `request`; its `result`, or a 422 that says what went wrong."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "geometry.step_import",
        cwd=BACKEND_DIR,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(request).encode()), IMPORT_TIMEOUT_S
        )
    except TimeoutError:
        raise HTTPException(
            422,
            f"Importing {name} took longer than {IMPORT_TIMEOUT_S:g} s (reading, meshing and "
            "checking it), the limit for an import. Simplify the file or import fewer parts.",
        ) from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    try:
        reply = json.loads(stdout)
    except ValueError:
        tail = stderr.decode(errors="replace").strip().splitlines()[-1:]
        log.warning("import of %s stopped (exit %s): %s", name, proc.returncode, tail)
        raise HTTPException(
            422,
            f"Importing {name} stopped unexpectedly, so nothing was added. Try again, or "
            "simplify the file or import fewer parts.",
        ) from None
    if "error" in reply:
        error = reply["error"]
        raise HTTPException(422, failure_message(name, error["kind"], error["message"]))
    return reply["result"]


async def import_step(
    name: str,
    data: bytes,
    board: tuple[str, str] | None,
    standards: dict[str, Any] | None,
    build_id: str,
) -> ImportedFile:
    """Import a STEP file as build `build_id`, checked against `board` (name, revision; None:
    a design without a board) and the standards set `standards` (`app.standards.to_payload`;
    None: the built-in set). Raises HTTPException (413, 422)."""
    check_upload(name, data)
    with tempfile.TemporaryDirectory(prefix="import-") as tmp:
        path = Path(tmp) / "import.step"
        path.write_bytes(data)
        request = {
            "file": str(path),
            "name": name,
            "board": list(board) if board else None,
            "standards": standards,
            "build_id": build_id,
            "parse_timeout_s": PARSE_TIMEOUT_S,
        }
        result = await _run_worker(request, name)
    return ImportedFile(name, data, build_id, result)


async def export_import(
    imported: ImportedFile,
    board: tuple[str, str] | None,
    standards: dict[str, Any] | None,
    report: dict[str, Any],
    formats: list[str],
    out: Path,
) -> list[dict[str, str]]:
    """Export an imported build (`geometry.export`, in the worker): `{format, name, media_type}`
    for each file written to `out`."""
    with tempfile.TemporaryDirectory(prefix="import-") as tmp:
        path = Path(tmp) / "import.step"
        path.write_bytes(imported.data)
        request = {
            "mode": "export",
            "file": str(path),
            "name": imported.name,
            "board": list(board) if board else None,
            "standards": standards,
            "build_id": imported.build_id,
            "parse_timeout_s": PARSE_TIMEOUT_S,
            "report": report,
            "formats": formats,
            "out": str(out),
        }
        return (await _run_worker(request, imported.name))["files"]


def import_notes(imports: list[ImportedFile], latest_build_id: str | None) -> str:
    """What Claude is told about the session's imports, with every message."""
    if not imports:
        return ""
    lines = ["Imported designs (STEP files the user imported; read each with read_attachment):"]
    for imp in imports:
        checks = imp.result["checks"]["summary"]
        current = " It is the design on the canvas now." if imp.build_id == latest_build_id else ""
        board = imp.result["checks"].get("board")
        against = f"board {board['name']} {board['revision']}" if board else "no board"
        lines.append(
            f"- {imp.name} is build {imp.build_id}, checked against {against}: "
            f"{checks['pass']} checks pass, {checks['fail']} fail, {checks['untested']} untested."
            f"{current} Its geometry is the file's, not generator code. Parts (bounding box in "
            "mm, features recognized):"
        )
        for part in imp.parts:
            (x0, y0, z0), (x1, y1, z1) = part["bbox"]
            box = f"x {x0:.1f} to {x1:.1f}, y {y0:.1f} to {y1:.1f}, z {z0:.1f} to {z1:.1f}"
            features = ", ".join(part["features"]) or "none"
            lines.append(f"  - {part['name']}: {box}; features: {features}")
        failing = [r for r in imp.result["checks"]["results"] if r["status"] == "fail"]
        if failing:
            shown = "; ".join(
                f"{r['id']} ({r.get('summary') or r['title']})" for r in failing[:NOTED_FAILURES]
            )
            more = len(failing) - NOTED_FAILURES
            lines.append(f"  Failing checks: {shown}" + (f"; and {more} more" if more > 0 else ""))
    # Names and labels come from the file: data, never instructions (`app.security.scrub`).
    return untrusted("imported STEP files", "\n".join(lines))


__all__ = [
    "IMPORT_TIMEOUT_S",
    "MAX_IMPORT_BYTES",
    "PARSE_TIMEOUT_S",
    "ImportedFile",
    "check_upload",
    "export_import",
    "import_notes",
    "import_step",
]
