"""What people read never shows internal IDs (QA fix briefs 2 and 7): a
change's reason and a map's flagged spots name checks and standards by their titles, and
Claude's text from separate messages doesn't run together.
"""

from __future__ import annotations

import asyncio
import json
import re

from test_agent import workspace

from app.agent.proposal import links, reason
from app.agent.risks import find_risks
from app.agent.run import run_agent
from app.agent.services import FIXTURES_DIR, CheckItem
from app.contracts.changes import CheckLink, GeometryDelta
from app.standards import load_standards

STANDARDS = load_standards()
WALL = next(s for s in STANDARDS.standards if s.check == "wall_thickness")


def test_a_change_names_the_checks_it_fixes_by_their_titles() -> None:
    d = GeometryDelta(kind="modified", path="wall")
    fixes = CheckLink(
        id=WALL.check, check=WALL.check, title=WALL.title, effect="fixes", before="fail"
    )
    breaks = CheckLink(id="draft_angle", check="draft_angle", effect="breaks", before="pass")
    text = reason(d, [fixes, breaks], [1], {})
    assert f"Fixes {WALL.title}." in text
    assert "On its own it fails Draft angle." in text
    assert WALL.check not in text and "draft_angle" not in text


def test_a_check_link_carries_the_results_title() -> None:
    d = GeometryDelta(kind="modified", path="wall")
    before = {WALL.check: CheckItem(WALL.check, WALL.check, "fail", title=WALL.title)}
    alone = {WALL.check: CheckItem(WALL.check, WALL.check, "pass", title=WALL.title)}
    (link,) = links(None, d, before, alone, None)  # type: ignore[arg-type]
    assert (link.effect, link.title) == ("fixes", WALL.title)


def test_flagged_spots_and_risks_name_the_standard_by_its_title() -> None:
    ids = {s.id for s in STANDARDS.standards}
    for path in sorted((FIXTURES_DIR / "maps").glob("*.json")):
        amap = json.loads(path.read_text())
        title = STANDARDS.get(amap["standard"]).title
        assert amap.get("standard_title") == title
        for spot in amap["flagged"]:
            assert title in spot["message"], spot["message"]
            assert not ids & set(re.findall(r"\w+", spot["message"])), spot["message"]
    for standard in (s for s in STANDARDS.standards if s.kind in ("min", "max")):
        limit = float(standard.value)
        measured = limit - 0.001 if standard.kind == "min" else limit + 0.001
        item = CheckItem(
            f"{standard.check}:x",
            standard.check,
            "pass",
            standard=standard.id,
            measured=measured,
            required=limit,
            unit=standard.unit,
        )
        (risk,) = find_risks([item], [], STANDARDS)
        assert f"({standard.id})" not in risk.what, risk.what


def _texts(ws) -> str:
    return "".join(e.delta for e in ws.sink.of_type("text"))


def test_text_from_separate_messages_is_separate_paragraphs() -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def driver(prompt, options):
        yield AssistantMessage(content=[TextBlock("Set the mode to auto.")], model="m")
        yield AssistantMessage(content=[TextBlock("The new revision changes:")], model="m")
        yield AssistantMessage(content=[TextBlock("A"), TextBlock("B")], model="m")
        yield ResultMessage("success", 1, 1, False, 1, "s")

    ws = workspace()
    asyncio.run(run_agent(ws, "go", driver=driver))
    assert _texts(ws) == "Set the mode to auto.\n\nThe new revision changes:\n\nA\n\nB"


def test_streamed_text_blocks_are_separate_paragraphs() -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

    def event(kind: str, **data) -> StreamEvent:
        return StreamEvent("u", "s", {"type": kind, **data})

    def block(text: str) -> list[StreamEvent]:
        return [
            event("content_block_start", content_block={"type": "text", "text": ""}),
            event("content_block_delta", delta={"type": "text_delta", "text": text[:4]}),
            event("content_block_delta", delta={"type": "text_delta", "text": text[4:]}),
            event("content_block_stop"),
        ]

    async def driver(prompt, options):
        for e in block("Mode is auto."):
            yield e
        yield AssistantMessage(content=[TextBlock("Mode is auto.")], model="m")
        for e in block("The new revision changes:"):
            yield e
        yield AssistantMessage(content=[TextBlock("The new revision changes:")], model="m")
        yield ResultMessage("success", 1, 1, False, 1, "s")

    ws = workspace()
    asyncio.run(run_agent(ws, "go", driver=driver))
    assert _texts(ws) == "Mode is auto.\n\nThe new revision changes:"
