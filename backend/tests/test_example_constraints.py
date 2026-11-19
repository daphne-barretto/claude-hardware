"""Constraints an example brings: a limit measured from the example's own design (the board
revision example's height limit, from its existing case) belongs to that example, not to the
default constraint set every session uses. Nothing here names the board or the limit's value:
they come from the examples list and its constraints file in `demos/`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.examples import examples
from app.spec import Spec
from app.standards import load_standards


def example_rules() -> list:
    """The rules the board revision example brings."""
    found = [s for e in examples() for s in e.extra_standards()]
    assert found, "the board revision example brings a constraint of its own"
    return found


def test_the_default_set_has_no_rule_an_example_brings() -> None:
    """Regression: the example's height limit was in the default set, so every session with
    any board got it. The default set (and a board session's constraints) leave it out."""
    defaults = load_standards()
    for rule in example_rules():
        assert not defaults.has(rule.id)
        assert rule.check not in defaults.checks
    spec = Spec(defaults)
    spec.active = True
    spec.has_board = lambda: True  # a session with some board, not the example's
    ids = {c.id for c in spec.constraints()}
    assert ids and not ids & {r.id for r in example_rules()}


@pytest.fixture
def real(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.main import app
    from app.session_api import CONFIG, SESSIONS, fixture_services

    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    return app


def test_a_session_on_the_example_checks_what_the_example_brings(real) -> None:
    from app.session_api import SESSIONS

    rules = example_rules()
    with TestClient(real) as client:
        listed = client.get("/api/standards").json()
        shown = {s["id"] for s in listed[0]["standards"]}
        assert not shown & {r.id for r in rules}  # the default set, as the picker lists it

        empty = client.post("/api/sessions", json={}).json()
        assert not SESSIONS.get(empty["id"]).ws.standards.has(rules[0].id)

        opened = client.post("/api/sessions", json={"example": examples()[0].id}).json()
        state = SESSIONS.get(opened["id"])
        for rule in rules:
            assert state.ws.standards.has(rule.id)
            assert rule.check in state.ws.standards.checks
            assert state.spec.standards.has(rule.id)


def test_the_example_demos_still_check_what_the_example_brings(real) -> None:
    """The demos recorded on the example keep the limit: their copies are checked against it."""
    from app import samples
    from app.session_api import SESSIONS

    rules = example_rules()
    on_board = [s for s in samples.catalogue().values() if s.session.board is not None]
    assert on_board
    with TestClient(real) as client:
        for sample in on_board:
            copy = client.post(f"/api/demos/{sample.session.id}/session").json()
            state = SESSIONS.get(copy["id"])
            for rule in rules:
                assert rule.check in state.ws.standards.checks
            # The recorded builds checked it, as before.
            latest = sample.json(sample.session.latest_build_id, "checks.json")
            assert {r["check"] for r in latest["results"]} >= {r.check for r in rules}
