"""File abuse: attachments and STEP imports (`docs/security.md`).

Every bad file gets a clear status and message; nothing a filename says reaches the disk as a
path; the server stays up.
"""

from __future__ import annotations

import io
import time

import pytest
from conftest import Browser

from app.attachments import MAX_ATTACHMENT_BYTES
from app.imports import MAX_IMPORT_BYTES


def upload(browser: Browser, sid: str, name: str, data: bytes, media_type: str = ""):
    files = {"file": (name, io.BytesIO(data), media_type or "application/octet-stream")}
    return browser.post(f"/api/sessions/{sid}/attachments", files=files)


def import_step(browser: Browser, sid: str, name: str, data: bytes):
    files = {"file": (name, io.BytesIO(data), "application/octet-stream")}
    return browser.post(f"/api/sessions/{sid}/imports", files=files)


# --- Attachments ------------------------------------------------------------------------------


def test_a_zero_byte_attachment_is_kept_and_served_empty(browser: Browser) -> None:
    sid = browser.new_session()
    r = upload(browser, sid, "empty.step", b"")
    assert r.status_code == 201, r.text
    got = browser.get(r.json()["url"])
    assert got.status_code == 200 and got.content == b""


def test_a_100_mb_attachment_is_refused_with_the_limit(browser: Browser) -> None:
    sid = browser.new_session()
    t = time.monotonic()
    r = upload(browser, sid, "huge.bin", b"\0" * (100 * 1024 * 1024))
    assert r.status_code == 413, r.status_code
    detail = r.json()["detail"]  # the limit, in bytes or in MB (`app.security.web`)
    assert str(MAX_ATTACHMENT_BYTES) in detail or f"{MAX_ATTACHMENT_BYTES // 1024**2} MB" in detail
    assert time.monotonic() - t < 30
    assert browser.get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd", "a/../../b.txt", "%2e%2e%2fx"],
)
def test_path_traversal_names_never_touch_the_disk_as_paths(
    browser: Browser, name: str, tmp_path
) -> None:
    sid = browser.new_session()
    # An image by its media type, so any name is accepted (`app.attachments.supported_file`).
    r = upload(browser, sid, name, b"secret", "image/png")
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    assert url.rsplit("/", 1)[-1].startswith("attachment-")  # stored by ID, not by name
    got = browser.get(url)
    assert got.content == b"secret"
    saved_as = got.headers["content-disposition"].split("''", 1)[-1]
    assert not saved_as.startswith(".") and "%2F" not in saved_as and "%5C" not in saved_as
    # Nothing was written outside the data directory.
    assert not (tmp_path / "etc").exists()


def test_duplicate_attachment_names_keep_both_files(browser: Browser) -> None:
    sid = browser.new_session()
    a = upload(browser, sid, "notes.step", b"first").json()
    b = upload(browser, sid, "notes.step", b"second").json()
    assert a["url"] != b["url"]
    assert browser.get(a["url"]).content == b"first"
    assert browser.get(b["url"]).content == b"second"


def test_an_image_that_is_not_an_image_reaches_claude_as_a_clear_note(
    browser: Browser, claude
) -> None:
    """A .png that is text: the run isn't broken by it; Claude is told it isn't readable."""
    from app.agent.photo import Prompt

    sid = browser.new_session()
    att = upload(browser, sid, "photo.png", b"definitely not a png", "image/png").json()
    claude.scripts.append(
        [
            ("read_attachment", {"name": "photo.png"}),
            ("text", "Read it."),
            ("finish", {"outcome": "blocked", "summary": "Read it."}),
        ]
    )
    r = browser.send(sid, "make a stand for this", attachments=[att])
    assert r.status_code == 202, r.text
    assert browser.settle(sid) == "idle"
    call = claude.drivers[0].calls[0]
    assert call.is_error and "not a readable image" in call.text
    prompt = Prompt("x", [("photo.png", "image/png", b"definitely not a png")])
    assert "not a readable image" in prompt.content()[0]["text"]


def test_an_attachment_on_someone_elses_url_is_not_read(browser: Browser, other: Browser) -> None:
    mine = browser.new_session()
    att = upload(browser, mine, "secret.step", b"secret").json()
    theirs = other.new_session()
    # The other browser sends my attachment's URL with its own message.
    assert other.get(att["url"]).status_code == 404
    from app.contracts.session import Attachment, MessageCreate
    from app.session_api import SESSIONS, attached_files

    body = MessageCreate(text="read it", attachments=[Attachment(**att)])
    assert attached_files(SESSIONS.by_id[theirs], body) == []


# --- STEP imports -----------------------------------------------------------------------------


def test_a_zero_byte_step_is_refused_with_a_clear_message(browser: Browser) -> None:
    sid = browser.new_session()
    r = import_step(browser, sid, "empty.step", b"")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]


def test_a_100_mb_step_is_refused_with_the_limit(browser: Browser) -> None:
    sid = browser.new_session()
    r = import_step(browser, sid, "big.step", b"ISO-10303-21;" + b" " * (100 * 1024 * 1024))
    assert r.status_code == 413, r.status_code
    assert f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB" in r.json()["detail"]


@pytest.mark.parametrize("name", ["model.stl", "model.step.exe", "model", "model.STEP.txt"])
def test_a_wrong_extension_is_refused(browser: Browser, name: str) -> None:
    sid = browser.new_session()
    r = import_step(browser, sid, name, b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    assert r.status_code == 422, r.text
    assert "STEP" in r.json()["detail"] or ".step" in r.json()["detail"]


def test_a_corrupt_step_is_refused_and_the_session_stays_usable(browser: Browser) -> None:
    sid = browser.new_session()
    data = b"ISO-10303-21;\nHEADER;\nthis is not step\x00\xff\xfe" * 100
    r = import_step(browser, sid, "corrupt.step", data)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]
    assert browser.status(sid) == "idle"
    assert browser.send(sid, "make a box 10 x 10 x 10 mm").status_code == 202
    browser.settle(sid)


def test_a_path_traversal_step_name_imports_under_its_own_name(browser: Browser, tmp_path) -> None:
    """The import worker gets the bytes, never a path from the name."""
    from build123d import Box, export_step

    path = tmp_path / "box.step"
    export_step(Box(10, 10, 10), str(path))
    sid = browser.new_session()
    r = import_step(browser, sid, "../../../../tmp/evil.step", path.read_bytes())
    assert r.status_code == 201, r.text
    assert not (tmp_path.parent / "evil.step").exists()


def test_a_step_with_a_thousand_bodies_imports_or_fails_clearly(browser: Browser, tmp_path) -> None:
    from build123d import Box, Compound, Pos, export_step

    boxes = [Pos(i % 40 * 3, i // 40 * 3, 0) * Box(1, 1, 1) for i in range(1000)]
    path = tmp_path / "many.step"
    export_step(Compound(boxes), str(path))
    sid = browser.new_session()
    t = time.monotonic()
    r = import_step(browser, sid, "many.step", path.read_bytes())
    took = time.monotonic() - t
    assert r.status_code in (201, 422), r.text
    if r.status_code == 422:
        assert r.json()["detail"]
    assert took < 300, took
    assert browser.get("/api/health").status_code == 200


def test_a_step_imports_into_a_new_empty_session(browser: Browser, claude, tmp_path) -> None:
    """Regression: importing into a session without a board was a 500 (no board to check
    against). It imports, checks without a board, reaches Claude, exports and restores."""
    from build123d import Box, export_step

    path = tmp_path / "plate.step"
    export_step(Box(30, 20, 3), str(path))
    sid = browser.new_session()
    r = import_step(browser, sid, "plate.step", path.read_bytes())
    assert r.status_code == 201, r.text
    build = r.json()
    assert build["board"] is None
    assert browser.send(sid, "what did I import?").status_code == 202
    browser.settle(sid)
    assert "checked against no board" in claude.prompts[-1]
    r = browser.post(
        f"/api/sessions/{sid}/exports", json={"build_id": build["id"], "formats": ["step", "stl"]}
    )
    assert r.status_code == 201, r.text
    r = browser.post(f"/api/sessions/{sid}/builds/{build['id']}/restore")
    assert r.status_code == 201, r.text
