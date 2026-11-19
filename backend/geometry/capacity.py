"""How much this instance runs at once, sized from the machine it runs on (`docs/architecture.md`).

Two limits keep an instance inside its memory:

- build slots (`BUILD_CONCURRENCY`): sandboxed builds and geometry workers at once
  (`geometry.slots`). Each takes about `BUILD_LIBS_MB` for build123d and OpenCascade plus up to
  `BUILD_MEMORY_MB` for its own geometry.
- agent runs (`MAX_AGENT_RUNS`): runs talking to Claude at once (`app.session_api`). Each starts
  an agent CLI of about `AGENT_RUN_MB`; a run spends most of its time waiting on the API, so
  there are several per build slot (`RUNS_PER_BUILD`).

Unset, both are sized from the machine: its memory (the container's cgroup limit when there is
one, in full; else, on a desktop or laptop that runs other things too, about half the machine's
memory, or the memory available now if that is less: `sysctl hw.memsize` and `vm_stat` on
macOS, `/proc/meminfo` on Linux) and its CPUs (the cgroup CPU quota or affinity when set). The
server and Next.js (`SERVER_MB`) come off first; what is left is split into build slots of
`BUILD_LIBS_MB + BUILD_MEMORY_MB` plus one agent CLI each (at least one slot, at most one per
CPU). Agent runs are `RUNS_PER_BUILD` per build slot, as many as the memory the build slots
leave pays for (at least one). A variable that is set always wins, and the agent runs are then
sized around the build slots it sets.
Nothing here loads the CAD kernel.
"""

from __future__ import annotations

import math
import os
import platform
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

CONCURRENCY_ENV = "BUILD_CONCURRENCY"
RUNS_ENV = "MAX_AGENT_RUNS"
MEMORY_ENV = "BUILD_MEMORY_MB"
# Fits a 2 GB instance: the server (~150 MB), Next.js (~100 MB), the agent CLI (~250 MB) and
# a build's own libraries (~450 MB) leave about 1 GB; see docs/architecture.md.
DEFAULT_MEMORY_MB = 768

SERVER_MB = 256  # the FastAPI server (56 MB) and Next.js (100 MB), with room to spare
AGENT_RUN_MB = 300  # one agent CLI during a run
BUILD_LIBS_MB = 550  # one sandboxed build or geometry worker, before its own geometry
RUNS_PER_BUILD = 3  # runs mostly wait on the API, so several share a build slot

CGROUP = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")
_UNLIMITED = 1 << 60  # cgroup v1 writes "no limit" as a number near 2**63


def positive_int(env: str, default: int | None) -> int | None:
    """A positive whole number from `env`, `default` when unset. Raises ValueError otherwise."""
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{env} must be a positive whole number, not {raw!r}") from None
    if value < 1:
        raise ValueError(f"{env} must be a positive whole number, not {raw!r}")
    return value


def build_memory_mb() -> int:
    """`BUILD_MEMORY_MB`: the memory a build may use on top of its libraries (default 768)."""
    value = positive_int(MEMORY_ENV, DEFAULT_MEMORY_MB)
    assert value is not None
    return value


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def cgroup_memory_mb(root: Path = CGROUP) -> int | None:
    """The container's memory limit (cgroup v2 `memory.max`, else v1 `memory.limit_in_bytes`),
    or None when there is none."""
    for path in (root / "memory.max", root / "memory" / "memory.limit_in_bytes"):
        raw = _read(path)
        if raw is None or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if 0 < value < _UNLIMITED:
            return value // (1024 * 1024)
    return None


def os_memory_mb(
    system: str | None = None,
    meminfo: Path = MEMINFO,
    sysctl: Callable[[], str] | None = None,
) -> int | None:
    """The machine's physical memory: `sysctl hw.memsize` on macOS, `/proc/meminfo` on Linux."""
    system = system or platform.system()
    if system == "Darwin":
        try:
            raw = sysctl() if sysctl else _sysctl_memsize()
            return int(raw.strip()) // (1024 * 1024)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    text = _read(meminfo)
    for line in (text or "").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024  # kB
    return None


def os_available_mb(
    system: str | None = None,
    meminfo: Path = MEMINFO,
    vm_stat: Callable[[], str] | None = None,
) -> int | None:
    """The memory free for new work now: free, inactive and speculative pages (`vm_stat`) on
    macOS, `MemAvailable` on Linux. None when it can't be read."""
    system = system or platform.system()
    if system == "Darwin":
        try:
            text = vm_stat() if vm_stat else _vm_stat()
        except (OSError, subprocess.SubprocessError):
            return None
        page = re.search(r"page size of (\d+) bytes", text)
        counts = dict(re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", text))
        if page is None or "free" not in counts:
            return None
        pages = sum(int(n) for n in counts.values())
        return pages * int(page.group(1)) // (1024 * 1024)
    for line in (_read(meminfo) or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024  # kB
    return None


def _vm_stat() -> str:
    return subprocess.run(["vm_stat"], capture_output=True, text=True, check=True, timeout=5).stdout


def _sysctl_memsize() -> str:
    return subprocess.run(
        ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True, timeout=5
    ).stdout


def cgroup_cpus(root: Path = CGROUP) -> int | None:
    """The container's CPU quota, rounded up (cgroup v2 `cpu.max`, else v1 `cpu.cfs_*`)."""
    raw = _read(root / "cpu.max")
    if raw is not None:
        quota, _, period = raw.partition(" ")
        if quota != "max":
            try:
                return max(1, math.ceil(int(quota) / int(period or 100000)))
            except ValueError:
                pass
        return None
    v1 = root / "cpu"
    quota_s, period_s = _read(v1 / "cpu.cfs_quota_us"), _read(v1 / "cpu.cfs_period_us")
    try:
        quota, period = int(quota_s or ""), int(period_s or "")
    except ValueError:
        return None
    return max(1, math.ceil(quota / period)) if quota > 0 and period > 0 else None


def os_cpus() -> int:
    """The CPUs this process may run on."""
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


# Outside a container the server shares the machine (a desktop or laptop: the browser, an
# editor), so it sizes from this share of the memory, or what is available now if less.
DESKTOP_SHARE = 0.5


@dataclass(frozen=True)
class Machine:
    memory_mb: int | None  # what the sizing uses; None: couldn't tell
    cpus: int
    memory_source: str  # "cgroup", "os" or "unknown"
    total_mb: int | None = None  # outside a container: the machine's whole memory


def desktop_memory_mb(total: int, available: int | None) -> int:
    """The memory to size from on a machine that isn't a container: about half of it, or the
    memory available now if that is less, so the rest of the machine keeps its headroom."""
    share = int(total * DESKTOP_SHARE)
    return min(share, available) if available else share


def detect(root: Path = CGROUP) -> Machine:
    """This machine's memory and CPUs, from its cgroup limits if it has any."""
    cpus = os_cpus()
    quota = cgroup_cpus(root)
    if quota is not None:
        cpus = min(cpus, quota)
    container = cgroup_memory_mb(root)
    machine = os_memory_mb()
    if container is not None and (machine is None or container <= machine):
        return Machine(container, cpus, "cgroup")  # a container keeps its whole limit
    if machine is not None:
        return Machine(desktop_memory_mb(machine, os_available_mb()), cpus, "os", machine)
    return Machine(None, cpus, "unknown")


@cache
def machine() -> Machine:
    """`detect()`, once per process."""
    return detect()


def size(
    memory_mb: int | None,
    cpus: int,
    build_memory: int = DEFAULT_MEMORY_MB,
    builds: int | None = None,
) -> tuple[int, int]:
    """(build slots, agent runs) for a machine with `memory_mb` and `cpus`; (1, 1) when the
    memory is unknown, as before sizing existed. `builds` fixes the build slots (set by
    `BUILD_CONCURRENCY`), and the agent runs are sized around them."""
    if memory_mb is None:
        return builds or 1, 1
    per_build = BUILD_LIBS_MB + build_memory
    if builds is None:
        # Each build slot comes with the memory for at least one agent CLI, so runs always fit.
        builds = (memory_mb - SERVER_MB) // (per_build + AGENT_RUN_MB)
        builds = max(1, min(builds, max(1, cpus)))
    left = memory_mb - SERVER_MB - builds * per_build
    runs = max(1, min(builds * RUNS_PER_BUILD, left // AGENT_RUN_MB))
    return builds, runs


@dataclass(frozen=True)
class Capacity:
    build_slots: int
    agent_runs: int
    build_memory_mb: int
    memory_mb: int | None
    cpus: int
    memory_source: str
    total_memory_mb: int | None  # outside a container: the machine's whole memory
    build_slots_from: str  # "env" (BUILD_CONCURRENCY) or "auto"
    agent_runs_from: str  # "env" (MAX_AGENT_RUNS) or "auto"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        memory = f"{self.memory_mb} MB ({self.memory_source})" if self.memory_mb else "unknown"
        if self.total_memory_mb:
            memory += f" of {self.total_memory_mb} MB"
        return (
            f"{self.build_slots} build slots ({self.build_slots_from}), "
            f"{self.agent_runs} agent runs ({self.agent_runs_from}), "
            f"{self.build_memory_mb} MB per build; memory {memory}, {self.cpus} CPUs"
        )


def _runs_from_env() -> int | None:
    """`MAX_AGENT_RUNS`, if set to a positive whole number (anything else: sized)."""
    try:
        return positive_int(RUNS_ENV, None)
    except ValueError:
        return None


def capacity(found: Machine | None = None) -> Capacity:
    """The limits now: each variable that is set, else sized from `found` (this machine)."""
    found = found or machine()
    build_memory = build_memory_mb()
    builds = positive_int(CONCURRENCY_ENV, None)
    runs = _runs_from_env()
    auto_builds, auto_runs = size(found.memory_mb, found.cpus, build_memory, builds)
    return Capacity(
        build_slots=builds or auto_builds,
        agent_runs=runs or auto_runs,
        build_memory_mb=build_memory,
        memory_mb=found.memory_mb,
        cpus=found.cpus,
        memory_source=found.memory_source,
        total_memory_mb=found.total_mb,
        build_slots_from="env" if builds else "auto",
        agent_runs_from="env" if runs else "auto",
    )


def build_slots() -> int:
    return capacity().build_slots


def agent_runs() -> int:
    return capacity().agent_runs


__all__ = [
    "Capacity",
    "Machine",
    "agent_runs",
    "build_memory_mb",
    "build_slots",
    "capacity",
    "cgroup_cpus",
    "cgroup_memory_mb",
    "desktop_memory_mb",
    "detect",
    "machine",
    "os_available_mb",
    "os_memory_mb",
    "size",
]
