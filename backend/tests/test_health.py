"""GET /health (proxied to /api/health) reports build identity, never secrets."""

import pytest
from fastapi.testclient import TestClient

from app import runtime
from app.main import app

SECRET = "sk-ant-test-not-a-real-key"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in (*runtime.COMMIT_VARS, "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_version_commit_and_key_presence(client, monkeypatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body.pop("capacity")["build_slots"] >= 1  # tests/test_capacity.py
    usage = body.pop("usage")
    assert body == {
        "status": "ok",
        "version": runtime.app_version(),
        "commit": "abc1234",
        "api_key_present": True,
    }
    # Today's Claude usage against the daily budget (docs/security.md): totals only.
    assert set(usage) == {
        "day",
        "spent_usd",
        "budget_usd",
        "runs",
        "input_tokens",
        "output_tokens",
        "limit_reached",
    }
    assert body["version"] not in ("", "0.0.0")
    assert SECRET not in response.text


def test_health_without_key_or_commit(client) -> None:
    body = client.get("/api/health").json()
    assert body["api_key_present"] is False
    assert body["commit"] == "unknown"


def test_blank_key_counts_as_missing(client, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert client.get("/api/health").json()["api_key_present"] is False


def test_git_commit_overrides_render_commit(client, monkeypatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "from-render")
    monkeypatch.setenv("GIT_COMMIT", "from-build")
    assert client.get("/api/health").json()["commit"] == "from-build"
