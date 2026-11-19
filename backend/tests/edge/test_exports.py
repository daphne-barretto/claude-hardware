"""Exports on every kind of design (`docs/security.md`): every format on a design with a board
(the example, on the contract fixtures) and one without (built for real), plus empty and
unknown part selections, unknown builds and formats."""

from __future__ import annotations

import pytest
from conftest import Browser, box_session

from app.contracts.session import ExportFormat

FORMATS = list(ExportFormat.__args__)


@pytest.fixture
def boardless(browser: Browser) -> tuple[str, dict]:
    return box_session(browser)


def _export(browser: Browser, sid: str, **body):
    return browser.post(f"/api/sessions/{sid}/exports", json=body)


def test_every_format_exports_a_design_without_a_board(browser: Browser, boardless) -> None:
    sid, build = boardless
    r = _export(browser, sid, build_id=build["id"], formats=FORMATS)
    assert r.status_code == 201, r.text
    files = r.json()["files"]
    assert {f["format"] for f in files} == set(FORMATS)
    for f in files:
        got = browser.get(f["url"])
        assert got.status_code == 200, f
        assert got.content, f
        assert got.headers["content-type"].startswith(f["media_type"].split(";")[0])


def test_every_format_exports_a_design_with_a_board(browser: Browser) -> None:
    sid = browser.example_session()
    build = browser.builds(sid)[0]
    r = _export(browser, sid, build_id=build["id"], formats=FORMATS)
    assert r.status_code == 201, r.text
    for f in r.json()["files"]:
        assert browser.get(f["url"]).status_code == 200, f


def test_one_part_of_a_design_without_a_board(browser: Browser, boardless) -> None:
    sid, build = boardless
    r = _export(browser, sid, build_id=build["id"], formats=["stl", "step"], parts=["box"])
    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    "body, status",
    [
        ({"formats": ["stl"], "parts": []}, 422),  # empty selection
        ({"formats": ["stl"], "parts": ["no_such_part"]}, 422),
        ({"formats": ["stl"], "parts": ["board"]}, 422),  # no board in this design
        ({"formats": ["stl"], "parts": ["../../etc/passwd"]}, 422),
        ({"formats": []}, 422),
        ({"formats": ["exe"]}, 422),
        ({"formats": ["stl"], "units": "furlong"}, 422),
    ],
)
def test_bad_export_requests_are_refused_with_a_reason(
    browser: Browser, boardless, body: dict, status: int
) -> None:
    sid, build = boardless
    r = _export(browser, sid, build_id=build["id"], **body)
    assert r.status_code == status, (r.status_code, r.text)
    assert r.json()["detail"]


def test_an_export_of_a_build_that_does_not_exist_is_404(browser: Browser) -> None:
    sid = browser.example_session()
    r = _export(browser, sid, build_id="build-nope", formats=["stl"])
    assert r.status_code == 404
    r = browser.get(f"/api/sessions/{sid}/exports/export-99/files/x.stl")
    assert r.status_code == 404
    r = browser.get(f"/api/sessions/{sid}/exports/export-1/files/..%2F..%2Fsessions.db")
    assert r.status_code == 404


def test_a_session_with_no_build_has_nothing_to_export(browser: Browser) -> None:
    sid = browser.new_session()
    r = _export(browser, sid, build_id="build-1", formats=["stl"])
    assert r.status_code == 404
    assert r.json()["detail"]
