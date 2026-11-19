"""How much an instance runs at once (`geometry.capacity`, docs/architecture.md "Memory"): sized
from the machine's memory (a container's cgroup limit first) and CPUs when `BUILD_CONCURRENCY` and
`MAX_AGENT_RUNS` are unset; a variable that is set always wins."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from geometry import capacity as cap
from geometry.capacity import Machine, capacity, size

GB = 1024


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    for env in (cap.CONCURRENCY_ENV, cap.RUNS_ENV, cap.MEMORY_ENV):
        monkeypatch.delenv(env, raising=False)


@pytest.mark.parametrize(
    ("memory_mb", "cpus", "builds", "runs"),
    [
        (512, 1, 1, 1),  # Render free: too small for a build, but never less than one of each
        (2 * GB, 1, 1, 1),  # the documented minimum: one build, one run, as before
        (2 * GB, 2, 1, 1),
        (4 * GB, 2, 2, 4),
        (4 * GB, 1, 1, 3),  # one CPU: one build slot; the memory it leaves pays for runs
        (8 * GB, 4, 4, 8),
        (8 * GB, 8, 4, 8),  # memory caps the builds: each comes with one agent CLI
        (16 * GB, 8, 8, 18),  # a 16 GB, 8-CPU laptop
        (16 * GB, 4, 4, 12),  # CPUs cap the builds; runs are a multiple of builds
        (64 * GB, 64, 40, 41),  # at least one run per build slot
    ],
)
def test_sizes_from_memory_and_cpus(memory_mb, cpus, builds, runs) -> None:
    assert size(memory_mb, cpus) == (builds, runs)


def test_sizes_never_go_below_one_and_unknown_memory_keeps_one_of_each() -> None:
    assert size(0, 0) == (1, 1)
    assert size(None, 8) == (1, 1)
    assert size(None, 8, builds=3) == (3, 1)


def test_a_bigger_build_budget_means_fewer_build_slots() -> None:
    assert size(8 * GB, 8, build_memory=768)[0] == 4
    assert size(8 * GB, 8, build_memory=2048)[0] == 2


def test_runs_are_sized_around_build_slots_that_are_set(monkeypatch) -> None:
    machine = Machine(16 * GB, 8, "os")
    monkeypatch.setenv(cap.CONCURRENCY_ENV, "1")
    c = capacity(machine)
    assert (c.build_slots, c.build_slots_from) == (1, "env")
    assert (c.agent_runs, c.agent_runs_from) == (3, "auto")


def test_set_variables_win(monkeypatch) -> None:
    machine = Machine(16 * GB, 8, "os")
    auto = capacity(machine)
    assert (auto.build_slots, auto.agent_runs) == (8, 18)
    assert auto.build_slots_from == auto.agent_runs_from == "auto"
    monkeypatch.setenv(cap.CONCURRENCY_ENV, "2")
    monkeypatch.setenv(cap.RUNS_ENV, "5")
    c = capacity(machine)
    assert (c.build_slots, c.agent_runs) == (2, 5)
    assert c.build_slots_from == c.agent_runs_from == "env"
    # A tiny machine still runs what it is told to.
    c = capacity(Machine(512, 1, "cgroup"))
    assert (c.build_slots, c.agent_runs) == (2, 5)


def test_bad_values(monkeypatch) -> None:
    machine = Machine(4 * GB, 2, "cgroup")
    monkeypatch.setenv(cap.RUNS_ENV, "lots")  # as before: a bad MAX_AGENT_RUNS is ignored
    assert capacity(machine).agent_runs == 4
    monkeypatch.setenv(cap.CONCURRENCY_ENV, "0")  # a bad BUILD_CONCURRENCY is an error
    with pytest.raises(ValueError, match=cap.CONCURRENCY_ENV):
        capacity(machine)


def test_cgroup_v2_memory_and_cpu(tmp_path) -> None:
    (tmp_path / "memory.max").write_text(f"{2 * 1024**3}\n")
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert cap.cgroup_memory_mb(tmp_path) == 2 * GB
    assert cap.cgroup_cpus(tmp_path) == 2  # 1.5 CPUs, rounded up
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert cap.cgroup_memory_mb(tmp_path) is None
    assert cap.cgroup_cpus(tmp_path) is None


def test_cgroup_v1_memory_and_cpu(tmp_path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "cpu").mkdir()
    (tmp_path / "memory" / "memory.limit_in_bytes").write_text(f"{512 * 1024**2}\n")
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("50000\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert cap.cgroup_memory_mb(tmp_path) == 512
    assert cap.cgroup_cpus(tmp_path) == 1
    # No limit: v1 writes a number near 2**63, and -1 for the CPU quota.
    (tmp_path / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("-1\n")
    assert cap.cgroup_memory_mb(tmp_path) is None
    assert cap.cgroup_cpus(tmp_path) is None


def test_no_cgroup(tmp_path) -> None:
    assert cap.cgroup_memory_mb(tmp_path) is None
    assert cap.cgroup_cpus(tmp_path) is None


def test_os_memory_on_macos_and_linux(tmp_path) -> None:
    assert cap.os_memory_mb("Darwin", sysctl=lambda: "17179869184\n") == 16 * GB
    assert cap.os_memory_mb("Darwin", sysctl=lambda: "nonsense") is None
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        4028856 kB\nMemFree:          123456 kB\n")
    assert cap.os_memory_mb("Linux", meminfo=meminfo) == 3934
    assert cap.os_memory_mb("Linux", meminfo=tmp_path / "missing") is None


def test_a_container_uses_its_cgroup_limit_not_the_hosts_memory(tmp_path, monkeypatch) -> None:
    (tmp_path / "memory.max").write_text(f"{2 * 1024**3}\n")
    monkeypatch.setattr(cap, "os_memory_mb", lambda: 64 * GB)
    monkeypatch.setattr(cap, "os_available_mb", lambda: 40 * GB)
    monkeypatch.setattr(cap, "os_cpus", lambda: 32)
    (tmp_path / "cpu.max").write_text("100000 100000\n")
    assert cap.detect(tmp_path) == Machine(2 * GB, 1, "cgroup")
    # Outside a container: half the machine's own memory (it runs other things too), and its CPUs.
    (tmp_path / "memory.max").unlink()
    (tmp_path / "cpu.max").unlink()
    assert cap.detect(tmp_path) == Machine(32 * GB, 32, "os", 64 * GB)
    monkeypatch.setattr(cap, "os_memory_mb", lambda: None)
    assert cap.detect(tmp_path) == Machine(None, 32, "unknown")


def test_health_reports_the_sizes_without_secrets(monkeypatch, tmp_path) -> None:
    from app.main import app

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(cap.RUNS_ENV, "3")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    with TestClient(app) as client:
        r = client.get("/api/health")
    body = r.json()["capacity"]
    assert body["agent_runs"] == 3 and body["agent_runs_from"] == "env"
    assert body["build_slots"] >= 1 and body["build_slots_from"] == "auto"
    assert body["build_memory_mb"] == cap.DEFAULT_MEMORY_MB
    assert body["cpus"] >= 1 and body["memory_source"] in ("cgroup", "os", "unknown")
    assert "sk-ant" not in r.text


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    25211.
Pages active:                                 309104.
Pages inactive:                               304264.
Pages speculative:                              3960.
Pages wired down:                             189939.
"""


def test_available_memory_on_macos_and_linux(tmp_path) -> None:
    pages = 25211 + 304264 + 3960
    assert cap.os_available_mb("Darwin", vm_stat=lambda: VM_STAT) == pages * 16384 // 1024**2
    assert cap.os_available_mb("Darwin", vm_stat=lambda: "nonsense") is None
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  4028512 kB\nMemAvailable:  2014256 kB\n")
    assert cap.os_available_mb("Linux", meminfo=meminfo) == 1967


def test_a_laptop_leaves_headroom(tmp_path, monkeypatch) -> None:
    """A 16 GB Mac (no cgroup) sizes from about half its memory, or less when less is free:
    about 3 to 4 build slots, not the 8 its whole memory and CPUs would allow."""
    monkeypatch.setattr(cap, "os_memory_mb", lambda: 16 * GB)
    monkeypatch.setattr(cap, "os_cpus", lambda: 8)
    monkeypatch.setattr(cap, "os_available_mb", lambda: 12 * GB)  # plenty free: half of it
    found = cap.detect(tmp_path)
    assert found == Machine(8 * GB, 8, "os", 16 * GB)
    assert capacity(found).build_slots == 4
    assert capacity(found).total_memory_mb == 16 * GB
    # Less free than half (a browser and an editor open): the free memory.
    monkeypatch.setattr(cap, "os_available_mb", lambda: 5209)
    assert capacity(cap.detect(tmp_path)).build_slots == 3
    # Unreadable: half.
    monkeypatch.setattr(cap, "os_available_mb", lambda: None)
    assert cap.detect(tmp_path).memory_mb == 8 * GB
    # A container keeps its whole limit, however much is free on the host.
    (tmp_path / "memory.max").write_text(f"{8 * 1024**3}\n")
    assert cap.detect(tmp_path) == Machine(8 * GB, 8, "cgroup")
    assert capacity(cap.detect(tmp_path)).build_slots == 4
