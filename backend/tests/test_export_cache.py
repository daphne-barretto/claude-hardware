"""Exports asked for again are copied from the cache instead of starting a geometry worker, which
takes seconds to load the CAD kernel, and the mock writes the export menu's default
request when it starts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import app.exports as exports
import geometry.worker as worker
from app.contracts.common import FIXTURES_DIR


def _report() -> dict:
    return json.loads((Path(FIXTURES_DIR) / "checks.json").read_text())


@pytest.fixture
def counted(monkeypatch, tmp_path):
    """Counts the geometry workers started; the cache lives in `tmp_path`."""
    monkeypatch.setattr(exports, "_EXPORT_CACHE", tmp_path / "cache")
    calls: list[str] = []
    real = worker.call

    def call(target, *args, **kwargs):
        calls.append(target)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(worker, "call", call)
    return calls


def test_a_repeated_export_is_copied_from_the_cache(counted, tmp_path):
    report = _report()
    first = exports.write_exports(tmp_path / "a", report, ["step", "build_note"])
    second = exports.write_exports(tmp_path / "b", report, ["step", "build_note"])
    assert len(counted) == 1
    assert [(f.format, f.name, f.media_type) for f in first] == [
        (f.format, f.name, f.media_type) for f in second
    ]
    for a, b in zip(first, second, strict=True):
        assert b.path.parent == tmp_path / "b"
        assert a.path.read_bytes() == b.path.read_bytes()
    assert sorted(p.name for p in (tmp_path / "b").iterdir()) == sorted(f.name for f in second)


def test_a_different_request_starts_a_worker(counted, tmp_path):
    report = _report()
    exports.write_exports(tmp_path / "a", report, ["build_note"])
    exports.write_exports(tmp_path / "b", {**report, "build_id": "other"}, ["build_note"])
    exports.write_exports(tmp_path / "c", report, ["source"])
    assert len(counted) == 3


def test_the_mock_prewarms_the_default_export(counted, tmp_path):
    from app import mock_api

    mock_api._prewarm_exports()
    report = {
        **_report(),
        "build_id": json.loads((Path(FIXTURES_DIR) / "build.json").read_text())["id"],
    }
    history = mock_api.default_export_history()
    exports.write_exports(tmp_path / "a", report, mock_api.DEFAULT_EXPORT_FORMATS, None, history)
    assert len(counted) == 1


def test_the_default_export_matches_the_export_menu():
    from app import mock_api

    menu = (
        Path(__file__).parents[2] / "frontend/src/components/canvas/exportFormats.ts"
    ).read_text()
    found = re.search(r"DEFAULT_FORMATS: FileFormat\[\] = (\[[^\]]*\])", menu)
    assert found, "DEFAULT_FORMATS is not in exportFormats.ts"
    # The build note is included by default and comes last.
    assert mock_api.DEFAULT_EXPORT_FORMATS == [*json.loads(found.group(1)), "build_note"]
