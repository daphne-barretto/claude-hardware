"""Feature tags carry the lines of code that create them.

The geometry backend records, for every feature a build returns, the statement in the generator
code that created it. The scenario comes from `demos/b-parasite/` and `standards.yaml`.
"""

from __future__ import annotations

import ast
import re
import textwrap
from functools import cache

import pytest

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.standards import load_standards
from geometry import DEFAULT_BACKEND, BuildResult, enclosure_parameters, get_backend

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
STANDARDS = load_standards()


@cache
def example_build() -> tuple[str, BuildResult]:
    engine = get_backend(DEFAULT_BACKEND)
    code = engine.example("enclosure")
    params = enclosure_parameters(load_board(BOARDS_DIR / CHANGES.old_file), STANDARDS)
    return code, engine.build(code, params)


def statement(code: str, lines: tuple[int, int]) -> ast.stmt:
    """The statement that spans exactly these lines."""
    found = [
        node
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.stmt) and (node.lineno, node.end_lineno) == tuple(lines)
    ]
    assert found, f"no statement spans lines {lines}"
    return found[-1]


def tag_argument(stmt: ast.stmt) -> ast.expr:
    """The tag passed by a `feature(tag, part, *tools)` call statement."""
    assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call), ast.unparse(stmt)
    call = stmt.value
    assert isinstance(call.func, ast.Name) and call.func.id == "feature", ast.unparse(stmt)
    return call.args[0]


def in_loop(code: str, stmt: ast.stmt) -> bool:
    """The statement is inside a `for` loop's body."""
    return any(
        isinstance(node, ast.For) and node.body[0].lineno <= stmt.lineno <= (node.end_lineno or 0)
        for node in ast.walk(ast.parse(code))
    )


def test_every_feature_of_the_example_build_has_the_lines_of_its_feature_call() -> None:
    code, result = example_build()
    assert result.features
    for feature in result.features:
        assert feature.lines is not None, feature.tag
        tag = tag_argument(statement(code, feature.lines))
        if isinstance(tag, ast.Constant):
            assert tag.value == feature.tag
        elif isinstance(tag, ast.JoinedStr):
            pattern = "".join(
                re.escape(v.value) if isinstance(v, ast.Constant) else ".+" for v in tag.values
            )
            assert re.fullmatch(pattern, feature.tag), (feature.tag, ast.unparse(tag))
        else:  # a tag read from the parameters, so a loop makes several features with it
            assert in_loop(code, statement(code, feature.lines)), feature.tag
    sources = result.feature_sources()
    assert [s["tag"] for s in sources] == [f.tag for f in result.features]
    assert {s["tag"] for s in sources} == set(result.tags)


def test_features_made_by_one_loop_share_its_statement() -> None:
    code, result = example_build()
    by_lines: dict[tuple[int, int], list[str]] = {}
    for f in result.features:
        by_lines.setdefault(f.lines, []).append(f.tag)
    shared = {lines: tags for lines, tags in by_lines.items() if len(tags) > 1}
    assert shared, "the example makes several features in a loop"
    for lines in shared:
        assert in_loop(code, statement(code, lines))


def build_with(body: str) -> BuildResult:
    """Run generator code (build123d) that defines `features` and `feature`-making code."""
    code = textwrap.dedent(body)
    return get_backend("build123d").build(code, {})


def lines_of(result: BuildResult) -> dict[str, tuple[int, int] | None]:
    return {f.tag: f.lines for f in result.features}


def test_lines_of_a_dict_appended_in_build() -> None:
    result = build_with(
        """\
        from build123d import Box


        def build(params):
            box = Box(1, 1, 1)
            features = []
            features.append({"tag": "box", "part": "p", "tools": [box]})
            for n in range(2):
                features.append(
                    dict(tag=f"cut_{n}", part="p", tools=[box])
                )
            return {"parts": {"p": box}, "features": features}
        """
    )
    assert lines_of(result) == {"box": (7, 7), "cut_0": (9, 11), "cut_1": (9, 11)}


def test_lines_of_a_call_through_helpers_that_pass_the_tag_on() -> None:
    result = build_with(
        """\
        from build123d import Box

        features = []


        def feature(tag, part, *tools):
            features.append({"tag": tag, "part": part, "tools": list(tools)})


        def cut(name, tool):
            feature(name, "p", tool)


        add = lambda tag, tool: feature(tag, "p", tool)


        def shell(tool):
            features.append({"tag": "shell", "part": "p", "tools": [tool]})


        def build(params):
            box = Box(1, 1, 1)
            shell(box)
            feature("direct", "p", box)
            cut(
                "nested",
                box,
            )
            add("lambda", box)
            return {"parts": {"p": box}, "features": features}
        """
    )
    assert lines_of(result) == {
        "shell": (18, 18),  # the helper makes its own tag: the statement in the helper
        "direct": (24, 24),
        "nested": (25, 28),
        "lambda": (29, 29),
    }


def test_lines_follow_the_code_not_a_file() -> None:
    """Lines count from the first line of the code as given, so they index `BuildSource.code`."""
    code, result = example_build()
    shifted = "# a comment\n\n" + code
    engine = get_backend(DEFAULT_BACKEND)
    params = enclosure_parameters(load_board(BOARDS_DIR / CHANGES.old_file), STANDARDS)
    moved = engine.build(shifted, params)
    assert [f.lines for f in moved.features] == [
        (f.lines[0] + 2, f.lines[1] + 2) for f in result.features
    ]


def test_a_feature_without_recorded_lines_cannot_be_served() -> None:
    from geometry import BuildError

    result = build_with(
        """\
        from build123d import Box


        def build(params):
            box = Box(1, 1, 1)
            made = {k: v for k, v in [("tag", "box"), ("part", "p"), ("tools", [box])]}
            return {"parts": {"p": box}, "features": [made]}
        """
    )
    assert result.features[0].lines is None
    with pytest.raises(BuildError, match="box"):
        result.feature_sources()
