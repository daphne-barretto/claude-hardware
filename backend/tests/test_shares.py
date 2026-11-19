"""Share links (`app.shares`): a read-only snapshot of a session (`view`) and a design anyone
with the link can copy (`copy`), owned by the sharer, at an unguessable `/share/<token>`.

The browser test of the same, with two browser contexts, is `frontend/e2e/live-share.spec.ts`.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_concurrent_users import Browser, own

from app.examples import examples
from app.main import app
from app.session_api import CONFIG, SESSIONS, fixture_services
from app.session_store import _STORES, session_store
from app.share_routes import READ_ONLY, TURNED_OFF

EXAMPLE = {"example": examples()[0].id}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import e2e_api

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)  # fast builds
    monkeypatch.setattr(CONFIG, "driver", e2e_api.StandInClaude)  # a scripted Claude
    SESSIONS.by_id.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.by_id.clear()


def wait(browser: Browser, sid: str, seconds: float = 30) -> dict:
    """The session once nothing runs in it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        session = browser.get(f"/api/sessions/{sid}").json()
        state = SESSIONS.by_id.get(sid)
        tasks = [t for t in (state.initial, state.run) if t] if state else []
        done = session["status"] == "waiting_for_answer" or all(t.done() for t in tasks)
        if session["status"] != "running" and done:
            return session
        time.sleep(0.05)
    raise AssertionError("session still running")


def started(owner: Browser, message: str | None = "Check it") -> str:
    """An example session with its first build and, unless None, a reply to `message`."""
    sid = owner.post("/api/sessions", json=EXAMPLE).json()["id"]
    wait(owner, sid)
    if message:
        assert (
            owner.post(f"/api/sessions/{sid}/messages", json={"text": message}).status_code == 202
        )
        wait(owner, sid)
    return sid


def share(owner: Browser, sid: str, **body: Any) -> dict:
    r = owner.post(f"/api/sessions/{sid}/shares", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def view(browser: Browser, link: dict, path: str = "", **kwargs: Any) -> Any:
    return browser.get(f"/api/shares/{link['token']}{path}", **kwargs)


def texts(messages: list[dict]) -> list[str]:
    return [m["text"] for m in messages]


# --- Create, view, update, revoke ------------------------------------------------------------


def test_a_link_is_unguessable_and_another_browser_sees_the_chat_and_model(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner)
    link = share(owner, sid)
    assert len(link["token"]) == 48 and int(link["token"], 16) >= 0  # 192 random bits
    assert link["url"] == f"/share/{link['token']}" and link["active"] and link["up_to_date"]
    assert share(owner, started(owner, None))["token"] != link["token"]

    # The other browser can't open the session itself, but can open the link.
    assert other.get(f"/api/sessions/{sid}").status_code == 404
    session = view(other, link).json()
    assert session["shared"] == "view" and session["id"] == f"share-{link['token']}"
    assert session["title"] == owner.get(f"/api/sessions/{sid}").json()["title"]
    assert texts(view(other, link, "/messages").json()) == texts(
        owner.get(f"/api/sessions/{sid}/messages").json()
    )
    builds = view(other, link, "/builds").json()
    assert [b["id"] for b in builds] == [
        b["id"] for b in owner.get(f"/api/sessions/{sid}/builds").json()
    ]
    build = builds[-1]
    assert build["mesh_url"].startswith(f"/api/shares/{link['token']}/builds/")
    for url in (build["mesh_url"], build["checks_url"], build["source_url"], build["board_url"]):
        assert other.get(url).status_code == 200, url
    for path in ("/parameters", "/comments", "/constraints", "/edits", "/sketches"):
        assert view(other, link, path).status_code == 200, path
    events = view(other, link, "/events", params={"follow": "false"}).text
    assert "build_ready" in events and f"/api/sessions/{sid}/" not in events
    found = other.post(
        f"/api/shares/{link['token']}/builds/{build['id']}/find", json={"query": "walls"}
    )
    assert found.status_code == 200
    # Viewing doesn't make the link a session of the viewer's.
    assert own(other) == []


def test_the_snapshot_is_unaffected_by_later_messages_until_it_is_updated(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner, None)
    link = share(owner, sid)
    before = view(other, link, "/messages").json()
    owner.post(f"/api/sessions/{sid}/messages", json={"text": "Check it"})
    wait(owner, sid)
    assert view(other, link, "/messages").json() == before
    [listed] = owner.get(f"/api/sessions/{sid}/shares").json()
    assert listed["token"] == link["token"] and not listed["up_to_date"]

    updated = owner.post(f"/api/sessions/{sid}/shares/update", json={"kind": "view"}).json()
    assert updated["token"] == link["token"] and updated["up_to_date"]
    assert "Check it" in texts(view(other, link, "/messages").json())


def test_stop_sharing_turns_the_link_off(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner, None)
    link = share(owner, sid)
    revoked = owner.post(f"/api/sessions/{sid}/shares/revoke", json={"kind": "view"})
    assert revoked.status_code == 200 and revoked.json()["active"] is False
    for path in ("", "/messages", "/builds"):
        r = view(other, link, path)
        assert r.status_code == 410 and r.json()["detail"] == TURNED_OFF
    assert other.post("/api/sessions", json={"from_share": link["token"]}).status_code == 410
    assert owner.get(f"/api/sessions/{sid}/shares").json() == []
    # Sharing again makes a new link; the old one stays off.
    again = share(owner, sid)
    assert again["token"] != link["token"] and view(other, link).status_code == 410
    assert client.get("/api/shares/" + "0" * 48).status_code == 404


def test_only_the_owner_can_share_update_or_stop(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner, None)
    link = share(owner, sid)
    assert other.get(f"/api/sessions/{sid}/shares").status_code == 404
    assert other.post(f"/api/sessions/{sid}/shares", json={}).status_code == 404
    assert other.post(f"/api/sessions/{sid}/shares/update", json={}).status_code == 404
    assert other.post(f"/api/sessions/{sid}/shares/revoke", json={}).status_code == 404
    assert view(other, link).status_code == 200
    # One live link of each kind.
    assert owner.post(f"/api/sessions/{sid}/shares", json={}).status_code == 409


def test_a_sample_cant_be_shared(client) -> None:
    from app import samples

    sample = next(iter(samples.catalogue()))
    assert client.post(f"/api/sessions/{sample}/shares", json={}).status_code == 409


# --- Read-only ----------------------------------------------------------------------------


def _mutations() -> list[tuple[str, str]]:
    """Every mutating session route the API declares, as (method, path)."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/sessions/{session_id}"):
            out += [(m.upper(), path) for m in ops if m in ("post", "put", "patch", "delete")]
    return out


def test_every_mutating_route_is_refused_on_a_shared_link(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner)
    link = share(owner, sid)
    build = view(other, link, "/builds").json()[-1]["id"]
    values = {
        "{build_id}": build,
        "{name}": "wall",
        "{constraint_id}": "c",
        "{comment_id}": "c",
        "{tag}": "t",
        "{export_id}": "e",
    }
    allowed = {"/builds/{build_id}/find", "/exports", "/telemetry"}  # they change nothing
    checked = 0
    for method, path in _mutations():
        rest = path.removeprefix("/api/sessions/{session_id}")
        if rest in allowed or rest.startswith("/shares"):
            continue
        for key, value in values.items():
            rest = rest.replace(key, value)
        r = other.request(method, f"/api/shares/{link['token']}{rest}", json={"text": "x"})
        if rest in ("/attachments", "/imports") and r.status_code == 415:
            continue  # a JSON body to an upload route: the web layer refuses it first
        assert r.status_code == 409, (method, rest, r.status_code)
        assert r.json()["detail"] == READ_ONLY
        checked += 1
    assert checked >= 15
    # Nothing reached the session.
    assert texts(owner.get(f"/api/sessions/{sid}/messages").json()) == texts(
        view(other, link, "/messages").json()
    )
    # And the inside ID of the snapshot is no way in either.
    assert other.get(f"/api/sessions/share-{link['token']}").status_code == 404
    assert (
        other.post(f"/api/sessions/share-{link['token']}/messages", json={"text": "x"}).status_code
        == 404
    )


def test_the_snapshot_leaves_out_the_owner_and_attached_files(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = owner.post("/api/sessions", json=EXAMPLE).json()["id"]
    wait(owner, sid)
    up = owner.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("notes.step", b"secret", "application/octet-stream")},
    ).json()
    owner.post(f"/api/sessions/{sid}/messages", json={"text": "Check it", "attachments": [up]})
    wait(owner, sid)
    link = share(owner, sid)
    record = session_store().load_share(link["token"])
    doc = str(record["snapshot"])
    assert owner.cookies["hardware_owner"] not in doc
    [shown] = [m for m in view(other, link, "/messages").json() if m["attachments"]]
    assert shown["attachments"][0]["name"] == "notes.step" and shown["attachments"][0]["url"] == ""
    aid = up["url"].rsplit("/", 1)[-1]
    assert view(other, link, f"/attachments/{aid}").status_code == 404

    owner.post(f"/api/sessions/{sid}/shares/update", json={"kind": "view", "attachments": True})
    [shown] = [m for m in view(other, link, "/messages").json() if m["attachments"]]
    assert other.get(shown["attachments"][0]["url"]).content == b"secret"


def test_a_link_survives_a_restart(client, tmp_path) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner)
    link = share(owner, sid)
    messages = view(other, link, "/messages").json()
    from app.shares import SHARES

    SHARES._views.clear()
    SESSIONS.by_id.clear()
    for store in _STORES.values():
        store.close()
    _STORES.clear()
    assert view(other, link, "/messages").json() == messages
    assert owner.get(f"/api/sessions/{sid}/shares").json()[0]["token"] == link["token"]


# --- Continue from this / Open a copy -------------------------------------------------------


def test_continue_from_this_makes_the_viewers_own_session(client) -> None:
    owner, other = Browser(client), Browser(client)
    sid = started(owner)
    link = share(owner, sid)
    r = other.post("/api/sessions", json={"from_share": link["token"]})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["id"] != sid and copy["title"].endswith("(copy)") and copy["shared"] is None
    assert own(other) == [copy["id"]] and copy["id"] not in own(owner)
    assert owner.get(f"/api/sessions/{copy['id']}").status_code == 404
    assert texts(other.get(f"/api/sessions/{copy['id']}/messages").json()) == texts(
        view(other, link, "/messages").json()
    )
    builds = other.get(f"/api/sessions/{copy['id']}/builds").json()
    assert builds and builds[-1]["mesh_url"].startswith(f"/api/sessions/{copy['id']}/")
    assert other.get(builds[-1]["mesh_url"]).status_code == 200
    # The copy takes messages; the source is unchanged.
    before = texts(owner.get(f"/api/sessions/{sid}/messages").json())
    assert (
        other.post(f"/api/sessions/{copy['id']}/messages", json={"text": "Check it"}).status_code
        == 202
    )
    wait(other, copy["id"])
    assert texts(owner.get(f"/api/sessions/{sid}/messages").json()) == before


def test_share_a_copy_shares_the_design_alone(client) -> None:
    owner, a, b = Browser(client), Browser(client), Browser(client)
    sid = started(owner)
    owner.post(
        f"/api/sessions/{sid}/comments",
        json={
            "build_id": owner.get(f"/api/sessions/{sid}").json()["latest_build_id"],
            "face_id": "f",
            "point": [0, 0, 0],
            "text": "a note",
        },
    )
    link = share(owner, sid, kind="copy")
    assert link["kind"] == "copy"
    session = view(a, link).json()
    assert session["shared"] == "copy"
    assert view(a, link, "/messages").json() == [] and view(a, link, "/comments").json() == []
    latest = owner.get(f"/api/sessions/{sid}").json()["latest_build_id"]
    assert [x["id"] for x in view(a, link, "/builds").json()] == [latest]
    assert view(a, link, "/changes").status_code == 404

    first = a.post("/api/sessions", json={"from_share": link["token"]}).json()
    second = b.post("/api/sessions", json={"from_share": link["token"]}).json()
    assert first["id"] != second["id"]
    assert own(a) == [first["id"]] and own(b) == [second["id"]]
    messages = a.get(f"/api/sessions/{first['id']}/messages").json()
    assert [m["text"] for m in messages] == ["Started from a shared design."]
    assert a.get(f"/api/sessions/{first['id']}/comments").json() == []
    assert [x["id"] for x in a.get(f"/api/sessions/{first['id']}/builds").json()] == [latest]
    params = a.get(f"/api/sessions/{first['id']}/parameters").json()
    copied = {p["name"]: p["value"] for p in params}
    for p in owner.get(f"/api/sessions/{sid}/parameters").json():
        assert copied[p["name"]] == p["value"], p["name"]
    # Each copy is its own: an edit in one changes neither the other nor the source.
    name = next(
        p["name"] for p in params if isinstance(p["value"], int | float) and p["unit"] == "mm"
    )
    value = next(p["value"] for p in params if p["name"] == name) + 1
    assert (
        a.request(
            "PATCH", f"/api/sessions/{first['id']}/parameters/{name}", json={"value": value}
        ).status_code
        == 200
    )
    wait(a, first["id"])

    def value_in(browser: Browser, s: str) -> Any:
        return next(
            p["value"]
            for p in browser.get(f"/api/sessions/{s}/parameters").json()
            if p["name"] == name
        )

    assert value_in(a, first["id"]) == value
    assert value_in(b, second["id"]) != value and value_in(owner, sid) != value

    owner.post(f"/api/sessions/{sid}/shares/revoke", json={"kind": "copy"})
    assert b.post("/api/sessions", json={"from_share": link["token"]}).status_code == 410
