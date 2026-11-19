"""The design agent: tools, hooks, locks, conflicts, rounds and the event stream.

These run without a model: `ScriptedDriver` plays tool calls through the same hooks and tools
the SDK would. `test_agent_live.py` runs the real model (slow, on demand).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import dataclass, field

import pytest
from PIL import Image

from app.agent.design import Design, get_path
from app.agent.events import EventSink
from app.agent.hooks import make_hooks
from app.agent.limits import stopped_text
from app.agent.run import UNFINISHED, RunConfig, options_for, run_agent, scenario_workspace
from app.agent.scripted import ScriptedDriver
from app.agent.services import (
    BuildOutcome,
    CheckItem,
    FixtureServices,
    GeometryServices,
    items_from_report,
    make_build,
)
from app.agent.tools import ATTACHMENT_CHARS, MAX_ROUNDS, TOOLS, AttachedFile, tool_name
from app.boards import BOARDS_DIR
from app.boards.changes import parse_changes_md
from app.contracts.models import MODEL_CATALOG
from app.contracts.session import validate_recording
from app.parameters import LockedParameterError
from app.standards import load_standards
from geometry.conflicts import conflict_locks, detect_conflicts

TOOL_NAMES = {
    "read_spec",
    "propose_plan",
    "read_board",
    "read_attachment",
    "write_design",
    "set_parameter",
    "build",
    "run_checks",
    "analyze",
    "find_in_model",
    "flag_risk",
    "ask_user",
    "reply_to_comment",
    "safety_notice",
    "finish",
    "estimate_object",
    "set_object_dimension",
}


@dataclass
class FakeServices:
    """Instant builds; the checks pass once `fixed(params)` is true."""

    board: object
    fixed: object = lambda params: False
    built: list = field(default_factory=list)

    def build(self, design):
        self.built.append(design)
        return BuildOutcome(make_build(f"build-{len(self.built)}", self.board), shapes=design)

    def checks(self, outcome):
        ok = self.fixed(outcome.shapes["params"])
        return [
            CheckItem("wall_thickness", "wall_thickness", "pass", standard="min_wall"),
            CheckItem("openings:X", "openings", "pass" if ok else "fail", component="X"),
        ]

    def analyze(self, outcome):
        return []


def workspace(answers: list[str] | None = None, fixed=lambda p: False, **kw):
    pending = list(answers or [])

    async def answer(question_id, text, options):
        return pending.pop(0)

    ws = scenario_workspace(answer=answer if answers is not None else None, **kw)
    if "services" not in kw:
        ws.services = FakeServices(ws.board, fixed)
    return ws


def run(ws, steps, prompt="Here is the new revision.", config=None):
    driver = ScriptedDriver(ws, steps)
    result = asyncio.run(run_agent(ws, prompt, config, driver=driver))
    return result, driver


def locked_wall(ws) -> float:
    """A hand edit: the wall half a millimetre thicker, locked."""
    value = ws.design.params["wall"] + 0.5
    ws.design.lock("wall", value)
    return value


def lock_conflict(ws) -> tuple[str, float]:
    """Lock the conflict fixture (`geometry.conflicts.conflict_locks`): a value that can't meet
    the clearance standard within the height limit."""
    ((path, value),) = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    ws.design.lock(path, value)
    return path, value


# --- Tools and options ------------------------------------------------------------------------


def test_the_mcp_server_offers_exactly_the_issues_tools():
    ws = workspace()
    assert set(TOOLS) == TOOL_NAMES
    server = ws.mcp_server()
    assert server["type"] == "sdk" and server["name"] == "hardware"


def test_options_give_only_the_tools_hooks_and_the_run_model_and_effort(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_EFFORT", raising=False)
    ws = workspace()
    opts = options_for(ws, RunConfig(model="claude-opus-5-5", effort="high").resolved())
    assert opts.tools == []
    assert set(opts.allowed_tools) == {tool_name(t) for t in TOOL_NAMES}
    assert opts.model == "claude-opus-5-5" and opts.effort == "high"
    assert opts.setting_sources == []
    edit = f"{tool_name('write_design')}|{tool_name('set_parameter')}"
    # First a catch-all that denies any tool that isn't the app's (docs/security.md).
    assert [m.matcher for m in opts.hooks["PreToolUse"]] == [None, edit]
    assert [m.matcher for m in opts.hooks["PostToolUse"]] == [edit]
    assert "4 fix rounds" in opts.system_prompt


def test_model_and_effort_default_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("AGENT_EFFORT", "low")
    config = RunConfig().resolved()
    assert (config.model, config.effort) == ("claude-sonnet-5-5", "low")
    config = RunConfig(model="claude-haiku-5-5", effort="max").resolved()
    assert (config.model, config.effort) == ("claude-haiku-5-5", "max")


def test_without_a_model_the_agent_uses_the_catalog_default(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    assert RunConfig().resolved().model == MODEL_CATALOG.default == "claude-opus-5-5"


def test_read_attachment_gives_text_in_pages_images_as_images_and_refuses_binary():
    board = BOARDS_DIR / parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text()).new_file
    (step,) = BOARDS_DIR.glob("*.step")
    out = io.BytesIO()
    Image.new("RGB", (8, 6), (1, 2, 3)).save(out, "PNG")
    png = out.getvalue()
    ws = workspace()
    ws.attach(
        [
            AttachedFile(f"rev/{board.name}", "application/json", board.read_bytes()),
            AttachedFile(step.name, "model/step", step.read_bytes()),
            AttachedFile("photo.png", "image/png", png),
            AttachedFile("blob.bin", "application/octet-stream", b"\x00\xff\x00"),
            AttachedFile("broken.png", "image/png", b"\x89PNG\r\n\x1a\n" + bytes(range(256))),
        ]
    )

    def read(args):
        return asyncio.run(ws.read_attachment(args))

    out = read({"name": f"rev/{board.name}"})
    assert not out.get("is_error")
    header, body = out["content"][0]["text"].split("\n\n", 1)
    assert json.loads(untrusted_body(body)) == json.loads(board.read_text())
    assert "application/json" in header

    text = step.read_bytes().decode("utf-8")
    pages, offset = [], 0
    while True:
        out = read({"name": step.name, "offset": offset})
        header, page = out["content"][0]["text"].split("\n\n", 1)
        page = untrusted_body(page)
        pages.append(page)
        offset += len(page)
        if "read on with offset" not in header:
            break
        assert f"offset {offset}" in header
    whole = "".join(pages) == text  # not in the assert: pytest's diff of 1 MB would take minutes
    assert whole, "the pages don't add up to the file"
    assert len(pages) == -(-len(text) // ATTACHMENT_CHARS)

    out = read({"name": "photo.png"})
    image = out["content"][1]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == png

    assert read({"name": "blob.bin"})["is_error"]
    broken = read({"name": "broken.png"})  # the API would refuse it, and the whole run
    assert broken["is_error"] and "not a readable image" in broken["content"][0]["text"]
    missing = read({"name": "nope"})
    assert missing["is_error"] and step.name in missing["content"][0]["text"]
    assert read({"name": step.name, "offset": -1})["is_error"]


def test_read_spec_and_read_board_come_from_the_data():
    ws = workspace()
    spec = json.loads(asyncio.run(ws.read_spec({}))["content"][0]["text"])
    assert {s["id"] for s in spec["standards"]} == {s.id for s in ws.standards.standards}
    assert spec["board"]["name"] == ws.board.name
    assert spec["board"]["revision"] == ws.board.revision
    assert spec["board"]["previous_revision"] == ws.previous.revision
    assert spec["board_changes"] == ws.changes and ws.changes
    assert spec["design"]["params"] == ws.design.params
    for revision, board in (("new", ws.board), ("previous", ws.previous)):
        out = asyncio.run(ws.read_board({"revision": revision}))
        assert json.loads(out["content"][0]["text"])["revision"] == board.revision
    assert asyncio.run(ws.read_board({"revision": "nope"}))["is_error"]


# --- Locked values ----------------------------------------------------------------------------


def test_locked_values_are_never_edited_without_approval():
    ws = workspace()
    value = locked_wall(ws)
    before = ws.design.snapshot()
    opening = ws.design.params["openings"][0]["id"]
    whole = dict(ws.design.params, wall=value + 1)
    steps = [
        ("set_parameter", {"name": "wall", "value": value - 0.5}),
        ("set_parameter", {"changes": [{"name": "wall", "value": 2}]}),
        ("write_design", {"params": whole}),
        ("text", "I can't change the wall."),
        ("set_parameter", {"name": f"openings.{opening}.x", "value": 1.0}),
    ]
    result, driver = run(ws, steps)
    assert [c.denied is not None for c in driver.calls] == [True, True, True, False]
    assert all("locked" in c.denied for c in driver.calls[:3])
    assert ws.design.params["wall"] == value == before["params"]["wall"]
    assert ws.design.locked == {"wall": value}
    assert get_path(ws.design.params, f"openings.{opening}.x") == 1.0


def test_the_tools_refuse_locked_edits_even_without_the_hook():
    ws = workspace()
    value = locked_wall(ws)
    out = asyncio.run(ws.set_parameter({"name": "wall", "value": value + 1}))
    assert out["is_error"] and "locked" in out["content"][0]["text"]
    with pytest.raises(LockedParameterError):
        ws.design.apply(dict(ws.design.params, wall=value + 1))
    assert ws.design.params["wall"] == value and ws.rounds == 0


def test_a_lock_inside_a_list_survives_replacing_the_list():
    ws = workspace()
    opening = ws.design.params["openings"][0]["id"]
    ws.design.lock(f"openings.{opening}.x")
    moved = [dict(o, x=o["x"] + 1) for o in ws.design.params["openings"]]
    _, driver = run(ws, [("set_parameter", {"name": "openings", "value": moved})])
    assert driver.calls[0].denied


def test_the_users_answer_approves_one_change_to_a_locked_value():
    ws = workspace(answers=["thinner"])
    value = locked_wall(ws)
    options = [
        {"id": "thinner", "label": "Use a thinner wall", "approves": ["wall"]},
        {"id": "keep", "label": "Keep the wall"},
    ]
    steps = [
        ("ask_user", {"question": "The wall is locked. Thin it?", "options": options}),
        ("set_parameter", {"name": "wall", "value": value - 0.5}),
        ("set_parameter", {"name": "wall", "value": value - 0.6}),
    ]
    _, driver = run(ws, steps)
    assert [c.denied is None for c in driver.calls] == [True, True, False]
    assert ws.design.params["wall"] == value - 0.5
    stored = ws.design.store.get("wall")
    assert stored.locked and stored.approval.by == "user"


def overriding_code(ws, path: str, value) -> str:
    """The design's code wrapped so the geometry uses `value` for `path`, whatever the params
    say: a code-only edit that would bypass a lock on the parameter."""
    return (
        ws.design.code
        + "\n\n_orig_build = build\n\n\ndef build(params):\n"
        + "    params = dict(params)\n"
        + f"    params[{path!r}] = {value!r}\n"
        + "    return _orig_build(params)\n"
    )


def test_new_code_cant_override_a_locked_value():
    ws = workspace()
    value = locked_wall(ws)
    (path,) = ws.design.locked
    before = ws.design.snapshot()
    code = overriding_code(ws, path, value + 1)
    steps = [
        ("write_design", {"code": code}),
        ("write_design", {"code": code, "params": ws.design.params}),
    ]
    _, driver = run(ws, steps)
    assert all(c.denied and "locked" in c.denied for c in driver.calls)
    assert ws.design.snapshot() == before and ws.rounds == 0
    # The tool refuses it too, without the hook.
    out = asyncio.run(ws.write_design({"code": code}))
    assert out["is_error"] and "locked" in out["content"][0]["text"]
    assert ws.design.code == before["code"] and ws.rounds == 0


def test_the_users_answer_approves_one_code_change_while_values_are_locked():
    ws = workspace(answers=["rewrite"])
    value = locked_wall(ws)
    (path,) = ws.design.locked
    first = ws.design.code + "\n# rewritten\n"
    second = first + "# again\n"
    options = [
        {"id": "rewrite", "label": "Rewrite the generator", "approves": ["code"]},
        {"id": "keep", "label": "Keep the generator"},
    ]
    steps = [
        ("ask_user", {"question": "Rewrite the generator?", "options": options}),
        ("write_design", {"code": first}),
        ("write_design", {"code": second}),
    ]
    _, driver = run(ws, steps)
    assert [c.denied is None for c in driver.calls] == [True, True, False]
    assert ws.design.code == first and ws.design.locked == {path: value}


def test_code_changes_freely_when_nothing_is_locked():
    ws = workspace()
    code = ws.design.code + "\n# rewritten\n"
    _, driver = run(ws, [("write_design", {"code": code})])
    assert driver.calls[0].denied is None and ws.design.code == code
    # Without locks there is nothing to approve.
    options = [{"id": "a", "label": "A", "approves": ["code"]}, {"id": "b", "label": "B"}]
    out = asyncio.run(ws.ask_user({"question": "?", "options": options}))
    assert out["is_error"]


# --- Conflicts --------------------------------------------------------------------------------


def test_run_checks_reports_the_real_conflict_detection():
    """`run_checks` lists exactly what conflict detection finds for the design's locks."""
    ws = workspace()
    assert asyncio.run(ws.check())["conflicts"] == []
    locked_wall(ws)  # a lock that leaves room is no conflict
    ws._changed()
    assert asyncio.run(ws.check())["conflicts"] == []
    path, value = lock_conflict(ws)
    ws._changed()
    report = asyncio.run(ws.check())
    expected = detect_conflicts(ws.board, ws.standards, ws.design.params, ws.design.locked)
    assert expected and report["conflicts"] == [c.to_json() for c in expected]
    (conflict,) = report["conflicts"]
    ids = {c["id"]: c for c in conflict["constraints"]}
    assert ids[path]["kind"] == "locked" and ids[path]["value"] == value
    assert {"min_component_clearance", "max_height"} <= set(ids)
    assert conflict["over_by"] > 0 and not ws.passing


def test_on_the_conflict_fixture_the_agent_asks_instead_of_guessing():
    ws = workspace(answers=["keep"], fixed=lambda p: True, services=None)
    ws.services = FixtureServices()  # contract fixtures
    path, value = lock_conflict(ws)
    options = [
        {"id": "thin", "label": "Change the locked value", "approves": [path]},
        {"id": "keep", "label": "Keep it and stop"},
    ]
    steps = [
        ("run_checks", {}),
        ("finish", {"outcome": "done", "summary": "guess"}),
        ("finish", {"outcome": "blocked", "summary": "guess"}),
        ("ask_user", {"question": "The locked value leaves no room.", "options": options}),
        ("finish", {"outcome": "blocked", "summary": "Waiting on the lock."}),
    ]
    result, driver = run(ws, steps)
    report = json.loads(driver.calls[0].text)
    assert report["conflicts"][0]["id"] == "stackup"
    assert [c.is_error for c in driver.calls] == [False, True, True, False, False]
    assert result.finished["outcome"] == "blocked" and result.error is None
    (question,) = ws.sink.of_type("question")
    assert [o.id for o in question.options] == ["thin", "keep"]
    assert get_path(ws.design.params, path) == value


def test_ask_user_waits_for_the_person():
    ws = workspace()
    options = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]

    async def scenario():
        task = asyncio.create_task(ws.ask_user({"question": "Which?", "options": options}))
        while not ws.sink.of_type("question"):
            await asyncio.sleep(0)
        question = ws.sink.of_type("question")[0]
        ws.answer_question(question.question_id, "b")
        return await task

    out = asyncio.run(scenario())
    assert json.loads(out["content"][0]["text"])["answer"]["id"] == "b"


# --- Rounds and rechecks ----------------------------------------------------------------------


def test_the_checks_rerun_after_every_edit():
    ws = workspace()
    steps = [("set_parameter", {"name": "draft", "value": 2.0 + n / 10}) for n in range(3)]
    _, driver = run(ws, steps)
    assert all(c.context and "Checks after this edit" in c.context[0] for c in driver.calls)
    assert len(ws.services.built) == 3
    assert [b.build.id for b in ws.sink.of_type("build_ready")] == ["build-1", "build-2", "build-3"]


def test_at_most_four_fix_rounds():
    assert MAX_ROUNDS == 4
    ws = workspace()
    steps = [("set_parameter", {"name": "draft", "value": 2.0 + n / 10}) for n in range(6)]
    result, driver = run(ws, steps)
    assert [c.denied is None for c in driver.calls] == [True] * 4 + [False] * 2
    assert "fix rounds" in driver.calls[4].denied
    assert result.rounds == 4
    rounds = {(e.round, e.rounds) for e in ws.sink.of_type("progress") if e.round}
    assert rounds == {(n, 4) for n in range(1, 5)}
    assert result.error == UNFINISHED


def test_an_answer_in_text_that_changes_nothing_ends_without_an_error():
    # Regression: a question answered in text, with no edit and no `finish`.
    ws = workspace()
    result, _ = run(ws, [("text", "That face is the sensor window.")], prompt="Why?")
    assert result.error is None and result.proposal is None
    assert not ws.sink.of_type("error")
    assert ws.sink.of_type("message_end")


def test_a_run_that_edits_but_never_finishes_is_an_error():
    ws = workspace()
    result, _ = run(ws, [("set_parameter", {"name": "draft", "value": 2.5})])
    assert result.error == UNFINISHED
    assert "without saying it was done" in result.error and "Retry" in result.error


def test_finish_done_needs_a_design_that_builds_not_every_check_passing():
    # Honest results, not a pass gate: done with a failing check lists the failure.
    ws = workspace(fixed=lambda p: False)
    steps = [
        ("run_checks", {}),
        ("finish", {"outcome": "done", "summary": "Moved what I could."}),
    ]
    result, driver = run(ws, steps)
    assert [c.is_error for c in driver.calls] == [False, False]
    assert result.finished == {"outcome": "done", "summary": "Moved what I could.", "rounds": 0}
    assert not result.passing and result.error is None
    assert "Checks still failing: Openings · X" in result.text
    assert "openings:X" in driver.calls[-1].text


def test_finish_done_with_every_check_passing_lists_no_failures():
    ws = workspace(fixed=lambda p: p["draft"] == 2.5)
    steps = [
        ("set_parameter", {"name": "draft", "value": 2.5}),
        ("finish", {"outcome": "done", "summary": "Moved the openings."}),
    ]
    result, driver = run(ws, steps)
    assert not driver.calls[-1].is_error
    assert result.finished == {"outcome": "done", "summary": "Moved the openings.", "rounds": 1}
    assert result.passing and "still failing" not in result.text


# --- Event stream -----------------------------------------------------------------------------


def test_every_step_streams_as_a_contract_event():
    ws = workspace(fixed=lambda p: p["draft"] == 2.5, sink=EventSink("msg-2"))
    steps = [
        ("text", "Revision changes."),
        ("read_spec", {}),
        ("read_board", {}),
        ("run_checks", {}),
        ("set_parameter", {"name": "draft", "value": 2.5}),
        ("finish", {"outcome": "done", "summary": "Done."}),
    ]
    run(ws, steps)
    events = validate_recording(ws.sink.recording())  # valid contract events, ids increasing
    types = [r.event.type for r in events]
    assert types[0] == "message_start" and types[-1] == "message_end"
    assert {"text", "progress", "build_ready"} <= set(types)
    progress = [e.text for e in ws.sink.of_type("progress")]
    assert len(progress) >= 6  # one per tool call, plus the recheck
    assert any("round 1 of 4" in p for p in progress)


def test_agent_builds_link_their_board_revision():
    ws = workspace(fixed=lambda p: True)
    build = make_build("build-1", ws.board)
    assert build.board_url == "builds/build-1/board"
    assert (build.board.name, build.board.revision) == (ws.board.name, ws.board.revision)
    assert all(e.message_id == "msg-2" for _, e in ws.sink.events)


def untrusted_body(text: str) -> str:
    """The data inside `read_attachment`'s `<untrusted_data>` tags (docs/security.md)."""
    assert text.startswith("<untrusted_data source=") and text.endswith("\n</untrusted_data>")
    return text.split("\n", 1)[1][: -len("\n</untrusted_data>")]


def test_a_failed_run_ends_with_an_error_event():
    ws = workspace()

    async def broken(prompt, options):
        raise RuntimeError("no connection")
        yield  # pragma: no cover

    result = asyncio.run(run_agent(ws, "go", driver=broken))
    # Said plainly, and retried first: a dropped connection is temporary.
    assert result.error == stopped_text("network", 3)
    assert "no connection" not in result.error and "RuntimeError" not in result.error
    assert ws.sink.types[-2:] == ["error", "message_end"]
    assert ws.sink.of_type("error")[0].retryable
    validate_recording(ws.sink.recording())


def test_fixture_services_serve_the_contract_fixtures():
    services = FixtureServices()
    outcome = services.build({})
    report = json.loads((services.fixtures_dir / "checks.json").read_text())
    assert services.checks(outcome) == items_from_report(report)
    assert outcome.build.id == "build-1" and services.build({}).build.id == "build-2"
    assert all(spot["faces"] for spot in services.analyze(outcome))


# --- Real geometry ----------------------------------------------------------------------------


def test_with_real_geometry_the_fixed_design_passes_every_check():
    """The existing enclosure fails on the new board; the scripted fix (the parameters derived
    for the new board) passes every check, built in the sandbox and checked exactly."""
    from geometry import enclosure_parameters

    standards = load_standards()
    ws = scenario_workspace(standards=standards)
    assert isinstance(ws.services, GeometryServices)
    fixed = enclosure_parameters(ws.board, standards)
    steps = [
        ("run_checks", {}),
        ("write_design", {"params": fixed}),
        ("finish", {"outcome": "done", "summary": "Updated the openings."}),
    ]
    # test_proposal.py proves the change set on real geometry; skip its builds here.
    result, driver = run(ws, steps, config=RunConfig(propose=False))
    before = json.loads(driver.calls[0].text)
    failing = {i["component"] for i in before["failing"]}
    assert failing and failing <= {c.ref for c in ws.board.components} | {None}
    assert result.finished["outcome"] == "done" and result.passing and result.rounds == 1
    ready = ws.sink.of_type("build_ready")
    assert ready[-1].build.checks.failed == 0 and ready[0].build.checks.failed > 0


def test_a_build_error_reports_the_line():
    ws = scenario_workspace()
    code = "def build(params):\n    x = 1\n    raise ValueError('boom')\n"
    _, driver = run(ws, [("write_design", {"code": code})])
    report = json.loads(driver.calls[0].context[0].split("\n", 1)[1])
    assert report["status"] == "failed" and report["error"]["line"] == 3
    assert ws.sink.of_type("build_ready")[0].build.status == "failed"


def test_design_hooks_are_independent_of_the_sdk_client():
    ws = workspace()
    hooks = make_hooks(ws)
    assert set(hooks) == {"PreToolUse", "PostToolUse"}
    assert isinstance(ws.design, Design)
