"""The build sandbox on hosts without the Linux kernel limits (macOS, for example).

The kernel limits (Landlock and seccomp) exist only on Linux. On any other host a build
must report `sandbox_unavailable`, or run without them when GEOMETRY_SANDBOX_KERNEL_LIMITS
is "optional" (local development only), instead of crashing because a Linux-only libc
function or /proc file is missing.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from geometry import run_build
from geometry.sandbox import KERNEL_LIMITS_ENV

BACKEND = Path(__file__).resolve().parents[1]
KERNEL_SANDBOX = BACKEND / "geometry" / "_kernel_sandbox.py"

SMALL_BUILD = textwrap.dedent(
    """\
    from build123d import *

    result = {"block": Box(params["size"], params["size"], params["size"])}
    """
)

not_linux = pytest.mark.skipif(
    sys.platform.startswith("linux"), reason="checks hosts without the Linux kernel limits"
)


def test_kernel_sandbox_reports_unavailable_on_a_host_without_linux_libc():
    # A libc with no Linux-only functions, as on macOS, where looking up `prctl` fails.
    # Runs in a separate interpreter so nothing here can touch this test process.
    script = textwrap.dedent(
        f"""\
        import ctypes, importlib.util, sys

        class NonLinuxLibc:
            def __init__(self, *args, **kwargs):
                pass

            def __getattr__(self, name):
                raise AttributeError(f"dlsym(RTLD_DEFAULT, {{name}}): symbol not found")

        ctypes.CDLL = NonLinuxLibc
        sys.platform = "darwin"
        spec = importlib.util.spec_from_file_location("ks", {str(KERNEL_SANDBOX)!r})
        ks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ks)
        try:
            ks.apply(["/usr"], ["/tmp"])
        except ks.KernelSandboxUnavailable as exc:
            print("unavailable:", exc)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("unavailable:"), proc.stdout + proc.stderr


@not_linux
def test_required_kernel_limits_report_sandbox_unavailable(monkeypatch):
    monkeypatch.delenv(KERNEL_LIMITS_ENV, raising=False)
    result = run_build(SMALL_BUILD, {"size": 10.0})
    assert not result.ok
    assert result.error.kind == "sandbox_unavailable", result.error
    assert not result.kernel_limits


@not_linux
def test_optional_kernel_limits_run_the_build_without_them(monkeypatch):
    monkeypatch.setenv(KERNEL_LIMITS_ENV, "optional")
    # A generous limit: this checks the build runs at all, not how fast a busy laptop is.
    result = run_build(SMALL_BUILD, {"size": 10.0}, timeout_s=300)
    assert result.ok, result.error
    assert set(result.shapes) == {"block"}
    assert not result.kernel_limits
