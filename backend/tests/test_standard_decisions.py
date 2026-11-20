"""Nothing breaks a team standard or changes a locked value silently: the user decides.

- A hand edit whose rebuild makes a check against a standard fail opens a decision: which
  standard, measured against required, where on the model, and the options (keep the value and
  accept the failure, let Claude adjust the rest, revert the edit). The value is never reverted
  on its own.
- Claude may not trade a standard or a locked value away by itself: `finish` "done" with a
  standard the run made fail is refused until the user has answered a question in the run.

The checks here are made up (a wall check against the `min_wall` standard), so the tests hold
for any board: no part or parameter is special.
"""

from __future__ import annotations

import pytest
import test_session_api as api
from test_session_api import CONFIG, SESSIONS, events, last_id, send, start, wait_until

from app.contracts.session import validate_event

# The session API tests' fixtures: the API on fast services, and Claude played by a script.
client, claude = api.client, api.claude

POINT = [1.0, 2.0, 3.0]


class WallCheckServices:
    """Instant builds; one check, `wall_thickness` against `min_wall`, that fails when the
    design's `wall` is under the standard's minimum."""

    def __init__(self, board, standards) -> None:
        from app.agent.services import FixtureServices

        self.board = board
        self.standards = standards
        self._ids = FixtureServices()._ids

    def build(self, design):
        from app.agent.services import BuildOutcome, make_build

        return BuildOutcome(make_build(f"build-{next(self._ids)}", self.board), shapes=design)

    def checks(self, outcome):
        from app.agent.services import CheckItem

        minimum = float(self.standards.get("min_wall").value)
        wall = float(outcome.shapes["params"]["wall"])
        return [
            CheckItem(
                "wall_thickness",
                "wall_thickness",
                "pass" if wall >= minimum else "fail",
                standard="min_wall",
                part="case",
                measured=wall,
                required=minimum,
                unit="mm",
                summary=f"The thinnest wall is {wall} mm.",
                point=POINT,
            )
        ]

    def analyze(self, outcome):
        return []


@pytest.fixture
def walls(monkeypatch) -> None:
    monkeypatch.setattr(CONFIG, "services", WallCheckServices)


def minimum_wall() -> float:
    from app.standards import load_standards

    return float(load_standards().get("min_wall").value)


def edit_below_the_standard(client) -> tuple[str, float, float, dict]:
    """A session whose hand edit puts the wall under the standard; the decision it opens."""
    sid, _ = start(client)
    original = SESSIONS.get(sid).ws.design.params["wall"]
    value = round(minimum_wall() - 0.5, 3)
    after = last_id(client, sid)
    r = client.patch(f"/api/sessions/{sid}/parameters/wall", json={"value": value})
    assert r.status_code == 200, r.text
    found = wait_until(client, sid, "waiting_for_answer", after)
    (question,) = [d for _, t, d in found if t == "question"]
    return sid, original, value, question


def test_a_hand_edit_that_breaks_a_standard_asks_the_user(client, claude, walls) -> None:
    sid, original, value, question = edit_below_the_standard(client)
    validate_event(question)
    assert {o["id"] for o in question["options"]} == {"keep", "adjust", "revert"}
    assert question["location"]["point"] == POINT
    conflict = question["conflict"]
    kinds = {c["kind"]: c for c in conflict["constraints"]}
    assert kinds["locked"]["id"] == "wall" and kinds["locked"]["value"] == value
    assert kinds["standard"]["id"] == "min_wall" and kinds["standard"]["relation"] == ">="
    assert conflict["over_by"] == pytest.approx(minimum_wall() - value)
    found = events(client, sid)
    said = "".join(d["delta"] for _, t, d in found if t == "text")
    assert "min_wall" in said and str(value) in said and str(minimum_wall()) in said
    # Never reverted on its own: the value stands, locked, until the user decides.
    ws = SESSIONS.get(sid).ws
    assert ws.design.params["wall"] == value and "wall" in ws.design.locked
    # A message meanwhile doesn't wait for the decision: it goes to Claude, the question is
    # left unanswered, and the edit stands as it is.
    claude.scripts.append([("text", "Carrying on.")])
    after = last_id(client, sid)
    send(client, sid, text="Carry on.")
    found = wait_until(client, sid, "idle", after)
    assert "Carrying on." in "".join(d["delta"] for _, t, d in found if t == "text")
    assert "left open" in claude.prompts[-1]
    assert ws.design.params["wall"] == value and "wall" in ws.design.locked
    assert SESSIONS.get(sid).edit_decision is None


def test_reverting_the_edit_puts_the_value_back_and_rebuilds(client, walls) -> None:
    sid, original, value, question = edit_below_the_standard(client)
    after = last_id(client, sid)
    answer = {"question_id": question["question_id"], "option_id": "revert"}
    send(client, sid, answer=answer)
    found = wait_until(client, sid, "idle", after)
    ws = SESSIONS.get(sid).ws
    assert ws.design.params["wall"] == original and "wall" not in ws.design.locked
    assert any(t == "build_ready" for _, t, _ in found)
    assert found[-1][1] == "message_end"


def test_keeping_the_value_accepts_the_failure_and_tells_claude(client, claude, walls) -> None:
    sid, original, value, question = edit_below_the_standard(client)
    after = last_id(client, sid)
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "keep"})
    found = wait_until(client, sid, "idle", after)
    assert found[-1][1] == "message_end"
    ws = SESSIONS.get(sid).ws
    assert ws.design.params["wall"] == value and "wall" in ws.design.locked

    claude.scripts.append([("text", "Noted.")])
    after = last_id(client, sid)
    send(client, sid, text="What now?")
    wait_until(client, sid, "idle", after)
    prompt = claude.prompts[-1]
    assert "min_wall" in prompt and "wall" in prompt and "accepted" in prompt.lower()


def test_letting_claude_adjust_starts_a_run_that_keeps_the_value(client, claude, walls) -> None:
    sid, original, value, question = edit_below_the_standard(client)
    claude.scripts.append([("text", "I'll look at the other values.")])
    after = last_id(client, sid)
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "adjust"})
    wait_until(client, sid, "idle", after)
    prompt = claude.prompts[-1]
    assert "wall" in prompt and "min_wall" in prompt and str(value) in prompt
    assert SESSIONS.get(sid).ws.design.params["wall"] == value


def test_claude_cant_finish_done_after_breaking_a_standard_on_its_own(
    client, claude, walls
) -> None:
    sid, found = start(client)
    low = round(minimum_wall() - 0.5, 3)
    claude.scripts.append(
        [
            ("set_parameter", {"name": "wall", "value": low}),
            ("run_checks", {}),
            ("finish", {"outcome": "done", "summary": "Thinner walls."}),
        ]
    )
    send(client, sid, text="Make the walls thinner.")
    wait_until(client, sid, "idle", found[-1][0])
    finish = claude.drivers[-1].calls[-1]
    assert finish.tool == "finish" and finish.is_error
    assert "min_wall" in finish.text and "ask_user" in finish.text


def test_claude_finishes_done_once_the_user_accepts_the_failure(client, claude, walls) -> None:
    sid, found = start(client)
    low = round(minimum_wall() - 0.5, 3)
    claude.scripts.append(
        [
            ("set_parameter", {"name": "wall", "value": low}),
            ("run_checks", {}),
            (
                "ask_user",
                {
                    "question": "The thinner wall fails min_wall. Keep it?",
                    "options": [
                        {"id": "keep", "label": "Keep the thinner wall"},
                        {"id": "back", "label": "Go back to the standard"},
                    ],
                },
            ),
            ("finish", {"outcome": "done", "summary": "Thinner walls, as you chose."}),
        ]
    )
    send(client, sid, text="Make the walls thinner.")
    asked = wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in asked if t == "question"]
    send(client, sid, answer={"question_id": question["question_id"], "option_id": "keep"})
    wait_until(client, sid, "idle", asked[-1][0])
    finish = claude.drivers[-1].calls[-1]
    assert finish.tool == "finish" and not finish.is_error, finish.text


def test_the_rules_say_only_the_user_trades_a_standard_or_a_lock_away() -> None:
    from app.agent.run import SYSTEM_PROMPT

    rules = " ".join(SYSTEM_PROMPT.split()).lower()
    assert "only the user" in rules
    assert "near" in rules and "ask_user" in rules
