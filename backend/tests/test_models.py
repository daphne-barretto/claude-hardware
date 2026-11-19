"""The model catalogue shared by the model picker and the session API."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.contracts.common import ContractError
from app.contracts.models import (
    FRONTEND_MODELS_PATH,
    MODEL_CATALOG,
    MODELS_PATH,
    ModelCatalog,
    check_model_id,
)
from app.contracts.session import MessageCreate
from app.examples import examples
from app.mock_api import SESSIONS, app

# The board revision example: these tests walk its board and existing design.
EXAMPLE = examples()[0].id


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    SESSIONS.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_the_contract_and_the_frontend_read_the_same_generated_catalogue() -> None:
    expected = MODEL_CATALOG.model_dump()
    assert json.loads(MODELS_PATH.read_text()) == expected
    assert json.loads(FRONTEND_MODELS_PATH.read_text()) == expected, (
        "run: cd backend && .venv/bin/python -m app.contracts"
    )


def test_the_catalogue_has_additional_models() -> None:
    assert MODEL_CATALOG.more_models
    assert MODEL_CATALOG.default in {m.id for m in MODEL_CATALOG.models}


def test_catalogue_rejects_duplicates_and_a_default_outside_the_main_list() -> None:
    data = MODEL_CATALOG.model_dump()
    with pytest.raises(ValueError, match="twice"):
        ModelCatalog.model_validate({**data, "more_models": data["models"][:1]})
    with pytest.raises(ValueError, match="default"):
        ModelCatalog.model_validate({**data, "default": MODEL_CATALOG.more_models[0].id})
    # An empty "More models" list is allowed: the picker hides the item.
    assert ModelCatalog.model_validate({**data, "more_models": []}).more_models == []


def test_message_model_must_come_from_the_catalogue() -> None:
    for m in MODEL_CATALOG.models + MODEL_CATALOG.more_models:
        assert MessageCreate(text="hi", model=m.id).model == m.id
    assert MessageCreate(text="hi").model is None
    with pytest.raises(ValueError):
        MessageCreate(text="hi", model="not-a-model")
    with pytest.raises(ContractError):
        check_model_id("not-a-model")


def test_mock_api_takes_an_additional_model_and_rejects_unknown_ones(client) -> None:
    sid = client.post("/api/sessions", json={"example": EXAMPLE}).json()["id"]
    extra = MODEL_CATALOG.more_models[0].id
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi", "model": "nope"})
    assert r.status_code == 422
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "hi", "model": extra})
    assert r.status_code == 202
    assert r.json()["model"] == extra
    assert client.get(f"/api/sessions/{sid}/messages").json()[0]["model"] == extra
