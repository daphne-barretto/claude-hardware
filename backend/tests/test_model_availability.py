"""The model picker offers only models the server's API key can use (regression: a retired model
was offered, the key couldn't use it, and choosing it broke the run with a raw error).

The server lists the key's models with the Models API (`GET /v1/models`, paginated) at startup
and now and then (`app.model_availability`), and serves the catalogue entries it found at
`GET /api/models`. A run whose model turns out to be unavailable anyway marks it unavailable
for everyone until the next successful refresh, and tells the user plainly, offering the
default. No live calls: the Models API is a fake (`httpx.MockTransport`).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_agent import workspace
from test_session_api import client, send, start, wait_until  # noqa: F401 (fixture)

from app import model_availability
from app.agent.faults import FlakyDriver
from app.agent.limits import classify_message, model_unavailable_text
from app.agent.run import RunConfig, run_agent
from app.contracts.models import MODEL_CATALOG
from app.model_availability import AVAILABILITY, list_models
from app.session_api import CONFIG

DEFAULT = next(m for m in MODEL_CATALOG.models if m.id == MODEL_CATALOG.default)
ALL = [m.id for m in MODEL_CATALOG.models + MODEL_CATALOG.more_models]


def models_api(ids: list[str], page: int = 2, seen: list | None = None) -> httpx.MockTransport:
    """A fake `GET /v1/models`: `ids` in pages of `page`, as the API pages them."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["x-api-key"] == "test-key"
        if seen is not None:
            seen.append(dict(request.url.params))
        after = request.url.params.get("after_id")
        start = ids.index(after) + 1 if after else 0
        chunk = ids[start : start + page]
        body = {
            "data": [{"type": "model", "id": i, "display_name": i} for i in chunk],
            "has_more": start + page < len(ids),
            "first_id": chunk[0] if chunk else None,
            "last_id": chunk[-1] if chunk else None,
        }
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def key_models(monkeypatch):
    """The models the fake API lists for the key; set the list, then refresh."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    listed: list[str] = []

    def fetch(api_key: str) -> list[str]:
        return list_models(api_key, transport=models_api(listed))

    monkeypatch.setattr(AVAILABILITY, "fetch", fetch)
    AVAILABILITY.reset()
    yield listed
    AVAILABILITY.reset()


def test_the_models_api_is_read_page_by_page() -> None:
    seen: list = []
    ids = [f"model-{n}" for n in range(5)]
    assert list_models("test-key", transport=models_api(ids, page=2, seen=seen)) == ids
    assert [p.get("after_id") for p in seen] == [None, "model-1", "model-3"]


def test_the_picker_offers_only_the_models_the_key_can_use(client, key_models) -> None:  # noqa: F811
    key_models += [i for i in ALL if i != "claude-sonnet-4-6"] + ["claude-other-1"]
    asyncio.run(AVAILABILITY.refresh())
    r = client.get("/api/models")
    assert r.status_code == 200, r.text
    served = r.json()
    ids = [m["id"] for m in served["models"] + served["more_models"]]
    assert "claude-sonnet-4-6" not in ids
    assert "claude-other-1" not in ids, "only catalogue entries, with their names"
    assert ids == [i for i in ALL if i != "claude-sonnet-4-6"], "in the catalogue's order"
    assert served["default"] == MODEL_CATALOG.default


def test_without_the_models_api_the_picker_offers_the_whole_catalogue(client, monkeypatch) -> None:  # noqa: F811
    def down(api_key: str) -> list[str]:
        raise httpx.ConnectError("unreachable")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(AVAILABILITY, "fetch", down)
    AVAILABILITY.reset()
    asyncio.run(AVAILABILITY.refresh())
    served = client.get("/api/models").json()
    assert [m["id"] for m in served["models"] + served["more_models"]] == ALL
    AVAILABILITY.reset()


def test_the_default_moves_when_the_key_cant_use_it(key_models) -> None:
    key_models += [i for i in ALL if i != MODEL_CATALOG.default]
    asyncio.run(AVAILABILITY.refresh())
    catalog = AVAILABILITY.catalog()
    assert catalog.default != MODEL_CATALOG.default
    assert catalog.default in {m.id for m in catalog.models}


def test_the_catalogue_lists_only_current_models() -> None:
    """Only the current generation and the one before it; no retired model."""
    assert MODEL_CATALOG.ids() == {
        "claude-fable-5-2",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    }


def test_more_models_are_the_previous_generation_by_their_api_ids() -> None:
    more = [(m.id, m.name, m.blurb) for m in MODEL_CATALOG.more_models]
    assert more == [
        ("claude-opus-4-8", "Opus 4.8", "Previous generation"),
        ("claude-sonnet-4-6", "Sonnet 4.6", "Previous generation"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5", "Previous generation"),
    ]


def test_more_models_the_key_cant_use_leave_the_picker(key_models) -> None:
    key_models += [i for i in ALL if i != "claude-opus-4-8"]
    asyncio.run(AVAILABILITY.refresh())
    more = [m.id for m in AVAILABILITY.catalog().more_models]
    assert more == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


def test_a_model_not_found_is_classified_as_unavailable() -> None:
    from claude_agent_sdk import AssistantMessage, TextBlock

    text = (
        'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: '
        'claude-retired-model"}}'
    )
    message = AssistantMessage(content=[TextBlock(text)], model="x", error="invalid_request")
    assert classify_message(message).reason == "model_unavailable"


def test_a_run_whose_model_is_unavailable_says_so_and_offers_the_default(key_models) -> None:
    key_models += ALL
    asyncio.run(AVAILABILITY.refresh())
    ws = workspace()
    model = "claude-sonnet-4-6"
    result = asyncio.run(
        run_agent(ws, "go", RunConfig(model=model), driver=FlakyDriver("model_not_found"))
    )
    (error,) = ws.sink.of_type("error")
    assert error.reason == "model_unavailable" and error.retryable
    assert error.message == model_unavailable_text(DEFAULT.name) == result.error
    assert DEFAULT.name in error.message and "not_found" not in error.message
    assert result.reason == "model_unavailable"


def test_an_unavailable_model_leaves_every_picker_until_the_next_refresh(
    client,  # noqa: F811
    key_models,
    monkeypatch,
) -> None:
    key_models += ALL
    asyncio.run(AVAILABILITY.refresh())
    model = "claude-sonnet-4-6"
    monkeypatch.setattr(
        CONFIG, "driver", lambda state, body, prompt: FlakyDriver("model_not_found")
    )
    sid, found = start(client)
    send(client, sid, text="go", model=model)
    after = wait_until(client, sid, "idle", found[-1][0])
    (error,) = [d for _, t, d in after if t == "error"]
    assert error["reason"] == "model_unavailable"
    served = client.get("/api/models").json()
    assert model not in [m["id"] for m in served["models"] + served["more_models"]]

    # Retry goes on with the default: the unavailable model isn't asked for again.
    asked: list[str | None] = []

    def driver(state, body, prompt):
        asked.append(body.model)
        from app.agent.scripted import ScriptedDriver

        return ScriptedDriver(state.ws, [("text", "Back on it.")])

    monkeypatch.setattr(CONFIG, "driver", driver)
    r = client.post(f"/api/sessions/{sid}/retry", json={"model": model})
    assert r.status_code == 202, r.text
    wait_until(client, sid, "idle", after[-1][0])
    assert asked == [MODEL_CATALOG.default]

    # The next successful refresh decides again.
    asyncio.run(AVAILABILITY.refresh())
    served = client.get("/api/models").json()
    assert model in [m["id"] for m in served["more_models"]]


def test_the_availability_is_refreshed_when_it_is_old(key_models, monkeypatch) -> None:
    key_models += ALL
    asyncio.run(AVAILABILITY.refresh())
    assert not AVAILABILITY.stale()
    monkeypatch.setattr(model_availability, "REFRESH_SECONDS", -1)
    assert AVAILABILITY.stale()
