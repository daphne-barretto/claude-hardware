"""standards.yaml loads, validates against the schema, and every standard names its check."""

import copy
from pathlib import Path

import pytest
import yaml

from app.boards import BOARDS_DIR
from app.examples import example_standards
from app.standards import (
    REPO_ROOT,
    StandardsError,
    json_schema,
    load_standards,
    parse_standards,
)

STANDARDS_PATH = REPO_ROOT / "standards.yaml"


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(STANDARDS_PATH.read_text())


def test_repo_standards_load_and_validate() -> None:
    standards = load_standards(STANDARDS_PATH)
    assert standards.standards
    assert len({s.id for s in standards.standards}) == len(standards.standards)


def test_default_path_is_the_repo_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STANDARDS_PATH", raising=False)
    assert load_standards() == load_standards(STANDARDS_PATH)


def test_standards_path_env_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: dict
) -> None:
    raw["standards"] = raw["standards"][:1]
    path = tmp_path / "standards.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("STANDARDS_PATH", str(path))
    assert len(load_standards().standards) == 1


def test_every_repo_standard_names_its_check(raw: dict) -> None:
    for entry in raw["standards"]:
        assert entry.get("check"), f"standard {entry.get('id')!r} names no check"
    standards = load_standards(STANDARDS_PATH)
    assert standards.checks == {s.check for s in standards.standards}


@pytest.mark.parametrize("bad_check", [None, "", "   ", "Not An Id"])
def test_a_standard_without_a_check_fails_to_load(raw: dict, bad_check: object) -> None:
    broken = copy.deepcopy(raw)
    if bad_check is None:
        del broken["standards"][0]["check"]
    else:
        broken["standards"][0]["check"] = bad_check
    with pytest.raises(StandardsError, match="check"):
        parse_standards(broken)


def test_missing_check_fails_when_loading_a_file(tmp_path: Path, raw: dict) -> None:
    del raw["standards"][-1]["check"]
    path = tmp_path / "standards.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(StandardsError, match="check"):
        load_standards(path)


def _first(raw: dict, kind: str) -> dict:
    return next(s for s in raw["standards"] if s["kind"] == kind)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["standards"].append(copy.deepcopy(r["standards"][0])), "duplicate"),
        (lambda r: r["standards"][0].update(unexpected=1), "unexpected"),
        (lambda r: r["standards"][0].update(kind="maybe"), "kind"),
        (lambda r: _first(r, "min").update(value="thick"), "numeric"),
        (lambda r: _first(r, "min").pop("unit"), "unit"),
        (lambda r: _first(r, "spec").update(value=3), "text"),
        (lambda r: _first(r, "tolerance").update(value=-0.1), "positive"),
        (lambda r: r.update(standards=[]), "standards"),
        (lambda r: r.update(version=2), "version"),
    ],
)
def test_schema_rejects_invalid_files(raw: dict, mutate, message: str) -> None:
    mutate(raw)
    with pytest.raises(StandardsError, match=message):
        parse_standards(raw)


def test_unreadable_file_raises_standards_error(tmp_path: Path) -> None:
    with pytest.raises(StandardsError):
        load_standards(tmp_path / "missing.yaml")


def test_json_schema_requires_check() -> None:
    schema = json_schema()
    standard = schema["$defs"]["Standard"]
    assert "check" in standard["required"]


def test_height_limit_derivation_is_documented() -> None:
    """A limit measured from the example's existing design is the example's (its constraints
    file), says how it was derived, and comes from a file of the example."""
    derived = [s for s in example_standards().standards if s.derivation is not None]
    assert derived, "the height limit must document how it was derived"
    for standard in derived:
        assert (REPO_ROOT / standard.derivation.source).parent == BOARDS_DIR


def test_height_limit_matches_the_existing_case() -> None:
    """Recompute the derived height from the example's existing case."""
    standard = next(s for s in example_standards().standards if s.derivation is not None)
    case = REPO_ROOT / standard.derivation.source
    if not case.exists():
        pytest.skip(f"{standard.derivation.source} is not committed yet")
    from build123d import import_step

    height = import_step(str(case)).bounding_box().size.Z
    assert round(height, 1) == pytest.approx(standard.value)


def test_spec_dimensions_must_be_positive_named_sizes(raw: dict) -> None:
    good = copy.deepcopy(raw)
    good["standards"][0]["dimensions"] = {"hole_diameter": 4.0}
    assert parse_standards(good).standards[0].dimensions == {"hole_diameter": 4.0}
    for bad in ({"hole_diameter": 0}, {"Hole Diameter": 4.0}):
        broken = copy.deepcopy(raw)
        broken["standards"][0]["dimensions"] = bad
        with pytest.raises(StandardsError):
            parse_standards(broken)


def test_insert_standard_is_measurable() -> None:
    inserts = next(
        s for s in load_standards(STANDARDS_PATH).standards if s.check == "insert_pockets"
    )
    assert inserts.dimensions
    assert {"hole_diameter", "insert_length", "screw_clearance"} <= set(inserts.dimensions)


def test_every_repo_standard_says_which_designs_it_applies_to(raw: dict) -> None:
    for entry in raw["standards"]:
        assert entry.get("applies_to") in ("any", "board", "object"), entry.get("id")


@pytest.mark.parametrize("bad", [None, "", "everything"])
def test_a_standard_without_applies_to_fails_to_load(raw: dict, bad: object) -> None:
    broken = copy.deepcopy(raw)
    if bad is None:
        del broken["standards"][0]["applies_to"]
    else:
        broken["standards"][0]["applies_to"] = bad
    with pytest.raises(StandardsError, match="applies_to"):
        parse_standards(broken)


def test_json_schema_requires_applies_to() -> None:
    standard = json_schema()["$defs"]["Standard"]
    assert "applies_to" in standard["required"]
