"""Tests for the session smoke test CI and QA run against a live app.

Run with: python3 -m unittest discover -s .github/scripts
"""

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_smoke

ROOT = Path(__file__).resolve().parents[2]
OWNER = "hardware_owner=browser-token-0123456789"


def serve(statuses):
    """A local app answering POST /api/sessions, .../messages and GET .../parameters
    with `statuses`."""
    seen = []
    cookies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, None))
            cookies.append(self.headers.get("Cookie"))
            status = statuses.get("parameters", 200)
            reply = statuses.get("parameters_body", [{"name": "wall", "value": 2}])
            if status >= 500:
                reply = {"detail": "Internal Server Error"}
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            cookies.append(self.headers.get("Cookie"))
            if self.path == "/api/sessions":
                status, reply = statuses["session"], {"id": "session-1"}
            else:
                status, reply = statuses["message"], {"id": "m1"}
            if status >= 500:
                reply = {"detail": "Internal Server Error"}
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if self.path == "/api/sessions":  # each browser owns its sessions
                self.send_header("Set-Cookie", f"{OWNER}; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.cookies = cookies
    return server, f"http://127.0.0.1:{server.server_port}", seen


class Smoke(unittest.TestCase):
    def run_against(self, session, message, **more):
        server, base, seen = serve({"session": session, "message": message, **more})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "qa-smoke.json"
                with redirect_stdout(io.StringIO()):
                    code = session_smoke.main(["session_smoke.py", base + "/", "--out", str(out)])
                return code, json.loads(out.read_text()), seen
        finally:
            server.shutdown()

    def test_passes_when_a_session_starts_takes_a_message_and_lists_parameters(self):
        code, result, seen = self.run_against(201, 202)
        self.assertEqual(code, 0)
        self.assertEqual(result["session"], "pass")
        self.assertEqual(
            [p for p, _ in seen],
            [
                "/api/sessions",
                "/api/sessions/session-1/messages",
                "/api/sessions/session-1/parameters",
            ],
        )
        self.assertTrue(seen[1][1]["text"].strip())

    def test_sends_back_the_cookie_that_owns_the_session(self):
        server, base, _ = serve({"session": 201, "message": 202})
        try:
            self.assertEqual(session_smoke.smoke(base), [])
        finally:
            server.shutdown()
        self.assertEqual(server.cookies, [None, OWNER, OWNER])

    def test_fails_when_fetching_the_parameters_returns_a_5xx(self):
        code, result, _ = self.run_against(201, 202, parameters=500)
        self.assertEqual(code, 1)
        self.assertEqual(result["session"], "fail")
        self.assertIn("parameters returned 500", result["detail"])

    def test_fails_when_the_parameters_are_not_a_list(self):
        code, result, _ = self.run_against(201, 202, parameters_body={"oops": 1})
        self.assertEqual(code, 1)
        self.assertIn("not a list", result["detail"])

    def test_fails_when_creating_a_session_returns_500(self):
        code, result, seen = self.run_against(500, 202)
        self.assertEqual(code, 1)
        self.assertEqual(result["session"], "fail")
        self.assertIn("500", result["detail"])
        self.assertEqual(len(seen), 1)

    def test_fails_when_posting_a_message_returns_a_5xx(self):
        code, result, _ = self.run_against(201, 503)
        self.assertEqual(code, 1)
        self.assertIn("503", result["detail"])

    def test_fails_when_the_app_is_down(self):
        server, base, _ = serve({"session": 201, "message": 202})
        server.shutdown()
        server.server_close()
        self.assertTrue(session_smoke.smoke(base))

    def test_waits_for_the_deployed_commit(self):
        reports = iter([None, "0ld", "abc1234def"])
        naps = []
        ok = session_smoke.wait_for_commit(
            "http://x", "abc1234", read=lambda _: next(reports), sleep=naps.append
        )
        self.assertTrue(ok)
        self.assertEqual(len(naps), 2)

    def test_gives_up_waiting_for_a_commit_that_never_deploys(self):
        ok = session_smoke.wait_for_commit(
            "http://x", "abc1234", read=lambda _: "0ld", sleep=lambda _: None, seconds=0
        )
        self.assertFalse(ok)


class Workflows(unittest.TestCase):
    def test_ci_smoke_tests_the_built_image(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        image = ci[ci.index("\n  image:") : ci.index("\n  ci:")]
        self.assertIn("docker build", image)
        self.assertGreater(image.index("session_smoke.py"), image.index("docker run -d"))


if __name__ == "__main__":
    unittest.main()
