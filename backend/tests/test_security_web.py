"""The HTTP layer (`docs/security.md`): CSRF, security headers, body size and upload time
limits, the event stream cap, disk quota and export cleanup, the production start check, plain
errors, no secret in any response, and a health check that stays fast under load."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app
from app.security import quota
from app.security.settings import UnsafeConfiguration, check_startup
from app.security.web import STREAMS, SecurityMiddleware, Streams
from app.session_api import CONFIG, SESSIONS, fixture_services

KEY = "sk-ant-api03-" + "Zz9" * 15


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    SESSIONS.by_id.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.by_id.clear()


# --- CSRF -----------------------------------------------------------------------------------------


def test_a_request_from_another_site_is_refused(client) -> None:
    r = client.post("/api/sessions", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["detail"] == (
        "This request came from another website, so it was blocked. Open the app directly and "
        "try again."
    )
    r = client.post("/api/sessions", json={}, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403
    r = client.post("/api/sessions", json={}, headers={"Origin": "null"})
    assert r.status_code == 403
    r = client.post("/api/sessions", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert (
        client.get("/api/sessions", headers={"Origin": "https://evil.example"}).status_code == 200
    )


def test_the_apps_own_origin_is_allowed(client, monkeypatch) -> None:
    ok = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/sessions", json={}, headers=ok).status_code == 201
    # Through the app's Next.js proxy, the browser's host is X-Forwarded-Host.
    with TestClient(app, client=("127.0.0.1", 1)) as proxied:
        headers = {"Origin": "http://localhost:3241", "X-Forwarded-Host": "localhost:3241"}
        assert proxied.post("/api/sessions", json={}, headers=headers).status_code == 201
    # Not through the proxy, X-Forwarded-Host isn't believed.
    r = client.post(
        "/api/sessions", json={}, headers={"Origin": "http://evil", "X-Forwarded-Host": "evil"}
    )
    assert r.status_code == 403
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://demo.example.com")
    r = client.post("/api/sessions", json={}, headers={"Origin": "https://demo.example.com"})
    assert r.status_code == 201
    # A script (no Origin, no Referer) can't be a forged browser request: allowed.
    assert client.post("/api/sessions", json={}).status_code == 201


def test_state_changing_bodies_must_be_json(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(
        f"/api/sessions/{sid}/messages",
        content="text=hello",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415
    assert r.json()["detail"] == "This request needs a JSON body (Content-Type: application/json)."
    r = client.post(
        f"/api/sessions/{sid}/messages", content="{}", headers={"Content-Type": "text/plain"}
    )
    assert r.status_code == 415
    # Uploads are forms.
    r = client.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("a.png", b"hi", "image/png")}
    )
    assert r.status_code == 201


# --- Security headers -----------------------------------------------------------------------------


def test_api_responses_carry_security_headers(client) -> None:
    for r in (client.get("/api/health"), client.get("/api/sessions"), client.get("/api/nope")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_an_attachment_is_downloaded_never_rendered(client) -> None:
    sid = client.post("/api/sessions", json={}).json()["id"]
    url = client.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("x.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")},
    ).json()["url"]
    r = client.get(url)
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


# --- Body size and upload time --------------------------------------------------------------------


def test_a_json_body_over_the_limit_is_refused(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_JSON_BODY_KB", "4")
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"text": "x" * 5000})
    assert r.status_code == 413
    assert (
        r.json()["detail"]
        == "This request is bigger than the 4 KB this demo accepts. Try a smaller one."
    )


def test_a_streamed_body_over_the_limit_is_cut_off(client, monkeypatch) -> None:
    """No Content-Length (chunked): counted as it arrives."""
    monkeypatch.setenv("MAX_JSON_BODY_KB", "4")
    sid = client.post("/api/sessions", json={}).json()["id"]

    def chunks():
        yield b'{"text": "'
        for _ in range(10):
            yield b"x" * 1000
        yield b'"}'

    r = client.post(
        f"/api/sessions/{sid}/messages",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_an_upload_over_the_limit_is_refused(client, monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "0.01")
    sid = client.post("/api/sessions", json={}).json()["id"]
    r = client.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("big.png", b"x" * 20_000, "image/png")}
    )
    assert r.status_code == 413
    assert r.json()["detail"].startswith("This file is bigger than the 0.01 MB this demo accepts.")


def test_a_slow_upload_is_stopped(monkeypatch) -> None:
    """A client trickling its body can't hold the server: the body has a deadline."""
    monkeypatch.setenv("UPLOAD_TIMEOUT_SECONDS", "0.2")
    seen: list[dict] = []

    async def app_reading_body(scope, receive, send):
        from fastapi import HTTPException

        try:
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break
        except HTTPException as error:
            seen.append({"status": error.status_code, "detail": error.detail})
            return

    async def slow_receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/sessions/s/attachments",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
        "client": ("127.0.0.1", 1),
        "query_string": b"",
    }
    asyncio.run(SecurityMiddleware(app_reading_body)(scope, slow_receive, send))
    assert seen == [
        {
            "status": 408,
            "detail": "The upload took longer than 0.2 seconds, so it was stopped. Check your "
            "connection and try again, or try a smaller file.",
        }
    ]


# --- Event streams --------------------------------------------------------------------------------


def test_open_streams_are_capped_per_owner_and_in_all(monkeypatch) -> None:
    monkeypatch.setenv("MAX_EVENT_STREAMS_PER_OWNER", "2")
    monkeypatch.setenv("MAX_EVENT_STREAMS", "3")
    streams = Streams()
    assert streams.open("a") is None and streams.open("a") is None
    assert streams.open("a") == (
        "You have too many tabs following chats live. Close a few tabs and reload this one."
    )
    assert streams.open("b") is None
    assert streams.open("c") == "The demo is busy right now. Please reload in a minute."
    streams.close("a")
    assert streams.open("c") is None


def test_a_stream_over_the_cap_is_refused_and_a_closed_one_frees_its_place(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_EVENT_STREAMS_PER_OWNER", "1")
    sid = client.post("/api/sessions", json={}).json()["id"]
    owner = client.cookies["hardware_owner"]
    STREAMS.by_owner[owner] = 1  # one stream already open in another tab
    STREAMS.total = 1
    r = client.get(f"/api/sessions/{sid}/events")
    assert r.status_code == 429 and "too many tabs" in r.json()["detail"]
    # A snapshot (follow=false) isn't a live stream and isn't capped.
    assert client.get(f"/api/sessions/{sid}/events", params={"follow": "false"}).status_code == 200
    STREAMS.close(owner)
    assert STREAMS.total == 0


# --- Disk quota and export cleanup ---------------------------------------------------------------


def test_the_disk_quota_refuses_new_files_plainly(client, monkeypatch) -> None:
    monkeypatch.setenv("OWNER_DISK_QUOTA_MB", "0.01")
    sid = client.post("/api/sessions", json={}).json()["id"]
    url = f"/api/sessions/{sid}/attachments"
    assert (
        client.post(url, files={"file": ("a.png", b"x" * 12_000, "image/png")}).status_code == 201
    )
    r = client.post(url, files={"file": ("b.png", b"x", "image/png")})
    assert r.status_code == 413
    assert r.json()["detail"] == (
        "This demo keeps up to 0.01 MB of files for each person, and yours is full, so this file "
        "can't be saved. Exported files are cleared after 24 hours, which frees up space."
    )
    # Another owner has their own quota.
    other = TestClient(app)
    sid2 = other.post("/api/sessions", json={}).json()["id"]
    assert (
        other.post(
            f"/api/sessions/{sid2}/attachments", files={"file": ("c.png", b"x", "image/png")}
        ).status_code
        == 201
    )


def test_old_exports_are_cleared(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = tmp_path / "exports" / "session-a" / "export-old"
    new = tmp_path / "exports" / "session-a" / "export-new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "part.step").write_bytes(b"x")
    stale = time.time() - 25 * 3600
    os.utime(old, (stale, stale))
    assert quota.cleanup_exports(force=True) == 1
    assert not old.exists() and new.exists()


def test_a_cleared_export_says_so(client) -> None:
    import test_session_api as api

    sid, _ = api.start(client)
    build = client.get(f"/api/sessions/{sid}/builds").json()[-1]
    export = client.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build["id"], "formats": ["step"]}
    ).json()
    for d in (Path(os.environ["DATA_DIR"]) / "exports").rglob(export["id"]):
        import shutil

        shutil.rmtree(d)
    r = client.get(export["files"][0]["url"])
    assert r.status_code == 404
    assert r.json()["detail"].endswith(
        "was cleared after a while to save space. Export the design again."
    )


def test_compressed_inputs_are_never_unpacked(client) -> None:
    """Zip bombs: no route unpacks an archive. A gzipped STEP file isn't a STEP import, a zip
    attachment is a binary Claude can't read, and a gzip request body is not inflated."""
    import gzip

    import test_session_api as api

    sid, _ = api.start(client)
    bomb = gzip.compress(b"0" * 5_000_000)
    r = client.post(f"/api/sessions/{sid}/imports", files={"file": ("x.step.gz", bomb)})
    assert r.status_code == 422 and "not a STEP file" in r.json()["detail"]
    r = client.post(
        f"/api/sessions/{sid}/messages",
        content=bomb,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert r.status_code in (400, 422)  # read as the bytes it is, never inflated


# --- Production start check -----------------------------------------------------------------------


def test_production_refuses_the_unsafe_sandbox_setting(monkeypatch) -> None:
    monkeypatch.setenv("GEOMETRY_SANDBOX_KERNEL_LIMITS", "optional")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    check_startup()  # local development: fine
    for name, value in (("RENDER", "true"), ("APP_ENV", "production")):
        monkeypatch.setenv(name, value)
        with pytest.raises(UnsafeConfiguration, match="refused in production"):
            check_startup()
        with pytest.raises(UnsafeConfiguration), TestClient(app):
            pass  # the server never starts serving
        monkeypatch.delenv(name)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("GEOMETRY_SANDBOX_KERNEL_LIMITS", "required")
    check_startup()


# --- Plain errors, no secrets ---------------------------------------------------------------------


def test_an_unexpected_error_is_plain(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def boom():
        raise RuntimeError(f"/app/backend/secret path {KEY}")

    monkeypatch.setattr("app.standards_sets.list_sets", lambda *a: boom())
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/standards")
    assert r.status_code == 500
    assert r.json() == {"detail": "Something went wrong on our side. Please try again in a moment."}


def test_an_error_detail_never_shows_a_server_path(client) -> None:
    from fastapi import HTTPException

    from app.main import _http_error

    error = HTTPException(422, f"bad file {os.environ['DATA_DIR']}/builds/x and /app/backend/y")
    body = json.loads(asyncio.run(_http_error(None, error)).body)
    assert os.environ["DATA_DIR"] not in body["detail"] and "/app/backend" not in body["detail"]


def test_no_response_carries_the_api_key(client, monkeypatch) -> None:
    import test_session_api as api

    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    sid, _ = api.start(client)
    build = client.get(f"/api/sessions/{sid}/builds").json()[-1]
    urls = [
        "/api/health",
        "/api/sessions",
        "/api/examples",
        "/api/standards",
        "/api/openapi.json",
        f"/api/sessions/{sid}",
        f"/api/sessions/{sid}/messages",
        f"/api/sessions/{sid}/builds",
        f"/api/sessions/{sid}/parameters",
        f"/api/sessions/{sid}/builds/{build['id']}/checks",
        f"/api/sessions/{sid}/builds/{build['id']}/source",
    ]
    for url in urls:
        r = client.get(url)
        assert KEY not in r.text and "ANTHROPIC_API_KEY=" not in r.text, url
    events = client.get(f"/api/sessions/{sid}/events", params={"follow": "false"}).text
    assert KEY not in events


# --- Health under load ----------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_stays_fast_under_load(tmp_path, monkeypatch) -> None:
    """With live event streams open and a burst of requests in flight, /api/health still answers
    within half a second."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(CONFIG, "services", fixture_services)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    async def load() -> list[float]:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            sids = [(await c.post("/api/sessions", json={})).json()["id"] for _ in range(3)]
            viewers = [httpx.AsyncClient(base_url=base, timeout=30) for _ in sids]
            # Live streams that stay open, each its own browser.
            streams = [
                asyncio.create_task(v.get(f"/api/sessions/{sid}/events"))
                for v, sid in zip(viewers, sids, strict=True)
            ]
            burst = [
                asyncio.create_task(c.get(f"/api/sessions/{sids[i % 3]}/messages"))
                for i in range(100)
            ]
            burst += [asyncio.create_task(c.get("/api/sessions")) for _ in range(50)]
            await asyncio.sleep(0.05)
            times = []
            for _ in range(5):
                began = time.monotonic()
                r = await c.get("/api/health")
                times.append(time.monotonic() - began)
                assert r.status_code == 200 and r.json()["status"] == "ok"
            await asyncio.gather(*burst)
            for s in streams:
                s.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
            for v in viewers:
                await v.aclose()
            return times

    try:
        times = asyncio.run(load())
    finally:
        server.should_exit = True
        thread.join(10)
    # The median, so one scheduling hiccup on a loaded test machine doesn't fail it.
    assert sorted(times)[len(times) // 2] < 0.5, times


def test_only_step_files_and_images_can_be_uploaded(client) -> None:
    """For now an upload is a STEP file to import or an image for reference; anything else is
    refused plainly (415), before it is stored."""
    sid = client.post("/api/sessions", json={}).json()["id"]
    url = f"/api/sessions/{sid}/attachments"
    for name, media_type in [
        ("case.step", "application/octet-stream"),
        ("case.STP", ""),
        ("mug.jpg", "image/jpeg"),
    ]:
        r = client.post(
            url, files={"file": (name, b"x", media_type)} if media_type else {"file": (name, b"x")}
        )
        assert r.status_code == 201, (name, r.text)
    for name, media_type in [
        ("notes.txt", "text/plain"),
        ("board.kicad_pcb", "application/octet-stream"),
        ("part.stl", "model/stl"),
    ]:
        r = client.post(url, files={"file": (name, b"x", media_type)})
        assert r.status_code == 415, (name, r.text)
        assert (
            r.json()["detail"]
            == f"{name} isn't supported yet. Upload a STEP file (.step, .stp) or an image."
        )
