"""Concurrency and ownership abuse (`docs/security.md`): spammed sends, doubled reviews,
sessions made in a burst, and a second browser guessing every ID on every route (IDOR)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import Browser, box_session

from app.main import app
from app.owners import OWNER_COOKIE


def test_twenty_sends_at_once_start_exactly_one_run(browser: Browser, claude) -> None:
    claude.delay = 0.05  # a run that takes a moment, so the sends overlap it
    sid = browser.new_session()
    with ThreadPoolExecutor(20) as pool:
        codes = list(
            pool.map(lambda i: browser.send(sid, f"make a box 1{i} x 10 x 10 mm"), range(20))
        )
    statuses = [r.status_code for r in codes]
    assert statuses.count(202) == 1, statuses
    assert set(statuses) <= {202, 409}
    for r in codes:
        if r.status_code == 409:
            assert r.json()["detail"]
    browser.settle(sid, keep=False)  # the one run's own messages only
    users = [m for m in browser.get(f"/api/sessions/{sid}/messages").json() if m["role"] == "user"]
    assert len(users) == 1
    assert len(claude.prompts) == 1


def test_the_same_answer_twice_at_once_is_taken_once(browser: Browser) -> None:
    sid = browser.example_session()
    assert browser.send(sid, "check the new revision").status_code == 202
    assert browser.settle(sid) == "waiting_for_answer"
    question = next(d for _, t, d in browser.events(sid) if t == "question")
    answer = {"question_id": question["question_id"], "option_id": "accept"}
    with ThreadPoolExecutor(5) as pool:
        codes = [
            r.status_code
            for r in pool.map(
                lambda _: browser.post(f"/api/sessions/{sid}/messages", json={"answer": answer}),
                range(5),
            )
        ]
    assert codes.count(202) == 1, codes
    browser.settle(sid)
    answers = [m for m in browser.get(f"/api/sessions/{sid}/messages").json() if m.get("answer")]
    assert len(answers) == 1


def _session_with_changes(browser: Browser, claude) -> tuple[str, list[str]]:
    """A design without a board and a Manual run that proposes two changes."""
    sid, _ = box_session(browser)
    changes = [{"name": "length", "value": 50}, {"name": "width", "value": 35}]
    claude.scripts.append(
        [
            ("set_parameter", {"changes": changes}),
            ("finish", {"outcome": "done", "summary": "Longer and wider."}),
        ]
    )
    assert browser.send(sid, "make it bigger", mode="manual").status_code == 202
    browser.settle(sid)
    change_set = browser.get(f"/api/sessions/{sid}/changes")
    assert change_set.status_code == 200, change_set.text
    return sid, [c["id"] for c in change_set.json()["changes"]]


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_the_same_review_twice_at_once_rebuilds_once(browser: Browser, claude, action: str) -> None:
    sid, ids = _session_with_changes(browser, claude)
    before = len(browser.builds(sid))
    body = {"accept": ids, "reject": []} if action == "accept" else {"accept": [], "reject": ids}
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: browser.post(f"/api/sessions/{sid}/changes", json=body), range(2))
        )
    codes = sorted(r.status_code for r in results)
    assert codes in ([200, 200], [200, 409]), [(r.status_code, r.text) for r in results]
    browser.settle(sid)
    assert len(browser.builds(sid)) - before <= 1  # never rebuilt twice for one review
    final = browser.get(f"/api/sessions/{sid}/changes").json()
    states = {c["status"] for c in final["changes"]}
    assert states == {"accepted" if action == "accept" else "rejected"}, final


def test_accept_and_reject_of_the_same_change_at_once_leave_one_answer(
    browser: Browser, claude
) -> None:
    sid, ids = _session_with_changes(browser, claude)
    bodies = [{"accept": ids[:1], "reject": []}, {"accept": [], "reject": ids[:1]}]
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda b: browser.post(f"/api/sessions/{sid}/changes", json=b), bodies)
        )
    assert all(r.status_code in (200, 409) for r in results)
    browser.settle(sid)
    final = {
        c["id"]: c["status"] for c in browser.get(f"/api/sessions/{sid}/changes").json()["changes"]
    }
    assert final[ids[0]] in ("accepted", "rejected")


def test_accept_and_reject_in_one_request_is_refused(browser: Browser, claude) -> None:
    sid, ids = _session_with_changes(browser, claude)
    r = browser.post(f"/api/sessions/{sid}/changes", json={"accept": ids[:1], "reject": ids[:1]})
    assert r.status_code in (409, 422), r.text


def test_twenty_sessions_made_at_once_get_distinct_ids(browser: Browser) -> None:
    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(lambda _: browser.post("/api/sessions", json={}), range(20)))
    assert all(r.status_code == 201 for r in results)
    ids = [r.json()["id"] for r in results]
    assert len(set(ids)) == 20
    listed = {s["id"] for s in browser.get("/api/sessions").json() if not s.get("sample")}
    assert set(ids) <= listed


def test_session_ids_are_not_guessable(browser: Browser) -> None:
    a, b = browser.new_session(), browser.new_session()
    assert not re.fullmatch(r"session-\d+", a) and not re.fullmatch(r"session-\d+", b)


# --- IDOR: every route, from another browser ---------------------------------------------------


def _filled_routes(sid: str, ids: dict[str, str]) -> list[tuple[str, str]]:
    """Every session route of the API (its OpenAPI document), with real IDs filled in."""
    out = []
    for path, methods in app.openapi()["paths"].items():
        if "{session_id}" not in path:
            continue
        url = path.replace("{session_id}", sid)
        for name, value in ids.items():
            url = url.replace("{" + name + "}", value)
        assert "{" not in url, url
        for method in sorted(
            m.upper() for m in methods if m in ("get", "post", "put", "patch", "delete")
        ):
            out.append((method, url))
    return out


def test_another_browser_gets_404_on_every_route_and_changes_nothing(
    browser: Browser, other: Browser
) -> None:
    sid, build = box_session(browser)
    bid = build["id"]
    state_before = browser.get(f"/api/sessions/{sid}").json()
    face = sorted(_state(sid).face_ids(_state(sid).build(bid)))[0]
    comment = browser.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": bid, "face_id": face, "point": [0, 0, 0], "text": "mine"},
    ).json()
    export = browser.post(
        f"/api/sessions/{sid}/exports", json={"build_id": bid, "formats": ["stl"]}
    ).json()
    attachment = browser.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("a.png", b"secret", "image/png")}
    ).json()
    ids = {
        "build_id": bid,
        "comment_id": comment["id"],
        "export_id": export["id"],
        "name": export["files"][0]["name"],
        "attachment_id": attachment["url"].rsplit("/", 1)[-1],
        "face_id": face,
        "map_type": "thickness",
        "constraint_id": "wall",
        "tag": "x",
        "item_id": "queued-0123456789abcdef",  # a queued message: the owner check comes first
    }
    routes = _filled_routes(sid, ids)
    assert len(routes) > 30
    leaks = []
    for method, url in routes:
        r = other.request(method, url, json={})
        # 415: a JSON body to an upload route, refused before the route (`app.security.web`).
        if r.status_code not in (404, 415, 422) or "secret" in r.text or "mine" in r.text:
            leaks.append((method, url, r.status_code, r.text[:120]))
    assert not leaks, leaks
    # Nothing changed for the owner.
    assert browser.get(f"/api/sessions/{sid}").json() == state_before
    assert [c["text"] for c in browser.get(f"/api/sessions/{sid}/comments").json()] == ["mine"]
    assert sid not in {s["id"] for s in other.get("/api/sessions").json()}


def test_a_malformed_owner_cookie_gets_a_fresh_owner(client, browser: Browser) -> None:
    sid = browser.new_session()
    for bad in ["", "x", "a" * 500, "../../etc", "\x00"]:
        client.cookies.clear()
        r = client.get(f"/api/sessions/{sid}", headers={"cookie": f"{OWNER_COOKIE}={bad}"})
        assert r.status_code == 404, (bad, r.status_code)


def test_the_owner_cookie_is_http_only_and_same_site(client) -> None:
    client.cookies.clear()
    cookie = client.get("/api/health").headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_a_cross_site_post_with_the_owners_cookie_is_refused(browser: Browser) -> None:
    """SameSite=Lax keeps the cookie off cross-site POSTs in browsers; this is the server's
    own check, for browsers and proxies that don't."""
    sid = browser.new_session()
    r = browser.post(
        f"/api/sessions/{sid}/messages",
        json={"text": "make a box 10 x 10 x 10 mm"},
        headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"},
    )
    assert r.status_code == 403


def test_a_form_post_is_not_taken_as_json(browser: Browser) -> None:
    """A cross-site HTML form can only send form or text bodies: the API takes JSON only."""
    sid = browser.new_session()
    for content_type in ("text/plain", "application/x-www-form-urlencoded"):
        r = browser.post(
            f"/api/sessions/{sid}/messages",
            content='{"text": "make a box 10 x 10 x 10 mm"}',
            headers={"content-type": content_type},
        )
        # Refused: 415 by the web layer's JSON-only check (`app.security.web`), else 422.
        assert r.status_code in (415, 422), (content_type, r.status_code)
    assert browser.get(f"/api/sessions/{sid}/messages").json() == []


def test_no_cors_grant_for_other_origins(browser: Browser) -> None:
    r = browser.request(
        "OPTIONS",
        "/api/sessions",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_guessing_a_saved_session_id_does_not_load_it(browser: Browser, other: Browser) -> None:
    from app.session_api import SESSIONS

    sid = browser.new_session()
    SESSIONS.by_id.pop(sid)  # saved, not in memory (after a restart, or evicted)
    assert other.get(f"/api/sessions/{sid}").status_code == 404
    assert sid not in SESSIONS.by_id  # another browser's guess loaded nothing


def _state(sid: str):
    from app.session_api import SESSIONS

    return SESSIONS.by_id[sid]
