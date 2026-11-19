"""Hostile design code through the API (`PUT .../source`, a hand edit to the code), as a user
or a misled model could send it (`docs/security.md`).

Each is stopped with a clear outcome (400 with the reason), the design is unchanged, and the
server stays responsive while the build runs: `/api/health` answers fast throughout. The
sandbox's own limits are tested in depth in `tests/test_sandbox.py`; this checks the whole path.
The build timeout is cut to a few seconds here so the suite stays quick.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest
from conftest import Browser, box_session

from geometry import sandbox

TIMEOUT_S = 20.0

PREFIX = "from build123d import Box\n\n\ndef build(params):\n"
RESULT = '    return {"parts": {"box": Box(1, 1, 1)}, "features": [], "materials": {}}\n'

# A build out of memory fails with `memory` (Linux only: `LINUX_ONLY`).
HOSTILE = {
    "infinite_loop": ("    while True:\n        pass\n", ("timeout", "exceeded")),
    "huge_memory": ("    x = bytearray(64 * 1024 ** 3)\n", ("memory", "MemoryError", "exceeded")),
    "huge_file": (
        "    f = open('big.bin', 'wb')\n    while True:\n        f.write(b'0' * 1048576)\n",
        ("disk", "limit", "File too large", "size", "timeout", "exceeded"),
    ),
    "import_os": ("    import os\n    os.system('echo hi')\n", ("os", "import", "not allowed")),
    "import_subprocess": (
        "    import subprocess\n    subprocess.run(['ls'])\n",
        ("subprocess", "import", "not allowed"),
    ),
    "import_socket": (
        "    import socket\n    socket.create_connection(('example.com', 80))\n",
        ("socket", "import", "not allowed"),
    ),
    "read_outside": (
        "    open('/etc/passwd').read()\n",
        ("forbidden", "not allowed", "Permission", "outside", "open"),
    ),
    "dunder_import_fork": (
        "    __import__('os').fork()\n",
        ("os", "import", "not allowed", "forbidden"),
    ),
}


@pytest.fixture(autouse=True)
def _short_builds(monkeypatch):
    real = sandbox.run_build

    def quick(*args, **kwargs):
        kwargs["timeout_s"] = TIMEOUT_S
        return real(*args, **kwargs)

    monkeypatch.setattr(sandbox, "run_build", quick)


NO_KERNEL_LIMITS = not sys.platform.startswith("linux")
# Reads outside the work directory are stopped by Landlock, which only Linux has; without the
# kernel limits (local macOS development) the read succeeds (`docs/architecture.md`).
# A build out of memory is stopped by its address-space cap (RLIMIT_AS), which macOS doesn't
# enforce: there the allocation succeeds lazily and the build simply finishes.
LINUX_ONLY = {
    "read_outside": pytest.mark.xfail(
        NO_KERNEL_LIMITS, strict=True, reason="Landlock read limits need Linux"
    ),
    "huge_memory": pytest.mark.skipif(
        NO_KERNEL_LIMITS, reason="macOS has no address-space cap (RLIMIT_AS): Linux only"
    ),
}
CASES = [pytest.param(c, marks=LINUX_ONLY[c]) if c in LINUX_ONLY else c for c in sorted(HOSTILE)]


@pytest.mark.parametrize("case", CASES)
def test_hostile_code_is_stopped_with_a_clear_outcome_and_the_server_stays_up(
    browser: Browser, case: str
) -> None:
    sid, build = box_session(browser)
    body, reasons = HOSTILE[case]
    code = PREFIX + body + RESULT
    builds_before = browser.builds(sid)
    source_before = browser.get(f"/api/sessions/{sid}/builds/{build['id']}/source").json()

    result: dict = {}

    def edit() -> None:
        result["r"] = browser.client.put(
            f"/api/sessions/{sid}/source", json={"build_id": build["id"], "code": code}
        )

    thread = threading.Thread(target=edit)
    started = time.monotonic()
    thread.start()
    slowest = 0.0
    while thread.is_alive():
        t = time.monotonic()
        assert browser.client.get("/api/health").status_code == 200
        slowest = max(slowest, time.monotonic() - t)
        time.sleep(0.1)
    thread.join()
    took = time.monotonic() - started

    r = result["r"]
    assert r.status_code == 400, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert any(word.lower() in detail.lower() for word in reasons), detail
    assert slowest < 1.0, f"health took {slowest:.2f}s while the build ran"
    assert took < TIMEOUT_S + 20, took
    # Nothing changed: no new build, the same code.
    assert browser.builds(sid) == builds_before
    after = browser.get(f"/api/sessions/{sid}/builds/{build['id']}/source").json()
    assert after == source_before


def test_secrets_never_reach_the_build(browser: Browser, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-edge-test-not-a-real-key")
    sid, build = box_session(browser)
    code = PREFIX + (
        "    import sys\n"
        "    raise ValueError('ENV=' + repr(sorted(sys.modules['os'].environ.items())))\n"
    )
    r = browser.put(f"/api/sessions/{sid}/source", json={"build_id": build["id"], "code": code})
    assert r.status_code == 400
    assert "sk-ant-edge-test" not in r.text
