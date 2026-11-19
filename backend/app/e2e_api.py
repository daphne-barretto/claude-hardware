"""The real backend with a scripted Claude, for end-to-end tests without a model or an API key.

`uvicorn app.e2e_api:app` serves `app.main` unchanged: the session API, the agent loop with its
tools and hooks, builds and checks. Only the model is replaced, by `ScriptedDriver` playing
`script()`. The board example's builds are the contract fixtures (`FixtureServices`), which are
fast; a design without a board is built and checked for real (`fixture_services`), since no
fixture can stand in for a design written from scratch.
Playwright runs it next to the mock API (`frontend/e2e/live-chat.spec.ts`), so the chat panel
is tested against the real endpoints in CI.

After the question, Accept ends the run done ("Done.", `Finished`) and Reject stops it there
("Kept the current design.", `Stopped after your answer: Reject`).

A message starting with `MOVE_OPENINGS` moves every opening that `demos/<board>/CHANGES.md` lists
as moved to where the new board needs it, and rebuilds: a rebuild that moves an opening, for the
canvas test against real builds (`frontend/e2e/live-canvas.spec.ts`).

A message starting with `TAKE_SLOT` or `HOLD_SLOT` takes a run slot (`MAX_AGENT_RUNS`; the
browser tests run this server with 1) as Claude's runs do, so two browsers can test the queue
(`frontend/e2e/live-users.spec.ts`): a `HOLD_SLOT` run keeps its slot until another run waits
in line behind it, then `HOLD_SECONDS` more, so the waiting run's place in line shows.
Other scripted runs take no slot, so the browser tests don't wait on each other.

A message starting with `SLOW_RUN` takes `SLOW_RUN_SECONDS` before it replies, so a test can send
more while Claude works (the message queue) or stop the run.

The script reports what reached the run: it writes the model, effort and mode from the agent's
options and prompt, and the selected face IDs from the prompt, so a test can check them in the
chat as well as in `GET /messages`. The scenario comes from `demos/<board>/CHANGES.md`.

A message starting with "Simulate" plays a limit or failure instead of Claude
(`app.agent.faults`, `FAULT_PROMPTS`), so the browser tests see what the chat says about each:
"Simulate rate limit" is retried by the CLI twice and then goes through; "Simulate out of
credits", "Simulate overloaded", "Simulate bad API key", "Simulate too long" and "Simulate
network error" fail as the API would (the last is retried by the run, then goes through);
"Simulate slow reply" takes `SLOW_SECONDS`, long enough to drop its stream or restart the server
under it, and finishes at once when retried. `POST /api/e2e/sessions/{id}/drop-streams` ends
the session's event streams (the browser reconnects); `POST /api/e2e/sessions/{id}/restart`
does to one session what a server restart does (its run is cut off, it loads again from the
store).

A session with no board is designed from scratch, as Claude would: "make a box L x W x H mm with
T mm walls" writes an open box of those sizes, and "an L bracket with two M4 holes" an L
bracket, each built and checked (`write_design`, `run_checks`) and finished with a summary and the
part's name (`title`: "Open box", "M4 L-bracket"), which names the session; a
design that fails a standard first asks whether to keep it (only the user accepts a failure).
Anything else there gets a question back.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter

from app.agent import limits
from app.agent.faults import FlakyDriver, real_sleep
from app.agent.scripted import ScriptedDriver, box_design, bracket_design
from app.agent.tools import Workspace
from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.contracts.session import MessageCreate
from app.main import app
from app.session_api import CONFIG, SESSIONS, SessionState, fixture_services
from app.standards import StandardsFile, load_standards

QUESTION = "Apply the proposed changes?"
MOVE_OPENINGS = "Move the openings"
TAKE_SLOT = "Take a run slot"
HOLD_SLOT = "Hold the run slot"
HOLDING = "Holding the run slot until another run waits for it."
HOLD_SECONDS = 3.0
HOLD_LIMIT_SECONDS = 60.0  # nobody came: let go anyway
SLOW_RUN = "Take your time"
SLOW_RUN_SECONDS = 6.0
SLOW_SECONDS = 40.0
SLOW_DONE = "Finished the slow reply."
RETRIED = "Picked up where the stopped reply left off."
# Message prefix -> (fault, FlakyDriver options). Delays are short so the tests stay fast.
FAULT_PROMPTS: dict[str, tuple[str, dict[str, Any]]] = {
    "Simulate rate limit": (
        "rate_limit",
        {"cli_retries": 2, "retry_delay_ms": 3000, "recover": True, "failures": 1},
    ),
    "Simulate overloaded": ("overloaded", {"cli_retries": 2, "retry_delay_ms": 1000}),
    "Simulate out of credits": ("billing", {}),
    "Simulate bad API key": ("auth", {}),
    "Simulate too long": ("too_large", {}),
    "Simulate network error": ("network", {"failures": 1}),
}
OPTIONS = [
    {"id": "accept", "label": "Accept", "description": "Apply the changes and rebuild"},
    {"id": "reject", "label": "Reject", "description": "Keep the current design"},
]


def moved_openings() -> dict[str, Any]:
    """`set_parameter` edits that put each moved opening where the new board has it."""
    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    new = load_board(BOARDS_DIR / doc.new_file)
    edits: dict[str, Any] = {}
    for row in doc.rows:
        if not row["Change"].startswith("moved"):
            continue
        component, kind = row["Component"].strip("`"), row["Opening"].strip("`")
        for o in new.openings:
            if o.component == component and o.kind == kind:
                edits[f"openings.{o.id}.x"] = o.position[0]
                edits[f"openings.{o.id}.y"] = o.position[1]
                edits[f"openings.{o.id}.rotation"] = o.rotation
    return edits


_NUMBER = r"(\d+(?:\.\d+)?)"
_BOX = re.compile(rf"\bbox\b.*?{_NUMBER}\s*[x×]\s*{_NUMBER}\s*[x×]\s*{_NUMBER}\s*mm", re.I)
_WALL = re.compile(rf"{_NUMBER}\s*mm\s+walls?", re.I)
_BRACKET = re.compile(r"\bL[ -]?bracket\b", re.I)
_SCREW = re.compile(r"\bM(\d+(?:\.\d+)?)\b")
# What Claude might choose for a bracket the request gives no sizes for (mm).
BRACKET_SIZE = {"length": 40.0, "width": 20.0, "height": 30.0, "thickness": 3.0}
CLEARANCE = 1.1  # a clearance hole is this much wider than its screw


def from_scratch(text: str, standards: StandardsFile) -> list[tuple[str, Any]] | None:
    """A design from scratch for the requests the stand-in knows: a box or an L bracket."""
    material = str(standards.get("material").value) if _has(standards, "material") else ""
    box = _BOX.search(text)
    if box:
        length, width, height = (float(v) for v in box.groups())
        wall = _WALL.search(text)
        thickness = float(wall.group(1)) if wall else 2.0
        code, params = box_design(length, width, height, thickness, material)
        what = f"an open box {length:g} x {width:g} x {height:g} mm with {thickness:g} mm walls"
        name = "Open box"
    elif _BRACKET.search(text):
        screw = _SCREW.search(text)
        hole = round(float(screw.group(1)) * CLEARANCE, 2) if screw else 4.5
        code, params = bracket_design(**BRACKET_SIZE, hole_diameter=hole, material=material)
        what = f"an L bracket with a {hole:g} mm clearance hole in each leg"
        name = f"M{screw.group(1)} L-bracket" if screw else "L-bracket"
    else:
        return None
    return [
        ("read_spec", {}),
        ("write_design", {"code": code, "params": params}),
        ("run_checks", {}),
        ("text", f"I designed {what}; every size is a parameter."),
        # Only the user can accept a failing standard: ask before finishing with one.
        ("ask_user", _accept_failures),
        (
            "finish",
            lambda ws: (
                {"outcome": "done", "summary": f"Designed {what}.", "title": name}
                if _kept(ws)
                else {
                    "outcome": "blocked",
                    "summary": f"Designed {what}; you'll change it.",
                    "title": name,
                }
            ),
        ),
    ]


def _failing_standards(ws: Workspace) -> list[str]:
    return sorted({i.standard for i in ws.failing() if i.standard})


def _accept_failures(ws: Workspace) -> dict | None:
    """The question a design that fails standards ends with (None: it fails none)."""
    if not _failing_standards(ws):
        return None
    # The failing checks by their titles, as people read them, not the standards' IDs.
    titles = sorted({i.title or i.check for i in ws.failing() if i.standard})
    return {
        "question": f"The design fails {', '.join(titles)}. Keep it as it is?",
        "options": [
            {"id": "keep", "label": "Keep it", "description": "Accept the failing checks"},
            {"id": "change", "label": "I'll change it", "description": "Stop here"},
        ],
    }


def _kept(ws: Workspace) -> bool:
    """The design fails no standard, or the user kept it anyway."""
    asked = [q for q in ws.questions if q.text.startswith("The design fails") and q.answer]
    return not _failing_standards(ws) or (bool(asked) and asked[-1].answer == "keep")


def _has(standards: StandardsFile, standard_id: str) -> bool:
    return any(s.id == standard_id for s in standards.standards)


def script(
    body: MessageCreate, prompt: str, options: Any, ws: Workspace | None = None
) -> list[tuple[str, Any]]:
    """The run for one message: what Claude would do, given what reached it. `ws` is the
    session's workspace (default: one with a board)."""
    received = (
        f"Run with model {options.model or 'default'}, effort {options.effort or 'default'}, "
        f"mode {'manual' if 'Mode: Manual' in prompt else 'auto'}."
    )
    selected = (body.selection.faces if body.selection else []) or []
    if body.text.startswith(SLOW_RUN):  # a reply in words: nothing to finish
        return [("text", f"{received} Took my time over: {body.text}")]
    if body.text.startswith("Simulate"):
        if "pressed Retry" in prompt:
            return [("text", RETRIED)]
        return [("text", "Claude is back: the reply went through.")]
    if body.text.startswith((TAKE_SLOT, HOLD_SLOT)):
        return [
            ("text", f"{received} Had a run slot."),
            ("finish", {"outcome": "blocked", "summary": "Had a run slot."}),
        ]
    if selected:
        # The faces as Claude got them in the prompt, not as the client sent them.
        in_prompt = [f for f in selected if f in prompt]
        return [
            ("text", f"{received} Selected faces: {', '.join(in_prompt) or 'none'}."),
            ("finish", {"outcome": "blocked", "summary": "Answered about the selection."}),
        ]
    if body.text.startswith(MOVE_OPENINGS):
        edits = moved_openings()
        return [
            ("text", received),
            ("set_parameter", {"changes": [{"name": k, "value": v} for k, v in edits.items()]}),
            ("run_checks", {}),
            ("text", f"Moved {', '.join(sorted({k.split('.')[1] for k in edits}))}."),
            ("finish", {"outcome": "blocked", "summary": "Moved the openings."}),
        ]
    if ws is not None and ws.board is None:
        steps = from_scratch(body.text, ws.standards or load_standards())
        if steps is not None:
            return [("text", received), *steps]
        options_ = [
            {"id": "box", "label": "A box", "description": "An open box with walls"},
            {"id": "bracket", "label": "An L bracket", "description": "Two legs, a hole in each"},
        ]
        return [
            ("text", received),
            ("read_spec", {}),
            ("ask_user", {"question": "What should I design?", "options": options_}),
            ("finish", {"outcome": "blocked", "summary": "Waiting on what to design."}),
        ]
    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    changes = "".join(f"\n- {row['Component']}: {row['Change']}" for row in doc.rows)
    return [
        ("text", received),
        ("read_spec", {}),
        ("read_board", {"revision": "new"}),
        ("run_checks", {}),
        ("text", f"The new revision changes:{changes}"),
        ("ask_user", {"question": QUESTION, "options": OPTIONS}),
        ("text", lambda ws: "Done." if _accepted(ws) else "Kept the current design."),
        (
            "finish",
            lambda ws: (
                {"outcome": "done", "summary": "Checked the new revision."}
                if _accepted(ws)
                else {"outcome": "blocked", "summary": "Kept the current design."}
            ),
        ),
    ]


def _accepted(ws: Workspace) -> bool:
    """The user answered Accept to the question: the run ends done, else it stops there."""
    asked = [q for q in ws.questions if q.text == QUESTION and q.answer]
    return bool(asked) and asked[-1].answer == "accept"


class StandInClaude(ScriptedDriver):
    """Builds its script once the run starts, when the agent's options are known."""

    def __init__(self, state: SessionState, body: MessageCreate, prompt: str):
        super().__init__(state.ws, [])
        self.body, self.prompt = body, prompt
        # Scripted runs start no Claude CLI, so only these take a run slot (`_run`).
        self.uses_run_slot = body.text.startswith((TAKE_SLOT, HOLD_SLOT))

    def __call__(self, prompt: str, options: Any):
        self.steps = script(self.body, prompt, options, self.ws)
        return super().__call__(prompt, options)

    async def _play(self) -> AsyncIterator[Any]:
        if self.body.text.startswith(SLOW_RUN):
            from claude_agent_sdk import AssistantMessage, TextBlock

            yield AssistantMessage(content=[TextBlock("Working on it.")], model="scripted")
            await asyncio.sleep(SLOW_RUN_SECONDS)
        if self.body.text.startswith(SLOW) and "pressed Retry" not in self.prompt:
            from claude_agent_sdk import AssistantMessage, TextBlock

            yield AssistantMessage(content=[TextBlock("Working on the slow reply.")], model="s")
            await asyncio.sleep(SLOW_SECONDS)
            yield AssistantMessage(content=[TextBlock(SLOW_DONE)], model="scripted")
            return
        if self.body.text.startswith(HOLD_SLOT):
            from claude_agent_sdk import AssistantMessage, TextBlock

            yield AssistantMessage(content=[TextBlock(HOLDING)], model="scripted")
            deadline = time.monotonic() + HOLD_LIMIT_SECONDS
            while not SESSIONS.queue and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            await asyncio.sleep(HOLD_SECONDS)
        async for message in super()._play():
            yield message


SLOW = "Simulate slow reply"


def stand_in(state: SessionState, body: MessageCreate, prompt: str) -> Any:
    """Claude played by the script, or by a fault for a "Simulate ..." message."""
    claude = StandInClaude(state, body, prompt)
    for prefix, (fault, opts) in FAULT_PROMPTS.items():
        if body.text.startswith(prefix) and "pressed Retry" not in prompt:
            return FlakyDriver(fault, claude, sleep=real_sleep, **opts)
    return claude


CONFIG.driver = stand_in
# The run's own retries (`limits.RetryPolicy`) wait a second, not ten.
limits.POLICY.base_s = 1.0

test_routes = APIRouter()


@test_routes.post("/api/e2e/sessions/{session_id}/drop-streams", include_in_schema=False)
async def drop_streams(session_id: str) -> dict:
    """End the session's event streams, as a dropped connection would: browsers reconnect."""
    state = SESSIONS.get(session_id)
    state.end_streams()
    await asyncio.sleep(0.5)
    state.closed = False
    return {"dropped": True}


@test_routes.post("/api/e2e/sessions/{session_id}/restart", include_in_schema=False)
async def restart(session_id: str) -> dict:
    """What a server restart does to one session (`Sessions.close`): its run is cut off as
    saved, its streams end, and it loads again from the store on the next request."""
    state = SESSIONS.get(session_id)
    state.store = None  # the saved session stays as the restart found it
    state.end_streams()
    for task in (state.run, state.initial):
        if task is not None and not task.done():
            task.cancel()
    SESSIONS.by_id.pop(session_id, None)
    return {"restarted": True}


app.include_router(test_routes)
# The board example's builds are the contract fixtures, so the browser tests don't wait on real
# geometry; the real services are walked through the demo in backend/tests/test_session_api.py,
# and the canvas on real builds in frontend/e2e/live-canvas.spec.ts. A design without a board
# is built for real.
CONFIG.services = fixture_services

__all__ = [
    "HOLDING",
    "HOLD_SLOT",
    "MOVE_OPENINGS",
    "OPTIONS",
    "QUESTION",
    "SLOW_RUN",
    "SLOW_RUN_SECONDS",
    "TAKE_SLOT",
    "app",
    "from_scratch",
    "moved_openings",
    "script",
]
