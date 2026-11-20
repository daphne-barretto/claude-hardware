"""A build sees only the CPUs its container may use, so OpenCascade doesn't start a thread per
host CPU and run out of address space under the build's memory cap."""

from geometry import sandbox
from geometry._sandbox_child import cpus_for_quota

HOST = list(range(16))


def test_a_one_cpu_quota_on_a_16_cpu_host_pins_the_build_to_one_cpu():
    assert cpus_for_quota("100000 100000\n", HOST) == [0]


def test_a_fractional_quota_rounds_up():
    assert cpus_for_quota("150000 100000", HOST) == [0, 1]


def test_no_quota_keeps_every_cpu():
    assert cpus_for_quota("max 100000", HOST) == HOST
    assert cpus_for_quota("", HOST) == HOST


def test_the_build_process_uses_few_malloc_arenas(tmp_path):
    assert sandbox._child_env(str(tmp_path))["MALLOC_ARENA_MAX"] == "2"
