"""Every check result says what it requires in words a person reads: the name of the check and a
value with its unit, never a raw expression ("> 0 after 0.201 mm") or a placeholder ("none").

The checks without a standard (the board and assembly checks) declare their requirement in
`geometry.report.BOARD_REQUIRED`; the standards' own come from `standards.yaml`. The scenario
comes from `demos/b-parasite/` and `standards.yaml`; nothing here names a board, part or dimension.
"""

from __future__ import annotations

import re
from functools import cache

import pytest

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.standards import load_standards
from geometry import DEFAULT_BACKEND, enclosure_parameters
from geometry import generate_enclosure as generate
from geometry.checks import ASSEMBLY_CHECKS, BOARD_CHECKS, CheckResult
from geometry.report import BOARD_REQUIRED, board_required, check_report

STANDARDS = load_standards()
CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
# A value the person reads as "no value": the text of Python's None, JSON's null, an empty string.
PLACEHOLDER = re.compile(r"^\s*(none|null|nil|undefined|n/?a|)\s*$", re.IGNORECASE)
# A raw comparison rather than words: "> 0 after 0.201 mm", "<= 2".
EXPRESSION = re.compile(r"^\s*[<>=!≤≥]")


def human(value: object) -> bool:
    return not (isinstance(value, str) and (PLACEHOLDER.match(value) or EXPRESSION.match(value)))


@cache
def report() -> dict:
    """The checks of the older revision's design, on that board."""
    board = load_board(BOARDS_DIR / CHANGES.old_file)
    params = enclosure_parameters(board, STANDARDS)
    result = generate(board, STANDARDS, params, DEFAULT_BACKEND)
    return check_report(board, STANDARDS, result, "build-test")


@pytest.mark.parametrize("check", [*BOARD_CHECKS, *ASSEMBLY_CHECKS])
def test_every_check_without_a_standard_declares_a_human_requirement(check: str) -> None:
    assert check in BOARD_REQUIRED, f"{check} has no requirement in BOARD_REQUIRED"
    required = board_required(CheckResult(check, "pass", limit=0.2))
    assert human(required["value"]), required
    if required["kind"] != "spec":
        assert required.get("unit"), f"{check}: a {required['kind']} limit needs a unit"


def test_a_check_with_no_declared_requirement_is_refused_rather_than_shown_raw() -> None:
    with pytest.raises(KeyError):
        board_required(CheckResult("not_a_check", "pass", limit=None))


def test_every_result_of_a_real_build_states_its_requirement_and_measurement_in_words() -> None:
    results = [r for r in report()["results"] if r["status"] in ("pass", "fail")]
    checks = {r["check"] for r in results}
    assert set(BOARD_CHECKS) & checks, "the build ran no board check"
    for r in results:
        req, measured = r["required"], r["measured"]
        assert human(req["value"]), (r["id"], req)
        assert human(measured["value"]), (r["id"], measured)
        assert "None" not in r["summary"] and not re.search(r"against [<>=]", r["summary"]), r
