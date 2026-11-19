"""The server process never loads the CAD kernel.

build123d and OpenCascade take about 430 MB once imported. Every build already loads them in
its sandbox subprocess, so the server holding a second copy is what pushed staging over its
memory limit. All geometry work (builds, checks, analysis, the existing design's parameters,
exports) runs in a subprocess; the server only passes data in and gets data back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# A fresh interpreter stands in for the server: this test process has loaded the kernel already.
SERVER = textwrap.dedent(
    """\
    import json, os, sys, time
    from fastapi.testclient import TestClient

    from app.agent.run import scenario_workspace
    from app.agent.services import GeometryServices
    from app.e2e_api import app  # app.main with a scripted Claude
    from app.examples import examples
    from app.session_api import CONFIG, SESSIONS, geometry_services

    CONFIG.services = geometry_services  # real builds, also for the example

    def kernel_modules():
        return sorted(m for m in ("OCP", "build123d", "cadquery") if m in sys.modules)

    report = {}
    with TestClient(app) as client:
        r = client.post("/api/sessions", json={"example": examples()[0].id})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        for _ in range(3000):
            if SESSIONS.get(sid).initial.done():
                break
            time.sleep(0.02)
        assert SESSIONS.get(sid).initial.done(), "the first build never finished"
        report["session"] = kernel_modules()

        # A run that designs from scratch: a sandboxed build, its checks, the check report.
        empty = client.post("/api/sessions", json={}).json()["id"]
        box = {"text": "make a box 30 x 20 x 10 mm"}
        r = client.post(f"/api/sessions/{empty}/messages", json=box)
        assert r.status_code == 202, r.text
        for _ in range(3000):
            state = SESSIONS.get(empty)
            if state.run is not None and state.run.done():
                break
            time.sleep(0.02)
        assert SESSIONS.get(empty).session.latest_build_id, "the run built nothing"
        report["run"] = kernel_modules()

        ws = scenario_workspace()
        assert isinstance(ws.services, GeometryServices)
        outcome = ws.services.build(ws.design.snapshot())
        assert outcome.ok, outcome.error
        items = ws.services.checks(outcome)
        flagged = ws.services.analyze(outcome)
        report["checks"] = len(items)
        report["statuses"] = sorted({i.status for i in items})
        report["flagged"] = len(flagged)
        report["build"] = kernel_modules()

        build = client.get(f"/api/sessions/{sid}").json()["latest_build_id"]
        r = client.post(
            f"/api/sessions/{sid}/exports", json={"build_id": build, "formats": ["step"]}
        )
        assert r.status_code == 201, r.text
        step = client.get(r.json()["files"][0]["url"])
        report["step"] = step.content.startswith(b"ISO-10303-21")
        report["export"] = kernel_modules()
    print("REPORT " + json.dumps(report))
    """
)


def test_the_server_never_loads_the_cad_kernel(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "DATA_DIR": str(tmp_path / "data"),
        "TELEMETRY_DIR": str(tmp_path / "telemetry"),
        "HOME": str(tmp_path),
    }
    # Where the sandbox can't apply its kernel limits (macOS), a developer allows that.
    if "GEOMETRY_SANDBOX_KERNEL_LIMITS" in os.environ:
        env["GEOMETRY_SANDBOX_KERNEL_LIMITS"] = os.environ["GEOMETRY_SANDBOX_KERNEL_LIMITS"]
    proc = subprocess.run(
        [sys.executable, "-c", SERVER],
        capture_output=True,
        text=True,
        cwd=BACKEND,
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    (line,) = [x for x in proc.stdout.splitlines() if x.startswith("REPORT ")]
    report = json.loads(line.removeprefix("REPORT "))
    assert report["checks"] > 0 and report["statuses"]
    assert report["step"] is True
    # After creating a session, after a build with its checks and analysis, after an export.
    assert report["session"] == [], f"loaded by creating a session: {report['session']}"
    assert report["run"] == [], f"loaded by a run's build and checks: {report['run']}"
    assert report["build"] == [], f"loaded by a build and its checks: {report['build']}"
    assert report["export"] == [], f"loaded by an export: {report['export']}"
