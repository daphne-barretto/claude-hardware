"""Repo conventions: the env template and ignored secrets."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_env_example_lists_anthropic_api_key() -> None:
    lines = (ROOT / ".env.example").read_text().splitlines()
    keys = {
        line.split("=", 1)[0].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert "ANTHROPIC_API_KEY" in keys


def test_env_is_gitignored() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False)
    assert result.returncode == 0, ".env must be gitignored"


def test_ci_job_is_named_ci() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "\n  ci:\n" in text
    assert "pull_request" in text
