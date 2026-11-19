"""Hand edits to the generator code, shared by the mock API (`app.mock_api`) and the real session
API (`app.session_api`): checking edited code before it is built, the source document of the
rebuild, and the note that gives the edit to Claude on its next turn.

Checking parses the code and never runs it; the build (in the geometry sandbox) runs it.
"""

from __future__ import annotations

import ast
import difflib
from typing import Any

from fastapi.responses import JSONResponse

from app.contracts.session import CodeError, ErrorBody

# Refused code: 400 with a `CodeError` (the line, when known, for the editor to point at).
CODE_ERROR = 400
CODE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    CODE_ERROR: {"model": CodeError},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
}


def code_error(code: str, filename: str = "enclosure.py") -> CodeError | None:
    """Why `code` can't be built, or None: it must parse and define `build(params)` at the top
    level, as the geometry interface calls it."""
    try:
        tree = ast.parse(code, filename)
    except SyntaxError as error:
        return CodeError(
            detail=f"Syntax error: {error.msg}",
            line=error.lineno if error.lineno and error.lineno >= 1 else None,
            column=error.offset if error.offset and error.offset >= 1 else None,
        )
    if not any(isinstance(n, ast.FunctionDef) and n.name == "build" for n in tree.body):
        return CodeError(detail="The code must define build(params).")
    return None


def build_error(error: dict | None, code: str) -> CodeError:
    """A failed build (the sandbox's `{kind, message, line}`) as a `CodeError`."""
    error = error or {}
    line = error.get("line")
    lines = len(code.splitlines())
    return CodeError(
        detail=f"The build failed: {error.get('message') or error.get('kind') or 'unknown error'}",
        line=line if isinstance(line, int) and 1 <= line <= lines else None,
    )


def code_error_response(error: CodeError) -> JSONResponse:
    return JSONResponse(error.model_dump(exclude_none=True), status_code=CODE_ERROR)


def edited_source(source: dict, build_id: str, code: str) -> dict:
    """The source document of a build made from hand-edited `code`, from the `BuildSource` of
    the build it was edited from. Feature lines past the end of the new code are dropped."""
    count = len(code.splitlines())
    features = [f for f in source.get("features", []) if f["lines"][1] <= count]
    return {**source, "build_id": build_id, "code": code, "features": features}


def code_edit_note(before: str, after: str, filename: str = "enclosure.py") -> str:
    """The hand edit as Claude gets it on its next turn: a unified diff of the generator code."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"a/{filename}",
            f"b/{filename}",
        )
    )
    return (
        "The user edited the generator code by hand since your last turn. The design's code "
        "is now the edited code (read_spec shows it). Keep the edit: don't undo it unless you "
        "ask the user (ask_user) first. The change:\n\n```diff\n" + diff.rstrip("\n") + "\n```"
    )
