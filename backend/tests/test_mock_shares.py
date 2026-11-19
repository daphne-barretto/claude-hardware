"""Share links in the mock session API (`app.mock_shares`), the same routes as the real API's
(`tests/test_shares.py`)."""

from __future__ import annotations

from test_mock_api import client, parse_sse, start, wait_for

__all__ = ["client"]


def after_run(client) -> str:
    """A mock session after its scripted run: a chat, builds and a question."""
    sid = start(client)
    client.post(f"/api/sessions/{sid}/messages", json={"text": "Update the case"})
    wait_for(client, sid, "question")
    return sid


def test_a_view_link_serves_a_read_only_snapshot(client) -> None:
    sid = after_run(client)
    r = client.post(f"/api/sessions/{sid}/shares", json={"kind": "view"})
    assert r.status_code == 201
    link = r.json()
    base = f"/api/shares/{link['token']}"
    assert client.get(base).json()["shared"] == "view"
    assert [m["text"] for m in client.get(base + "/messages").json()] == [
        m["text"] for m in client.get(f"/api/sessions/{sid}/messages").json()
    ]
    builds = client.get(base + "/builds").json()
    assert builds and all(b["mesh_url"].startswith(base + "/") for b in builds)
    assert client.get(builds[-1]["mesh_url"]).status_code == 200
    events = parse_sse(client.get(base + "/events", params={"follow": "false"}).text)
    assert any(e[1] == "question" for e in events)
    assert client.post(base + "/messages", json={"text": "x"}).status_code == 409
    assert client.patch(base + "/parameters/wall", json={"value": 3}).status_code == 409
    assert client.get(f"/api/sessions/{sid}/shares").json()[0]["up_to_date"]

    revoked = client.post(f"/api/sessions/{sid}/shares/revoke", json={"kind": "view"})
    assert revoked.json()["active"] is False
    assert client.get(base).status_code == 410


def test_continue_and_open_a_copy_make_new_sessions(client) -> None:
    sid = after_run(client)
    view = client.post(f"/api/sessions/{sid}/shares", json={"kind": "view"}).json()
    copy = client.post(f"/api/sessions/{sid}/shares", json={"kind": "copy"}).json()
    assert client.get(f"/api/shares/{copy['token']}/messages").json() == []
    assert len(client.get(f"/api/shares/{copy['token']}/builds").json()) == 1

    cont = client.post("/api/sessions", json={"from_share": view["token"]}).json()
    assert cont["id"] != sid and cont["title"].endswith("(copy)")
    assert len(client.get(f"/api/sessions/{cont['id']}/messages").json()) == len(
        client.get(f"/api/sessions/{sid}/messages").json()
    )
    opened = client.post("/api/sessions", json={"from_share": copy["token"]}).json()
    assert [m["text"] for m in client.get(f"/api/sessions/{opened['id']}/messages").json()] == [
        "Started from a shared design."
    ]
    builds = client.get(f"/api/sessions/{opened['id']}/builds").json()
    assert len(builds) == 1 and builds[0]["mesh_url"].startswith(f"/api/sessions/{opened['id']}/")
    assert client.get(builds[0]["mesh_url"]).status_code == 200
