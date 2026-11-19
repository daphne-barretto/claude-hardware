"""The Docker image's runtime stage ships everything the live session API reads.

The test rebuilds the image's file layout from the `Dockerfile` (its runtime-stage COPY and
ENV lines), with `/app` mapped to a temporary directory, and starts a session there. It runs
in a subprocess so the repo checkout, which has every file, can't hide a missing one.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def runtime_stage(dockerfile: Path) -> list[str]:
    """The instructions of the stage named `runtime`, with continuation lines joined."""
    lines: list[str] = []
    buf = ""
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        buf += line[:-1] + " " if line.endswith("\\") else line
        if not line.endswith("\\"):
            lines.append(buf)
            buf = ""
    stage: list[str] = []
    inside = False
    for line in lines:
        if line.upper().startswith("FROM "):
            inside = line.split()[-1] == "runtime"
            continue
        if inside:
            stage.append(line)
    return stage


def build_layout(root: Path) -> dict[str, str]:
    """Copy the runtime stage's files from the repo into `root` (standing in for `/`)."""
    workdir = "/"
    env: dict[str, str] = {}
    for line in runtime_stage(REPO / "Dockerfile"):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "WORKDIR":
            workdir = rest.strip()
            (root / workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)
        elif word == "COPY":
            args = shlex.split(rest)
            if any(a.startswith("--from") for a in args):
                continue
            *srcs, dest = args
            dest_path = root / os.path.join(workdir, dest).lstrip("/")
            for src in srcs:
                source = REPO / src
                target = dest_path / source.name if dest.endswith("/") else dest_path
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        elif word == "ENV":
            parts = shlex.split(rest)
            if parts and "=" not in parts[0]:
                env[parts[0]] = " ".join(parts[1:])
            else:
                env.update(p.split("=", 1) for p in parts)
    return {
        k: v.replace("/app", str(root / "app")) if v.startswith("/app") else v
        for k, v in env.items()
    }


SCRIPT = """
import sys
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
r = c.post("/api/sessions", json={})
assert r.status_code == 201, (r.status_code, r.text)
sid = r.json()["id"]
r = c.get(f"/api/sessions/{sid}/parameters")
assert r.status_code == 200, (r.status_code, r.text)
r = c.post(f"/api/sessions/{sid}/messages", json={"text": "make a box"})
assert r.status_code == 202, (r.status_code, r.text)
print("ok")
"""


def test_image_layout_can_start_a_session_and_take_a_message(tmp_path: Path) -> None:
    env = build_layout(tmp_path)
    backend = tmp_path / "app" / "backend"
    assert (backend / "app" / "main.py").exists(), "the runtime stage should copy the backend"
    # No credentials: the message must be accepted without starting a real Claude run.
    dropped = {"FIXTURES_DIR", "PYTHONPATH", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    run_env = {k: v for k, v in os.environ.items() if k not in dropped}
    run_env.update(env)
    run_env.pop("PORT", None)
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=backend,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-3000:]
