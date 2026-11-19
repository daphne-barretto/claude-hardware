"""The demo script (`demos/b-parasite/demo-script.md`) after Start, walked over HTTP with a scripted
Claude.

`test_telemetry.py` walks it to check that every user action and agent step is logged. The
scenario comes from `demos/b-parasite/`, `standards.yaml`, `fixtures/` and
`demos/b-parasite/demo-script.md`.
"""

from __future__ import annotations

import json
import struct

import test_session_api as api

from app.contracts.checks import validate_check_report
from app.contracts.common import FIXTURES_DIR
from app.contracts.maps import validate_analysis_map
from app.contracts.mesh import validate_mesh
from app.contracts.session import Build, Comment, Export, Message, Parameter
from app.session_api import SESSIONS
from app.standards import load_standards
from geometry.conflicts import conflict_locks

CREDENTIALS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def parameters(client, sid: str) -> dict[str, Parameter]:
    found = client.get(f"/api/sessions/{sid}/parameters").json()
    return {p.name: p for p in map(Parameter.model_validate, found)}


def walk(client, sid: str, found, claude: api.Claude) -> dict:
    """The demo script after Start, over HTTP, with the runs scripted (`claude`). Returns what
    was served."""
    standards = load_standards()
    doc = api.changes()
    script = claude.scripts.append
    served: dict = {"messages": []}

    # 1. Start: the existing enclosure, built and checked.
    (first,) = [d for _, t, d in found if t == "build_ready"]
    build = Build.model_validate(first["build"])
    mesh = validate_mesh(client.get(build.mesh_url).content)
    validate_check_report(client.get(build.checks_url).json(), standards, mesh)

    # 2-3. The new revision arrives; Claude lists the changes and proposes the fix.
    change_list = "".join(f"\n- {c['Component']}: {c['Change']}" for c in doc.rows)
    script(
        [
            ("read_board", {"revision": "new"}),
            ("run_checks", {}),
            ("text", f"The new revision changes:{change_list}"),
            ("set_parameter", {"name": "clearance", "value": 0.6}),
            (
                "ask_user",
                {
                    "question": "Apply the proposed changes?",
                    "options": [
                        {"id": "accept", "label": "Accept"},
                        {"id": "reject", "label": "Reject"},
                    ],
                },
            ),
            ("finish", {"outcome": "blocked", "summary": "Waiting on review."}),
        ]
    )
    prompt = json.loads((FIXTURES_DIR / "session.json").read_text())["prompt"]
    api.send(client, sid, text=prompt, model="claude-opus-5-5", effort="high")
    found = api.wait_until(client, sid, "waiting_for_answer", found[-1][0])
    text = "".join(d["delta"] for _, t, d in found if t == "text")
    assert all(row["Component"] in text for row in doc.rows)
    builds = [Build.model_validate(d["build"]) for _, t, d in found if t == "build_ready"]
    assert builds
    for b in builds:
        report = client.get(b.checks_url).json()
        validate_check_report(report, standards, mesh)
        assert report["summary"] == b.checks.model_dump()

    # 4. Review: answer, then ask about a selected face.
    (question,) = [d for _, t, d in found if t == "question"]
    api.send(client, sid, answer={"question_id": question["question_id"], "option_id": "accept"})
    found = api.wait_until(client, sid, "idle", found[-1][0])
    gltf = json.loads(client.get(build.mesh_url).text)
    part, face = next(
        (n["extras"]["part"], f["id"])
        for n in gltf["nodes"]
        for f in n.get("extras", {}).get("faces", [])
    )
    script(
        [
            ("text", "That face moved with the sensor window."),
            ("finish", {"outcome": "blocked", "summary": "Answered."}),
        ]
    )
    api.send(client, sid, text="Why did this change?", selection={"part": part, "faces": [face]})
    found = api.wait_until(client, sid, "idle", found[-1][0])
    messages = [
        Message.model_validate(m) for m in client.get(f"/api/sessions/{sid}/messages").json()
    ]
    assert messages[-1].role == "assistant" and "sensor window" in messages[-1].text
    served["messages"] = [(m.id, m.role, m.text) for m in messages]

    # 5. Direct edit: lock a value by hand; Claude reworks the rest around it.
    draft = parameters(client, sid)["draft"].value
    r = client.patch(f"/api/sessions/{sid}/parameters/draft", json={"value": draft + 0.5})
    assert r.status_code == 200 and r.json()["locked"]
    script(
        [
            ("set_parameter", {"name": "clearance", "value": 0.55}),
            ("finish", {"outcome": "blocked", "summary": "Reworked around the draft."}),
        ]
    )
    api.send(client, sid, text="Rework the design around my draft.")
    found = api.wait_until(client, sid, "idle", found[-1][0])
    params = parameters(client, sid)
    assert params["draft"].locked and params["draft"].value == draft + 0.5
    assert params["clearance"].value == 0.55 and not params["clearance"].locked

    # 6. Conflict: lock a value that can't satisfy the standards; Claude asks what to do.
    ws = SESSIONS.get(sid).ws
    ((path, value),) = conflict_locks(ws.board, ws.standards, ws.design.params).items()
    r = client.patch(f"/api/sessions/{sid}/parameters/{path}", json={"value": value})
    assert r.status_code == 200 and r.json()["locked"]
    script(
        [
            ("run_checks", {}),
            (
                "ask_user",
                {
                    "question": "The locked value conflicts with the standards. What now?",
                    "options": [
                        {"id": "unlock", "label": "Change it", "approves": [path]},
                        {"id": "keep", "label": "Keep it"},
                    ],
                },
            ),
            ("finish", {"outcome": "blocked", "summary": "Kept the lock."}),
        ]
    )
    api.send(client, sid, text="Check it again.")
    found = api.wait_until(client, sid, "waiting_for_answer", found[-1][0])
    (question,) = [d for _, t, d in found if t == "question"]
    assert [o["id"] for o in question["options"]] == ["unlock", "keep"]
    api.send(client, sid, answer={"question_id": question["question_id"], "option_id": "keep"})
    api.wait_until(client, sid, "idle", found[-1][0])
    served["parameters"] = {n: (p.value, p.locked) for n, p in parameters(client, sid).items()}

    # 7. Comment: pinned to a face, still on the model after it rebuilds.
    builds = [Build.model_validate(b) for b in client.get(f"/api/sessions/{sid}/builds").json()]
    r = client.post(
        f"/api/sessions/{sid}/comments",
        json={"build_id": builds[0].id, "face_id": face, "point": [0, 0, 0], "text": "Check this"},
    )
    assert r.status_code == 201
    comment = Comment.model_validate(r.json())
    latest = builds[-1]
    later = json.loads(client.get(latest.mesh_url).text)
    assert face in {f["id"] for n in later["nodes"] for f in n.get("extras", {}).get("faces", [])}
    assert client.get(f"/api/sessions/{sid}/comments").json() == [comment.model_dump(mode="json")]

    # 8. Compare: every build is kept with its checks, maps, mesh and source.
    assert len(builds) >= 3
    assert client.get(f"/api/sessions/{sid}").json()["latest_build_id"] == latest.id
    served["builds"] = {}
    for b in builds:
        artifacts = {
            "build": client.get(f"/api/sessions/{sid}/builds/{b.id}").json(),
            "mesh": client.get(b.mesh_url).text,
            "checks": client.get(b.checks_url).json(),
            "source": client.get(b.source_url).json(),
            "maps": {},
        }
        for map_type in b.maps:
            amap = client.get(f"/api/sessions/{sid}/builds/{b.id}/maps/{map_type}").json()
            validate_analysis_map(amap, mesh, standards)
            artifacts["maps"][map_type] = amap
        served["builds"][b.id] = artifacts

    # 9. Export STEP, STL and the build note, and check the files open.
    r = client.post(
        f"/api/sessions/{sid}/exports",
        json={"build_id": latest.id, "formats": ["step", "stl", "build_note"]},
    )
    assert r.status_code == 201
    export = Export.model_validate(r.json())
    files = {f.format: client.get(f.url) for f in export.files}
    assert files["step"].content.startswith(b"ISO-10303-21")
    stl = files["stl"].content
    (count,) = struct.unpack_from("<I", stl, 80)
    assert count > 0 and len(stl) == 84 + 50 * count, "a binary STL"
    assert latest.id in files["build_note"].text
    served["exports"] = sorted(files)
    return served
