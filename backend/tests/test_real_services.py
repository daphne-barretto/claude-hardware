"""The agent on the real implementations: builds in the sandbox, checks from the check
runner, conflicts from conflict detection and every build in the build store."""

from __future__ import annotations

import asyncio
import json

from app.agent.run import scenario_workspace
from app.agent.services import (
    MESH_FILE,
    GeometryServices,
    items_from_report,
    map_file,
)
from app.builds import BuildStore, default_store
from app.contracts.checks import validate_check_report
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from geometry import enclosure_parameters
from geometry.conflicts import conflict_locks


def test_the_agent_uses_the_real_services_and_the_store_in_the_data_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ws = scenario_workspace()
    assert isinstance(ws.services, GeometryServices)
    assert ws.services.store.root == default_store().root == tmp_path / "builds"


def test_every_build_is_checked_by_the_check_runner_and_stored_with_its_parent(tmp_path):
    ws = scenario_workspace()
    store = BuildStore(tmp_path / "builds")
    services = ws.services = GeometryServices(ws.board, ws.standards, store=store)

    # The existing design on the new board: the check runner's report, stored with the build.
    report = asyncio.run(ws.check())
    first = ws.latest
    record = store.get(first.build.id)
    assert record.parent is None and record.code == ws.design.code
    assert record.parameters == ws.design.params
    assert record.board.revision == ws.board.revision
    mesh = validate_mesh(store.file(record.id, MESH_FILE))
    validate_check_report(record.checks, ws.standards, mesh)
    for map_type in first.build.maps:
        amap = json.loads(store.file(record.id, map_file(map_type)).read_text())
        validate_analysis_map(amap, mesh, ws.standards)
    # What the agent sees is that report, with each check's feature.
    items = services.checks(first)
    assert [i.id for i in items] == [r["id"] for r in record.checks["results"]]
    assert [i.status for i in items] == [i.status for i in items_from_report(record.checks)]
    assert report["summary"] == record.checks["summary"] and report["failing"]
    assert any(i.feature for i in items)

    # The fix: a child of the first build, passing every check; the diff shows the change.
    fixed = enclosure_parameters(ws.board, ws.standards)
    ws.design.params = fixed
    ws._changed()
    report = asyncio.run(ws.check())
    assert report["summary"]["fail"] == 0 and ws.passing
    second = store.get(ws.latest.build.id)
    assert second.parent == record.id
    assert [b.id for b in store.history(second.id)] == [second.id, record.id]
    diff = store.diff(record.id, second.id)
    assert diff["parameters"] and diff["features"]
    # Face IDs carry over from the build before where the faces match.
    before = {f["id"] for n in mesh_doc(store, record.id)["nodes"] for f in faces(n)}
    after = {f["id"] for n in mesh_doc(store, second.id)["nodes"] for f in faces(n)}
    assert len(before & after) > len(after) / 2


def test_a_failed_build_is_reported_and_not_stored(tmp_path):
    ws = scenario_workspace()
    store = BuildStore(tmp_path / "builds")
    services = GeometryServices(ws.board, ws.standards, store=store)
    outcome = services.build(dict(ws.design.snapshot(), code="def build(params):\n    1 / 0\n"))
    assert not outcome.ok and outcome.build.status == "failed" and outcome.error["line"] == 2
    assert store.builds() == []


def test_conflicts_come_from_conflict_detection_with_mesh_faces(tmp_path):
    ws = scenario_workspace()
    ws.services = GeometryServices(ws.board, ws.standards, store=BuildStore(tmp_path))
    ((path, value),) = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    ws.design.lock(path, value)
    report = asyncio.run(ws.check())
    (conflict,) = report["conflicts"]
    locked = [c for c in conflict["constraints"] if c["kind"] == "locked"]
    assert [(c["id"], c["value"]) for c in locked] == [(path, value)]
    mesh_faces = {
        f["id"] for n in mesh_doc(ws.services.store, ws.latest.build.id)["nodes"] for f in faces(n)
    }
    assert conflict["location"]["faces"] and set(conflict["location"]["faces"]) <= mesh_faces
    assert not ws.passing


def mesh_doc(store: BuildStore, build_id: str) -> dict:
    return json.loads(store.file(build_id, MESH_FILE).read_text())


def faces(node: dict) -> list[dict]:
    return node.get("extras", {}).get("faces", [])
