"""Runtime facts about the running build: version, commit and data directory.

Everything here comes from the environment so the same image runs locally, in CI,
on staging and on demo.
"""

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE = "claude-hardware-backend"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Render sets RENDER_GIT_COMMIT for every deploy; GIT_COMMIT covers local and CI builds.
COMMIT_VARS = ("GIT_COMMIT", "RENDER_GIT_COMMIT")


def app_version() -> str:
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "0.0.0"


def commit_sha() -> str:
    for name in COMMIT_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return "unknown"


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def data_dir() -> Path:
    """The writable data directory. It may be empty or missing (Render's free plan wipes it)."""
    configured = os.environ.get("DATA_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DATA_DIR


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
