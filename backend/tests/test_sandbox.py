"""Sandboxed build runner.

Malicious or runaway build code is stopped and reported; a normal build returns in
under 10 s. The escape tests first reach the real `os` module and import machinery
through object subclasses, so they exercise the enforcement layers behind the import
allowlist, not only the allowlist itself. The tests under "With the Python guard switched
off" go further: the build code disables the audit hook first, so only the kernel limits
(Landlock and seccomp) are left to stop it.

These tests need a host that can apply the kernel limits; CI also runs them inside the
production image.
"""

import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from build123d import Shape

from geometry import run_build, sandbox
from geometry.sandbox import DEFAULT_TIMEOUT_S, KERNEL_LIMITS_ENV

# The kernel limits (Landlock, seccomp, the address-space cap and the Linux-only calls the escape
# tests make) exist only on Linux: on macOS (GEOMETRY_SANDBOX_KERNEL_LIMITS=optional for local
# builds) these tests can't hold. CI runs them on Linux and in the production image.
LINUX_KERNEL_LIMITS = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="needs the sandbox's kernel limits: Linux only"
)

# Reach the real os module and __import__ without using a blocked import.
ESCAPE = textwrap.dedent(
    """\
    _wrap = [c for c in ().__class__.__base__.__subclasses__() if c.__name__ == "_wrap_close"]
    os = _wrap[0].__init__.__globals__["sys"].modules["os"]
    real_import = os.sys.modules["_frozen_importlib"].__import__
    """
)
ESCAPE_LINES = ESCAPE.count("\n")

NORMAL_BUILD = textwrap.dedent(
    """\
    from build123d import *

    with BuildPart() as base:
        Box(params["length"], params["width"], params["height"])
        fillet(base.edges().filter_by(Axis.Z), radius=params["radius"])
        with Locations((0, 0, params["height"] / 2)):
            Cylinder(params["hole"], params["height"], mode=Mode.SUBTRACT)
    print("built")
    result = {"base": base.part, "lid": Box(params["length"], params["width"], 2)}
    """
)
NORMAL_PARAMS = {"length": 60.0, "width": 40.0, "height": 20.0, "radius": 3.0, "hole": 5.0}


def assert_stopped(result, kind, line=None):
    assert not result.ok
    assert result.shapes == {}
    assert result.error.kind == kind, result.error
    if line is not None:
        assert result.error.line == line, result.error


# A normal build


def test_normal_build_returns_shapes_in_under_10_seconds():
    result = run_build(NORMAL_BUILD, NORMAL_PARAMS)
    assert result.ok, result.error
    assert result.duration_s < 10
    assert set(result.shapes) == {"base", "lid"}
    base = Shape.cast(result.shapes["base"])
    assert base.is_valid
    assert 0 < base.volume < 60 * 40 * 20
    assert abs(Shape.cast(result.shapes["lid"]).volume - 60 * 40 * 2) < 1e-6
    assert result.output == "built\n"


def test_single_shape_result_is_named_result():
    result = run_build("from build123d import Box\nresult = Box(1, 2, 3)")
    assert result.ok, result.error
    assert abs(Shape.cast(result.shapes["result"]).volume - 6) < 1e-9


def test_default_timeout_is_30_seconds():
    assert DEFAULT_TIMEOUT_S == 30


# Structured errors with line numbers


def test_syntax_error_reports_line():
    assert_stopped(run_build("x = 1\ny = (\n"), "syntax", line=2)


def test_runtime_error_reports_line():
    code = "from build123d import Box\nb = Box(1, 1, 1)\nraise ValueError('bad size')\n"
    result = run_build(code)
    assert_stopped(result, "runtime", line=3)
    assert "bad size" in result.error.message


def test_missing_result_is_reported():
    assert_stopped(run_build("x = 1"), "no_result")


def test_non_shape_result_is_reported():
    assert_stopped(run_build("result = 42"), "runtime")


# Imports


@pytest.mark.parametrize("module", ["os", "socket", "subprocess", "ctypes", "importlib", "sys"])
def test_imports_outside_allowlist_are_blocked(module):
    result = run_build(f"import math\nimport {module}\n")
    assert_stopped(result, "blocked_import", line=2)
    assert module in result.error.message


def test_dunder_import_is_blocked_too():
    assert_stopped(run_build("__import__('os')"), "blocked_import", line=1)


# Network


def test_network_is_blocked():
    code = ESCAPE + "s = real_import('socket')\ns.create_connection(('1.1.1.1', 80), timeout=2)\n"
    result = run_build(code)
    assert_stopped(result, "forbidden", line=ESCAPE_LINES + 2)
    assert "network" in result.error.message


def test_raw_socket_is_blocked():
    code = ESCAPE + "s = real_import('socket')\ns.socket()\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 2)


# Files


def test_file_write_outside_temp_dir_is_blocked(tmp_path):
    target = tmp_path / "evil.txt"
    result = run_build(f"x = 1\nopen({str(target)!r}, 'w').write('x')\n")
    assert_stopped(result, "forbidden", line=2)
    assert not target.exists()


def test_low_level_write_outside_temp_dir_is_blocked(tmp_path):
    target = tmp_path / "evil.txt"
    code = ESCAPE + f"os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT)\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 1)
    assert not target.exists()


def test_deleting_files_outside_temp_dir_is_blocked(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    code = ESCAPE + f"os.remove({str(target)!r})\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 1)
    assert target.read_text() == "keep"


def test_symlink_out_of_temp_dir_does_not_allow_writes(tmp_path):
    target = tmp_path / "evil.txt"
    code = ESCAPE + f"os.symlink({str(target)!r}, 'link')\nopen('link', 'w').write('x')\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 2)
    assert not target.exists()


def test_writes_inside_temp_dir_are_allowed_and_cleaned_up():
    code = ESCAPE + textwrap.dedent(
        """\
        with open("scratch.txt", "w") as f:
            f.write("ok")
        print(os.getcwd())
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    result = run_build(code)
    assert result.ok, result.error
    workdir = Path(result.output.strip())
    assert not workdir.exists()


def test_swallowed_violation_is_still_reported(tmp_path):
    target = tmp_path / "evil.txt"
    code = textwrap.dedent(
        f"""\
        try:
            open({str(target)!r}, "w")
        except BaseException:
            pass
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    assert_stopped(run_build(code), "forbidden", line=2)
    assert not target.exists()


# Processes and environment


def test_starting_a_process_is_blocked():
    code = ESCAPE + "os.system('echo escaped')\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 1)
    code = ESCAPE + "real_import('subprocess').run(['true'])\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 1)


def test_native_library_loading_is_blocked():
    code = ESCAPE + "real_import('ctypes').CDLL(None)\n"
    assert_stopped(run_build(code), "forbidden", line=ESCAPE_LINES + 1)


def test_secrets_are_not_passed_to_the_build(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    code = ESCAPE + "print(sorted(os.environ))\nfrom build123d import Box\nresult = Box(1, 1, 1)\n"
    result = run_build(code)
    assert result.ok, result.error
    assert "ANTHROPIC_API_KEY" not in result.output


# Runaway code


def test_infinite_loop_is_stopped_at_the_timeout():
    result = run_build("from build123d import Box\nwhile True:\n    pass\n", timeout_s=4)
    assert_stopped(result, "timeout")
    assert result.duration_s < 4 + 2


@LINUX_KERNEL_LIMITS
def test_huge_allocation_is_stopped():
    result = run_build("x = 1\nblob = bytearray(10**11)\n")
    assert_stopped(result, "memory", line=2)


@LINUX_KERNEL_LIMITS
def test_growing_allocation_is_stopped_at_the_memory_cap():
    code = "chunks = []\nwhile True:\n    chunks.append(bytearray(10**7))\n"
    result = run_build(code, memory_mb=256, timeout_s=20)
    assert_stopped(result, "memory", line=3)


def test_hard_exit_is_reported_as_a_crash():
    result = run_build(ESCAPE + "os._exit(7)\n")
    assert_stopped(result, "crash")
    assert "7" in result.error.message


# With the Python guard switched off. Each way of disabling the audit hook is the kind of
# thing build code can do from inside the interpreter; the kernel must still stop it.
# Kernel refusals surface as PermissionError, reported as "runtime" errors, which also
# shows the hook (whose errors are "forbidden") really was off.

HOOK_OFF = {
    # A known escape: find the guard on the stack and switch it off.
    "frame_walk": ESCAPE
    + textwrap.dedent(
        """\
        _f = os.sys._getframe()
        while _f is not None:
            if _f.f_locals.get("guard") is not None:
                _f.f_locals["guard"].active = False
            _f = _f.f_back
        """
    ),
    # Find the guard through the garbage collector and switch it off.
    "gc": ESCAPE
    + textwrap.dedent(
        """\
        for _o in real_import("gc").get_objects():
            if type(_o).__name__ == "Guard":
                _o.active = False
        """
    ),
    # Leave it on but widen its rules: everything is "inside", nothing is blocked.
    "rules": ESCAPE
    + textwrap.dedent(
        """\
        for _o in real_import("gc").get_objects():
            if type(_o).__name__ == "Guard":
                _o.workdir = "/"
                type(_o).audit.__globals__["_BLOCKED_EVENTS"].clear()
        """
    ),
}
HOOK_OFF_LINES = {name: code.count("\n") for name, code in HOOK_OFF.items()}
hook_off = pytest.mark.parametrize("bypass", sorted(HOOK_OFF))


def assert_kernel_stopped(result, bypass, line):
    assert_stopped(result, "runtime", line=HOOK_OFF_LINES[bypass] + line)
    assert "PermissionError" in result.error.message, result.error


@LINUX_KERNEL_LIMITS
def test_kernel_limits_apply_by_default():
    result = run_build("from build123d import Box\nresult = Box(1, 1, 1)")
    assert result.ok, result.error
    assert result.kernel_limits


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_write_outside_temp_dir(bypass, tmp_path):
    target = tmp_path / "escaped.txt"
    code = HOOK_OFF[bypass] + f"open({str(target)!r}, 'w').write('x')\n"
    assert_kernel_stopped(run_build(code), bypass, line=1)
    assert not target.exists()


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_delete_outside_temp_dir(bypass, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    code = HOOK_OFF[bypass] + f"os.remove({str(target)!r})\n"
    assert_kernel_stopped(run_build(code), bypass, line=1)
    assert target.read_text() == "keep"


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_connect(bypass):
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        code = HOOK_OFF[bypass] + (
            f"real_import('socket').create_connection(('127.0.0.1', {port}), timeout=2)\n"
        )
        assert_kernel_stopped(run_build(code), bypass, line=1)
        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            server.accept()


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_open_sockets_through_ctypes(bypass):
    code = HOOK_OFF[bypass] + textwrap.dedent(
        """\
        print(real_import("ctypes").CDLL(None).socket(2, 1, 0))
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    result = run_build(code)
    assert result.ok, result.error
    assert result.output.strip() == "-1"


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_start_processes(bypass, tmp_path):
    target = tmp_path / "escaped.txt"
    code = HOOK_OFF[bypass] + f"real_import('subprocess').run(['touch', {str(target)!r}])\n"
    assert_kernel_stopped(run_build(code), bypass, line=1)
    code = HOOK_OFF[bypass] + "os.fork()\n"
    assert_kernel_stopped(run_build(code), bypass, line=1)
    assert not target.exists()


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_signal_other_processes(bypass):
    code = HOOK_OFF[bypass] + "os.kill(os.getppid(), 0)\n"
    assert_kernel_stopped(run_build(code), bypass, line=1)


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_change_file_metadata_outside_temp_dir(bypass, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    os.chmod(target, 0o640)
    os.utime(target, (1_000_000, 1_000_000))
    before = target.stat()
    for call in (
        f"os.chmod({str(target)!r}, 0o777)",
        f"os.chmod({str(target)!r}, 0)",
        f"os.utime({str(target)!r}, (0, 0))",
        f"os.utime({str(target)!r})",
        f"os.chown({str(target)!r}, -1, -1)",
        f"os.truncate({str(target)!r}, 0)",
    ):
        assert_kernel_stopped(run_build(HOOK_OFF[bypass] + call + "\n"), bypass, line=1)
    after = target.stat()
    assert after.st_mode == before.st_mode
    assert after.st_mtime == before.st_mtime
    assert target.read_text() == "keep"


def _posix_acl(mode):
    """A `system.posix_acl_access` value that sets user, group and other to `mode`'s bits."""
    entries = [(0x01, (mode >> 6) & 7), (0x04, (mode >> 3) & 7), (0x20, mode & 7)]
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, perm, 0xFFFFFFFF) for tag, perm in entries
    )


def _xattrs(path):
    return {name: os.getxattr(path, name) for name in os.listxattr(path)}


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_change_extended_attributes_outside_temp_dir(bypass, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    os.chmod(target, 0o600)
    # Only meaningful where the filesystem takes both kinds of xattr: check on another file.
    probe = tmp_path / "probe.txt"
    probe.write_text("")
    os.chmod(probe, 0o600)
    try:
        os.setxattr(target, "user.keep", b"1")
        os.setxattr(probe, "system.posix_acl_access", _posix_acl(0o777))
    except OSError as exc:
        pytest.skip(f"the filesystem doesn't support user xattrs and POSIX ACLs: {exc}")
    assert stat.S_IMODE(probe.stat().st_mode) == 0o777  # an ACL really is a chmod
    before_mode, before_xattrs = target.stat().st_mode, _xattrs(target)

    acl = _posix_acl(0o777)
    for call in (
        f"os.setxattr({str(target)!r}, 'user.pwn', b'1')",
        f"os.setxattr({str(target)!r}, 'system.posix_acl_access', {acl!r})",
        f"os.setxattr({str(target)!r}, 'system.posix_acl_access', {acl!r}, follow_symlinks=False)",
        f"os.removexattr({str(target)!r}, 'user.keep')",
        f"os.removexattr({str(target)!r}, 'user.keep', follow_symlinks=False)",
        # fsetxattr is refused even on the build's own work directory.
        "_fd = os.open('.', os.O_RDONLY)\nos.setxattr(_fd, 'user.pwn', b'1')",
    ):
        line = call.count("\n") + 1
        assert_kernel_stopped(run_build(HOOK_OFF[bypass] + call + "\n"), bypass, line=line)
    assert target.stat().st_mode == before_mode
    assert _xattrs(target) == before_xattrs


def test_truncate_is_refused_by_seccomp_where_landlock_cannot():
    from geometry import _kernel_sandbox as k

    for machine in ("x86_64", "aarch64"):
        assert "truncate" in k.denied_syscalls(machine, abi=2)
        assert "truncate" not in k.denied_syscalls(machine, abi=3)
        for name in ("fchmodat", "fchownat", "utimensat", "fchmod", "fchown"):
            assert name in k.denied_syscalls(machine, abi=6)


def test_every_xattr_changing_call_is_refused_by_seccomp():
    from geometry import _kernel_sandbox as k

    calls = ("setxattr", "lsetxattr", "fsetxattr", "removexattr", "lremovexattr")
    calls += ("fremovexattr", "setxattrat", "removexattrat")
    for machine in ("x86_64", "aarch64"):
        denied = k.denied_syscalls(machine, abi=6)
        for name in calls:
            assert name in denied, (machine, name)


@LINUX_KERNEL_LIMITS
@hook_off
def test_guard_bypass_still_cannot_change_other_processes_limits(bypass):
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    nice = os.getpriority(os.PRIO_PROCESS, 0)
    cpus = sorted(os.sched_getaffinity(0))
    for call in (
        # Same values as now, so nothing changes even if this test fails.
        f"real_import('resource').prlimit(os.getppid(), {resource.RLIMIT_NOFILE}, "
        f"({soft}, {hard}))",
        f"os.setpriority(os.PRIO_PROCESS, os.getppid(), {nice})",
        f"os.setpriority(os.PRIO_USER, 0, {nice})",
        f"os.sched_setaffinity(os.getppid(), {cpus})",
    ):
        assert_kernel_stopped(run_build(HOOK_OFF[bypass] + call + "\n"), bypass, line=1)


@LINUX_KERNEL_LIMITS
def test_builds_can_still_set_their_own_limits():
    code = ESCAPE + textwrap.dedent(
        """\
        r = real_import("resource")
        r.setrlimit(r.RLIMIT_NOFILE, r.getrlimit(r.RLIMIT_NOFILE))
        os.sched_setaffinity(0, os.sched_getaffinity(0))
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    result = run_build(code)
    assert result.ok, result.error


# The parent process: what the child sends back can't hurt it.


@LINUX_KERNEL_LIMITS
@hook_off
def test_flooding_the_output_pipes_is_stopped_without_growing_the_server(bypass):
    import resource

    code = HOOK_OFF[bypass] + textwrap.dedent(
        """\
        _chunk = b"x" * 2**20
        for _ in range(1500):
            for _fd in range(1, 10):  # stderr, and the manifest wherever it is
                try:
                    os.write(_fd, _chunk)
                except OSError:
                    pass
        """
    )
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB
    result = run_build(code)
    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    assert_stopped(result, "output")
    assert result.duration_s < 10
    assert grown_mb < 100, f"the server grew by {grown_mb:.0f} MB"


def _forge_manifest(text, before="", bypass="frame_walk"):
    """Build code that writes `text` as its manifest and exits, as a guard bypass could."""
    return (
        HOOK_OFF[bypass]
        + before
        + textwrap.dedent(
            f"""\
            _f = os.sys._getframe()
            while "manifest" not in _f.f_locals:
                _f = _f.f_back
            _f.f_locals["manifest"].write({text!r})
            _f.f_locals["manifest"].flush()
            os._exit(0)
            """
        )
    )


@pytest.mark.parametrize(
    "forged",
    [
        {"shapes": [], "error": {"kind": "x", "bogus": 1}, "output": ""},
        {"shapes": [], "error": {"kind": "made_up", "message": "m", "line": None}},
        {"shapes": [], "error": {"kind": "runtime", "message": 1, "line": None}},
        {"shapes": [], "error": "oops"},
        {"shapes": [{"name": "a", "file": "../../etc/passwd"}], "error": None},
        {"shapes": [{"name": "a"}], "error": None},
        {"shapes": "all", "error": None},
        {"shapes": [], "error": None},
        {"shapes": [], "error": None, "output": 5},
        {},
        [],
        "text",
    ],
)
def test_a_forged_manifest_is_reported_as_a_crash(forged):
    result = run_build(_forge_manifest(json.dumps(forged)))
    assert_stopped(result, "crash")
    assert "invalid response" in result.error.message


def test_a_manifest_that_is_not_json_or_utf8_is_reported_as_a_crash():
    assert_stopped(run_build(_forge_manifest("{not json")), "crash")
    code = HOOK_OFF["frame_walk"] + "os.write(2, b'\\xff\\xfe bad bytes')\nos._exit(3)\n"
    result = run_build(code)
    assert_stopped(result, "crash")
    assert "3" in result.error.message


@pytest.mark.parametrize(
    "plant",
    [
        "os.mkfifo('shape_0.brep')\n",  # would block the server's reader forever
        "os.symlink('/etc/passwd', 'shape_0.brep')\n",
    ],
)
def test_shape_files_must_be_regular_files_the_child_wrote(plant):
    manifest = json.dumps({"shapes": [{"name": "a", "file": "shape_0.brep"}], "error": None})
    result = run_build(_forge_manifest(manifest, before=plant))
    assert_stopped(result, "crash")
    assert "not a regular file" in result.error.message
    assert result.duration_s < 10


# A BREP file the parser would expand to several times its size: a valid header and a
# long list of line curves. Written by the build into its work directory.
_BIG_BREP = textwrap.dedent(
    """\
    def _write_brep(name, megabytes):
        _line = b"1 0 0 0 1 0 0\\n"
        _block = _line * (2**20 // len(_line))
        _count = megabytes * (2**20 // len(_line))
        _fd = os.open(name, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(_fd, b"DBRep_DrawableShape\\n\\nCASCADE Topology V1, (c) Matra-Datavision\\n")
        os.write(_fd, b"Locations 0\\nCurve2ds 0\\nCurves %d\\n" % _count)
        for _ in range(megabytes):
            os.write(_fd, _block)
        os.close(_fd)
    """
)


def _oversized_shapes(files, bypass="frame_walk"):
    """Build code that writes `files` ({name: megabytes}) and a manifest that lists them."""
    writes = "".join(f"_write_brep({name!r}, {mb})\n" for name, mb in files.items())
    shapes = [{"name": f"s{i}", "file": name} for i, name in enumerate(files)]
    manifest = json.dumps({"shapes": shapes, "error": None})
    return _forge_manifest(manifest, before=_BIG_BREP + writes, bypass=bypass)


@pytest.mark.parametrize(
    "files",
    [
        {"shape_0.brep": 40},  # one file over the limit
        {"shape_0.brep": 12, "shape_1.brep": 12, "shape_2.brep": 12},  # over it in total
    ],
)
def test_oversized_shape_files_are_refused_before_parsing(files):
    assert sum(files.values()) * 2**20 > sandbox.SHAPE_FILES_LIMIT
    result = run_build(_oversized_shapes(files))
    assert_stopped(result, "output")
    assert "MB allowed" in result.error.message
    assert result.duration_s < 10


def test_too_many_shape_files_are_refused_before_parsing():
    count = sandbox.MAX_SHAPES + 1
    plant = f"for _i in range({count}):\n    open(f'shape_{{_i}}.brep', 'w').close()\n"
    shapes = [{"name": f"s{i}", "file": f"shape_{i}.brep"} for i in range(count)]
    result = run_build(_forge_manifest(json.dumps({"shapes": shapes, "error": None}), plant))
    assert_stopped(result, "output")
    assert f"{count} shapes" in result.error.message


@LINUX_KERNEL_LIMITS
@hook_off
def test_oversized_shape_files_do_not_grow_the_server(bypass):
    """The build stays under its own caps and still can't make the server parse 40 MB."""
    code = _oversized_shapes({"shape_0.brep": 40}, bypass=bypass)
    # A fresh process stands in for the server, so the high-water mark of its memory
    # isn't hidden by whatever earlier tests left in this one.
    server = textwrap.dedent(
        f"""\
        import json, resource
        from geometry import run_build
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        r = run_build({code!r})
        grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        print(json.dumps({{"kind": r.error and r.error.kind, "grown_mb": grown / 1024}}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", server], capture_output=True, text=True, check=True
    )
    report = json.loads(proc.stdout.splitlines()[-1])  # OpenCascade may print to stdout
    assert report["grown_mb"] < 100, f"the server grew by {report['grown_mb']:.0f} MB"
    assert report["kind"] == "output"


def test_shape_files_under_the_limits_still_load():
    code = "from build123d import Box\nresult = {'a': Box(1, 1, 1), 'b': Box(1, 1, 2)}\n"
    result = run_build(code)
    assert result.ok, result.error
    assert set(result.shapes) == {"a", "b"}


# The child starts with nothing loaded but itself and the kernel limits.


def test_the_child_loads_no_app_code_before_the_kernel_limits():
    code = ESCAPE + textwrap.dedent(
        """\
        _mods = os.sys.modules
        print(sorted(m for m in _mods if m == "app" or m.startswith(("app.", "geometry"))))
        print(hasattr(_mods["geometry"], "run_build"))
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    result = run_build(code)
    assert result.ok, result.error
    loaded, package_init_ran = result.output.splitlines()
    assert loaded == str(["geometry", "geometry._kernel_sandbox", "geometry._sandbox_child"])
    assert package_init_ran == "False"


# Secrets: none are passed, and none can be read.


@LINUX_KERNEL_LIMITS
@hook_off
def test_files_outside_the_python_install_cannot_be_read(bypass, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("ANTHROPIC_API_KEY=sk-test-not-a-real-key\n")
    # This file stands in for the repo checkout (and its .env): in CI it sits in the repo,
    # in the image it is mounted next to the installed app.
    for path in (secret, Path(__file__), "/etc/passwd"):
        code = HOOK_OFF[bypass] + f"print(open({str(path)!r}).read())\n"
        result = run_build(code)
        assert_kernel_stopped(result, bypass, line=1)
        assert "sk-test" not in result.output


@LINUX_KERNEL_LIMITS
def test_the_parent_environment_cannot_be_read():
    """The server's own environment holds ANTHROPIC_API_KEY; /proc must not expose it."""
    canary = "sk-test-canary-not-a-real-key"
    code = HOOK_OFF["frame_walk"] + textwrap.dedent(
        """\
        try:
            print(open(f"/proc/{os.getppid()}/environ").read())
        except PermissionError:
            print("refused")
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )
    # A stand-in server that has the key in its environment and runs the build. It
    # reports only yes/no answers, so no real environment ends up in the test output.
    server = textwrap.dedent(
        f"""\
        import json, os
        from geometry import run_build
        own = open(f"/proc/{{os.getpid()}}/environ").read()
        r = run_build({code!r})
        print(json.dumps({{
            "key_in_own_environ": {canary!r} in own,
            "error": str(r.error),
            "refused": r.output.strip() == "refused",
            "key_in_output": {canary!r} in r.output,
        }}))
        """
    )
    env = {**os.environ, "ANTHROPIC_API_KEY": canary}
    proc = subprocess.run(
        [sys.executable, "-c", server], env=env, capture_output=True, text=True, check=True
    )
    report = json.loads(proc.stdout)
    assert report["key_in_own_environ"]  # the key really is there to be stolen
    assert report["error"] == "None"
    assert report["refused"]
    assert not report["key_in_output"]


# When the host can't apply the kernel limits


def _run_child_without_landlock(kernel_limits, code):
    """Run the real child with Landlock reported as unavailable."""
    with tempfile.TemporaryDirectory() as workdir:
        allowed = sandbox.SAFE_IMPORTS | {"build123d"}
        request = sandbox._request(code, {}, workdir, allowed, 30, 2048, kernel_limits)
        script = (
            sandbox.CHILD_BOOTSTRAP
            + "import geometry._kernel_sandbox as k\n"
            + "k.landlock_abi = lambda: 0\n"
            + sandbox.CHILD_MAIN
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script, sandbox._GEOMETRY_DIR],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            cwd=workdir,
            check=False,
        )
    return json.loads(proc.stdout)


def test_builds_are_refused_when_kernel_limits_are_unavailable(tmp_path):
    target = tmp_path / "escaped.txt"
    code = f"open({str(target)!r}, 'w')\nfrom build123d import Box\nresult = Box(1, 1, 1)\n"
    manifest = _run_child_without_landlock("required", code)
    assert manifest["error"]["kind"] == "sandbox_unavailable"
    assert "Landlock" in manifest["error"]["message"]
    assert manifest["shapes"] == []
    assert not target.exists()


def test_optional_kernel_limits_run_without_them_and_say_so():
    code = "from build123d import Box\nresult = Box(1, 1, 1)\n"
    manifest = _run_child_without_landlock("optional", code)
    assert manifest["error"] is None
    assert manifest["kernel_limits"] is False
    assert len(manifest["shapes"]) == 1


def test_kernel_limits_are_required_unless_opted_out(monkeypatch):
    monkeypatch.delenv(KERNEL_LIMITS_ENV, raising=False)
    assert sandbox._kernel_limits_setting() == "required"
    monkeypatch.setenv(KERNEL_LIMITS_ENV, "optional")
    assert sandbox._kernel_limits_setting() == "optional"
    monkeypatch.setenv(KERNEL_LIMITS_ENV, "off")
    with pytest.raises(ValueError):
        run_build("result = 1")


# Disk use: the work directory is capped in total size and entry count, not
# just per file, and the build can't hide what it writes from that cap.


class _FreeSpaceLow:
    """Tracks the lowest free space of a filesystem while a build runs."""

    def __init__(self, path):
        import threading

        self.path = path
        self.before = self._free()
        self.lowest = self.before
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _free(self):
        st = os.statvfs(self.path)
        return st.f_bavail * st.f_frsize

    def _watch(self):
        while not self._stop.wait(0.002):
            self.lowest = min(self.lowest, self._free())

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


# Writes `count` files of `megabytes` each, every one well under the per-file cap.
def _many_files(count, megabytes, before=""):
    assert megabytes < sandbox.DEFAULT_FILE_MB
    return before + textwrap.dedent(
        f"""\
        _chunk = b"x" * ({megabytes} * 1024 * 1024)
        for _i in range({count}):
            with open(f"fill_{{_i}}.bin", "wb") as _f:
                _f.write(_chunk)
        from build123d import Box
        result = Box(1, 1, 1)
        """
    )


# Far more than the cap: 8 GB in 1 MB files, if nothing stopped it.
_FILL_COUNT, _FILL_MB = 8192, 1
# The filesystem may dip by the cap plus what the build writes between two checks.
_FREE_SPACE_MARGIN = sandbox.WORKDIR_BYTES_LIMIT + 256 * 1024 * 1024


def _assert_free_space_kept(watch):
    drop = watch.before - watch.lowest
    assert drop < _FREE_SPACE_MARGIN, f"free space fell by {drop / 2**20:.0f} MB"
    after = watch._free()
    assert watch.before - after < 64 * 1024 * 1024, "the work directory was not removed"


def test_many_files_under_the_per_file_cap_are_stopped_at_the_total_size():
    with _FreeSpaceLow(tempfile.gettempdir()) as watch:
        result = run_build(_many_files(_FILL_COUNT, _FILL_MB))
    assert_stopped(result, "disk")
    assert f"{sandbox.WORKDIR_BYTES_LIMIT // 2**20} MB" in result.error.message
    assert result.duration_s < 10
    _assert_free_space_kept(watch)


def test_many_empty_files_are_stopped_at_the_file_count():
    code = "for _i in range(1_000_000):\n    open(f'e_{_i}', 'w').close()\nresult = 1\n"
    with _FreeSpaceLow(tempfile.gettempdir()) as watch:
        result = run_build(code)
    assert_stopped(result, "disk")
    assert f"more than {sandbox.WORKDIR_ENTRIES_LIMIT} files" in result.error.message
    assert result.duration_s < 10
    _assert_free_space_kept(watch)


@hook_off
def test_writing_after_closing_the_pipes_is_still_stopped(bypass):
    """The parent keeps checking after the child closes its stdout and stderr."""
    close = "os.close(0)\nos.close(1)\nos.close(2)\n"
    code = _many_files(_FILL_COUNT, _FILL_MB, before=HOOK_OFF[bypass] + close)
    with _FreeSpaceLow(tempfile.gettempdir()) as watch:
        result = run_build(code)
    assert_stopped(result, "disk")
    _assert_free_space_kept(watch)


def test_disk_use_is_checked_again_after_the_build_ends(monkeypatch):
    """What a build writes after the last check while it runs is still caught."""
    monkeypatch.setattr(sandbox, "DISK_POLL_S", 60.0)  # no check while it runs
    monkeypatch.setattr(sandbox, "WORKDIR_BYTES_LIMIT", 2 * 1024 * 1024)
    result = run_build(_many_files(3, 1))
    assert_stopped(result, "disk")


def test_hard_links_are_counted_once():
    links = sandbox.WORKDIR_BYTES_LIMIT // (20 * 2**20) + 2  # over the cap if double counted
    code = _many_files(1, 20, before=ESCAPE) + "".join(
        f"os.link('fill_0.bin', 'link_{i}.bin')\n" for i in range(links)
    )
    result = run_build(code)
    assert result.ok, result.error


def test_scratch_files_under_the_caps_are_fine():
    result = run_build(_many_files(20, 2))
    assert result.ok, result.error
    assert result.duration_s < 10


@LINUX_KERNEL_LIMITS
@hook_off
@pytest.mark.parametrize(
    "call",
    [
        "os.remove('scratch')",  # delete an open file: its space would be invisible
        "os.rename('scratch', 'other')",
        "os.mkdir('sub')",  # the work directory stays flat
        "os.open('.', os.O_TMPFILE | os.O_RDWR, 0o600)",  # an unnamed file
    ],
)
def test_guard_bypass_cannot_hide_files_from_the_disk_cap(bypass, call):
    code = HOOK_OFF[bypass] + "_keep = open('scratch', 'w')\n" + call + "\n"
    assert_kernel_stopped(run_build(code), bypass, line=2)


def test_unnamed_files_are_refused_by_seccomp_on_every_architecture():
    from geometry import _kernel_sandbox as k

    for machine in ("x86_64", "aarch64"):
        numbers = k._ARCH[machine][2]
        prog = k.seccomp_program(machine, pid=1, abi=6)
        for name in ("openat", "openat2", "open"):
            if name in numbers:
                assert (k._JEQ, 0, 1 if name == "openat2" else 4, numbers[name]) in prog
        assert any(insn == (k._JSET, 0, 1, k._O_TMPFILE) for insn in prog)


# In-memory files: their pages live in shared memory, outside the build's
# address space, so RLIMIT_AS doesn't count them and the disk cap can't see them.

# Calls not in the os module go through libc, which returns -1 and sets errno; the call's
# line raises it, as an os function would.
_LIBC = '_c = real_import("ctypes")\n_libc = _c.CDLL(None, use_errno=True)\n'
_RAISE = "raise OSError(_c.get_errno(), os.strerror(_c.get_errno()))"
_IN_MEMORY_FILES = {
    "memfd_create": "os.memfd_create('fill')",
    # Refused even on kernels without it (ENOSYS otherwise).
    "memfd_secret": "if _libc.syscall({memfd_secret}, 0) < 0: " + _RAISE,
    # System V shared memory, the same hole; a segment would also outlive the build.
    # IPC_PRIVATE, IPC_CREAT | 0600.
    "shmget": "if _libc.shmget(0, 1 << 20, 0o1600) < 0: " + _RAISE,
}


@LINUX_KERNEL_LIMITS
@hook_off
@pytest.mark.parametrize("name", sorted(_IN_MEMORY_FILES))
def test_guard_bypass_cannot_make_in_memory_files(bypass, name):
    import platform

    from geometry import _kernel_sandbox as k

    call = _IN_MEMORY_FILES[name].format(**k._ARCH[platform.machine()][2])
    code = HOOK_OFF[bypass] + _LIBC + call + "\n"
    assert_kernel_stopped(run_build(code), bypass, line=_LIBC.count("\n") + 1)


def test_in_memory_files_are_refused_by_seccomp_on_every_architecture():
    from geometry import _kernel_sandbox as k

    for machine in ("x86_64", "aarch64"):
        denied = k.denied_syscalls(machine, abi=6)
        for name in ("memfd_create", "memfd_secret", "shmget", "shmat"):
            assert name in denied, (machine, name)
