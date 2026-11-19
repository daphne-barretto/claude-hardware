"""A session's name says what is being designed.

- Claude names the part when it finishes a design whose identity is new or changed (`finish`
  `title`, 2 to 5 words); the session is renamed and a `title` event updates the header and
  Recents live. A small edit leaves the name. Until Claude names it, the first request names it.
- The user renames a session (`PATCH /api/sessions/{id}`, the owner only); from then on the
  name is theirs (`title_locked`) and Claude's names are ignored, with a note to Claude.
- Examples, samples and given titles keep their names unless the user renames them.

Claude is played by a script on instant builds, so nothing here names a real part or board.
"""

from __future__ import annotations

import pytest
import test_session_api as api
from test_session_api import CONFIG, events, last_id, send, wait_until

from app.contracts.session import Session, validate_event
from app.examples import examples
from app.samples import catalogue as sample_catalogue
from app.titles import clean_title

client, claude = api.client, api.claude

CODE = "def build(params):\n    return {}\n"


class InstantServices:
    """Builds that are ready at once, with no checks: the design's identity is all that varies."""

    def __init__(self, board, standards) -> None:
        from app.agent.services import FixtureServices

        # The fixture build files are an example's, so a design without a board builds on it.
        self.board = board or examples()[0].boards()[1]
        self._ids = FixtureServices()._ids

    def build(self, design):
        from app.agent.services import BuildOutcome, make_build

        return BuildOutcome(make_build(f"build-{next(self._ids)}", self.board), shapes=design)

    def checks(self, outcome):
        return []

    def analyze(self, outcome):
        return []


@pytest.fixture
def instant(monkeypatch) -> None:
    monkeypatch.setattr(CONFIG, "services", InstantServices)


def empty(client, **body) -> str:
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def design_run(claude, size: float, title: str | None) -> None:
    finish = {"outcome": "done", "summary": "Designed it."}
    if title is not None:
        finish["title"] = title
    claude.scripts.append(
        [
            ("write_design", {"code": CODE, "params": {"size": size}}),
            ("run_checks", {}),
            ("finish", finish),
        ]
    )


def run(client, sid: str, text: str) -> list:
    after = last_id(client, sid)
    send(client, sid, text=text)
    return wait_until(client, sid, "idle", after)


def title_of(client, sid: str) -> str:
    return client.get(f"/api/sessions/{sid}").json()["title"]


def title_events(found: list) -> list[dict]:
    return [d for _, t, d in found if t == "title"]


def test_claude_names_a_new_design_and_the_header_hears_it(client, claude, instant) -> None:
    sid = empty(client)
    design_run(claude, 40, "Phone stand")
    found = run(client, sid, "make something to hold my phone on the desk")
    assert not claude.drivers[-1].calls[-1].is_error, claude.drivers[-1].calls[-1].text
    assert title_of(client, sid) == "Phone stand"
    (event,) = title_events(found)
    validate_event(event)
    assert event["title"] == "Phone stand" and event["message_id"]
    listed = {s["id"]: s["title"] for s in client.get("/api/sessions").json()}
    assert listed[sid] == "Phone stand"


def test_until_claude_names_it_the_first_request_does(client, claude, instant) -> None:
    sid = empty(client)
    design_run(claude, 40, None)
    run(client, sid, "make a sphere")
    assert title_of(client, sid) == "Make a sphere"


def test_a_new_part_renames_and_a_small_edit_does_not(client, claude, instant) -> None:
    sid = empty(client)
    design_run(claude, 40, "Open box")
    run(client, sid, "make a box")
    # A small edit: Claude leaves the name (no title), or gives the same one.
    design_run(claude, 42, None)
    found = run(client, sid, "a bit bigger")
    assert title_of(client, sid) == "Open box" and not title_events(found)
    design_run(claude, 43, "Open box")
    found = run(client, sid, "a touch bigger again")
    assert title_of(client, sid) == "Open box" and not title_events(found)
    # The part changes: Claude names the new one.
    design_run(claude, 80, "Phone stand")
    found = run(client, sid, "turn it into a phone stand")
    assert title_of(client, sid) == "Phone stand"
    assert [e["title"] for e in title_events(found)] == ["Phone stand"]


def test_a_manual_rename_locks_the_name(client, claude, instant) -> None:
    sid = empty(client)
    after = last_id(client, sid)
    r = client.patch(f"/api/sessions/{sid}", json={"title": "  My desk stand  "})
    assert r.status_code == 200, r.text
    session = Session.model_validate(r.json())
    assert session.title == "My desk stand" and session.title_locked
    (event,) = title_events(events(client, sid, after))
    assert event["title"] == "My desk stand" and event["message_id"] is None

    design_run(claude, 40, "Phone stand")
    found = run(client, sid, "make a phone stand")
    assert title_of(client, sid) == "My desk stand" and not title_events(found)
    finish = claude.drivers[-1].calls[-1]
    assert not finish.is_error and "named" in finish.text.lower()
    # The user can still rename it.
    r = client.patch(f"/api/sessions/{sid}", json={"title": "Stand v2"})
    assert r.json()["title"] == "Stand v2"


def test_only_the_owner_renames(client, instant) -> None:
    sid = empty(client)
    mine = dict(client.cookies)
    client.cookies.clear()
    r = client.patch(f"/api/sessions/{sid}", json={"title": "Not yours"})
    assert r.status_code == 404
    client.cookies.clear()
    for name, value in mine.items():
        client.cookies.set(name, value)
    assert title_of(client, sid) != "Not yours"


def test_examples_samples_and_given_titles_keep_their_names(client, claude, instant) -> None:
    example = examples()[0]
    sid = empty(client, example=example.id)
    finish = {"outcome": "done", "summary": "Checked it.", "title": "Phone stand"}
    claude.scripts.append([("run_checks", {}), ("finish", finish)])
    run(client, sid, "check it")
    assert title_of(client, sid) == example.title
    assert "named" in claude.drivers[-1].calls[-1].text.lower()

    given = empty(client, title="Bracket for the shelf")
    design_run(claude, 40, "Phone stand")
    run(client, given, "make a phone stand")
    assert title_of(client, given) == "Bracket for the shelf"

    for sample_id in list(sample_catalogue())[:1]:
        r = client.patch(f"/api/sessions/{sample_id}", json={"title": "Mine now"})
        assert r.status_code == 409


def test_a_rename_is_validated(client, instant) -> None:
    sid = empty(client)
    for bad in ("", "   ", "\x00\x07", "**  **"):
        r = client.patch(f"/api/sessions/{sid}", json={"title": bad})
        assert r.status_code == 422, (bad, r.text)
    r = client.patch(f"/api/sessions/{sid}", json={"title": "<b>Box</b> **lid**\n" + "x" * 80})
    title = r.json()["title"]
    assert len(title) <= 60 and "<" not in title and "*" not in title and "\n" not in title
    assert title.startswith("Box lid")


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Phone stand", "Phone stand"),
        ("  # Phone   stand ", "Phone stand"),
        ("`M4` L-bracket", "M4 L-bracket"),
        ("[Hub](http://x) enclosure", "Hub enclosure"),
        ("a\tb\x1bc", "a b c"),
        ("", None),
    ],
)
def test_titles_are_cleaned(raw, clean) -> None:
    assert clean_title(raw) == clean


def test_a_bad_title_from_claude_is_refused_and_the_name_stays(client, claude, instant) -> None:
    sid = empty(client)
    design_run(claude, 40, "**")
    run(client, sid, "make a sphere")
    finish = claude.drivers[-1].calls[-1]
    assert finish.is_error and "title" in finish.text
    assert title_of(client, sid) == "Make a sphere"


def test_the_mock_renames_as_the_real_api_does(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app import mock_api

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    mock_api.SESSIONS.clear()
    with TestClient(mock_api.app) as mock:
        sid = mock.post("/api/sessions", json={}).json()["id"]
        r = mock.patch(f"/api/sessions/{sid}", json={"title": " <i>Desk</i> stand "})
        assert r.status_code == 200 and r.json()["title"] == "Desk stand"
        assert r.json()["title_locked"]
        assert mock.patch(f"/api/sessions/{sid}", json={"title": "**"}).status_code == 422
        text = mock.get(f"/api/sessions/{sid}/events", params={"follow": "false"}).text
        assert "event: title" in text
