"""Contracts and fixtures.

The validators here are the contract tests the backend reuses: the tessellator runs
`validate_mesh` on its glTF, the check runner `validate_check_report`, the analysis maps
`validate_analysis_map`, and the session API `assert_matches_session_api` on its OpenAPI document
(see docs/architecture.md).
"""

from __future__ import annotations

import copy
import json
from functools import cache

import pytest

from app.boards import BOARDS_DIR, load_board
from app.boards.changes import parse_changes_md
from app.contracts.checks import validate_check_report
from app.contracts.common import CONTRACTS_DIR, FIXTURES_DIR, REPO_ROOT, ContractError
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import FACE_ID, load_gltf, read_accessor, validate_mesh
from app.contracts.schemas import generated
from app.contracts.session import (
    Build,
    BuildSource,
    assert_matches_session_api,
    contract_openapi,
    validate_event,
    validate_recording,
)
from app.examples import example_standards, examples
from app.fixtures.compare import stale_files
from app.fixtures.generate import boards, checks_for, design_for, generate, maps_for
from app.fixtures.geometry import CUT_KINDS, Enclosure, opening_tags
from app.fixtures.gltf import PartMesh
from app.standards import load_standards

CHANGES = parse_changes_md((BOARDS_DIR / "CHANGES.md").read_text())


@cache
def standards():
    return example_standards()


@cache
def mesh():
    return validate_mesh(FIXTURES_DIR / "enclosure.gltf")


def fixture(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


# --- contracts/ documents every format; generated files are current ----------------------------


def test_contracts_readme_documents_every_format() -> None:
    text = (CONTRACTS_DIR / "README.md").read_text()
    for heading in ("## Mesh", "## Check results", "## Analysis maps", "## Session API"):
        assert heading in text
    for name in generated():
        assert f"`{name}`" in text, f"README does not mention {name}"


def test_generated_contract_files_are_current() -> None:
    for name, text in generated().items():
        path = CONTRACTS_DIR / name
        assert path.exists(), f"run: cd backend && .venv/bin/python -m app.contracts ({name})"
        assert json.loads(path.read_text()) == json.loads(text), f"contracts/{name} is stale"


def test_session_api_is_an_openapi_document_with_every_resource() -> None:
    doc = contract_openapi()
    assert doc["openapi"].startswith("3.")
    paths = " ".join(doc["paths"])
    for resource in (
        "sessions",
        "messages",
        "events",
        "builds",
        "parameters",
        "comments",
        "exports",
    ):
        assert f"/{resource}" in paths
    events = doc["paths"]["/api/sessions/{session_id}/events"]["get"]["responses"]["200"]
    assert "text/event-stream" in events["content"]
    one_of = doc["components"]["schemas"]["SessionEvent"]["oneOf"]
    names = {ref["$ref"].rsplit("/", 1)[1] for ref in one_of}
    for event in ("TextEvent", "ProgressEvent", "QuestionEvent", "BuildReadyEvent", "ErrorEvent"):
        assert event in names


# --- fixtures/ is generated from demos/b-parasite/ and standards.yaml and is current --------------


def test_fixtures_are_current() -> None:
    stale = stale_files(generate(), FIXTURES_DIR)
    assert not stale, f"run: cd backend && .venv/bin/python -m app.fixtures ({stale})"


def test_fixtures_follow_the_standards_file(tmp_path) -> None:
    """Changing a standard changes the fixtures: nothing is hardcoded."""
    text = (REPO_ROOT / "standards.yaml").read_text()
    changed = tmp_path / "standards.yaml"
    changed.write_text(
        text.replace(
            "value: 2.0\n    unit: mm\n    check: wall_thickness",
            "value: 2.4\n    unit: mm\n    check: wall_thickness",
        )
    )
    files = generate(examples()[0].with_standards(load_standards(changed)))
    amap = json.loads(files["maps/wall_thickness.json"])
    assert amap["legend"]["limit"] == 2.4
    params = {p["name"]: p["value"] for p in json.loads(files["parameters.json"])}
    assert params["wall"] == pytest.approx(2.4 * 1.25)


# --- the fixtures validate ---------------------------------------------------------------------


def test_mesh_fixture_has_parts_stable_ids_and_feature_names() -> None:
    info = mesh()
    old, _, _ = boards()
    assert set(info.parts) == {"base", "lid"}
    assert info.asset.units == "mm"
    for tag in opening_tags(old).values():
        assert tag in info.features
    for n in range(1, len(old.mounting_holes) + 1):
        assert f"boss_{n}" in info.features
    assert all(part.edges for part in info.parts.values())
    # IDs are names, not positions in a list: regenerating gives the same IDs.
    assert info.face_parts == validate_mesh(json.loads(generate()["enclosure.gltf"])).face_parts


def test_check_results_fixture_validates() -> None:
    validate_check_report(fixture("checks.json"), standards(), mesh())


def test_analysis_map_fixtures_validate() -> None:
    """The maps flag a spot under its standard exactly where that standard's check fails, and
    show some flagged spots for the frontend to work with."""
    report = fixture("checks.json")
    status = {r["standard"]: r["status"] for r in report["results"] if r.get("standard")}
    flagged = []
    for name in ("wall_thickness", "draft"):
        amap = validate_analysis_map(fixture(f"maps/{name}.json"), mesh(), standards())
        under = [s for s in amap.flagged if s.kind in ("thin", "low_draft")]
        assert bool(under) == (status[amap.standard] == "fail"), name
        flagged += amap.flagged
    assert flagged, "the map fixtures should show flagged spots"


def test_recorded_session_ends_with_the_builds_and_a_question() -> None:
    lines = (FIXTURES_DIR / "session-events.jsonl").read_text().splitlines()
    events = [r.event for r in validate_recording(lines)]
    types = [e.type for e in events]
    assert types[-2:] == ["question", "message_end"]
    assert "build_ready" in types and "progress" in types and "text" in types
    progress = [e for e in events if e.type == "progress"]
    assert all(e.round and e.rounds for e in progress)
    # The existing enclosure on the revision it was designed for, then on the new one.
    builds = [e.build.model_dump(mode="json") for e in events if e.type == "build_ready"]
    assert builds == [fixture("previous-build.json"), fixture("build.json")]


def test_every_board_change_fails_a_check_at_its_component() -> None:
    report = validate_check_report(fixture("checks.json"), standards(), mesh())
    failing = {r.component for r in report.results if r.status == "fail"}
    for row in CHANGES.rows:
        component = row["Component"].strip("`")
        assert component in failing, f"no failing check at {component}"


def test_the_design_passes_its_own_board_openings() -> None:
    old, _, _ = boards()
    enc = Enclosure(design_for(old, standards()))
    parts = enc.build()
    maps = maps_for(enc, [PartMesh(p) for p in parts], standards())
    report = checks_for(enc, {p.name: p for p in parts}, old, standards(), maps)
    for r in report["results"]:
        if r["check"] in ("opening_alignment", "antenna_keepout"):
            assert r["status"] == "pass", r


def test_fixtures_use_the_board_files() -> None:
    _, new, _ = boards()
    assert new == load_board(BOARDS_DIR / CHANGES.new_file)
    report = fixture("checks.json")
    assert report["board"] == {"name": new.name, "revision": new.revision}
    cut = [o.id for o in new.openings if o.kind in CUT_KINDS]
    ids = {r.get("opening") for r in report["results"]}
    assert set(cut) <= ids


# --- the validators reject what breaks the contract --------------------------------------------


def _gltf():
    return copy.deepcopy(json.loads((FIXTURES_DIR / "enclosure.gltf").read_text()))


def test_mesh_validator_rejects_contract_breaks() -> None:
    doc = _gltf()
    doc["asset"]["extras"]["units"] = "m"
    with pytest.raises(ContractError, match="asset.extras"):
        validate_mesh(doc)

    doc = _gltf()
    node = next(n for n in doc["nodes"] if "mesh" in n)
    node["extras"]["faces"][1]["id"] = node["extras"]["faces"][0]["id"]
    with pytest.raises(ContractError, match="duplicate face id"):
        validate_mesh(doc)

    doc = _gltf()
    node = next(n for n in doc["nodes"] if "mesh" in n)
    node["extras"]["faces"].append({"id": "unused.face", "feature": "unused"})
    with pytest.raises(ContractError, match="faces without triangles"):
        validate_mesh(doc)

    doc = _gltf()
    del doc["meshes"][0]["primitives"][0]["attributes"][FACE_ID]
    with pytest.raises(ContractError, match=FACE_ID):
        validate_mesh(doc)

    doc = _gltf()
    next(n for n in doc["nodes"] if "mesh" in n)["translation"] = [1, 0, 0]
    with pytest.raises(ContractError, match="board coordinates"):
        validate_mesh(doc)


def test_mesh_reader_decodes_positions_in_millimetres() -> None:
    doc, buffers = load_gltf(FIXTURES_DIR / "enclosure.gltf")
    prim = doc["meshes"][0]["primitives"][0]
    acc = doc["accessors"][prim["attributes"]["POSITION"]]
    positions = read_accessor(doc, buffers, prim["attributes"]["POSITION"])
    assert len(positions) == acc["count"]
    zs = [p[2] for p in positions]
    assert min(zs) == pytest.approx(acc["min"][2]) and max(zs) == pytest.approx(acc["max"][2])


def test_check_validator_rejects_contract_breaks() -> None:
    good = fixture("checks.json")

    data = copy.deepcopy(good)
    data["results"] = [r for r in data["results"] if r.get("standard") != "min_wall"]
    data["summary"] = {
        s: sum(r["status"] == s for r in data["results"])
        for s in ("pass", "fail", "untested", "not_applicable")
    }
    with pytest.raises(ContractError, match="min_wall"):
        validate_check_report(data, standards(), mesh())

    data = copy.deepcopy(good)
    data["summary"]["pass"] += 1
    with pytest.raises(ContractError, match="summary"):
        validate_check_report(data, standards(), mesh())

    data = copy.deepcopy(good)
    result = next(r for r in data["results"] if r.get("standard") == "min_wall")
    result["required"]["value"] = 1.0
    with pytest.raises(ContractError, match="required"):
        validate_check_report(data, standards(), mesh())

    data = copy.deepcopy(good)
    next(r for r in data["results"] if r["status"] == "fail")["location"] = None
    with pytest.raises(ContractError, match="location"):
        validate_check_report(data, standards(), mesh())

    data = copy.deepcopy(good)
    next(r for r in data["results"] if r["status"] == "fail")["location"]["faces"] = ["no.face"]
    with pytest.raises(ContractError, match="unknown face"):
        validate_check_report(data, standards(), mesh())


def test_map_validator_rejects_misaligned_values() -> None:
    data = fixture("maps/draft.json")
    data["parts"][0]["values"].append(1.0)
    with pytest.raises(ContractError, match="mesh vertices"):
        validate_analysis_map(data, mesh(), standards())

    data = fixture("maps/draft.json")
    data["standard"] = "min_wall"
    with pytest.raises(ContractError):
        validate_analysis_map(data, mesh(), standards())


def test_event_validators_reject_contract_breaks() -> None:
    with pytest.raises(ContractError):
        validate_event(
            {
                "type": "question",
                "message_id": "m",
                "question_id": "q",
                "text": "?",
                "options": [{"id": "only", "label": "Only one"}],
            }
        )
    with pytest.raises(ContractError):
        validate_event({"type": "unknown"})
    with pytest.raises(ContractError, match="not open"):
        validate_recording(
            ['{"id": 1, "delay_ms": 0, "event": {"type": "text", "message_id": "m", "delta": "x"}}']
        )


def test_openapi_check_rejects_a_changed_api() -> None:
    from app.mock_api import app

    assert_matches_session_api(app.openapi())
    changed = copy.deepcopy(app.openapi())
    del changed["paths"]["/api/sessions/{session_id}/comments"]["post"]
    changed["components"]["schemas"]["Session"]["properties"].pop("status")
    with pytest.raises(ContractError) as error:
        assert_matches_session_api(changed)
    assert "comments: missing" in str(error.value)
    assert "schema Session: differs" in str(error.value)


def test_docs_explain_the_mock_api_and_the_contract_tests() -> None:
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text()
    mock = doc.split("### Mock API", 1)[1].split("\n## ", 1)[0]
    assert "make dev MOCK_API=1" in mock and "Playwright" in mock
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "app.mock_api:app" in makefile
    tests = doc.split("## Contracts", 1)[1].split("\n## ", 1)[0]
    for validator in ("validate_mesh", "validate_check_report", "validate_analysis_map",
                      "assert_matches_session_api"):  # fmt: skip
        assert validator in tests


def test_fixture_build_source_is_the_backends_generator_code() -> None:
    from geometry import get_backend
    from geometry.interface import CODE_FILENAME

    source = BuildSource.model_validate(fixture("build-source.json"))
    build = Build.model_validate(fixture("build.json"))
    assert source.build_id == build.id
    assert build.source_url == f"builds/{build.id}/source"
    backend = get_backend(source.backend)
    assert source.code in backend.examples().values()
    assert source.filename.removesuffix(".py") in backend.examples()
    namespace: dict = {}
    exec(compile(source.code, CODE_FILENAME, "exec"), namespace)
    assert callable(namespace["build"])


def test_fixture_build_source_gives_every_feature_its_part_and_lines() -> None:
    import ast

    source = BuildSource.model_validate(fixture("build-source.json"))
    assert source.features
    statements = {
        (node.lineno, node.end_lineno)
        for node in ast.walk(ast.parse(source.code))
        if isinstance(node, ast.stmt)
    }
    lines = source.code.splitlines()
    for f in source.features:
        assert tuple(f.lines) in statements, f"{f.tag}: {f.lines} is not one statement"
        text = "\n".join(lines[f.lines[0] - 1 : f.lines[1]])
        assert "feature(" in text or "features.append(" in text, f"{f.tag}: {text}"


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda s: s["features"][0].update(lines=[3, 2]), "end before they start"),
        (lambda s: s["features"][0].update(lines=[0, 1]), "greater than or equal to 1"),
        (lambda s: s["features"][0].update(lines=[1, 10**6]), "past the end"),
        (lambda s: s["features"][0].pop("part"), "part"),
        (lambda s: s.pop("features"), "features"),
    ],
)
def test_build_source_contract_rejects_bad_feature_lines(change, problem) -> None:
    source = copy.deepcopy(fixture("build-source.json"))
    change(source)
    with pytest.raises(ValueError, match=problem):
        BuildSource.model_validate(source)


def test_fixture_feature_lines_are_recorded_by_running_the_code() -> None:
    """The fixture's lines are what the geometry backend records for a build of its code."""
    from geometry import enclosure_parameters, get_backend

    source = BuildSource.model_validate(fixture("build-source.json"))
    old = load_board(BOARDS_DIR / CHANGES.old_file)
    result = get_backend(source.backend).build(source.code, enclosure_parameters(old, standards()))
    # `locked` is for features sketched by hand; the code's own features have its default.
    assert [f.model_dump(mode="json", exclude_defaults=True) for f in source.features] == (
        result.feature_sources()
    )


# --- the board revision of a build ------------------------------------------------------------


def test_session_api_serves_the_board_of_a_build_in_the_board_schema() -> None:
    from app.boards import Board

    doc = contract_openapi()
    op = doc["paths"]["/api/sessions/{session_id}/builds/{build_id}/board"]["get"]
    ok = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/Board"}
    assert "404" in op["responses"]
    # The contract's Board is the board JSON schema of `backend/app/boards/SCHEMA.md` (FastAPI
    # leaves out null defaults of response fields, which does not change what validates).
    board = Board.model_json_schema(
        ref_template="#/components/schemas/{model}", mode="serialization"
    )
    schemas = doc["components"]["schemas"]
    for name, schema in {**board.pop("$defs"), "Board": board}.items():
        assert _without_null_defaults(schemas[name]) == _without_null_defaults(schema), name
    assert "board_url" in schemas["Build"]["required"]


def test_fixture_build_board_url_names_a_board_file() -> None:
    from app.boards import find_board

    build = Build.model_validate(fixture("build.json"))
    assert build.board_url == f"builds/{build.id}/board"
    board = find_board(build.board.name, build.board.revision)
    files = [p for p in BOARDS_DIR.glob("*.json") if load_board(p) == board]
    assert len(files) == 1
    with pytest.raises(LookupError):
        find_board(build.board.name, "no-such-revision")


def _without_null_defaults(value):
    if isinstance(value, dict):
        return {
            k: _without_null_defaults(v)
            for k, v in value.items()
            if not (k == "default" and v is None)
        }
    if isinstance(value, list):
        return [_without_null_defaults(v) for v in value]
    return value


def test_the_history_fixtures_follow_their_contracts() -> None:
    """The older revision's build and checks, and the diff between the revisions."""
    from app.contracts.session import BuildDiff

    old, new, _ = boards()
    previous = Build.model_validate(fixture("previous-build.json"))
    assert (previous.board.name, previous.board.revision) == (old.name, old.revision)
    report = validate_check_report(fixture("previous-checks.json"), standards(), mesh())
    assert report.build_id == previous.id
    assert previous.checks.model_dump() == fixture("previous-checks.json")["summary"]
    diff = BuildDiff.model_validate(fixture("diff.json"))
    assert (diff.old, diff.new) == (previous.id, fixture("build.json")["id"])
    assert diff.features and diff.parameters


def test_selection_components_description_does_not_promise_opening_faces() -> None:
    """The frontend sends a component pick only in `components`."""
    from app.contracts.session import Selection

    description = Selection.model_fields["components"].description or ""
    assert "`faces` has their openings" not in description
    assert "not added to `faces`" in description
    for name in ("session-api.openapi.json", "telemetry-event.schema.json"):
        text = (CONTRACTS_DIR / name).read_text()
        assert "`faces` has their openings" not in text, name


def test_risk_flags_fixture_validates_and_points_at_the_mesh() -> None:
    """The fixture risks are found on the fixture checks and maps, each at faces of the mesh,
    with a fix whose edits are design parameters."""
    from app.agent.design import has_path
    from app.agent.risks import find_risks
    from app.agent.services import FixtureServices, items_from_report
    from app.contracts.changes import RiskFlags
    from geometry import enclosure_parameters

    flags = RiskFlags.model_validate(fixture("risk-flags.json"))
    assert flags.flags, "the fixture should flag at least one risk"
    assert flags.build == fixture("build.json")["id"]
    report = items_from_report(fixture("checks.json"))
    found = {r.id for r in find_risks(report, FixtureServices().analyze(None), standards())}
    gltf = fixture("enclosure.gltf")
    faces = {f["id"] for n in gltf["nodes"] for f in (n.get("extras") or {}).get("faces", [])}
    params = enclosure_parameters(boards()[0], standards())
    for flag in flags.flags:
        assert flag.id in found and flag.status == "proposed"
        assert flag.location.faces and set(flag.location.faces) <= faces
        assert flag.location.point is not None
        assert all(has_path(params, path) for path in flag.fix.edits)
