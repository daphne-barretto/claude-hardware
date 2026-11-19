"""Access control (`docs/security.md`): unguessable IDs, the owner check on every session route
(two owners, every route: no IDOR), unowned sessions claimable by nobody, and the owner cookie's
`Secure` flag."""

from __future__ import annotations

import inspect
import re

import pytest
import test_session_api as api
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import history_api, message_queue, modeling_api, session_api, shares
from app.main import app
from app.owners import OWNER_COOKIE
from app.session_api import CONFIG, SESSIONS, fixture_services, session_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.by_id.clear()


def first_comment(c: TestClient, text: str = "Check this") -> tuple[str, dict]:
    """A session on the first example with a comment pinned to its first build."""
    from test_comments import _latest, _mesh_faces, _pin

    sid, _ = api.start(c)
    build = _latest(c, sid)
    _, _, face = _mesh_faces(c, build)[0]
    return sid, _pin(c, sid, build, face, text)


# --- Unguessable IDs ------------------------------------------------------------------------------


def test_ids_are_random_with_readable_prefixes(client) -> None:
    sid, comment = first_comment(client)
    assert re.fullmatch(r"session-[0-9a-f]{24}", sid)
    assert re.fullmatch(r"comment-[0-9a-f]{16}", comment["id"])
    build = client.get(f"/api/sessions/{sid}/builds").json()[-1]
    export = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build["id"], "formats": ["step"]}
    ).json()
    assert re.fullmatch(r"export-[0-9a-f]{16}", export["id"])
    attachment = client.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("a.png", b"hello", "image/png")}
    ).json()
    assert re.fullmatch(r".*/attachments/attachment-[0-9a-f]{16}", attachment["url"])
    # Queued messages, share links and standards sets too.
    queued = client.post(f"/api/sessions/{sid}/queue", json={"text": "later"}).json()
    assert re.fullmatch(r"queued-[0-9a-f]{16}", queued["id"])
    link = client.post(f"/api/sessions/{sid}/shares", json={"kind": "view"}).json()
    assert re.fullmatch(r"[0-9a-f]{48}", link["token"])
    own = client.post("/api/standards", json={"name": "Mine"}).json()
    assert re.fullmatch(r"set_[0-9a-f]{16}", own["id"])
    many = {client.post("/api/sessions", json={}).json()["id"] for _ in range(30)}
    assert len(many) == 30


def test_real_builds_and_imports_get_random_build_ids() -> None:
    from app.builds import new_build_id

    ids = {new_build_id() for _ in range(200)}
    assert len(ids) == 200 and all(re.fullmatch(r"build-[0-9a-f]{12}", i) for i in ids)


def test_the_samples_keep_their_ids(client) -> None:
    samples = [s for s in client.get("/api/sessions").json() if s.get("sample")]
    for sample in samples:
        assert client.get(f"/api/sessions/{sample['id']}").status_code == 200


# --- Every route checks the owner -----------------------------------------------------------------

# Every router with session routes: the session API, modeling, the message queue, the design
# timeline and the share links' owner routes (a link's own routes take a token, not a session).
ROUTERS = [session_api.router, modeling_api.router, message_queue.router, history_api.router]
SESSION_ROUTES = [
    r
    for r in [*(r for router in ROUTERS for r in router.routes), *shares.router.routes]
    if isinstance(r, APIRoute) and "{session_id}" in r.path
]


def test_every_session_route_handler_checks_the_owner() -> None:
    """Statically: each handler loads its session through `SESSIONS.get`, which 404s another
    owner's session (or through a helper that does)."""
    helpers = {"_change_constraint", "backend.links", "backend.create", "backend.update"}
    helpers |= {"backend.revoke"}
    for route in SESSION_ROUTES:
        source = inspect.getsource(route.endpoint)
        assert "SESSIONS.get(session_id)" in source or any(h in source for h in helpers), route.path
    assert "SESSIONS.get(session_id)" in inspect.getsource(session_api._change_constraint)
    for name in ("links", "create", "update", "revoke"):  # the share links' owner routes
        assert "SESSIONS.get(session_id)" in inspect.getsource(getattr(shares.SHARES, name))
    paths = {r.path for r in SESSION_ROUTES}
    for path in ("/queue", "/queue/{item_id}/send", "/stop", "/retry", "/history/undo", "/shares"):
        assert "/api/sessions/{session_id}" + path in paths, path
    assert "/api/sessions/{session_id}/standards" in paths


class Owner:
    """A browser: its own cookie jar on the shared app."""

    def __init__(self) -> None:
        self.c = TestClient(app)  # no lifespan: the fixture's client runs it

    def __getattr__(self, name):
        return getattr(self.c, name)


def test_another_owner_gets_404_on_every_route(client) -> None:
    """Owner A makes a session with a build, comment, export, attachment and a run; owner B
    tries every session route with A's IDs and valid bodies: 404 every time, and A's data is
    unchanged."""
    a, b = client, Owner()
    sid, comment = first_comment(a)
    build = a.get(f"/api/sessions/{sid}/builds").json()[-1]
    export = a.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build["id"], "formats": ["step"]}
    ).json()
    attachment = a.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("a.png", b"hello", "image/png")}
    ).json()
    from test_comments import _mesh_faces

    face = _mesh_faces(a, build)[0][2]
    ids = {
        "session_id": sid,
        "build_id": build["id"],
        "comment_id": comment["id"],
        "export_id": export["id"],
        "name": export["files"][0]["name"],
        "attachment_id": attachment["url"].rsplit("/", 1)[-1],
        "map_type": build["maps"][0] if build.get("maps") else "thickness",
        "face_id": face,
        "constraint_id": "c-1",
        "tag": "fillet_hand_1",
    }
    point = comment["point"]
    bodies = {
        ("POST", "/messages"): {"text": "hello"},
        ("POST", "/revisions/next"): {},
        ("PUT", "/source"): {"build_id": build["id"], "code": "def build(params):\n    pass\n"},
        ("POST", "/changes"): {"accept": [], "reject": []},
        ("POST", "/risks"): {"accept": [], "reject": []},
        ("POST", "/find"): {"query": "holes"},
        ("PATCH", "/parameters/{name}"): {"value": 1},
        ("PATCH", "/constraints/{constraint_id}"): {"value": 1},
        ("POST", "/comments"): {
            "build_id": build["id"],
            "face_id": face,
            "point": point,
            "text": "mine now",
        },
        ("PATCH", "/comments/{comment_id}"): {"resolved": True},
        ("POST", "/replies"): {"text": "hi"},
        ("POST", "/telemetry"): {"kind": "view", "view": "map"},
        ("POST", "/exports"): {"build_id": build["id"], "formats": ["step"]},
        ("POST", "/sketches"): {
            "build_id": build["id"],
            "face_id": face,
            "shape": "circle",
            "operation": "cut",
            "width": 2,
            "u": 0,
            "v": 0,
            "depth": 1,
        },
        ("POST", "/edits"): {"build_id": build["id"], "kind": "fillet", "edges": [], "radius": 1},
        ("PATCH", "/edits/{tag}"): {"suppressed": True},
        ("PUT", "/standards"): {"standards": "team"},
        ("PATCH", "{session_id}"): {"title": "mine now"},  # the rename
        ("POST", "/queue"): {"text": "mine now"},
        ("PATCH", "/queue/{item_id}"): {"text": "mine now"},
        ("POST", "/retry"): {"mode": "auto"},
        ("POST", "/shares"): {"kind": "view"},
        ("POST", "/shares/update"): {"kind": "view"},
        ("POST", "/shares/revoke"): {"kind": "view"},
    }
    before = {
        path: a.get(f"/api/sessions/{sid}/{path}").json()
        for path in ("messages", "comments", "builds", "parameters")
    }
    refused_by_body = []
    for route in SESSION_ROUTES:
        url = route.path.format(**{k: ids.get(k, "x") for k in re.findall(r"{(\w+)", route.path)})
        for method in route.methods:
            body = next(
                (v for (m, end), v in bodies.items() if m == method and route.path.endswith(end)),
                None,
            )
            if route.path.endswith(("/attachments", "/imports")) and method == "POST":
                r = b.request(
                    method, url, files={"file": ("x.step", b"ISO-10303-21;", "text/plain")}
                )
            elif method in ("POST", "PUT", "PATCH"):
                r = b.request(method, url, json=body if body is not None else {})
            else:
                r = b.request(method, url, params={"follow": "false", "against": build["id"]})
            assert r.status_code in (404, 422), (method, url, r.status_code, r.text)
            if r.status_code == 422:
                refused_by_body.append((method, route.path))
            else:
                assert r.json()["detail"] == f"no session {sid}", (method, url, r.text)
    # Only bodies this test can't make valid stop before the owner check; the static test above
    # covers those handlers.
    assert set(refused_by_body) <= {
        ("POST", "/api/sessions/{session_id}/sketches"),
        ("POST", "/api/sessions/{session_id}/edits"),
        ("PATCH", "/api/sessions/{session_id}/edits/{tag}"),
    }, refused_by_body
    # B's list has none of A's, and A's data is as it was.
    assert sid not in [s["id"] for s in b.get("/api/sessions").json()]
    for path, value in before.items():
        assert a.get(f"/api/sessions/{sid}/{path}").json() == value, path


def test_a_session_saved_without_an_owner_is_nobodys(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    state = SESSIONS.by_id[sid]
    state.owner = None  # saved before sessions had owners
    state.save()
    SESSIONS.by_id.clear()
    other = Owner()
    assert other.get(f"/api/sessions/{sid}").status_code == 404
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert session_store().owners()[sid] is None, "nobody claimed it"


# --- The owner cookie -----------------------------------------------------------------------------


def cookie(**kw) -> str:
    c = TestClient(app, **kw)
    return c.get("/api/standards").headers["set-cookie"]


def test_the_cookie_is_secure_over_https(monkeypatch) -> None:
    monkeypatch.delenv("RENDER", raising=False)
    plain = cookie()
    assert plain.startswith(f"{OWNER_COOKIE}=") and "HttpOnly" in plain
    assert "SameSite=Lax" in plain and "Secure" not in plain
    assert "Secure" in cookie(base_url="https://testserver")
    # Behind the app's own proxy, which says the browser used HTTPS.
    proxied = cookie(client=("127.0.0.1", 1), headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in proxied
    # The header from anyone else is not believed.
    assert "Secure" not in cookie(headers={"X-Forwarded-Proto": "https"})
    monkeypatch.setenv("RENDER", "true")  # Render serves HTTPS only
    assert "Secure" in cookie()


def test_ids_are_not_found_by_counting(client) -> None:
    client.post("/api/sessions", json={})
    for n in range(1, 5):
        assert client.get(f"/api/sessions/session-{n}").status_code == 404
