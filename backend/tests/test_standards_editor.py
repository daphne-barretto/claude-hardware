"""The standards editor: the rule catalogue, each browser's own sets, and changing a session's set.

Every rule is of a catalogue kind, so every rule has a check (CLAUDE.md). The built-in set
(`standards.yaml`) is read-only; a browser duplicates it or starts a set, edits it, and the API
validates everything. A session switched to a set (or to its edited version) is rechecked for
real (a design from scratch, built in the sandbox and checked by the check runner) and the chat
says what changed; Claude gets the set from the next message and has no way to change it.
Values come from `standards.yaml` and the catalogue, never written here.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.agent.tools import TOOLS, Workspace
from app.contracts.checks import CheckReport, validate_check_report
from app.contracts.common import ContractError
from app.main import app
from app.mock_api import app as mock_app
from app.owners import OWNER_COOKIE
from app.session_api import CONFIG, SESSIONS, fixture_services
from app.standards import load_standards
from app.standards_catalog import BY_CHECK, KINDS, plain
from app.standards_changes import change_notice, check_changes, rule_changes

BOX = "make a box 40 x 30 x 20 mm"  # no word names a standard: the set alone sets the limits


@pytest.fixture(autouse=True)
def _keep_config(monkeypatch):
    monkeypatch.setattr(CONFIG, "driver", CONFIG.driver)
    monkeypatch.setattr(CONFIG, "services", CONFIG.services)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real session API with the end-to-end backend's stand-in Claude (real builds and
    checks for a design without a board)."""
    from app import e2e_api

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(params=["real", "mock"])
def api(request, client):
    """The standards routes on the real API and on the mock API: the same contract."""
    if request.param == "real":
        yield client
        return
    with TestClient(mock_app) as test_client:
        yield test_client


def other_browser(client: TestClient) -> TestClient:
    c = TestClient(client.app)
    c.cookies.set(OWNER_COOKIE, "another-browser-token-0123456789")
    return c


def rules_of(standards) -> list[dict]:
    """The rules of a set as the editor sends them back."""
    out = []
    for s in standards:
        rule = {
            "check": s["check"],
            "title": s["title"],
            "description": s["description"],
            "value": s["value"],
            "applies_to": s["applies_to"],
            "enabled": s["enabled"],
            "aliases": s["aliases"],
        }
        if s.get("dimensions"):
            rule["dimensions"] = s["dimensions"]
        if s.get("material"):
            rule["material"] = s["material"]
        out.append(rule)
    return out


def duplicate(api, name="Copy") -> dict:
    built_in = api.get("/api/standards").json()[0]
    r = api.post("/api/standards", json={"name": name, "duplicate_of": built_in["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def wall_kind():
    return BY_CHECK["wall_thickness"]


# --- Catalogue ---------------------------------------------------------------------------------


def test_the_catalogue_has_one_kind_per_standard_check_the_app_implements() -> None:
    from geometry.checks import ASSEMBLY_CHECKS, BOARD_CHECKS, CHECKS

    standard_checks = set(CHECKS) - set(BOARD_CHECKS) - set(ASSEMBLY_CHECKS)
    assert {k.check for k in KINDS} == standard_checks
    for k in KINDS:
        if k.kind != "spec":
            assert k.unit and k.min is not None and k.max is not None and k.min < k.max, k.check
        for d in k.dimensions:
            assert d.min < d.max, (k.check, d.name)
        assert "{value}" in k.phrase


def test_every_built_in_rule_is_a_catalogue_kind_with_its_id_and_unit() -> None:
    for s in load_standards().standards:
        kind = BY_CHECK[s.check]
        assert (s.id, s.kind, s.unit) == (kind.standard_id, kind.kind, kind.unit)
        if kind.kind != "spec":
            assert kind.min <= float(s.value) <= kind.max


def test_get_kinds_lists_the_catalogue_with_the_built_in_values_as_suggestions(api) -> None:
    kinds = api.get("/api/standards/kinds").json()
    assert [k["check"] for k in kinds] == [k.check for k in KINDS]
    built_in = {s.check: s for s in load_standards().standards}
    for k in kinds:
        assert k["suggested"]["value"] == built_in[k["check"]].value
        assert k["suggested"]["label"] == plain(built_in[k["check"]])


def test_a_rule_reads_in_plain_language() -> None:
    wall = load_standards().get("min_wall")
    assert plain(wall) == f"Walls at least {float(wall.value):.1f} mm"


# --- Sets: CRUD, read-only, owners -------------------------------------------------------------


def test_the_built_in_set_is_listed_first_and_read_only(api) -> None:
    sets = api.get("/api/standards").json()
    built_in = sets[0]
    assert built_in["id"] == load_standards().id and built_in["built_in"] is True
    assert api.patch(f"/api/standards/{built_in['id']}", json={"name": "Mine"}).status_code == 403
    assert api.delete(f"/api/standards/{built_in['id']}").status_code == 403
    assert api.get("/api/standards").json()[0] == built_in


def test_duplicate_rename_edit_and_delete_a_set(api) -> None:
    copy = duplicate(api, "Hardware copy")
    assert copy["built_in"] is False and copy["duplicated_from"] == load_standards().id
    assert [s["id"] for s in copy["standards"]] == [s.id for s in load_standards().standards]
    rules = rules_of(copy["standards"])
    wall = next(r for r in rules if r["check"] == "wall_thickness")
    wall["value"] = float(wall["value"]) + 1
    draft = next(r for r in rules if r["check"] == "draft_angle")
    draft["enabled"] = False
    rules = [r for r in rules if r["check"] != "material"]
    r = api.patch(f"/api/standards/{copy['id']}", json={"name": "Thicker", "standards": rules})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["name"] == "Thicker"
    got = {s["check"]: s for s in saved["standards"]}
    assert got["wall_thickness"]["value"] == wall["value"]
    assert got["wall_thickness"]["label"].endswith(f"{wall['value']:.1f} mm")
    assert got["draft_angle"]["enabled"] is False and "material" not in got
    assert api.get(f"/api/standards/{copy['id']}").json() == saved
    assert [s["id"] for s in api.get("/api/standards").json()] == [load_standards().id, copy["id"]]
    assert api.delete(f"/api/standards/{copy['id']}").status_code == 204
    assert api.get(f"/api/standards/{copy['id']}").status_code == 404


def test_a_new_set_starts_empty_and_takes_rules_from_the_catalogue(api) -> None:
    r = api.post("/api/standards", json={"name": "Brackets"})
    assert r.status_code == 201 and r.json()["standards"] == []
    sid = r.json()["id"]
    kind = wall_kind()
    rule = {"check": kind.check, "value": kind.min, "enabled": True, "aliases": ["wall"]}
    r = api.patch(f"/api/standards/{sid}", json={"standards": [rule]})
    assert r.status_code == 200, r.text
    (saved,) = r.json()["standards"]
    # The id, comparison, unit, title and applies_to follow from the kind.
    assert (saved["id"], saved["kind"], saved["unit"]) == (kind.standard_id, kind.kind, kind.unit)
    assert saved["title"] == kind.title and saved["applies_to"] == kind.applies_to
    assert saved["description"]  # said in plain words when none was given


def test_sets_belong_to_the_browser_that_made_them(client) -> None:
    mine = duplicate(client, "Mine")
    other = other_browser(client)
    assert [s["id"] for s in other.get("/api/standards").json()] == [load_standards().id]
    assert other.get(f"/api/standards/{mine['id']}").status_code == 404
    assert other.patch(f"/api/standards/{mine['id']}", json={"name": "x"}).status_code == 404
    assert other.delete(f"/api/standards/{mine['id']}").status_code == 404
    assert (
        other.post("/api/standards", json={"name": "x", "duplicate_of": mine["id"]}).status_code
        == 404
    )
    # Nor can another browser's session choose it.
    sid = other.post("/api/sessions", json={}).json()["id"]
    r = other.post(f"/api/sessions/{sid}/messages", json={"text": "hi", "standards": mine["id"]})
    assert r.status_code == 404
    assert (
        other.put(f"/api/sessions/{sid}/standards", json={"standards": mine["id"]}).status_code
        == 404
    )
    assert client.get(f"/api/standards/{mine['id']}").status_code == 200


def test_sets_are_saved_in_the_session_store(client, tmp_path) -> None:
    from app.session_store import session_store

    mine = duplicate(client, "Kept")
    owner = client.cookies.get(OWNER_COOKIE)
    assert [d["id"] for d in session_store().standards_sets(owner)] == [mine["id"]]


# --- Validation ----------------------------------------------------------------------------------


def _problems(r) -> list[dict]:
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"] and body["problems"]
    return body["problems"]


@pytest.mark.parametrize(
    ("change", "field", "words"),
    [
        (lambda k: {"value": k.min / 2}, "value", "the check accepts"),
        (lambda k: {"value": k.max * 2}, "value", "up to"),
        (lambda k: {"value": "thick"}, "value", "needs a number in mm"),
        (lambda k: {"unit": "deg"}, "unit", "measured in mm"),
        (lambda k: {"applies_to": "everything"}, "applies_to", "not a kind of design"),
        (lambda k: {"title": "  "}, "title", "title"),
        (lambda k: {"dimensions": {"hole_diameter": 4}}, "dimensions", "takes no sizes"),
        (lambda k: {"aliases": ["wall", "Wall"]}, "aliases", "listed twice"),
    ],
)
def test_an_invalid_rule_is_refused_with_a_clear_message(api, change, field, words) -> None:
    sid = api.post("/api/standards", json={"name": "Checked"}).json()["id"]
    kind = wall_kind()
    rule = {"check": kind.check, "value": kind.min, "enabled": True, "aliases": [], **change(kind)}
    problems = _problems(api.patch(f"/api/standards/{sid}", json={"standards": [rule]}))
    assert problems[0]["rule"] == 0 and problems[0]["field"] == field
    assert words in problems[0]["message"]
    assert api.get(f"/api/standards/{sid}").json()["standards"] == []  # nothing saved


def test_set_level_problems_unknown_kinds_duplicates_and_names(api) -> None:
    sid = api.post("/api/standards", json={"name": "One"}).json()["id"]
    api.post("/api/standards", json={"name": "Two"})
    kind = wall_kind()
    wall = {"check": kind.check, "value": kind.min, "enabled": True, "aliases": ["wall"]}
    draft = BY_CHECK["draft_angle"]
    rules = [
        wall,
        {**wall},  # a second rule of the same kind
        {"check": "made_up_check", "value": 1, "enabled": True, "aliases": []},
        {"check": draft.check, "value": draft.min, "enabled": True, "aliases": ["WALL"]},
    ]
    problems = _problems(api.patch(f"/api/standards/{sid}", json={"standards": rules}))
    by_rule = {p["rule"]: p for p in problems}
    assert "one rule of each kind" in by_rule[1]["message"]
    assert "catalogue" in by_rule[2]["message"] and "needs code" in by_rule[2]["message"]
    assert "already names" in by_rule[3]["message"]
    for name in ("two", " ", load_standards().name):
        problems = _problems(api.patch(f"/api/standards/{sid}", json={"name": name}))
        assert problems[0]["field"] == "name"
    assert _problems(api.post("/api/standards", json={"name": ""}))[0]["field"] == "name"


def test_a_spec_rule_needs_its_text_and_every_size() -> None:
    from app.contracts.session import StandardInput
    from app.standards_sets import SetProblems, validate_rules

    kind = BY_CHECK["insert_pockets"]
    with pytest.raises(SetProblems) as error:
        validate_rules([StandardInput(check=kind.check, value=" ", dimensions={})])
    fields = {p.field for p in error.value.problems}
    assert "value" in fields and {d.name for d in kind.dimensions} <= fields


# --- Checks, the report and the session ----------------------------------------------------------


def sse(client, sid: str) -> list[tuple[str, dict]]:
    r = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"})
    out = []
    for frame in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def wait_idle(client, sid: str) -> list[tuple[str, dict]]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        found = sse(client, sid)
        status = client.get(f"/api/sessions/{sid}").json()["status"]
        if status == "idle" and found and found[-1][0] in ("message_end", "question"):
            return found
        if status == "waiting_for_answer" and found and found[-1][0] in ("message_end", "question"):
            # The stand-in asks before finishing with a failing standard (`app.e2e_api`): keep
            # the design, as a user would.
            asked = [d for t, d in found if t == "question"][-1]
            if not asked["text"].startswith("The design fails"):
                return found
            answer = {"question_id": asked["question_id"], "option_id": "keep"}
            r = client.post(f"/api/sessions/{sid}/messages", json={"text": "", "answer": answer})
            assert r.status_code == 202, r.text
        time.sleep(0.05)
    raise AssertionError(f"{sid} never went idle")


def latest_report(client, sid: str) -> dict:
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    return client.get(builds[-1]["checks_url"]).json()


def by_check(report: dict) -> dict[str, dict]:
    return {r["check"]: r for r in report["results"]}


def notice(events, after: int) -> str:
    return "".join(d["delta"] for kind, d in events[after:] if kind == "text")


def test_a_session_switched_to_an_edited_set_is_rechecked_and_told_what_changed(client) -> None:
    built_in = load_standards()
    wall = built_in.get("min_wall")
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX})
    wait_idle(client, sid)
    before = by_check(latest_report(client, sid))
    assert before["wall_thickness"]["status"] == "pass"
    measured = before["wall_thickness"]["measured"]["value"]

    # A copy whose minimum wall is above the box's walls, with the draft rule turned off.
    copy = duplicate(client, "Stricter")
    rules = rules_of(copy["standards"])
    for r in rules:
        if r["check"] == "wall_thickness":
            r["value"] = round(measured + 1, 1)
        if r["check"] == "draft_angle":
            r["enabled"] = False
    assert (
        client.patch(f"/api/standards/{copy['id']}", json={"standards": rules}).status_code == 200
    )

    seen = len(sse(client, sid))
    r = client.put(f"/api/sessions/{sid}/standards", json={"standards": copy["id"]})
    assert r.status_code == 202 and r.json()["standards"] == copy["id"]
    events = wait_idle(client, sid)
    after = latest_report(client, sid)
    validate_check_report(after, load_rules(client, copy["id"]))
    results = by_check(after)
    assert results["wall_thickness"]["status"] == "fail"
    assert results["wall_thickness"]["required"]["value"] == round(measured + 1, 1)
    # A rule turned off is reported as not run, with the reason.
    assert results["draft_angle"]["status"] == "untested"
    assert "Turned off" in results["draft_angle"]["reason"]
    text = notice(events, seen)
    assert "Stricter" in text and "Now failing" in text and wall.title in text
    assert "No longer checked" in text and built_in.get("min_draft").title in text
    assert "next message" in text
    assert client.get(f"/api/sessions/{sid}").json()["standards"] == copy["id"]

    # Editing the set in use and choosing it again rechecks with the edit.
    for r in rules:
        if r["check"] == "wall_thickness":
            r["value"] = wall.value
    client.patch(f"/api/standards/{copy['id']}", json={"standards": rules})
    seen = len(sse(client, sid))
    client.put(f"/api/sessions/{sid}/standards", json={"standards": copy["id"]})
    events = wait_idle(client, sid)
    assert by_check(latest_report(client, sid))["wall_thickness"]["status"] == "pass"
    assert "Now passing" in notice(events, seen)


def load_rules(client, set_id: str):
    from app.session_store import session_store
    from app.standards_sets import find_set

    return find_set(session_store(), client.cookies.get(OWNER_COOKIE), set_id)


def test_a_rule_that_does_not_apply_stays_not_applicable_when_turned_off(client) -> None:
    """A design without a board: a turned-off board rule is not applicable, not 'not run'."""
    copy = duplicate(client, "Off")
    rules = rules_of(copy["standards"])
    board_rule = next(r for r in rules if r["applies_to"] == "board")
    board_rule["enabled"] = False
    client.patch(f"/api/standards/{copy['id']}", json={"standards": rules})
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX, "standards": copy["id"]})
    wait_idle(client, sid)
    result = by_check(latest_report(client, sid))[board_rule["check"]]
    assert result["status"] == "not_applicable" and result["reason"]


def test_the_contract_allows_a_reason_only_where_a_standard_was_not_checked() -> None:
    standards = load_standards()
    s = standards.standards[0]
    base = {
        "id": s.check,
        "check": s.check,
        "title": s.title,
        "standard": s.id,
        "status": "untested",
        "summary": "not run",
        "covers": "x",
        "reason": "Turned off.",
    }
    untested = {"contract": "check-results/1", "build_id": "b1", "board": None}
    report = {**untested, "results": [base], "summary": {"pass": 0, "fail": 0, "untested": 1}}
    CheckReport.model_validate(report)
    # A rule that is on can't be reported as not run.
    with pytest.raises(ContractError):
        validate_check_report(report, standards.model_copy(update={"standards": [s]}))
    off = s.model_copy(update={"enabled": False})
    validate_check_report(report, standards.model_copy(update={"standards": [off]}))


def test_claude_gets_the_new_set_with_the_next_message_and_read_spec_lists_it(client) -> None:
    prompts: list[str] = []
    from app import e2e_api

    class Recording(e2e_api.StandInClaude):
        def __init__(self, state, body, prompt):
            prompts.append(prompt)
            super().__init__(state, body, prompt)

    CONFIG.driver = Recording
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX})
    wait_idle(client, sid)
    copy = duplicate(client, "For Claude")
    rules = rules_of(copy["standards"])
    rules[0]["enabled"] = False
    client.patch(f"/api/standards/{copy['id']}", json={"standards": rules})
    client.put(f"/api/sessions/{sid}/standards", json={"standards": copy["id"]})
    wait_idle(client, sid)
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX})
    wait_idle(client, sid)
    assert "For Claude" in prompts[-1] and "Only the user changes constraints" in prompts[-1]
    assert "For Claude" not in prompts[0]

    state = SESSIONS.get(sid)
    import asyncio

    spec = json.loads(asyncio.run(state.ws.read_spec({}))["content"][0]["text"])
    assert spec["standards_set"]["id"] == copy["id"]
    assert rules[0]["title"] in spec["standards_set"]["turned_off"]
    assert rules[0]["check"] not in {s["check"] for s in spec["standards"]}


def test_claude_has_no_tool_that_changes_standards() -> None:
    reads_or_checks = {"read_spec", "run_checks"}
    for name in TOOLS:
        if "standard" in name or "spec" in name:
            assert name in reads_or_checks, name
    assert not hasattr(Workspace, "set_standards")


def test_a_message_after_the_first_cannot_change_the_set(client) -> None:
    copy = duplicate(client)
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX})
    wait_idle(client, sid)
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "x", "standards": copy["id"]})
    assert r.status_code == 409 and "PUT" in r.json()["detail"]


def test_a_session_keeps_its_copy_of_the_set_across_a_restart(client, tmp_path) -> None:
    copy = duplicate(client, "Survives")
    sid = client.post("/api/sessions", json={}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": BOX, "standards": copy["id"]})
    wait_idle(client, sid)
    SESSIONS.by_id.clear()
    client.delete(f"/api/standards/{copy['id']}")  # deleting the set changes no session
    assert SESSIONS.get(sid).ws.standards.id == copy["id"]


# --- The notice --------------------------------------------------------------------------------


def test_rule_changes_and_check_changes_say_what_changed() -> None:
    from types import SimpleNamespace as Item

    old = load_standards()
    wall = old.get("min_wall")
    new = old.model_copy(
        update={
            "standards": [
                s.model_copy(update={"value": float(s.value) + 1}) if s.id == wall.id else s
                for s in old.standards
                if s.check != "material"
            ]
        }
    )
    changes = rule_changes(old, new)
    assert any(c.startswith(wall.title) and "→" in c for c in changes)
    assert any(c.startswith("removed") for c in changes)
    before = [
        Item(id="a", status="pass", title="A", summary=""),
        Item(id="b", status="fail", title="B", summary=""),
    ]
    after = [
        Item(id="a", status="fail", title="A", summary="A: Fails."),
        Item(id="b", status="pass", title="B", summary=""),
    ]
    found = check_changes(before, after)
    assert [i.id for i in found["failing"]] == ["a"] and [i.id for i in found["passing"]] == ["b"]
    text = change_notice(old, new, before, after)
    assert "Now failing" in text and "Now passing" in text and "Rule changes" in text
