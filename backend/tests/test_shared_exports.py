"""The real session API and the mock API share their export code through the public
`app.exports` module instead of the real API importing the mock's private helpers."""

from __future__ import annotations

import ast
import inspect

from app import exports, mock_api, session_api


def imports_from(module, source_module: str) -> list[str]:
    """Every name `module` imports from `source_module`, at any depth (function-level too)."""
    tree = ast.parse(inspect.getsource(module))
    return [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == source_module
        for alias in node.names
    ]


def test_session_api_imports_no_private_name_from_the_mock_api() -> None:
    private = [name for name in imports_from(session_api, "app.mock_api") if name.startswith("_")]
    assert private == []


def test_both_apis_use_the_shared_export_helpers() -> None:
    for module in (mock_api, session_api):
        assert module.write_exports is exports.write_exports, module.__name__
        assert module.fixture_face_ids is exports.fixture_face_ids, module.__name__


def test_neither_api_calls_the_exporters_itself() -> None:
    """Only `app.exports` writes export files, so the two APIs can't drift apart."""
    for module in (mock_api, session_api):
        assert "export_build" not in imports_from(module, "geometry.export"), module.__name__
