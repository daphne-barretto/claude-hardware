"""Cost abuse (`docs/security.md`): per-owner and per-IP rate limits, the app-wide daily spend
cap and its usage in `/api/health`. Claude is played by `ScriptedDriver`; nothing calls a model.
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient
from test_session_api import Claude, events, wait_until

from app.main import app
from app.security import budget, limits
from app.security.clients import client_ip
from app.session_api import CONFIG, SESSIONS, fixture_services
from app.session_store import SqliteStore, session_store

FINISH = [("text", "Done."), ("finish", {"outcome": "done", "summary": "Nothing to change."})]
ANSWER = [("text", "It is a box.")]


@pytest.fixture
def claude(monkeypatch) -> Claude:
    fake = Claude()
    monkeypatch.setattr(CONFIG, "driver", fake)
    return fake


def make_client(ip: str | None = None) -> TestClient:
    """A browser: its own cookie jar; from `ip` (through the app's proxy) when given."""
    if ip is None:
        return TestClient(app)
    return TestClient(app, client=("127.0.0.1", 40000), headers={"X-Forwarded-For": ip})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with make_client() as c:
        yield c
    SESSIONS.by_id.clear()


def new_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def run(c: TestClient, claude: Claude, sid: str, text: str = "What is it?", steps=None):
    claude.scripts.append(steps or ANSWER)
    r = c.post(f"/api/sessions/{sid}/messages", json={"text": text})
    if r.status_code == 202:
        wait_until(c, sid, "idle")
    return r


# --- Rate limits --------------------------------------------------------------------------------


def test_sessions_per_day_per_owner(api, monkeypatch) -> None:
    monkeypatch.setenv("SESSIONS_PER_DAY", "2")
    new_session(api)
    new_session(api)
    r = api.post("/api/sessions", json={})
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail.startswith("You've started 2 chats today, the most this demo allows in a day.")
    assert "You can start another in about 24 hours" in detail
    assert 23 * 3600 < int(r.headers["Retry-After"]) <= 24 * 3600
    # Another browser (another owner) on a private address is not limited by this one's count.
    with make_client() as other:
        assert other.post("/api/sessions", json={}).status_code == 201


def test_sessions_per_day_per_ip_across_owners(api, monkeypatch) -> None:
    """Dropping the cookie doesn't help: the client's IP address is counted too."""
    monkeypatch.setenv("SESSIONS_PER_DAY", "2")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    for _ in range(2):
        with make_client("81.2.69.160") as browser:  # a new cookie each time
            assert browser.post("/api/sessions", json={}).status_code == 201
    with make_client("81.2.69.160") as browser:
        assert browser.post("/api/sessions", json={}).status_code == 429
    with make_client("81.2.69.161") as elsewhere:
        assert elsewhere.post("/api/sessions", json={}).status_code == 201


def test_messages_per_minute(api, claude, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGES_PER_MINUTE", "2")
    sid = new_session(api)
    assert run(api, claude, sid).status_code == 202
    assert run(api, claude, sid).status_code == 202
    r = run(api, claude, sid)
    assert r.status_code == 429
    assert r.json()["detail"] == (
        "You're sending messages faster than this demo allows (2 a minute). Please wait a "
        "moment and try again in less than a minute."
    )
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    # A minute later it resets.
    now = time.time()
    monkeypatch.setattr(limits, "clock", lambda: now + 61)
    assert run(api, claude, sid).status_code == 202


def test_runs_per_hour(api, claude, monkeypatch) -> None:
    monkeypatch.setenv("RUNS_PER_HOUR", "1")
    sid = new_session(api)
    assert run(api, claude, sid).status_code == 202
    r = run(api, claude, sid)
    assert r.status_code == 429
    assert r.json()["detail"] == (
        "You've reached this demo's limit of 1 replies from Claude an hour. You can send "
        "another in about 60 minutes. Meanwhile you can still edit the design by hand."
    )
    # The refused message changed nothing: no user message stored, no run started.
    assert len(api.get(f"/api/sessions/{sid}/messages").json()) == 2
    assert claude.scripts == [ANSWER]


def test_runs_per_hour_per_ip(api, claude, monkeypatch) -> None:
    monkeypatch.setenv("RUNS_PER_HOUR", "1")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    with make_client("81.2.69.160") as a, make_client("81.2.69.160") as b:
        assert run(a, claude, new_session(a)).status_code == 202
        assert run(b, claude, new_session(b)).status_code == 429


def test_writes_per_minute(api, monkeypatch) -> None:
    monkeypatch.setenv("WRITES_PER_MINUTE", "3")
    sid = new_session(api)  # 1
    url = f"/api/sessions/{sid}/telemetry"
    assert api.post(url, json={"kind": "view", "view": "map"}).status_code == 204  # 2
    assert api.post(url, json={"kind": "view", "view": "map"}).status_code == 204  # 3
    r = api.post(url, json={"kind": "view", "view": "map"})
    assert r.status_code == 429
    assert r.json()["detail"].startswith("That's a lot of changes in a short time (3 a minute")
    assert api.get(f"/api/sessions/{sid}").status_code == 200, "reads aren't writes"


def test_when_says_plainly() -> None:
    assert limits.when(5) == "in less than a minute"
    assert limits.when(61) == "in about 2 minutes"
    assert limits.when(3600) == "in about 60 minutes"
    assert limits.when(5 * 3600) == "in about 5 hours"


# --- Client IP ----------------------------------------------------------------------------------


def scope(peer: str, xff: str | None = None) -> dict:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return {"client": (peer, 1), "headers": headers}


def test_client_ip_trusts_forwarded_for_only_from_the_proxy(monkeypatch) -> None:
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    # Locally nothing is trusted: the connecting address.
    assert client_ip(scope("127.0.0.1", "81.2.69.160")) == "127.0.0.1"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    # The entry the trusted proxy appended, not the one the client wrote before it.
    assert client_ip(scope("127.0.0.1", "6.6.6.6, 81.2.69.160")) == "81.2.69.160"
    # Not through the proxy: the header is the client's own, ignored.
    assert client_ip(scope("81.2.69.1", "6.6.6.6")) == "81.2.69.1"
    assert client_ip(scope("127.0.0.1", "not an ip")) is None
    monkeypatch.delenv("TRUSTED_PROXY_HOPS")
    monkeypatch.setenv("RENDER", "true")  # Render's edge appends the client: one hop
    assert client_ip(scope("127.0.0.1", "6.6.6.6, 81.2.69.160")) == "81.2.69.160"


# --- Daily budget ---------------------------------------------------------------------------------


def test_the_daily_budget_defaults_to_50_dollars(monkeypatch) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD")
    assert budget.budget_usd() == 50.0


def test_runs_record_their_cost_and_health_reports_it(api, claude) -> None:
    sid = new_session(api)
    run(api, claude, sid, steps=FINISH)  # the scripted driver reports $0.001 a step
    health = api.get("/api/health").json()
    usage = health["usage"]
    assert usage["day"] == budget.today()
    assert usage["runs"] == 1 and usage["spent_usd"] == pytest.approx(0.002)
    assert usage["input_tokens"] == 2000 and usage["output_tokens"] == 200
    assert usage["budget_usd"] == 100000 and usage["limit_reached"] is False
    # Kept in the session store's database: a restart (a new store on it) still sees it.
    again = SqliteStore(session_store().root)
    assert again.usage(budget.today())["cost_usd"] == pytest.approx(0.002)
    again.close()


def test_the_budget_refuses_new_runs_once_reached(api, claude, monkeypatch, caplog) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.0025")
    sid = new_session(api)
    with caplog.at_level(logging.WARNING, logger="app.security.budget"):
        assert run(api, claude, sid, steps=FINISH).status_code == 202  # $0.002: 80%
    assert any("80% of the $0.00 budget" in r.getMessage() for r in caplog.records)
    assert run(api, claude, sid).status_code == 202  # $0.001 more: $0.003, over
    r = run(api, claude, sid)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail.startswith(
        "The demo has reached today's limit for Claude, so it can't take new requests right now. "
        "It resets at midnight UTC (in "
    )
    assert detail.endswith(
        "Meanwhile you can still open your chats and the demos, and edit designs by hand."
    )
    assert int(r.headers["Retry-After"]) <= 24 * 3600
    assert api.get("/api/health").json()["usage"]["limit_reached"] is True
    # Everything else still works: opening the chat, its events, a new session.
    assert api.get(f"/api/sessions/{sid}/messages").status_code == 200
    assert events(api, sid)
    assert api.post("/api/sessions", json={}).status_code == 201


def test_a_running_run_finishes_when_the_budget_is_reached(api, claude, monkeypatch) -> None:
    """The cap refuses new runs; one already going is not cut off."""
    sid = new_session(api)

    def steps(ws):
        monkeypatch.setenv("DAILY_BUDGET_USD", "0")  # reached while this run is going
        return [("text", "Still"), ("text", " going.")]

    claude.scripts.append(steps)
    assert api.post(f"/api/sessions/{sid}/messages", json={"text": "Go"}).status_code == 202
    found = wait_until(api, sid, "idle")
    assert [t for _, t, _ in found if t == "error"] == []
    said = "".join(d["delta"] for _, t, d in found if t == "text")
    assert said == "Still going."
    assert run(api, claude, sid).status_code == 503


def test_a_result_without_cost_is_estimated_high() -> None:
    assert budget.cost_of(None, 1_000_000, 0) == 15.0
    assert budget.cost_of(None, 0, 1_000_000) == 75.0
    assert budget.cost_of(0.5, 10**9, 10**9) == 0.5


def test_health_has_no_secrets(api, monkeypatch) -> None:
    key = "sk-ant-api03-" + "x" * 40
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    r = api.get("/api/health")
    assert r.json()["api_key_present"] is True
    assert key not in r.text and "x" * 20 not in r.text
