"""Photo to part with the real model: the evaluation set in `evals/photos/`. Slow and
billed, so on demand only (the `agent-eval` workflow runs it):

    cd backend && .venv/bin/pytest -m slow tests/test_photo_live.py

Each photo goes to Claude attached to its request, as in the chat. It is scored on two things:
did the part build, and did the checks run on it. `test_photo.py` checks the set and the
scoring without a model.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path

import pytest
import yaml

from app.agent.events import EventSink
from app.agent.run import RunConfig, RunResult, empty_workspace, run_agent
from app.agent.tools import AttachedFile, Workspace

PHOTOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "photos"


def photo_set() -> list[dict]:
    return yaml.safe_load((PHOTOS_DIR / "photos.yaml").read_text())["photos"]


def photo_file(entry: dict) -> AttachedFile:
    path = PHOTOS_DIR / entry["file"]
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return AttachedFile(path.name, media_type, path.read_bytes())


def score(ws: Workspace, result: RunResult) -> dict:
    """Did the part build, and did the checks run on the design Claude left?"""
    return {
        "built": any(b.ok for b in ws.builds),
        "checked": ws.report is not None,
        "estimated": ws.object is not None,
        "error": result.error,
    }


def score_lines(scores: dict[str, dict]) -> list[str]:
    lines = []
    for photo, s in scores.items():
        line = f"{photo}: built={s['built']} checked={s['checked']} estimated={s['estimated']}"
        lines.append(line + (f" error={s['error']}" if s["error"] else ""))
    return lines


async def first_option(question_id: str, text: str, options: list[dict]) -> str:
    """A person who takes the first option of every question."""
    return options[0]["id"]


def photo_workspace(n: int) -> Workspace:
    """A new session's workspace, as the empty chat starts it: no board, no design."""
    return empty_workspace(sink=EventSink(f"msg-{n}"), answer=first_option)


async def one_photo(n: int, entry: dict) -> tuple[Workspace, RunResult]:
    ws = photo_workspace(n)
    file = photo_file(entry)
    ws.attach([file])
    prompt = (
        f"{entry['request']}\n\nAttached files, in your workspace: read each with "
        f"read_attachment by its name.\n- {file.name} ({file.media_type}): a photo, shown with "
        "this message"
    )
    return ws, await run_agent(ws, prompt, RunConfig())


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        reason="needs ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN",
    ),
]


def test_every_photo_builds_a_part_and_runs_its_checks():
    async def runs():
        entries = photo_set()
        return entries, await asyncio.gather(*(one_photo(n, e) for n, e in enumerate(entries, 1)))

    entries, outcomes = asyncio.run(runs())
    scores = {e["id"]: score(ws, r) for e, (ws, r) in zip(entries, outcomes, strict=True)}
    lines = score_lines(scores)
    print("\n".join(["", "photo-eval report:", *lines]))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## photo-eval\n\n" + "".join(f"- {line}\n" for line in lines))
    assert all(s["built"] and s["checked"] for s in scores.values()), lines
