"""Shared test setup.

Every test writes its data (builds, exports, and sessions) under a temporary `DATA_DIR`, and
records its sessions' telemetry in a temporary directory, never in the repository's
`data/` or the developer's real log directory. A test that needs its own directory sets the
variable itself. A run's automatic retries (`app.agent.limits.POLICY`) don't wait: a test of
the backoff passes its own policy with a fake clock. The Models API is never called
(`app.model_availability`).

Test sharding for CI.

`pytest --shard=k/n` runs the k-th of n shards: every n-th collected test, starting at
the k-th. Each CI shard job runs one of them, so the suite's wall time is split
across runners. Taking every n-th test (not every n-th file) spreads the slow
parametrized groups and the slow files evenly. Without `--shard` every test runs.
"""

from __future__ import annotations

import os
import sys

import pytest

# Off Linux (local macOS development) the build sandbox can't apply its kernel limits, so builds
# run with the Python guard only, as `make edge` runs them there; the tests of the
# kernel limits themselves are Linux-only (tests/test_sandbox.py).
if not sys.platform.startswith("linux"):
    os.environ.setdefault("GEOMETRY_SANDBOX_KERNEL_LIMITS", "optional")


@pytest.fixture(autouse=True)
def _data_dirs(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
    monkeypatch.setenv("TELEMETRY_DIR", str(tmp_path_factory.mktemp("telemetry")))


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    from app.agent import limits

    async def no_wait(seconds: float) -> None:
        return None

    monkeypatch.setattr(limits.POLICY, "sleep", no_wait)


@pytest.fixture(autouse=True)
def _no_models_api(monkeypatch):
    """Never the real Models API: every catalogue model is available unless a test says not."""
    from app.contracts.models import MODEL_CATALOG
    from app.model_availability import AVAILABILITY

    monkeypatch.setattr(AVAILABILITY, "fetch", lambda api_key: sorted(MODEL_CATALOG.ids()))
    AVAILABILITY.reset()
    yield
    AVAILABILITY.reset()


# The abuse limits (`app.security`) are tested in `test_security*.py`, which set their own
# values; every other test runs with room to spare, and each test starts with fresh counts.
ROOMY_LIMITS = {
    "RUNS_PER_HOUR": "100000",
    "SESSIONS_PER_DAY": "100000",
    "MESSAGES_PER_MINUTE": "100000",
    "WRITES_PER_MINUTE": "100000",
    "DAILY_BUDGET_USD": "100000",
}


@pytest.fixture(autouse=True)
def _roomy_limits(monkeypatch):
    from app.security.limits import COUNTERS
    from app.security.web import STREAMS

    for name, value in ROOMY_LIMITS.items():
        monkeypatch.setenv(name, value)
    COUNTERS.reset()
    STREAMS.by_owner.clear()
    STREAMS.total = 0
    yield
    COUNTERS.reset()


def parse_shard(value: str) -> tuple[int, int]:
    """'2/3' -> (2, 3). Raises ValueError unless 1 <= k <= n."""
    k, sep, n = value.partition("/")
    if not sep:
        raise ValueError(f"expected k/n, got {value!r}")
    index, total = int(k), int(n)
    if not 1 <= index <= total:
        raise ValueError(f"shard {value!r} is out of range")
    return index, total


def shard_items(items: list, index: int, total: int) -> tuple[list, list]:
    """Split collected items into (kept, deselected) for shard index of total."""
    kept = [item for i, item in enumerate(items) if i % total == index - 1]
    deselected = [item for i, item in enumerate(items) if i % total != index - 1]
    return kept, deselected


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        default="",
        help="run only shard k of n (k/n): every n-th test, starting at the k-th",
    )


def pytest_configure(config):
    value = config.getoption("--shard")
    if value:
        try:
            parse_shard(value)
        except ValueError as e:
            raise pytest.UsageError(f"--shard: {e}") from e


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    value = config.getoption("--shard")
    if not value:
        return
    kept, deselected = shard_items(items, *parse_shard(value))
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
