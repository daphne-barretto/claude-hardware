"""The design agent handles any request, not only the board revision.

A session with no board, no spec and no existing design is a first-class case: the agent's
tools work there, and it can write, build and check a first design. The system prompt and the
tool descriptions name no board, part or dimension of the example in `demos/b-parasite/`.
"""

from __future__ import annotations

import asyncio
import json
import re

from app.agent.events import EventSink
from app.agent.run import SYSTEM_PROMPT, run_agent
from app.agent.scripted import ScriptedDriver, box_design
from app.agent.services import GeometryServices
from app.agent.tools import TOOLS, Workspace
from app.boards import BOARDS_DIR, load_board
from app.builds import BuildStore
from app.standards import load_standards

BOX = "make a box 40 x 30 x 20 mm with 2 mm walls"


def empty_workspace(tmp_path, sink: EventSink | None = None) -> Workspace:
    """A session's workspace before anyone brought a board or a design."""
    standards = load_standards()
    return Workspace(
        standards=standards,
        services=GeometryServices(None, standards, store=BuildStore(tmp_path / "builds")),
        sink=sink or EventSink("msg-1"),
    )


def scenario_words() -> set[str]:
    """Names from the example in `demos/b-parasite/`: board names and revisions, component
    references."""
    words: set[str] = set()
    for path in BOARDS_DIR.glob("*.json"):
        board = load_board(path)
        words |= {board.name, f"{board.name}-{board.revision}"}
        words |= {c.ref for c in board.components if len(c.ref) > 1}
    return words


def mentions(text: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_])", text) is not None


def test_the_agent_creates_a_first_design_in_a_session_with_no_board(tmp_path) -> None:
    ws = empty_workspace(tmp_path)
    code, params = box_design(40, 30, 20, 2, str(ws.standards.get("material").value))
    steps = [
        ("read_spec", {}),
        ("read_board", {}),
        ("build", {}),
        ("write_design", {"code": code, "params": params}),
        ("run_checks", {}),
        # A standard the design fails is the user's to accept: Claude asks before finishing.
        (
            "ask_user",
            lambda ws: (
                None
                if not any(i.standard for i in ws.failing())
                else {
                    "question": "The box fails some standards. Keep it as it is?",
                    "options": [
                        {"id": "keep", "label": "Keep it"},
                        {"id": "stop", "label": "Stop"},
                    ],
                }
            ),
        ),
        ("finish", {"outcome": "done", "summary": "A 40 x 30 x 20 mm box with 2 mm walls."}),
    ]

    async def keep(question_id, text, options):
        return "keep"

    ws.answer = keep
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, BOX, driver=driver))
    spec, board, build, write, checks, *asked, finish = driver.calls

    assert not spec.is_error and json.loads(spec.text)["design"] is None
    assert not board.is_error and "no board" in board.text.lower()
    assert build.is_error and "write_design" in build.text  # nothing to build yet: says how
    assert not write.is_error and not checks.is_error and not finish.is_error
    assert result.error is None and result.finished["outcome"] == "done"
    built = [e.build for e in ws.sink.of_type("build_ready")]
    assert built and built[-1].status == "ready" and built[-1].board is None


def test_the_system_prompt_is_a_general_hardware_design_assistant() -> None:
    first = SYSTEM_PROMPT.split("\n\n")[0].lower()
    assert "enclosure" not in first and "board" not in first
    for word in scenario_words():
        assert not mentions(SYSTEM_PROMPT, word), word


def test_no_tool_names_the_example() -> None:
    text = json.dumps(TOOLS)
    for word in scenario_words():
        assert not mentions(text, word), word
