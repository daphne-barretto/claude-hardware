"""The chat line that lists a finished design's failing checks names them as people do (QA sweep:
`frontend/e2e/qa/`, board-less design). It showed the internal check IDs, such as `draft_angle`."""

from app.agent.services import CheckItem
from app.agent.tools import failing_line
from app.standards import load_standards


def test_failing_checks_are_named_by_their_standard_title_not_their_id():
    standards = load_standards()
    standard = next(s for s in standards.standards if s.unit == "deg")
    item = CheckItem(
        id=standard.check,
        check=standard.check,
        status="fail",
        standard=standard.id,
        measured=0.0,
        required=standard.value,
        unit=standard.unit,
    )
    line = failing_line([item], standards)
    assert standard.title in line
    assert standard.check not in line and standard.id not in line
    assert f"measured 0.0°, required {standard.value}°" in line


def test_a_check_without_a_standard_reads_without_underscores():
    item = CheckItem(id="no_interference", check="no_interference", status="fail")
    line = failing_line([item], None)
    assert "No interference" in line
    assert "no_interference" not in line


def test_a_component_check_names_its_component():
    standards = load_standards()
    standard = next(s for s in standards.standards if s.unit == "mm")
    item = CheckItem(
        id=f"{standard.check}:X",
        check=standard.check,
        status="fail",
        standard=standard.id,
        component="X",
    )
    line = failing_line([item], standards)
    assert f"{standard.title} · X" in line
