"""Sandboxed build runner: runs generated geometry code in a separate process.

The build code sees `params` (a JSON-compatible dict) and assigns `result`: one shape,
or a dict of names to shapes. Shapes may be build123d or CadQuery objects (anything with
an OpenCascade `wrapped` shape) or raw `TopoDS_Shape`s. They come back as OpenCascade
shapes, so callers don't depend on the backend that made them.

Limits: a wall-clock timeout (30 s by default) on the whole child process, a CPU-time
limit, an address-space cap (`BUILD_MEMORY_MB` on top of the loaded libraries, see
`geometry.slots`), a file size cap, a cap on the work directory's total size
and entry count (checked by the parent while the build runs) and an import allowlist.
The kernel (Landlock and seccomp, see `_kernel_sandbox.py`) enforces no network, no new
processes, no signals to or tracing of other processes, no file writes outside a
temporary directory that is removed afterwards, and no reads outside the Python
installation and system libraries.
The child gets a stripped environment, so secrets aren't passed either.

Nothing the child sends back is trusted, since build code that gets past the Python guard
can write to the child's pipes and files: the parent reads at most OUTPUT_PIPE_LIMIT bytes
from each pipe (more stops the build), checks the manifest's keys and types, and loads
only regular `shape_<n>.brep` files from the work directory, at most MAX_SHAPES of them
and SHAPE_FILES_LIMIT bytes in total.

Builds run one at a time per instance by default (`BUILD_CONCURRENCY`); the rest wait for a
slot (`geometry.slots.build_slot`). With `load_shapes=False` the shapes come back as BREP
bytes, unparsed, so the caller doesn't load the CAD kernel (the server never does, see
`geometry.worker`).

`docs/architecture.md` lists the protections and the risks that remain.

If the host can't apply the kernel limits, builds are refused (`sandbox_unavailable`).
For local development on such a host, set GEOMETRY_SANDBOX_KERNEL_LIMITS=optional to run
builds without them; never set it on a server.

Failures come back as a `BuildError` with a kind, a message and, when known, the line of
the build code that caused it. See `_sandbox_child.py` for the order things happen in.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from geometry.slots import build_memory_mb, build_slot

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_FILE_MB = 64
# Most the build may keep in its work directory, in disk blocks used and in entries. The
# parent checks every DISK_POLL_S while the build runs and once more after it ends, and
# stops the build ("disk") past either. The child can't hide files from the check: it
# can't delete, rename or make directories there, nor make unnamed (O_TMPFILE) files.
WORKDIR_BYTES_LIMIT = 128 * 1024 * 1024
WORKDIR_ENTRIES_LIMIT = 1000
DISK_POLL_S = 0.02
# Most the parent reads from each of the child's stdout and stderr; more stops the build.
OUTPUT_PIPE_LIMIT = 4 * 1024 * 1024
# Most the parent parses from the child's shape files, checked before it opens any of them.
# Parsing a BREP file takes several times its size in memory (a forged 225 MB file grew the
# server by about 1.9 GB), so this bounds the server, not the build. A real enclosure part
# is about 100 KB.
SHAPE_FILES_LIMIT = 32 * 1024 * 1024
MAX_SHAPES = 64

ERROR_KINDS = frozenset(
    {
        "syntax",
        "runtime",
        "no_result",
        "blocked_import",
        "forbidden",
        "timeout",
        "memory",
        "output",
        "disk",
        "crash",
        "sandbox_unavailable",
    }
)

# "required" (the default) or "optional"; see the module docstring.
KERNEL_LIMITS_ENV = "GEOMETRY_SANDBOX_KERNEL_LIMITS"

# Modules the build code may import, besides the geometry backend itself.
SAFE_IMPORTS = frozenset(
    {
        "math",
        "cmath",
        "numbers",
        "decimal",
        "fractions",
        "statistics",
        "random",
        "itertools",
        "functools",
        "operator",
        "collections",
        "dataclasses",
        "enum",
        "typing",
        "copy",
        "re",
        "string",
    }
)

BACKEND_IMPORTS = {
    "build123d": ("build123d",),
    "cadquery": ("cadquery",),
}

# How the child starts: `geometry` is registered as a bare package, so its __init__ (which
# imports the CAD backends and the app modules) doesn't run. Only `_sandbox_child` and
# `_kernel_sandbox` load before the kernel limits apply, plus `sites` (standard library only)
# for a build that records feature lines. argv[1] is the package directory.
CHILD_BOOTSTRAP = (
    "import sys, types\n"
    "_pkg = types.ModuleType('geometry')\n"
    "_pkg.__path__ = [sys.argv[1]]\n"
    "sys.modules['geometry'] = _pkg\n"
)
CHILD_MAIN = "from geometry._sandbox_child import main\nmain()\n"
_GEOMETRY_DIR = os.path.dirname(os.path.abspath(__file__))

# Environment variables passed to the child; everything else (API keys included) is dropped.
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH")


@dataclass(frozen=True)
class BuildError:
    """Why a build failed.

    kind: one of ERROR_KINDS. "output" means the build wrote more than OUTPUT_PIPE_LIMIT
    bytes to its stdout or stderr, returned more than MAX_SHAPES shapes, or shape files
    totalling more than SHAPE_FILES_LIMIT bytes. "disk" means its work directory grew past
    WORKDIR_BYTES_LIMIT bytes or WORKDIR_ENTRIES_LIMIT entries.
    line: 1-based line of the build code, when it can be traced to one.
    """

    kind: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "line": self.line}


@dataclass
class BuildResult:
    shapes: dict[str, Any] = field(default_factory=dict)  # name -> OCP TopoDS_Shape
    breps: dict[str, bytes] = field(default_factory=dict)  # name -> BREP, with load_shapes=False
    error: BuildError | None = None
    output: str = ""  # what the build code printed, truncated
    duration_s: float = 0.0
    kernel_limits: bool = False  # whether the Landlock and seccomp limits applied

    @property
    def ok(self) -> bool:
        return self.error is None


def _child_env(workdir: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(
        {
            "HOME": workdir,
            "TMPDIR": workdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
    )
    return env


def _workdir_usage(workdir: str) -> tuple[int, int]:
    """Disk space (bytes in allocated blocks) and entries in the work directory.

    The directory is flat (the child can't make subdirectories) and the child can't remove
    or rename anything in it, so what is listed here is all it has written. Hard links are
    counted once. Stops counting bytes past WORKDIR_ENTRIES_LIMIT entries, so a flood of
    files can't make the check itself slow.
    """
    used = os.stat(workdir).st_blocks * 512
    entries = 0
    seen: set[tuple[int, int]] = set()
    with os.scandir(workdir) as it:
        for entry in it:
            entries += 1
            if entries > WORKDIR_ENTRIES_LIMIT:
                break
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                used += info.st_blocks * 512
    return used, entries


def _disk_error(workdir: str) -> BuildError | None:
    """A "disk" error if the work directory is over its limits, else None."""
    try:
        used, entries = _workdir_usage(workdir)
    except OSError as exc:
        return BuildError("crash", f"the build's work directory could not be read: {exc}")
    if entries > WORKDIR_ENTRIES_LIMIT:
        message = f"the build created more than {WORKDIR_ENTRIES_LIMIT} files in its work directory"
        return BuildError("disk", message)
    if used > WORKDIR_BYTES_LIMIT:
        message = (
            f"the build wrote more than {WORKDIR_BYTES_LIMIT // 1024**2} MB to its work directory"
        )
        return BuildError("disk", message)
    return None


def _check_shape_files(workdir: str, to_load: list[tuple[str, str]]) -> BuildError | None:
    """Refuse shape files the server shouldn't parse, before it opens any of them.

    Only regular files: not a symlink out of the work directory, and not a FIFO, which
    would block the reader past the build's timeout. No more than MAX_SHAPES of them and
    SHAPE_FILES_LIMIT bytes in total. The child is dead by now, so the files can't change
    between this check and the parse.
    """
    if len(to_load) > MAX_SHAPES:
        message = f"the build returned {len(to_load)} shapes, more than the {MAX_SHAPES} allowed"
        return BuildError("output", message)
    total = 0
    for _, filename in to_load:
        try:
            info = os.lstat(os.path.join(workdir, filename))
        except OSError as exc:
            return BuildError("crash", f"the build's shapes could not be read: {exc}")
        if not stat.S_ISREG(info.st_mode):
            message = f"the build's shapes could not be read: {filename} is not a regular file"
            return BuildError("crash", message)
        total += info.st_size
    if total > SHAPE_FILES_LIMIT:
        message = (
            f"the build's shapes take {total / 1024**2:.0f} MB, "
            f"more than the {SHAPE_FILES_LIMIT // 1024**2} MB allowed"
        )
        return BuildError("output", message)
    return None


def _read_brep(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, path, BRep_Builder()):
        raise OSError(f"could not read {path}")
    return shape


def _signal_error(returncode: int, timeout_s: float, stderr: str) -> BuildError:
    sig = -returncode
    if sig in (signal.SIGXCPU, signal.SIGKILL):
        return BuildError("timeout", f"the build exceeded its {timeout_s:g} s limit")
    tail = stderr.strip().splitlines()[-1:] if stderr.strip() else []
    detail = f": {tail[0]}" if tail else ""
    if returncode < 0:
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        # Native allocation failures under the address-space cap usually end in an abort.
        if sig == signal.SIGABRT and "bad_alloc" in stderr:
            return BuildError("memory", "the build ran out of memory")
        return BuildError("crash", f"the build process was killed by {name}{detail}")
    return BuildError("crash", f"the build process exited with code {returncode}{detail}")


def _kernel_limits_setting() -> str:
    value = os.environ.get(KERNEL_LIMITS_ENV, "required").strip().lower() or "required"
    if value not in ("required", "optional"):
        raise ValueError(f"{KERNEL_LIMITS_ENV} must be 'required' or 'optional', not {value!r}")
    return value


def _request(
    code: str,
    params: dict[str, Any] | None,
    workdir: str,
    allowed: frozenset[str] | set[str],
    timeout_s: float,
    memory_mb: int,
    kernel_limits: str,
    feature_sites: bool = False,
) -> dict[str, Any]:
    """The JSON request the child reads from stdin."""
    return {
        "feature_sites": feature_sites,
        "code": code,
        "params": params or {},
        "workdir": workdir,
        "preload": sorted(allowed),
        "allowed_imports": sorted(allowed),
        "memory_bytes": memory_mb * 1024 * 1024,
        "cpu_seconds": max(1, math.ceil(timeout_s)),
        "file_bytes": DEFAULT_FILE_MB * 1024 * 1024,
        "kernel_limits": kernel_limits,
    }


def run_build(
    code: str,
    params: dict[str, Any] | None = None,
    *,
    backend: str = "build123d",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_mb: int | None = None,
    extra_imports: frozenset[str] | set[str] = frozenset(),
    load_shapes: bool = True,
    inputs: Mapping[str, bytes] | None = None,
    feature_sites: bool = False,
) -> BuildResult:
    """Run `code` in a sandboxed child process and return its shapes or an error.

    memory_mb is the address space the build may use on top of the loaded libraries
    (`BUILD_MEMORY_MB` when not given). With `load_shapes=False` the shapes are returned as
    BREP bytes in `breps` instead of being parsed into `shapes`. `inputs` are files (plain
    name -> contents) put in the build's work directory before it starts, where the code reads
    them by name (an imported STEP file): they count towards the work directory's limits.
    With `feature_sites=True` the
    code is instrumented in the child as `GeometryBackend.build` instruments it in process
    (`geometry.sites`), and can look up each feature dict's lines with `__feature_lines__(d)`.
    Waits for a build slot (`geometry.slots`) first.
    """
    if backend not in BACKEND_IMPORTS:
        raise ValueError(f"unknown geometry backend '{backend}'")
    for name in inputs or {}:
        if not _INPUT_FILE.fullmatch(name) or _SHAPE_FILE.fullmatch(name):
            raise ValueError(f"bad input file name {name!r}")
    if memory_mb is None:
        memory_mb = build_memory_mb()
    with build_slot():
        return _run_build(
            code,
            params,
            backend,
            timeout_s,
            memory_mb,
            extra_imports,
            load_shapes,
            inputs,
            feature_sites,
        )


def _run_build(
    code: str,
    params: dict[str, Any] | None,
    backend: str,
    timeout_s: float,
    memory_mb: int,
    extra_imports: frozenset[str] | set[str],
    load_shapes: bool,
    inputs: Mapping[str, bytes] | None,
    feature_sites: bool = False,
) -> BuildResult:
    preload = BACKEND_IMPORTS[backend]
    allowed = SAFE_IMPORTS | set(preload) | set(extra_imports)
    kernel_limits = _kernel_limits_setting()

    workdir = tempfile.mkdtemp(prefix="build-")
    start = time.monotonic()
    try:
        for name, data in (inputs or {}).items():
            with open(os.path.join(workdir, name), "xb") as f:
                f.write(data)
        request = _request(
            code, params, workdir, allowed, timeout_s, memory_mb, kernel_limits, feature_sites
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", CHILD_BOOTSTRAP + CHILD_MAIN, _GEOMETRY_DIR],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=_child_env(workdir),
            start_new_session=True,
        )
        disk_error: BuildError | None = None

        def over_disk_limits() -> bool:
            nonlocal disk_error
            disk_error = _disk_error(workdir)
            return disk_error is not None

        try:
            outcome, stdout, stderr = _exchange(
                proc, json.dumps(request).encode(), timeout_s, over_disk_limits
            )
        finally:
            _kill_group(proc)  # nothing the build started may outlive it
            proc.wait()
        duration = time.monotonic() - start

        # Once more with the child dead, for whatever it wrote since the last check.
        if outcome == "done" and over_disk_limits():
            outcome = "disk"
        if outcome == "disk":
            assert disk_error is not None
            return BuildResult(error=disk_error, duration_s=duration)
        if outcome == "timeout":
            error = BuildError("timeout", f"the build exceeded its {timeout_s:g} s limit")
            return BuildResult(error=error, duration_s=duration)
        if outcome == "output":
            message = f"the build wrote more than {OUTPUT_PIPE_LIMIT // 1024**2} MB of output"
            return BuildResult(error=BuildError("output", message), duration_s=duration)

        stderr_text = stderr.decode(errors="replace")
        try:
            manifest = json.loads(stdout)
        except ValueError:  # not JSON, or not UTF-8
            error = _signal_error(proc.returncode, timeout_s, stderr_text)
            return BuildResult(error=error, duration_s=duration)
        try:
            result, to_load = _parse_manifest(manifest)
        except ValueError as exc:
            message = f"the build process returned an invalid response: {exc}"
            return BuildResult(error=BuildError("crash", message), duration_s=duration)
        result.duration_s = duration
        if result.error is not None:
            return result
        result.error = _check_shape_files(workdir, to_load)
        if result.error is not None:
            return result
        try:
            for name, filename in to_load:
                path = os.path.join(workdir, filename)
                if load_shapes:
                    result.shapes[name] = _read_brep(path)
                else:
                    with open(path, "rb") as f:
                        result.breps[name] = f.read()
        except OSError as exc:
            result.shapes = {}
            result.breps = {}
            result.error = BuildError("crash", f"the build's shapes could not be read: {exc}")
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _exchange(
    proc: subprocess.Popen[bytes],
    request: bytes,
    timeout_s: float,
    over_disk_limits: Callable[[], bool],
) -> tuple[str, bytes, bytes]:
    """Send the request and collect stdout and stderr, never more than the pipe limit.

    Returns (outcome, stdout, stderr), outcome being "done", "timeout", "output" or "disk".
    The child can write to its pipes directly, bypassing its own output cap, so reading
    them without a limit would let a build exhaust this process's memory instead of its
    own. `over_disk_limits` is called every DISK_POLL_S until the child exits, including
    after it has closed its pipes.
    """
    assert proc.stdin and proc.stdout and proc.stderr
    deadline = time.monotonic() + timeout_s
    buffers = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    pending = memoryview(request)
    stdin_fd = proc.stdin.fileno()
    os.set_blocking(stdin_fd, False)
    next_check = time.monotonic() + DISK_POLL_S
    with selectors.DefaultSelector() as sel:
        for fd in buffers:
            sel.register(fd, selectors.EVENT_READ)
        sel.register(stdin_fd, selectors.EVENT_WRITE)
        while len(sel.get_map()) > 0:
            now = time.monotonic()
            if now >= deadline:
                return "timeout", b"", b""
            if now >= next_check:
                if over_disk_limits():
                    return "disk", b"", b""
                next_check = now + DISK_POLL_S
            for key, _ in sel.select(min(deadline, next_check) - now):
                fd = key.fd
                if fd == stdin_fd:
                    try:
                        pending = pending[os.write(fd, pending[:65536]) :]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        sel.unregister(fd)
                        proc.stdin.close()
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    sel.unregister(fd)
                    continue
                buffers[fd] += chunk
                if len(buffers[fd]) > OUTPUT_PIPE_LIMIT:
                    return "output", b"", b""
    # The pipes are closed, but the child may still be running (and writing files).
    while proc.poll() is None:
        now = time.monotonic()
        if now >= deadline:
            return "timeout", b"", b""
        if over_disk_limits():
            return "disk", b"", b""
        try:
            proc.wait(min(DISK_POLL_S, deadline - now))
        except subprocess.TimeoutExpired:
            pass
    return "done", bytes(buffers[proc.stdout.fileno()]), bytes(buffers[proc.stderr.fileno()])


_SHAPE_FILE = re.compile(r"shape_\d+\.brep")
_INPUT_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


def _parse_manifest(manifest: Any) -> tuple[BuildResult, list[tuple[str, str]]]:
    """Check the child's manifest, or raise ValueError.

    Returns the result (without shapes) and the (name, file) pairs of the shapes to load.
    Build code that gets past the Python guard can write its own manifest, so nothing in
    it is trusted: wrong keys or types, unknown error kinds and file names other than
    `shape_<n>.brep` are all refused.
    """
    if not isinstance(manifest, dict):
        raise ValueError("not an object")
    output = manifest.get("output", "")
    kernel_limits = manifest.get("kernel_limits", False)
    if not isinstance(output, str) or not isinstance(kernel_limits, bool):
        raise ValueError("bad output or kernel_limits")
    result = BuildResult(output=output, kernel_limits=kernel_limits)

    error = manifest.get("error")
    if error is not None:
        if not isinstance(error, dict) or set(error) != {"kind", "message", "line"}:
            raise ValueError("bad error")
        kind, message, line = error["kind"], error["message"], error["line"]
        if kind not in ERROR_KINDS or not isinstance(message, str):
            raise ValueError("bad error kind or message")
        if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
            raise ValueError("bad error line")
        result.error = BuildError(kind, message, line)
        return result, []

    shapes = manifest.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        raise ValueError("no shapes")
    to_load: list[tuple[str, str]] = []
    for entry in shapes:
        if not isinstance(entry, dict) or set(entry) != {"name", "file"}:
            raise ValueError("bad shape entry")
        name, filename = entry["name"], entry["file"]
        if not isinstance(name, str) or not name or name in dict(to_load):
            raise ValueError("bad shape name")
        if not isinstance(filename, str) or not _SHAPE_FILE.fullmatch(filename):
            raise ValueError("bad shape file name")
        to_load.append((name, filename))
    return result, to_load


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
