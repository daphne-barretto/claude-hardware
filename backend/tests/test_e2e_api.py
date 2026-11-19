"""The scripted backend for end-to-end tests: the real app with only the model scripted."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.boards import BOARDS_DIR
from app.boards.changes import parse_changes_md
from app.contracts.models import MODEL_CATALOG
from app.contracts.session import MessageCreate, Selection
from app.session_api import CONFIG, fixture_services, prompt_for


@pytest.fixture(autouse=True)
def _keep_driver(monkeypatch):
    # Importing app.e2e_api swaps the driver and the services; other tests need the defaults.
    monkeypatch.setattr(CONFIG, "driver", CONFIG.driver)
    monkeypatch.setattr(CONFIG, "services", CONFIG.services)


def _texts(steps) -> str:
    return "\n".join(p for kind, p in steps if kind == "text" and isinstance(p, str))


def test_a_message_runs_the_tools_lists_the_changes_and_asks() -> None:
    from app import e2e_api

    model = MODEL_CATALOG.default
    body = MessageCreate(text="Check it", model=model, effort="low", mode="manual")
    steps = e2e_api.script(body, prompt_for(body), SimpleNamespace(model=model, effort="low"))
    kinds = [k for k, _ in steps]
    assert {"read_board", "run_checks", "ask_user"} <= set(kinds)
    assert kinds.index("run_checks") < kinds.index("ask_user") < kinds.index("finish")
    text = _texts(steps)
    assert f"Run with model {model}, effort low, mode manual." in text
    for row in parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text()).rows:
        assert row["Component"] in text


def test_a_selection_is_reported_from_the_prompt() -> None:
    from app import e2e_api

    body = MessageCreate(text="Why?", selection=Selection(part="p", faces=["p.face_1"], edges=[]))
    steps = e2e_api.script(body, prompt_for(body), SimpleNamespace(model=None, effort=None))
    assert "Selected faces: p.face_1." in _texts(steps)
    assert "mode auto" in _texts(steps)
    # Without the face in the prompt, it says none reached the run.
    unsent = e2e_api.script(body, "Why?", SimpleNamespace(model=None, effort=None))
    assert "Selected faces: none." in _texts(unsent)


def test_move_openings_moves_each_opening_changes_md_lists_as_moved() -> None:
    from app import e2e_api
    from app.boards import load_board

    doc = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    new = load_board(BOARDS_DIR / doc.new_file)
    moved = [r for r in doc.rows if r["Change"].startswith("moved")]
    assert moved
    body = MessageCreate(text=e2e_api.MOVE_OPENINGS)
    steps = e2e_api.script(body, prompt_for(body), SimpleNamespace(model=None, effort=None))
    kinds = [k for k, _ in steps]
    assert kinds.index("set_parameter") < kinds.index("run_checks")
    edits = {c["name"]: c["value"] for c in dict(steps)["set_parameter"]["changes"]}
    for row in moved:
        component, kind = row["Component"].strip("`"), row["Opening"].strip("`")
        o = next(o for o in new.openings if o.component == component and o.kind == kind)
        assert edits[f"openings.{o.id}.x"] == o.position[0]
        assert edits[f"openings.{o.id}.y"] == o.position[1]
        assert edits[f"openings.{o.id}.rotation"] == o.rotation


@pytest.mark.parametrize("option", ["accept", "reject"])
def test_the_final_status_agrees_with_how_the_run_ended(tmp_path, monkeypatch, option) -> None:
    """Answering the scripted question ends the run. Its last progress line says how it ended:
    never that it is still waiting on a decision once the decision is made, and not "Stopped"
    after a reply that says it is done."""
    from fastapi.testclient import TestClient
    from test_session_api import send, start, wait_until

    from app import e2e_api
    from app.session_api import SESSIONS

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The import set these only the first time; the autouse fixture put the defaults back.
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(e2e_api.app) as client:
        sid, found = start(client)
        send(client, sid, text="Check it")
        found = wait_until(client, sid, "waiting_for_answer", found[-1][0])
        (question,) = [d for _, t, d in found if t == "question"]
        assert question["text"] == e2e_api.QUESTION
        answer = {"question_id": question["question_id"], "option_id": option}
        send(client, sid, answer=answer)
        found = wait_until(client, sid, "idle", found[-1][0])
    progress = [d["text"] for _, t, d in found if t == "progress"]
    text = "".join(d["delta"] for _, t, d in found if t == "text")
    assert progress and "waiting" not in progress[-1].lower()
    if "Done" in text:
        assert progress[-1] == "Finished"
    else:
        assert progress[-1].startswith("Stopped")
