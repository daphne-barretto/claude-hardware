"""Telemetry.

The demo script (`demos/b-parasite/demo-script.md`) is walked over HTTP with a scripted Claude
(`demo_walk.py`), and its telemetry log is checked: every line is a schema-valid event, and
every kind of user action and agent step is in it. The metrics
report runs over every recorded session; and no secret reaches the log.
The scenario comes from `demos/b-parasite/`, `standards.yaml`, `fixtures/` and
`demos/b-parasite/demo-script.md`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import demo_walk
import jsonschema
import pytest
import test_session_api as api
from fastapi.testclient import TestClient

from app.agent.services import BuildOutcome, FixtureServices
from app.contracts.common import CONTRACTS_DIR, REPO_ROOT
from app.contracts.session import Build
from app.contracts.telemetry import AGENT_KINDS, USER_KINDS, validate_telemetry_event
from app.exports import fixture_mesh_parts
from app.main import app
from app.session_api import CONFIG, SESSIONS, fixture_services
from app.telemetry import __main__ as cli
from app.telemetry.log import (
    REDACTED,
    default_dir,
    log_dirs,
    prune,
    read_log,
    redact,
    telemetry_dir,
)
from app.telemetry.report import load_sessions, metrics
from app.telemetry.session import error_source

SCHEMA = json.loads((CONTRACTS_DIR / "telemetry-event.schema.json").read_text())
# What the frontend reports for actions only it sees: a view of each kind, each choice, and an
# accepted and a rejected change.
CLIENT_ACTIONS = [
    {"kind": "view", "view": "map", "value": "wall_thickness"},
    {"kind": "view", "view": "section", "value": "x"},
    {"kind": "view", "view": "compare", "value": "previous"},
    {"kind": "view", "view": "camera", "value": "top"},
    {"kind": "choice", "setting": "model", "value": "claude-opus-5-5"},
    {"kind": "choice", "setting": "effort", "value": "high"},
    {"kind": "choice", "setting": "mode", "value": "manual"},
    {"kind": "change_decision", "change_id": "clearance", "decision": "accept"},
    {"kind": "change_decision", "change_id": "clearance", "decision": "reject"},
]


@pytest.fixture
def logs(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "telemetry"
    monkeypatch.setenv("TELEMETRY_DIR", str(directory))
    return directory


@pytest.fixture
def client(tmp_path, monkeypatch, logs):
    # The demo walk (demo_walk.py) on the contract fixtures, which are fast; the real
    # services are walked through the demo in test_session_api.py.
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


@pytest.fixture
def claude(monkeypatch) -> api.Claude:
    fake = api.Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


def walk_demo(client, claude) -> tuple[str, Path]:
    """Walk the demo script live, with the frontend's own reports; the session and its log."""
    sid, found = api.start(client)
    demo_walk.walk(client, sid, found, claude)
    for body in CLIENT_ACTIONS:
        r = client.post(f"/api/sessions/{sid}/telemetry", json=body)
        assert r.status_code == 204, r.text
    return sid, SESSIONS.get(sid).telemetry.log.dir


def of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


def test_the_demo_script_logs_every_action_and_agent_step(client, claude) -> None:
    _, folder = walk_demo(client, claude)
    events = read_log(folder)

    # Every line is a schema-valid event, versioned and in order.
    for n, event in enumerate(events, 1):
        jsonschema.validate(event, SCHEMA)
        validate_telemetry_event(event)
        assert event["v"] == 1 and event["seq"] == n
    kinds = {e["kind"] for e in events}
    assert set(USER_KINDS) <= kinds, set(USER_KINDS) - kinds
    assert set(AGENT_KINDS) <= kinds, set(AGENT_KINDS) - kinds

    # User actions: messages with the selection sent as context, model, effort and mode.
    messages = of(events, "message")
    assert len(messages) == 4
    assert messages[0]["model"] == "claude-opus-5-5" and messages[0]["effort"] == "high"
    assert all(m["mode"] == "auto" for m in messages)
    assert any(m["selection"] and m["selection"]["faces"] for m in messages)
    answers = of(events, "answer")
    assert [a["option_id"] for a in answers] == ["accept", "keep"]
    assert all(a["since_question_ms"] is not None for a in answers)
    edits = of(events, "parameter_edit")
    assert [e["name"] for e in edits][0] == "draft" and all(e["locked"] for e in edits)
    assert of(events, "comment")[0]["text"] == "Check this"
    assert set(of(events, "export")[0]["formats"]) == {"step", "stl", "build_note"}
    views = {(e["view"], e["value"]) for e in of(events, "view")}
    assert {"map", "section", "compare", "camera"} <= {v for v, _ in views}
    assert {e["setting"] for e in of(events, "choice")} == {"model", "effort", "mode"}
    decisions = of(events, "change_decision")
    assert [d["decision"] for d in decisions] == ["accept", "reject"]
    assert all(d["since_proposal_ms"] is not None for d in decisions), "time from proposal"

    # Agent steps: tool calls with inputs, outputs and latency; builds with hashes; checks.
    calls = of(events, "tool_call")
    tools = {c["tool"] for c in calls}
    assert {"read_board", "run_checks", "set_parameter", "ask_user", "finish"} <= tools
    for c in calls:
        assert isinstance(c["input"], dict) and c["latency_ms"] >= 0
        assert c["output"] is not None or c["denied"]
    builds = of(events, "build")
    assert len(builds) >= 3
    for b in builds:
        assert len(b["code_hash"]) == 64 and b["parameters"]
        assert b["status"] == "failed" or (b["shapes_hash"] and b["artifacts"])
    assert all(c["summary"] and isinstance(c["failing"], list) for c in of(events, "checks"))
    assert [r["round"] for r in of(events, "fix_round")][:1] == [1]
    assert {q["question_id"] for q in of(events, "question")} == {a["question_id"] for a in answers}
    assert of(events, "conflict")[0]["conflicts"], "the locked value conflicts"
    assert of(events, "proposal")[0]["changes"]

    # Timing and cost: every run ends with its outcome, latency, tokens, cost, model, effort.
    starts, ends = of(events, "run_start"), of(events, "run_end")
    assert len(starts) == len(ends) == len(messages)
    for end in ends:
        assert end["outcome"] in ("done", "blocked") and end["latency_ms"] >= 0
        assert end["input_tokens"] and end["output_tokens"] and end["cost_usd"] is not None
        assert end["model"]
    assert ends[0]["effort"] == "high"

    # The stream: every session event, as the session served it.
    stream = [e["event"] for e in of(events, "stream")]
    served = [d for _, _, d in api.events(client, of(events, "session_start")[0]["session_id"])]
    assert [e["type"] for e in stream] == [d["type"] for d in served]

    # A canvas state after every step: the start, then each user action on the session.
    steps = of(events, "canvas")
    assert [s["step"] for s in steps] == list(range(len(steps)))
    assert [s["after"] for s in steps] == ["session_start"] + [
        e["kind"]
        for e in events
        if e["kind"] in ("message", "answer", "parameter_edit", "comment", "export")
    ]


def test_the_logged_canvas_lists_the_parameters_the_app_serves(client, claude) -> None:
    """The canvas state logged at a step lists the parameters GET /parameters serves then,
    grouped values that measure on the model (`boss.radius`) included, so the log holds what
    the user saw."""
    sid, _ = api.start(client)
    (start,) = of(read_log(SESSIONS.get(sid).telemetry.log.dir), "canvas")
    served = client.get(f"/api/sessions/{sid}/parameters").json()
    assert any("." in p["name"] and p["dimension"] for p in served), "a grouped value measures"
    assert start["state"]["parameters"] == served


def test_the_metrics_report_runs_over_all_recorded_sessions(
    client, claude, logs, monkeypatch, capsys
) -> None:
    walk_demo(client, claude)

    class Passing(FixtureServices):  # a design that passes every check after one fix
        def checks(self, outcome):
            return [replace(i, status="pass") for i in super().checks(outcome)]

    monkeypatch.setattr(CONFIG, "services", lambda board, standards: Passing())
    sid, found = api.start(client)
    claude.scripts.append(
        [
            ("set_parameter", {"name": "clearance", "value": 0.6}),
            ("finish", {"outcome": "done", "summary": "Fixed."}),
        ]
    )
    api.send(client, sid, text="Fix it")
    api.wait_until(client, sid, "idle", found[-1][0])
    # A decision on this run's change set (each run proposes only what it changed, so the demo
    # walk's decisions, sent after a run that changed nothing, belong to no proposal).
    change = client.get(f"/api/sessions/{sid}/changes").json()["changes"][0]["id"]
    body = {"kind": "change_decision", "change_id": change, "decision": "reject"}
    assert client.post(f"/api/sessions/{sid}/telemetry", json=body).status_code == 204
    sessions = load_sessions(logs)
    assert len(sessions) == 2
    m = metrics(sessions)
    assert m["sessions"] == 2 and m["runs"] == 5
    assert m["proposals"]["total"] >= 1
    assert m["proposals"]["accepted_without_edits"] <= m["proposals"]["total"]
    assert m["proposals"]["median_ms_to_decision"] is not None
    assert m["rounds_to_pass"] == {"runs": 1, "mean": 1, "median": 1, "max": 1}
    assert m["asks_vs_guesses"]["asks"] == 2 and m["asks_vs_guesses"]["questions"] == 2
    assert m["failing_checks"] and all(n > 0 for _, n in m["failing_checks"])
    assert dict(m["overrides"]["parameter_edits"])["draft"] == 1
    assert dict(m["overrides"]["rejected_changes"]) == {change: 1}
    assert m["cost"]["total_usd"] > 0 and m["cost"]["accepted_changes"] >= 0
    if m["cost"]["accepted_changes"]:
        assert m["cost"]["per_accepted_change_usd"] > 0
    assert m["abandoned_sessions"] == 0

    assert cli.main(["report", "--dir", str(logs)]) == 0
    text = capsys.readouterr().out
    for label in (
        "Proposals accepted without edits",
        "Rounds to pass",
        "Asks vs. guesses",
        "Checks that fail most",
        "Hand edits (overrides)",
        "Cost per accepted change",
    ):
        assert label in text
    assert cli.main(["report", "--json"]) == 0  # TELEMETRY_DIR by default
    assert json.loads(capsys.readouterr().out)["sessions"] == 2


def test_no_secret_reaches_the_log(client, claude, logs, monkeypatch) -> None:
    key = "sk-ant-api03-" + "Q" * 40
    other = "ghp_" + "Z" * 36
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("SOME_SERVICE_SECRET", "hunter2-hunter2")
    sid, found = api.start(client)
    claude.scripts.append(
        [
            ("text", f"Your key is {key} and the token is {other}."),
            ("read_board", {"revision": "new", "api_key": key}),
            ("finish", {"outcome": "done", "summary": f"Bearer {other}"}),
        ]
    )
    api.send(client, sid, text=f"my key is {key}, password hunter2-hunter2")
    api.wait_until(client, sid, "idle", found[-1][0])
    r = client.post(
        f"/api/sessions/{sid}/telemetry", json={"kind": "view", "view": "map", "value": key}
    )
    assert r.status_code == 204

    files = [p for p in logs.rglob("*") if p.is_file()]
    assert files
    text = "\n".join(p.read_text() for p in files)
    for secret in (key, other, "hunter2-hunter2", "Q" * 20):
        assert secret not in text
    assert REDACTED in text
    message = of(read_log(log_dirs(logs)[0]), "message")[0]
    # The session API takes the key out before telemetry sees it (docs/security.md); telemetry
    # redacts what is left.
    assert message["text"] == f"my key is [removed: API key], password {REDACTED}"


def test_redaction_covers_nested_fields_and_key_shapes(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-value-123456")
    data = {
        "headers": {"Authorization": "anything", "x-api-key": "k"},
        "list": ["oauth-value-123456", "sk-ant-oat01-abcdefgh", "plain text"],
    }
    assert redact(data) == {
        "headers": {"Authorization": REDACTED, "x-api-key": REDACTED},
        "list": [REDACTED, REDACTED, "plain text"],
    }


def test_errors_and_abandoned_runs_are_logged(client, claude, monkeypatch) -> None:
    # An API error: no credentials and no driver.
    monkeypatch.setattr(CONFIG, "driver", lambda state, body, prompt: None)
    for name in demo_walk.CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    sid, found = api.start(client)
    api.send(client, sid, text="Hi")
    api.wait_until(client, sid, "idle", found[-1][0])
    events = read_log(SESSIONS.get(sid).telemetry.log.dir)
    (error,) = of(events, "error")
    assert error["source"] == "api"
    assert of(events, "run_end")[0]["outcome"] == "error"

    # A failed build.
    class Failing(FixtureServices):
        def build(self, design):
            outcome = super().build(design)
            failed = outcome.build.model_copy(update={"status": "failed"})
            return BuildOutcome(Build.model_validate(failed.model_dump()), {"line": 3})

    monkeypatch.setattr(CONFIG, "driver", claude)
    monkeypatch.setattr(CONFIG, "services", lambda board, standards: Failing())
    sid, found = api.start(client)
    claude.scripts.append([("build", {}), ("finish", {"outcome": "done", "summary": "x"})])
    api.send(client, sid, text="Build it")
    api.wait_until(client, sid, "idle", found[-1][0])
    events = read_log(SESSIONS.get(sid).telemetry.log.dir)
    assert any(b["status"] == "failed" for b in of(events, "build"))
    assert "build" in {e["source"] for e in of(events, "error")}
    assert of(events, "run_end")[0]["outcome"] == "error", "finish refused: never passed"

    # Sandbox stops are told apart from other errors.
    assert error_source("The sandbox stopped the build: out of memory") == "sandbox"
    assert error_source("The run stopped: overloaded") == "api"

    # A session left mid-run (waiting on a question when the server stops).
    monkeypatch.setattr(CONFIG, "services", lambda board, standards: FixtureServices())
    sid, found = api.start(client)
    claude.scripts.append(
        [
            (
                "ask_user",
                {
                    "question": "Which?",
                    "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                },
            ),
        ]
    )
    api.send(client, sid, text="Ask me")
    api.wait_until(client, sid, "waiting_for_answer", found[-1][0])
    folder = SESSIONS.get(sid).telemetry.log.dir
    client.__exit__(None, None, None)  # the server stops
    (left,) = of(read_log(folder), "abandoned")
    assert left["status"] == "waiting_for_answer"
    sessions = load_sessions(folder.parent)
    assert metrics(sessions)["abandoned_sessions"] >= 1


def test_the_telemetry_schema_accepts_every_field_its_models_log() -> None:
    # Regression: a field added to a logged model (the build's `restored_from`) without
    # regenerating the contract made every log line carrying it invalid.
    from app.contracts.telemetry import telemetry_json_schema

    committed = SCHEMA["$defs"]
    for name, model in telemetry_json_schema()["$defs"].items():
        assert name in committed, f"contracts/telemetry-event.schema.json lacks {name}"
        missing = set(model.get("properties", {})) - set(committed[name].get("properties", {}))
        assert not missing, f"contracts/telemetry-event.schema.json: {name} rejects {missing}"


def test_the_frontend_reports_need_their_fields(client) -> None:
    sid, _ = api.start(client)
    r = client.post(f"/api/sessions/{sid}/telemetry", json={"kind": "view"})
    assert r.status_code == 422
    r = client.post(f"/api/sessions/{sid}/telemetry", json={"kind": "nonsense"})
    assert r.status_code == 422
    r = client.post("/api/sessions/nope/telemetry", json={"kind": "view", "view": "map"})
    assert r.status_code == 404


def test_logs_live_outside_the_repository_and_expire(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TELEMETRY_DIR")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert telemetry_dir() == default_dir()
    assert not default_dir().resolve().is_relative_to(REPO_ROOT.resolve())
    monkeypatch.setenv("TELEMETRY_DIR", "off")
    assert telemetry_dir() is None

    old, new = tmp_path / "session-1-old", tmp_path / "session-2-new"
    for folder in (old, new):
        folder.mkdir()
        (folder / "events.jsonl").write_text("")
    long_ago = time.time() - 40 * 86400
    os.utime(old / "events.jsonl", (long_ago, long_ago))
    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", "30")
    assert prune(tmp_path) == [old]
    assert not old.exists() and new.exists()


def test_recording_can_be_turned_off(client, claude, monkeypatch, logs) -> None:
    monkeypatch.setenv("TELEMETRY_DIR", "off")
    sid, _ = api.start(client)
    assert SESSIONS.get(sid).telemetry is None
    r = client.post(f"/api/sessions/{sid}/telemetry", json={"kind": "view", "view": "map"})
    assert r.status_code == 204
    assert not logs.exists()


def test_an_export_of_some_parts_is_logged_with_them(client, claude) -> None:
    """#359: the `export` event records the parts asked for."""
    sid, folder = walk_demo(client, claude)
    latest = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    part = fixture_mesh_parts()[0]
    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": latest, "formats": ["stl", "build_note"], "parts": [part]},
    )
    assert r.status_code == 201, r.text

    exports = of(read_log(folder), "export")
    assert "parts" not in exports[0], "the whole assembly logs no parts"
    assert exports[-1]["parts"] == [part]
    for event in exports:
        jsonschema.validate(event, SCHEMA)
