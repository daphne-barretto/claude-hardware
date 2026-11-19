"""What a new session shows (from walking the app on the real backend):

- The constraint chips list only the standards that apply to the design, by the same rule as the
  check report (`Standard.applies`), plus every constraint the user gave in their own words.
- A session started empty is named from its first request; an example keeps its title.

Claude is the scripted stand-in of the end-to-end backend; the standards come from
`standards.yaml`, so no standard or board is named here.
"""

from __future__ import annotations

import pytest
import test_general_sessions as general
from test_general_sessions import new_session, run

from app.examples import examples
from app.spec import Spec, extract
from app.standards import load_standards
from app.titles import DEFAULT_TITLE, title_for

# The general session tests' fixtures: the API with the stand-in Claude.
client, _keep_config = general.client, general._keep_config


def board_only() -> list:
    return [s for s in load_standards().standards if not s.applies(has_board=False)]


def test_chips_of_a_design_without_a_board_list_only_the_standards_that_apply(client) -> None:
    assert board_only(), "standards.yaml has no standard that applies only with a board"
    session = new_session(client)
    found = run(client, session.id, "Something for my desk")
    (chips,) = [d["constraints"] for kind, d in found if kind == "constraints"][-1:]
    applies = {s.id for s in load_standards().standards if s.applies(has_board=False)}
    assert {c["id"] for c in chips} == applies
    served = client.get(f"/api/sessions/{session.id}/constraints").json()
    assert {c["id"] for c in served} == applies


def test_a_constraint_in_the_users_words_stays_even_if_it_needs_a_board() -> None:
    standards = load_standards()
    for standard in board_only():
        for alias in standard.aliases:
            text = f"Something for my desk with a 3 mm {alias}"
            if any(m.standard == standard.id for m in extract(text, standards)):
                break
        else:
            continue
        spec = Spec(standards)
        spec.has_board = lambda: False
        spec.has_object = lambda: False
        spec.describe(text)
        chips = {c.id: c for c in spec.constraints()}
        assert standard.id in chips and chips[standard.id].source == "user"
        others = {s.id for s in board_only()} - {standard.id}
        assert not others & set(chips)
        return
    pytest.skip("no board-only standard can be given in words")


def test_a_session_started_empty_is_named_from_its_first_request(client) -> None:
    session = new_session(client)
    assert session.title == DEFAULT_TITLE
    run(client, session.id, "make a sphere")
    assert client.get(f"/api/sessions/{session.id}").json()["title"] == "Make a sphere"
    listed = {s["id"]: s["title"] for s in client.get("/api/sessions").json()}
    assert listed[session.id] == "Make a sphere"


def test_an_example_and_a_given_title_are_kept(client) -> None:
    example = examples()[0]
    session = new_session(client, example=example.id)
    assert session.title == example.title
    named = new_session(client, title="My bracket")
    run(client, named.id, "make a sphere")
    assert client.get(f"/api/sessions/{named.id}").json()["title"] == "My bracket"


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("make a sphere", "Make a sphere"),
        ("  Please   make a sphere!  ", "Make a sphere"),
        (
            "Can you design a phone stand with a 60 degree tilt?",
            "Design a phone stand with a 60 degree",
        ),
        ("a box\nwith more detail on the next line", "A box"),
        ("", None),
        ("???", None),
    ],
)
def test_a_title_is_a_short_form_of_the_request(text, title) -> None:
    assert title_for(text) == title


def test_a_long_request_is_cut_at_a_word() -> None:
    text = "an enclosure for a weather station " * 5
    title = title_for(text)
    assert title is not None and len(title) <= 40
    assert text.strip().lower().startswith(title.lower())
