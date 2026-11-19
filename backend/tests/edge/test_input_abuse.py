"""Input abuse: what a skeptical tester types into every field (`docs/security.md`).

Text is stored and served back exactly as sent (escaping is the renderer's job, and the
Playwright edge specs check it renders inert); sizes, empties and nonsense numbers are refused
with a message that says what is wrong.
"""

from __future__ import annotations

import math

import pytest
from conftest import Browser, box_session

WEIRD_TEXT = [
    "😀🔩🛠️ 👩🏽‍🔧 box",  # emoji, a ZWJ sequence and skin tone
    "صندوق بعرض 40 مم",  # RTL
    "‮reversed‬ text",  # bidi override
    "zero​width‍join﻿bom",
    "tab\tnew\nline\rreturn\x0bvt\x0cff",
    "<script>alert(1)</script><img src=x onerror=alert(2)>",
    "# Title\n**bold** [link](javascript:alert(1)) ![img](http://example.invalid/x.png)",
    "' OR 1=1; DROP TABLE sessions; --",
    "{{7*7}} ${7*7} %s %n",
]


def test_an_empty_or_whitespace_message_is_refused_with_a_reason(browser: Browser) -> None:
    sid = browser.new_session()
    for text in ["", " ", "\n\t  　"]:
        r = browser.send(sid, text)
        assert r.status_code in (409, 422), r.text
        assert "needs text" in r.json()["detail"]
    assert browser.get(f"/api/sessions/{sid}/messages").json() == []
    assert browser.status(sid) == "idle"


def test_a_100k_character_message_is_taken_and_served_back_whole(browser: Browser, claude) -> None:
    sid = browser.new_session()
    text = ("Make it sturdier. " * 6000)[:100_000]
    r = browser.send(sid, text)
    assert r.status_code == 202, r.text
    browser.settle(sid)
    messages = browser.get(f"/api/sessions/{sid}/messages").json()
    assert messages[0]["text"] == text
    assert claude.prompts[0].startswith(text.strip())


def test_a_message_over_the_size_limit_is_refused_with_a_clear_message(browser: Browser) -> None:
    sid = browser.new_session()
    r = browser.send(sid, "x" * 2_000_000)
    assert r.status_code in (413, 422), r.status_code
    said = r.text.lower()
    assert "characters" in said or "too long" in said or "bigger than" in said, r.text
    assert browser.get(f"/api/sessions/{sid}/messages").json() == []


@pytest.mark.parametrize("text", WEIRD_TEXT)
def test_odd_characters_round_trip_in_titles_messages_and_comments(
    browser: Browser, text: str
) -> None:
    sid = browser.example_session()
    r = browser.post("/api/sessions", json={"title": text})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == text
    assert browser.get(f"/api/sessions/{r.json()['id']}").json()["title"] == text

    assert browser.send(sid, text).status_code == 202
    browser.settle(sid)
    assert browser.get(f"/api/sessions/{sid}/messages").json()[0]["text"] == text

    build = browser.builds(sid)[0]
    face = _a_face(browser, sid, build["id"])
    r = browser.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": build["id"], "face_id": face, "point": [0, 0, 0], "text": text},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = browser.post(f"/api/sessions/{sid}/comments/{cid}/replies", json={"text": text})
    assert r.status_code == 201
    comment = next(c for c in browser.get(f"/api/sessions/{sid}/comments").json() if c["id"] == cid)
    assert comment["text"] == text and comment["replies"][-1]["text"] == text
    # JSON, never HTML: nothing a browser would render from the API itself.
    assert (
        browser.get(f"/api/sessions/{sid}/comments")
        .headers["content-type"]
        .startswith("application/json")
    )


def test_an_empty_comment_or_reply_is_refused(browser: Browser) -> None:
    sid = browser.example_session()
    build = browser.builds(sid)[0]
    face = _a_face(browser, sid, build["id"])
    body = {"build_id": build["id"], "face_id": face, "point": [0, 0, 0], "text": ""}
    assert browser.post(f"/api/sessions/{sid}/comments", json=body).status_code == 422
    body["text"] = "ok"
    cid = browser.post(f"/api/sessions/{sid}/comments", json=body).json()["id"]
    r = browser.post(f"/api/sessions/{sid}/comments/{cid}/replies", json={"text": ""})
    assert r.status_code == 422


def test_a_huge_title_is_refused_or_kept_whole(browser: Browser) -> None:
    title = "T" * 100_000
    r = browser.post("/api/sessions", json={"title": title})
    if r.status_code == 201:
        assert browser.get(f"/api/sessions/{r.json()['id']}").json()["title"] == title
    else:
        assert r.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"text": ',
        '{"text": 5}',
        '{"text": null}',
        '{"text": "hi", "mode": "yolo"}',
        '{"text": "hi", "effort": "infinite"}',
        '{"text": "hi", "model": "gpt-4"}',
        '{"text": "hi", "comments": ["../../etc/passwd"]}',
        "[]",
    ],
)
def test_malformed_message_bodies_are_422_not_500(browser: Browser, body: str) -> None:
    sid = browser.new_session()
    r = browser.post(
        f"/api/sessions/{sid}/messages",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["detail"]
    assert browser.status(sid) == "idle"


# --- Absurd dimensions ---------------------------------------------------------------------

ABSURD = [1e9, -1e9, "abc", "", "1e400", True, [1, 2], {"x": 1}]


@pytest.mark.parametrize("value", ABSURD, ids=repr)
def test_absurd_hand_edits_to_a_length_are_refused_with_a_clear_message(
    browser: Browser, value
) -> None:
    sid, _ = box_session(browser)
    before = {p["name"]: p for p in browser.get(f"/api/sessions/{sid}/parameters").json()}
    r = browser.patch(f"/api/sessions/{sid}/parameters/length", json={"value": value})
    assert r.status_code == 422, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert "length" in detail if isinstance(detail, str) else detail  # or pydantic's list
    after = {p["name"]: p for p in browser.get(f"/api/sessions/{sid}/parameters").json()}
    assert after["length"] == before["length"]


@pytest.mark.parametrize("value", [0, -5])
def test_a_zero_or_negative_size_is_refused_or_reported_by_the_rebuild(
    browser: Browser, claude, value
) -> None:
    """A size of 0 or below can't be built. Either the edit is refused (422, naming the
    parameter) or it is taken and the next build says it failed: never a silent bad model."""
    sid, build = box_session(browser)
    r = browser.patch(f"/api/sessions/{sid}/parameters/length", json={"value": value})
    if r.status_code == 422:
        assert "length" in r.json()["detail"]
        return
    assert r.status_code == 200, r.text
    claude.scripts.append([("run_checks", {}), ("finish", {"outcome": "done", "summary": "x"})])
    assert browser.send(sid, "check it").status_code == 202
    browser.settle(sid)
    text = " ".join(
        str(d) for _, t, d in browser.events(sid) if t in ("error", "text", "tool_result")
    )
    newest = browser.builds(sid)[-1]
    assert newest["id"] != build["id"] or "fail" in text.lower(), text[-2000:]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_in_a_hand_edit_are_refused(browser: Browser, literal: str) -> None:
    sid, _ = box_session(browser)
    r = browser.patch(
        f"/api/sessions/{sid}/parameters/length",
        content=f'{{"value": {literal}}}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422, (r.status_code, r.text)
    values = [p["value"] for p in browser.get(f"/api/sessions/{sid}/parameters").json()]
    assert all(not (isinstance(v, float) and not math.isfinite(v)) for v in values)


def test_a_hand_edit_to_an_unknown_parameter_is_404(browser: Browser) -> None:
    sid, _ = box_session(browser)
    for name in ["nope", "../../etc/passwd", "length.x.y", "%00"]:
        r = browser.patch(f"/api/sessions/{sid}/parameters/{name}", json={"value": 3})
        assert r.status_code == 404, (name, r.status_code)


@pytest.mark.parametrize("value", [0, -1, "abc", 1e9])
def test_absurd_sketch_sizes_are_refused(browser: Browser, value) -> None:
    sid = browser.example_session()
    build = browser.builds(sid)[0]
    face = _a_face(browser, sid, build["id"])
    r = browser.post(
        f"/api/sessions/{sid}/sketches",
        json={
            "build_id": build["id"],
            "face_id": face,
            "shape": "circle",
            "operation": "cut",
            "center": [0, 0],
            "size": {"diameter": value},
            "depth": value,
        },
    )
    assert r.status_code in (404, 409, 422), (r.status_code, r.text)
    assert r.status_code != 500


def test_a_request_for_absurd_sizes_still_gets_an_honest_answer(browser: Browser) -> None:
    """A message asking for a 0 or 1e9 mm box is the model's to answer; the run still ends
    cleanly (no stuck session), whatever it builds."""
    sid = browser.new_session()
    assert browser.send(sid, "make a box 0 x 0 x 0 mm").status_code == 202
    assert browser.settle(sid) in ("idle", "waiting_for_answer")
    sid = browser.new_session()
    assert browser.send(sid, "make a box 1000000000 x 1 x 1 mm").status_code == 202
    assert browser.settle(sid) in ("idle", "waiting_for_answer")
    kinds = [t for _, t, _ in browser.events(sid)]
    assert "message_end" in kinds


def _a_face(browser: Browser, sid: str, build_id: str) -> str:
    """A face ID of the build's mesh."""
    from app.session_api import SESSIONS

    state = SESSIONS.by_id[sid]
    return sorted(state.face_ids(state.build(build_id)))[0]
