"""Feature source lines for code Claude wrote.

A build of generator code runs in the sandbox, which now records where each feature is created
the same way an in-process build does (`geometry.sites`), and returns the lines with the shapes.
So the source of every stored build lists its features, not only the backends' own generators.

Claude's code is played by the backend's enclosure generator, rewritten so it is none of the
backend examples and its lines move: a header, a helper that makes features, and a loop. The
scenario comes from `demos/b-parasite/` and `standards.yaml`.
"""

from __future__ import annotations

from functools import cache

import pytest

from app.agent.services import GeometryServices, _layout
from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.builds import BuildStore
from app.contracts.session import BuildSource
from app.session_api import stored_source
from app.standards import load_standards
from geometry import DEFAULT_BACKEND, enclosure_parameters, get_backend
from geometry.sandbox import run_build

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()

HEADER = '''"""An enclosure, rewritten."""

# Features made here and in a helper move every line of the generator below.


def marker(tag, part, tool):
    return {"tag": tag, "part": part, "tools": [tool]}
'''

EXTRA = """

def _with_markers(out, params):
    from build123d import Box, Pos

    for i in range(2):
        out["features"].append(
            marker(f"marker_{i}", "lid", Pos(i * 2.0, 0, 0) * Box(0.5, 0.5, 0.5))
        )
    out["features"].append(
        {
            "tag": "marker_end",
            "part": "lid",
            "tools": [Box(0.5, 0.5, 0.5)],
        }
    )
    return out


_build = build


def build(params):
    return _with_markers(_build(params), params)
"""


def board():
    return load_board(BOARDS_DIR / CHANGES.new_file)


@cache
def claudes_code() -> str:
    """Generator code that is none of the backend's examples."""
    engine = get_backend(DEFAULT_BACKEND)
    code = HEADER + engine.example("enclosure") + EXTRA
    assert code not in engine.examples().values()
    return code


def params() -> dict:
    return enclosure_parameters(board(), STANDARDS)


def test_a_build_of_claudes_code_serves_every_features_lines(tmp_path) -> None:
    """Built in the sandbox and stored, its source lists one entry per feature with the lines
    `GeometryBackend.build` records running the same code in process."""
    code = claudes_code()
    services = GeometryServices(board(), STANDARDS, store=BuildStore(tmp_path / "builds"))
    outcome = services.build({"code": code, "params": params(), "backend": DEFAULT_BACKEND})
    assert outcome.ok, outcome.error
    record = services.store.get(outcome.build.id)
    source = BuildSource.model_validate(stored_source(record))

    expected = get_backend(DEFAULT_BACKEND).build(code, params()).feature_sources()
    assert [f.model_dump(exclude={"locked"}) for f in source.features] == [
        {**f, "lines": tuple(f["lines"])} for f in expected
    ]
    assert source.code == code and source.filename == "design.py"
    # Features made in the helper are listed at the call that passed their tag, as in process.
    tags = [f.tag for f in source.features]
    assert {"marker_0", "marker_1", "marker_end"} <= set(tags)
    assert len(tags) == len(expected) > 3


def test_the_sandbox_records_lines_only_when_asked() -> None:
    from app.agent.services import SANDBOX_EPILOGUE, unpack_shapes

    code = claudes_code() + SANDBOX_EPILOGUE
    plain = run_build(code, params(), backend=DEFAULT_BACKEND)
    assert plain.ok, plain.error
    assert all(f.lines is None for f in unpack_shapes(plain.shapes, DEFAULT_BACKEND).features)
    recorded = run_build(code, params(), backend=DEFAULT_BACKEND, feature_sites=True)
    assert recorded.ok, recorded.error
    features = unpack_shapes(recorded.shapes, DEFAULT_BACKEND).features
    assert features and all(f.lines is not None for f in features)


@pytest.mark.parametrize(
    ("code", "kind", "line"),
    [
        ("x = 1\ny = (\n", "syntax", 2),
        ("x = 1\n\n__import__('os')\n", "blocked_import", 3),
        ("import math\nf = {'tag': 'a'}\nraise ValueError('boom')\n", "runtime", 3),
        ("f = {'tag': 'a'}\n\nopen('/tmp/escape', 'w')\n", "forbidden", 3),
    ],
)
def test_instrumented_builds_are_stopped_as_before_on_the_same_line(code, kind, line) -> None:
    """Recording the lines changes neither what the sandbox refuses nor the line it reports."""
    for feature_sites in (False, True):
        result = run_build(code, feature_sites=feature_sites)
        assert result.error is not None, feature_sites
        assert (result.error.kind, result.error.line) == (kind, line), feature_sites


@pytest.mark.parametrize("lines", ["x-2", "3-1", "0-0", "1"])
def test_forged_feature_lines_are_refused(lines) -> None:
    """The lines come back from the build in the shape names, so they are checked."""
    names = {"part|lid|enclosure|0.0,0.0,0.0|": b"", f"tool|0|0|{lines}|lid|a": b""}
    with pytest.raises(ValueError):
        _layout(names)


def test_shape_names_without_lines_still_unpack() -> None:
    _, _, _, _, tools = _layout({"part|lid|enclosure|0.0,0.0,0.0|": b"", "tool|0|0||lid|a|b": b""})
    assert tools == {0: ("lid", "a|b", None, [(0, b"")])}


def test_a_build_that_records_lines_loads_only_sites_besides_the_child() -> None:
    """Like `test_sandbox.py`'s check on what the child loads before the kernel limits: the
    only addition is `geometry.sites`, and only for a build that records feature lines."""
    code = (
        "_mods = __builtins__.__dict__['__import__']('sys').modules\n"
        "print(sorted(m for m in _mods if m == 'app' or m.startswith(('app.', 'geometry'))))\n"
        "from build123d import Box\n"
        "result = Box(1, 1, 1)\n"
    )
    for feature_sites, extra in ((False, []), (True, ["geometry.sites"])):
        result = run_build(code, feature_sites=feature_sites, extra_imports={"sys"})
        assert result.ok, result.error
        expected = sorted(
            ["geometry", "geometry._kernel_sandbox", "geometry._sandbox_child", *extra]
        )
        assert result.output.strip() == str(expected)
