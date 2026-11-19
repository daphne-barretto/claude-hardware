"""The Docker image ships `demos/b-parasite/` and the enclosure generator finds it there."""

import os
import subprocess
import sys
from pathlib import Path

import yaml

from app.boards import BOARDS_DIR, REPO_BOARDS_DIR
from geometry import selftest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def boards_dir_in(env: dict[str, str]) -> str:
    """`BOARDS_DIR` as a fresh interpreter resolves it under `env`."""
    return subprocess.run(
        [sys.executable, "-c", "from app.boards import BOARDS_DIR; print(BOARDS_DIR)"],
        cwd=BACKEND,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_boards_dir_defaults_to_the_repo() -> None:
    # The example board's folder in demos/: the one with the examples list.
    assert REPO_BOARDS_DIR.parent == ROOT / "demos"
    assert (REPO_BOARDS_DIR / "examples.yaml").is_file()
    # Unset or empty means the repo's directory.
    assert Path(boards_dir_in({"BOARDS_DIR": ""})) == REPO_BOARDS_DIR


def test_boards_dir_env_var_overrides(tmp_path: Path) -> None:
    assert Path(boards_dir_in({"BOARDS_DIR": str(tmp_path)})) == tmp_path


def test_boards_dir_follows_demos_dir(tmp_path: Path) -> None:
    (tmp_path / "some-board").mkdir()
    (tmp_path / "some-board" / "examples.yaml").write_text("examples: []\n")
    env = {"BOARDS_DIR": "", "DEMOS_DIR": str(tmp_path)}
    assert Path(boards_dir_in(env)) == tmp_path / "some-board"


def test_image_copies_demos_and_sets_boards_dir() -> None:
    lines = [line.split() for line in (ROOT / "Dockerfile").read_text().splitlines()]
    copies = {(words[1], words[2]) for words in lines if words[:1] == ["COPY"] and len(words) == 3}
    envs = [words[1] for words in lines if words[:1] == ["ENV"] and len(words) == 2]
    (dest,) = [dst for src, dst in copies if src.rstrip("/") == "demos"]
    # The board folder is found in the demos, so the image names no board.
    assert f"DEMOS_DIR={dest}" in envs
    assert not any(env.startswith("BOARDS_DIR=") for env in envs)
    # The build context must not exclude the demos.
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert not any(pattern.strip("/*") == "demos" for pattern in ignored)


def test_ci_runs_the_selftest_inside_the_built_image() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    steps = jobs["image"]["steps"]
    runs = [step.get("run", "") for step in steps]
    build = next(i for i, run in enumerate(runs) if run.startswith("docker build"))
    selftest_steps = [i for i, run in enumerate(runs) if "python -m geometry.selftest" in run]
    assert selftest_steps and selftest_steps[0] > build
    assert "docker run" in runs[selftest_steps[0]]


def test_selftest_covers_every_board_revision() -> None:
    files = selftest.board_files()
    assert files and all(f.parent == BOARDS_DIR for f in files)
    assert {f.name for f in files} == {f.name for f in BOARDS_DIR.glob("*.json")}


def test_selftest_generates_and_exports_every_revision(tmp_path: Path) -> None:
    exported = selftest.run(tmp_path)
    assert [p.name for p in exported] == [
        f"{f.stem}-enclosure.step" for f in selftest.board_files()
    ]
    assert all(p.stat().st_size > 0 for p in exported)


def test_selftest_fails_without_board_data(tmp_path: Path) -> None:
    try:
        selftest.run(tmp_path, boards_dir=tmp_path)
    except RuntimeError as error:
        assert "no board revisions" in str(error)
    else:
        raise AssertionError("selftest passed with no boards")
