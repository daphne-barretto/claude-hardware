"""Runs trusted geometry work in a short-lived subprocess, so the server never loads the CAD kernel.

build123d and OpenCascade take about 430 MB once imported, and the memory stays with the
process that imported them. The server would hold that for its whole life on top of every
build's own copy, so it hands geometry work (checks and analysis of a build, the
existing design's parameters, exports) to `call`, which runs one function in a fresh Python
process inside a build slot (`geometry.slots`) and returns its result. The process exits when
the function returns, and its memory goes with it.

Only our own code runs here: generated build code still runs in the sandbox
(`geometry.sandbox`), and what it returns reaches the worker as BREP bytes. Arguments and
results travel as pickles between two processes running the same code.

Inside a worker, `call` runs the function in-process, so worker code can use it freely.
"""

from __future__ import annotations

import importlib
import json
import os
import pickle
import subprocess
import sys
import traceback
from typing import Any

from geometry.slots import build_slot

DEFAULT_TIMEOUT_S = 600.0
_IN_WORKER = False

_BOOTSTRAP = (
    "import json, sys\n"
    "_header = json.loads(sys.stdin.buffer.readline())\n"
    "sys.path[:] = _header['sys_path']\n"
    "from geometry.worker import main\n"
    "main(_header)\n"
)


class WorkerError(RuntimeError):
    """The worker died, timed out, or raised an exception that couldn't come back as is."""


def _resolve(target: str) -> Any:
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def in_worker() -> bool:
    """Whether this process is a geometry worker (where loading the kernel is fine)."""
    return _IN_WORKER


def call(target: str, *args: Any, timeout_s: float = DEFAULT_TIMEOUT_S, **kwargs: Any) -> Any:
    """`module:function(*args, **kwargs)`, run in a geometry worker. Exceptions it raises are
    raised here, with the worker's traceback as a note."""
    if _IN_WORKER:
        return _resolve(target)(*args, **kwargs)
    header = json.dumps({"sys_path": sys.path, "target": target}).encode() + b"\n"
    payload = pickle.dumps((args, kwargs))
    with build_slot():
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _BOOTSTRAP],
                input=header + payload,
                capture_output=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise WorkerError(f"{target} took longer than {timeout_s:g} s") from None
    try:
        status, value, trace = pickle.loads(proc.stdout)
    except Exception:  # noqa: BLE001 - the worker died before answering
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise WorkerError(
            f"{target} failed (exit code {proc.returncode}): {' '.join(tail)}"
        ) from None
    if status == "ok":
        return value
    value.add_note(f"In the geometry worker:\n{trace}")
    raise value


def main(header: dict[str, Any]) -> None:
    global _IN_WORKER
    _IN_WORKER = True
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # native prints go to stderr, never into the answer
    try:
        args, kwargs = pickle.loads(sys.stdin.buffer.read())
        answer: tuple[str, Any, str] = ("ok", _resolve(header["target"])(*args, **kwargs), "")
    except BaseException as exc:  # noqa: BLE001 - every failure goes back to the caller
        trace = traceback.format_exc()
        try:
            pickle.dumps(exc)
        except Exception:  # noqa: BLE001
            exc = WorkerError(f"{type(exc).__name__}: {exc}")
        answer = ("error", exc, trace)
    try:
        data = pickle.dumps(answer)
    except Exception as exc:  # noqa: BLE001 - an unpicklable result
        data = pickle.dumps(("error", WorkerError(f"the result can't be sent back: {exc}"), ""))
    out.write(data)
    out.flush()
    os._exit(0)  # the kernel's finalizers take time and memory for nothing
