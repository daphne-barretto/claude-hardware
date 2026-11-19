"""Find in model: queries in words resolve to the right faces.

Expected faces are worked out here from the fixtures (`fixtures/`, generated from
`demos/b-parasite/` and `standards.yaml`) and from a real build of the enclosure generator, never
typed in, so the tests follow the scenario files.
"""

from __future__ import annotations

import json
import re
from functools import cache

import pytest
from fastapi.testclient import TestClient

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.contracts.common import FIXTURES_DIR
from app.contracts.maps import AnalysisMap
from app.contracts.mesh import MeshInfo, validate_mesh
from app.contracts.session import FindResult, Parameter
from app.examples import examples
from app.find import HOLE_KINDS, find_in_model
from app.standards import load_standards

# The board revision example: these tests walk its board and existing design.
EXAMPLE = examples()[0].id

STANDARDS = load_standards()


@cache
def fixture_mesh() -> MeshInfo:
    return validate_mesh(FIXTURES_DIR / "enclosure.gltf")


@cache
def fixture_maps() -> dict[str, AnalysisMap]:
    build = json.loads((FIXTURES_DIR / "build.json").read_text())
    return {
        t: AnalysisMap.model_validate_json((FIXTURES_DIR / "maps" / f"{t}.json").read_text())
        for t in build["maps"]
    }


def fixture_parameters() -> list[Parameter]:
    return [
        Parameter.model_validate(p)
        for p in json.loads((FIXTURES_DIR / "parameters.json").read_text())
    ]


def find(query: str) -> FindResult:
    return find_in_model(query, fixture_mesh(), fixture_parameters(), fixture_maps(), STANDARDS)


def faces_of(result: FindResult) -> set[str]:
    return {m.face for m in result.matches}


def all_faces(mesh: MeshInfo) -> list[tuple[str, str, str]]:
    """(part, face, feature) of every face."""
    return [(p.name, f.id, f.feature) for p in mesh.parts.values() for f in p.faces]


def face_extremes(map_type: str) -> dict[str, tuple[float, float]]:
    """Each face's lowest and highest value on a fixture map, from its vertices."""
    mesh, amap = fixture_mesh(), fixture_maps()[map_type]
    out: dict[str, list[float]] = {}
    for pv in amap.parts:
        part = mesh.parts[pv.part]
        for vertex, value in enumerate(pv.values):
            if value is not None:
                out.setdefault(part.faces[part.face_index[vertex]].id, []).append(value)
    return {face: (min(v), max(v)) for face, v in out.items()}


def plural(word: str) -> str:
    return word + ("es" if word.endswith("s") else "s")


# --- Feature type ---------------------------------------------------------------------------


def feature_kinds() -> dict[str, set[str]]:
    """Kind (the feature tag before its number or component) -> its faces, for features that
    aren't a whole part."""
    mesh = fixture_mesh()
    kinds: dict[str, set[str]] = {}
    for _part, face, feature in all_faces(mesh):
        if feature in mesh.parts:
            continue
        kinds.setdefault(feature.split("_", 1)[0].lower(), set()).add(face)
    return kinds


def test_the_fixture_has_feature_kinds_to_find() -> None:
    assert len(feature_kinds()) >= 2


@pytest.mark.parametrize("kind", sorted(feature_kinds()))
def test_a_feature_type_finds_its_faces(kind: str) -> None:
    for query in (plural(kind), f"all {plural(kind)}", kind):
        result = find(query)
        assert result.status == "found", (query, result)
        assert faces_of(result) == feature_kinds()[kind], query
        assert kind in result.understood.lower()


def test_a_component_reference_finds_the_features_named_after_it() -> None:
    mesh = fixture_mesh()
    named = {f for _, _, f in all_faces(mesh) if "_" in f and not f.split("_", 1)[1].isdigit()}
    assert named
    for feature in named:
        ref = feature.split("_", 1)[1]
        result = find(ref.lower())
        assert faces_of(result) == {face for _, face, f in all_faces(mesh) if f == feature}


def test_a_feature_type_on_a_part() -> None:
    mesh = fixture_mesh()
    for kind, faces in feature_kinds().items():
        for part in mesh.parts:
            result = find(f"{plural(kind)} on the {part}")
            expected = {f for f in faces if mesh.face_parts[f] == part}
            assert faces_of(result) == expected
            assert result.status == ("found" if expected else "none")


# --- Size -----------------------------------------------------------------------------------


def sized_features() -> dict[str, float]:
    """Feature -> diameter, from the parameters that drive a feature's radius."""
    return {
        p.feature: 2 * float(p.value)
        for p in fixture_parameters()
        if p.feature and p.name.endswith("radius")
    }


def test_a_size_finds_the_features_that_size_drives() -> None:
    sizes = sized_features()
    assert sizes, "the fixture parameters size no feature"
    kind = next(iter(sizes)).split("_", 1)[0]
    mesh = fixture_mesh()
    smallest = min(sizes.values())
    for limit, op in (
        (smallest - 0.1, "over"),
        (smallest + 0.1, "under"),
        (smallest - 0.1, "under"),
    ):
        result = find(f"{plural(kind)} {op} {limit:.2f} mm")
        expected_features = {
            f for f, d in sizes.items() if (d > limit if op == "over" else d < limit)
        }
        expected = {face for _, face, f in all_faces(mesh) if f in expected_features}
        assert faces_of(result) == expected, (op, limit)
        assert "diameter" in result.understood
        for m in result.matches:
            assert m.value == pytest.approx(sizes[m.feature]) and m.unit == "mm"
    radius = find(f"{plural(kind)} with radius over {smallest / 2 - 0.1:.2f} mm")
    assert faces_of(radius) == {face for _, face, f in all_faces(mesh) if f in sizes}


def test_a_fastener_size_finds_its_holes_in_a_real_build() -> None:
    changes = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())
    board = load_board(BOARDS_DIR / changes.old_file)
    from geometry import DEFAULT_BACKEND, enclosure_parameters, generate_enclosure
    from geometry.tessellate import tessellate

    result = generate_enclosure(
        board, STANDARDS, enclosure_parameters(board, STANDARDS), DEFAULT_BACKEND
    )
    mesh = validate_mesh(tessellate(result, source={}).to_gltf())
    sizes = {
        m
        for s in STANDARDS.standards
        if s.kind == "spec"
        for m in re.findall(r"\bM\d+", str(s.value))
    }
    assert sizes, "no standard names a fastener size"
    holes = {face for _, face, f in all_faces(mesh) if f.split("_", 1)[0] in HOLE_KINDS}
    assert holes
    for size in sizes:
        found = find_in_model(f"all {size} holes", mesh, [], {}, STANDARDS)
        assert found.status == "found"
        assert faces_of(found) == holes
        assert size in found.understood


def test_a_fastener_size_the_standards_dont_use() -> None:
    sizes = {int(m) for s in STANDARDS.standards for m in re.findall(r"\bM(\d+)", str(s.value))}
    other = f"M{max(sizes) + 1}"
    result = find(f"{other} holes")
    assert result.status == "none" and not result.matches
    assert other in result.understood


def test_a_fastener_size_without_holes_in_the_build_says_so() -> None:
    (size,) = {m for s in STANDARDS.standards for m in re.findall(r"\bM\d+", str(s.value))}
    kinds = {f.split("_", 1)[0] for _, _, f in all_faces(fixture_mesh())}
    if kinds & set(HOLE_KINDS):
        pytest.skip("the fixture has holes")
    result = find(f"all {size} holes")
    assert result.status == "none"
    assert "This build has no" in result.understood


# --- Analysis values --------------------------------------------------------------------------


def test_faces_under_a_draft_angle() -> None:
    extremes = face_extremes("draft")
    # Just over the fixture's lowest draft, so there are faces under it.
    cut = round(min(lo for lo, _ in extremes.values()) + 0.05, 2)
    expected = {f for f, (lo, _) in extremes.items() if lo < cut}
    assert expected
    for query in (
        f"faces under {cut:g} degree of draft",
        f"faces under {cut:g}° of draft",
        f"draft less than {cut:g} deg",
    ):
        result = find(query)
        assert result.status == "found", query
        assert faces_of(result) == expected, query
        for m in result.matches:
            assert m.value == pytest.approx(extremes[m.face][0]) and m.unit == "deg"
    # Without a number, low draft is judged against the standard.
    limit = float(STANDARDS.get("min_draft").value)
    low = find("low draft")
    assert faces_of(low) == {f for f, (lo, _) in extremes.items() if lo < limit}
    draft = STANDARDS.get("min_draft")
    assert draft.title.lower() in low.understood.lower() and draft.id not in low.understood


def test_faces_over_a_draft_angle_and_between_two() -> None:
    extremes = face_extremes("draft")
    values = sorted({hi for _, hi in extremes.values()})
    # Between the two lowest values, or just under the only one (every face drafted alike).
    cut = (values[0] + values[1]) / 2 if len(values) > 1 else values[0] - 0.05
    over = find(f"faces over {cut:.3f} degrees of draft")
    assert faces_of(over) == {f for f, (_, hi) in extremes.items() if hi > cut}
    between = find(f"faces between 0 and {cut:.3f} degrees of draft")
    assert faces_of(between) == {f for f, (lo, _) in extremes.items() if lo <= cut}
    assert "between" in between.understood


def test_walls_thinner_than_a_thickness() -> None:
    extremes = face_extremes("wall_thickness")
    walls = {f for f in extremes if "wall" in f.split(".")[-1]}
    # Just over the thinnest wall face, so some walls are under it.
    cut = min(extremes[f][0] for f in walls) + 0.1
    result = find(f"walls thinner than {cut:.2f} mm")
    assert faces_of(result) == {f for f in walls if extremes[f][0] < cut}
    assert result.matches
    anything = find(f"faces with wall thickness under {cut:.2f} mm")
    assert faces_of(anything) == {f for f, (lo, _) in extremes.items() if lo < cut}


def test_thin_walls_use_the_standard_and_thick_sections_the_flagged_spots() -> None:
    limit = float(STANDARDS.get("min_wall").value)
    extremes = face_extremes("wall_thickness")
    thin = find("thin walls")
    assert faces_of(thin) == {
        f for f, (lo, _) in extremes.items() if lo < limit and "wall" in f.split(".")[-1]
    }
    wall = STANDARDS.get("min_wall")
    assert wall.title.lower() in thin.understood.lower() and wall.id not in thin.understood
    thick = find("thick sections")
    flagged = {
        face
        for s in fixture_maps()["wall_thickness"].flagged
        if s.kind == "thick"
        for face in s.location.faces
    }
    assert flagged and faces_of(thick) == flagged


# --- Unknown queries --------------------------------------------------------------------------


def test_an_unknown_query_says_what_it_understood_and_asks() -> None:
    result = find("purple widgets")
    assert result.status == "unclear" and not result.matches
    assert result.understood
    assert "“purple”" in result.question and "“widgets”" in result.question
    assert result.question.rstrip().endswith(".") or "?" in result.question


def test_a_partly_understood_query_names_the_part_it_understood() -> None:
    kind = sorted(feature_kinds())[0]
    result = find(f"shiny {plural(kind)}")
    assert result.status == "unclear" and not result.matches
    assert kind in result.understood.lower()
    assert "“shiny”" in result.question


def test_a_misspelling_suggests_the_word_it_is_close_to() -> None:
    kind = max(feature_kinds(), key=len)
    typo = kind[:-1] + kind[-1] * 2 + "x"
    result = find(typo)
    assert result.status == "unclear"
    assert f"“{kind}”" in result.question


def test_a_number_without_a_quantity_asks_what_it_measures() -> None:
    result = find("faces under 3 mm")
    assert result.status == "unclear"
    assert "?" in result.question


def test_a_quantity_in_the_wrong_unit_asks() -> None:
    result = find("draft under 1 mm")
    assert result.status == "unclear" and "deg" in result.question


def test_nothing_to_search_for_asks() -> None:
    result = find("all faces")
    assert result.status == "unclear" and result.question


# --- The session API --------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch):
    from app.mock_api import SESSIONS, app

    monkeypatch.setenv("MOCK_API_SPEED", "0")
    SESSIONS.clear()
    with TestClient(app) as client:
        yield client


def test_the_mock_api_finds_in_the_fixture_build(mock_client) -> None:
    sid = mock_client.post("/api/sessions", json={"example": EXAMPLE}).json()["id"]
    build = json.loads((FIXTURES_DIR / "build.json").read_text())
    url = f"/api/sessions/{sid}/builds/{build['id']}/find"
    r = mock_client.post(url, json={"query": "flagged"})
    assert r.status_code == 200, r.text
    assert FindResult.model_validate(r.json()) == find("flagged")
    unclear = mock_client.post(url, json={"query": "purple widgets"}).json()
    assert unclear["status"] == "unclear" and unclear["question"]
    assert mock_client.post(url, json={"query": ""}).status_code == 422
    missing = mock_client.post(
        f"/api/sessions/{sid}/builds/no-such-build/find", json={"query": "x"}
    )
    assert missing.status_code == 404


def test_the_session_api_finds_in_its_build(tmp_path, monkeypatch) -> None:
    from app.main import app
    from app.session_api import SESSIONS

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    SESSIONS.by_id.clear()
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"example": EXAMPLE}).json()["id"]
        for _ in range(6000):  # the first build is real: seconds, more on a busy machine
            builds = client.get(f"/api/sessions/{sid}/builds").json()
            if builds:
                break
            import time

            time.sleep(0.02)
        build = builds[-1]
        # The build is real (its own mesh and maps), so find in its own feature tags.
        r = client.post(f"/api/sessions/{sid}/builds/{build['id']}/find", json={"query": "bosses"})
        assert r.status_code == 200, r.text
        result = FindResult.model_validate(r.json())
        mesh = validate_mesh(client.get(f"/api/sessions/{sid}/builds/{build['id']}/mesh").content)
        assert result.status == "found"
        assert faces_of(result) <= set(mesh.face_parts)
        r = client.post(f"/api/sessions/{sid}/builds/missing/find", json={"query": "low draft"})
        assert r.status_code == 404
