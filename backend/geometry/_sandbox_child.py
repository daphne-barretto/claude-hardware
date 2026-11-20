"""Child side of the sandboxed build runner. Run only by `geometry.sandbox.run_build`.

Protocol: the parent writes one JSON request to stdin and reads one JSON manifest from
stdout. Shapes are written as BREP files into the request's work directory.

Order matters here:

0. The parent starts this module through `sandbox.CHILD_BOOTSTRAP`, which skips the
   `geometry` package's __init__, so nothing but this module, `_kernel_sandbox` and the
   standard library is loaded before step 1.
1. Apply the kernel limits (`_kernel_sandbox`: Landlock and seccomp) while the process
   is still single threaded, so every thread started later inherits them. They are the
   security boundary: no network, no new processes, no signals or tracing of other
   processes, writes only in the work directory, and reads only from the Python
   installation and system libraries. If the host can't apply them the build is refused,
   unless the request says they are optional (local development only).
2. Import the geometry backend and the allowed modules.
3. Apply resource limits (address space, CPU time, file size).
4. Install an audit hook and an import guard. They run inside the build's own
   interpreter, so build code can switch them off; they are not a security layer. They
   exist to stop the common cases early with a clear message and the build line.
5. Run the build code, then serialize what it assigned to `result`. With `feature_sites` in
   the request, the code is instrumented first (`geometry.sites`, the same as an in-process
   build): the build code gets `__feature_site__`, which records the statement that made each
   feature dict, and `__feature_lines__`, which looks a dict's lines up. They only record and
   read line numbers; the code still runs under every limit above.
"""

from __future__ import annotations

import builtins
import importlib
import io
import json
import os
import resource
import signal
import sys
import sysconfig
import traceback
from typing import Any

from geometry import _kernel_sandbox

BUILD_FILENAME = "<build>"
# With `feature_sites`: the name the build code looks a feature dict's lines up by.
FEATURE_LINES = "__feature_lines__"
BUILD_MODULE = "__build__"
OUTPUT_LIMIT = 20_000

# Events refused outright once the build code is running.
_BLOCKED_EVENTS = {
    # Network
    "socket.__new__": "network access",
    "socket.connect": "network access",
    "socket.bind": "network access",
    "socket.sendto": "network access",
    "socket.sendmsg": "network access",
    "socket.getaddrinfo": "network access",
    "socket.gethostbyname": "network access",
    "socket.gethostbyaddr": "network access",
    "socket.getnameinfo": "network access",
    "urllib.Request": "network access",
    "http.client.connect": "network access",
    "ftplib.connect": "network access",
    "smtplib.connect": "network access",
    "poplib.connect": "network access",
    "imaplib.open": "network access",
    "nntplib.connect": "network access",
    "telnetlib.Telnet.open": "network access",
    "webbrowser.open": "network access",
    # Processes and signals
    "subprocess.Popen": "starting a process",
    "os.system": "starting a process",
    "os.exec": "starting a process",
    "os.posix_spawn": "starting a process",
    "os.spawn": "starting a process",
    "os.fork": "starting a process",
    "os.forkpty": "starting a process",
    "pty.spawn": "starting a process",
    "os.startfile": "starting a process",
    "os.kill": "sending a signal",
    "os.killpg": "sending a signal",
    "signal.pthread_kill": "sending a signal",
    # Native code and environment
    "ctypes.dlopen": "loading a native library",
    "ctypes.dlsym": "loading a native library",
    "ctypes.call_function": "calling native code",
    "os.putenv": "changing the environment",
    "os.unsetenv": "changing the environment",
}

# Events whose path arguments must stay inside the work directory.
_PATH_EVENTS = {
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.chflags": (0,),
    "os.link": (0, 1),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.setxattr": (0,),
    "os.removexattr": (0,),
    "os.chdir": (0,),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (1,),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (0, 1),
    "shutil.chown": (0,),
    "shutil.make_archive": (0,),
    "shutil.unpack_archive": (1,),
}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

# Builtins the build code doesn't get.
_REMOVED_BUILTINS = ("breakpoint", "input", "exit", "quit", "help", "copyright", "credits")


class SandboxViolation(BaseException):  # noqa: N818 - reported to the user as is
    """Raised when build code does something the sandbox forbids.

    A BaseException so that `except Exception` in build code doesn't swallow it; the
    violation is also recorded, so it is reported even if the code catches it anyway.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _build_line() -> int | None:
    """Line of the innermost build-code frame on the current stack."""
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_filename == BUILD_FILENAME:
            return frame.f_lineno
        frame = frame.f_back
    return None


def _traceback_line(exc: BaseException) -> int | None:
    lines = [
        f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename == BUILD_FILENAME
    ]
    return lines[-1] if lines else None


class Guard:
    def __init__(self, workdir: str, allowed_imports: set[str]) -> None:
        self.workdir = os.path.realpath(workdir)
        self.allowed_imports = allowed_imports
        self.violations: list[dict[str, Any]] = []
        self.active = False

    def violate(self, kind: str, message: str) -> SandboxViolation:
        if not self.violations:
            self.violations.append({"kind": kind, "message": message, "line": _build_line()})
        return SandboxViolation(kind, message)

    def _inside(self, path: Any) -> bool:
        if isinstance(path, int):  # an already open descriptor
            return True
        path = os.fsdecode(os.fspath(path))
        real = os.path.realpath(path)
        return os.path.commonpath([real, self.workdir]) == self.workdir

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.active:
            return
        reason = _BLOCKED_EVENTS.get(event)
        if reason is not None:
            raise self.violate("forbidden", f"{reason} is not allowed in a build ({event})")
        if event == "open":
            path, mode, flags = args
            writes = (mode is not None and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            if writes and path is not None and not self._inside(path):
                raise self.violate(
                    "forbidden", f"writing outside the build's temporary directory: {path}"
                )
            return
        positions = _PATH_EVENTS.get(event)
        if positions is not None:
            for i in positions:
                if i < len(args) and args[i] is not None and not self._inside(args[i]):
                    raise self.violate(
                        "forbidden",
                        f"changing files outside the build's temporary directory: {args[i]}",
                    )

    def make_import(self, real_import: Any) -> Any:
        guard = self

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
            caller = sys._getframe(1)
            if guard.active and caller.f_code.co_filename == BUILD_FILENAME:
                top = name.partition(".")[0]
                if level != 0 or top not in guard.allowed_imports:
                    raise guard.violate("blocked_import", f"import of '{name}' is not allowed")
            return real_import(name, globals, locals, fromlist, level)

        return guarded_import


def _readable_paths() -> list[str]:
    """What the build may read: the Python installation, system libraries and itself."""
    paths = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    paths.update(sysconfig.get_paths()[k] for k in ("stdlib", "platstdlib", "purelib", "platlib"))
    paths.update(
        {
            "/usr",
            "/lib",
            "/lib64",
            "/lib32",
            "/etc/ld.so.cache",  # the dynamic loader's library index
            "/dev/urandom",
            "/dev/zero",
            "/sys/devices/system/cpu",  # CPU count for thread pools
            f"/proc/{os.getpid()}",  # this process only; /proc/self resolves here
        }
    )
    return sorted(paths)


def _apply_kernel_limits(workdir: str) -> tuple[bool, str]:
    try:
        _kernel_sandbox.apply(_readable_paths(), [workdir, "/dev/null"])
    except _kernel_sandbox.KernelSandboxUnavailable as exc:
        return False, str(exc)
    return True, ""


def cpus_for_quota(cpu_max: str, available: list[int]) -> list[int]:
    """The CPUs a build may use: as many as the container's CPU quota (cgroup v2 `cpu.max`).

    A container capped at one CPU on a 16-CPU host still sees all 16, and OpenCascade starts a
    thread for each, each reserving its own stack and malloc arena. Under the build's address
    space cap that can run out before the build starts ("cannot allocate memory for
    thread-local data"). Pinning the build to its quota keeps the thread count honest.
    """
    parts = cpu_max.split()
    if len(parts) != 2 or parts[0] == "max":
        return available
    quota, period = int(parts[0]), int(parts[1])
    count = max(1, -(-quota // period))  # round up
    return sorted(available)[:count]


def _limit_cpus() -> None:
    if not hasattr(os, "sched_setaffinity"):
        return  # macOS
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            cpu_max = f.read()
    except OSError:
        return
    available = sorted(os.sched_getaffinity(0))
    cpus = cpus_for_quota(cpu_max, available)
    if cpus != available:
        os.sched_setaffinity(0, cpus)


def _set_limits(memory_bytes: int, cpu_seconds: int, file_bytes: int) -> None:
    try:
        with open("/proc/self/status") as f:
            status = f.read()
    except FileNotFoundError:
        # No /proc (macOS). Only reachable when the kernel limits are optional, i.e. local
        # development; macOS doesn't enforce RLIMIT_AS anyway, so the memory cap is skipped.
        status = None
    if status is not None:
        current = int(status.split("VmSize:")[1].split()[0]) * 1024
        address_space = current + memory_bytes
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Oversized writes fail with an error instead of killing the process.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)


def _as_topods(value: Any) -> Any:
    from OCP.TopoDS import TopoDS_Shape

    if isinstance(value, TopoDS_Shape):
        return value
    wrapped = getattr(value, "wrapped", None)
    if isinstance(wrapped, TopoDS_Shape):
        return wrapped
    part = getattr(value, "part", None)  # a build123d BuildPart context
    if part is not None and part is not value:
        return _as_topods(part)
    return None


def _collect(result: Any) -> dict[str, Any]:
    items = result.items() if isinstance(result, dict) else [("result", result)]
    shapes = {}
    for name, value in items:
        if not isinstance(name, str) or not name:
            raise TypeError("shape names in `result` must be non-empty strings")
        shape = _as_topods(value)
        if shape is None:
            raise TypeError(f"`result` entry '{name}' is not a shape ({type(value).__name__})")
        if shape.IsNull():
            raise ValueError(f"`result` entry '{name}' is an empty shape")
        shapes[name] = shape
    if not shapes:
        raise ValueError("`result` holds no shapes")
    return shapes


def _error(kind: str, message: str, line: int | None = None) -> dict[str, Any]:
    return {"kind": kind, "message": message, "line": line}


def main() -> None:
    request = json.loads(sys.stdin.read())
    manifest = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # native prints go to stderr, never into the manifest

    workdir = request["workdir"]
    # `sites` (standard library only) is loaded only for a build that records feature lines, and
    # before the kernel limits, which keep the package directory out of reach afterwards.
    sites = importlib.import_module("geometry.sites") if request.get("feature_sites") else None
    _limit_cpus()
    kernel_limits, reason = _apply_kernel_limits(workdir)
    if not kernel_limits and request["kernel_limits"] != "optional":
        message = f"the build sandbox can't be applied on this host: {reason}"
        error = _error("sandbox_unavailable", message)
        manifest.write(json.dumps({"shapes": [], "error": error, "output": ""}))
        manifest.flush()
        os._exit(0)

    allowed = set(request["allowed_imports"])
    for module in request["preload"]:
        __import__(module)
    from OCP.BRepTools import BRepTools

    os.chdir(workdir)
    _set_limits(request["memory_bytes"], request["cpu_seconds"], request["file_bytes"])

    guard = Guard(workdir, allowed)
    sys.addaudithook(guard.audit)
    builtins.__import__ = guard.make_import(builtins.__import__)
    for name in _REMOVED_BUILTINS:
        builtins.__dict__.pop(name, None)

    captured = io.StringIO()
    sys.stdout = sys.stderr = captured
    namespace: dict[str, Any] = {
        "__name__": BUILD_MODULE,
        "__builtins__": builtins,
        "params": request["params"],
    }
    error = None
    shapes: dict[str, Any] = {}
    guard.active = True
    try:
        if sites is not None:
            recorded = sites.instrument(request["code"], BUILD_FILENAME)
            namespace[sites.HOOK] = recorded.hook
            namespace[FEATURE_LINES] = recorded.lines
            code = compile(recorded.tree, BUILD_FILENAME, "exec")
        else:
            code = compile(request["code"], BUILD_FILENAME, "exec")
        exec(code, namespace)
        if "result" not in namespace:
            error = _error("no_result", "the build code did not assign `result`")
        else:
            shapes = _collect(namespace["result"])
    except SyntaxError as exc:
        line = exc.lineno if exc.filename == BUILD_FILENAME else None
        error = _error("syntax", f"{type(exc).__name__}: {exc.msg}", line)
    except SandboxViolation as exc:
        error = _error(exc.kind, exc.message, _traceback_line(exc))
    except MemoryError as exc:
        error = _error("memory", "the build ran out of memory", _traceback_line(exc))
    except RecursionError as exc:
        error = _error("runtime", f"RecursionError: {exc}", _traceback_line(exc))
    except BaseException as exc:  # noqa: BLE001 - every failure is reported, none escapes
        error = _error("runtime", f"{type(exc).__name__}: {exc}", _traceback_line(exc))

    if guard.violations:  # reported even when the build code swallowed the exception
        error = guard.violations[0]
        shapes = {}

    files = []
    if error is None:
        try:
            for i, (name, shape) in enumerate(shapes.items()):
                filename = f"shape_{i}.brep"
                if not BRepTools.Write_s(shape, os.path.join(workdir, filename)):
                    raise OSError(f"could not write shape '{name}'")
                files.append({"name": name, "file": filename})
        except BaseException as exc:  # noqa: BLE001
            error = _error("runtime", f"{type(exc).__name__}: {exc}")
            files = []

    output = captured.getvalue()
    if len(output) > OUTPUT_LIMIT:
        output = output[:OUTPUT_LIMIT] + "\n[output truncated]"
    manifest.write(
        json.dumps(
            {"shapes": files, "error": error, "output": output, "kernel_limits": kernel_limits}
        )
    )
    manifest.flush()
    os._exit(0)  # skip finalizers and atexit handlers the build code may have left


if __name__ == "__main__":
    main()
