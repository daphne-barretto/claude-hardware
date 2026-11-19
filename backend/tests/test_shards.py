"""`pytest --shard=k/n` splits the suite for CI's parallel test jobs."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# This directory's conftest, by its path: run with tests/edge, `conftest` is the edge one's.
_spec = importlib.util.spec_from_file_location(
    "tests_conftest", Path(__file__).with_name("conftest.py")
)
assert _spec is not None and _spec.loader is not None
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)
parse_shard, shard_items = _conftest.parse_shard, _conftest.shard_items

BACKEND = Path(__file__).resolve().parents[1]


def collect(*args: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if "::" in line]


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7])
def test_shards_split_the_items_without_overlap_or_gaps(total):
    items = list(range(23))
    shards = [shard_items(items, k, total) for k in range(1, total + 1)]
    kept = [item for shard, _ in shards for item in shard]
    assert sorted(kept) == items
    for shard, deselected in shards:
        assert sorted(shard + deselected) == items
        assert len(shard) in (len(items) // total, len(items) // total + 1)


def test_the_whole_suite_runs_across_the_shards():
    files = ["tests/test_standards.py", "tests/test_boards.py", "tests/test_shards.py"]
    everything = collect(*files)
    assert len(everything) > 4
    shards = [collect(*files, f"--shard={k}/4") for k in range(1, 5)]
    assert sorted(t for shard in shards for t in shard) == sorted(everything)
    assert all(shards)


@pytest.mark.parametrize("value", ["1/4", "4/4", "1/1"])
def test_parses_valid_shards(value):
    k, n = parse_shard(value)
    assert 1 <= k <= n


@pytest.mark.parametrize("value", ["0/4", "5/4", "2", "a/b", "1/0"])
def test_rejects_bad_shards(value):
    with pytest.raises(ValueError):
        parse_shard(value)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", f"--shard={value}"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--shard" in result.stderr
